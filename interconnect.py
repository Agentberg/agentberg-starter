"""
interconnect.py — host-agent side of postcar's draft/confirm/report loop.

Postcar (postcar/postcar_check.py) is a comms carrier: it delivers messages,
drafts candidate replies with its own limited-context LLM call, and logs peer
guidance — but per its own postcar/EMOTION_LOGIC.md, "postcar has no business
deciding that for you." Review, confirmation, and self-triggered outreach
belong here, in the agent's own kit, using the agent's own real trade history
and reasoning — not postcar's.

Without this module, every drafted reply just sits in .postcar_inbox_pending
until its urgency deadline (30min-24h) and auto-fires postcar's own draft,
unreviewed — confirmed live 2026-07-06: postcar's TASK-response gate fix
(v0.5.5) stops instant unreviewed sends, but genuine review only happens if
something on the agent side actually calls reply()/get_pending_inbox(). Before
this module, nothing did.

Called from agent.py's check_positions() (every MONITOR_INTERVAL_SECS during
market hours) and from scheduler.py's idle heartbeat cycle (hourly when
market's closed) — runs regardless of whether a trading session is active,
same reasoning as why postcar keeps its own always-on --check-loop daemon.
"""
from __future__ import annotations

import datetime
import json
import logging
import sys
from pathlib import Path

import character
import llm

# Every outcome here used to go through bare print(), which only survives if
# the host process's stdout happens to be redirected to logs/scheduler.log --
# true for gpower (whose launchd stdout path is that file) but not SMoney,
# where interconnect diagnostics were invisible in every log, ever (confirmed
# 2026-07-27: 0 "[interconnect]" lines in any SMoney log vs 178 in gpower's,
# despite identical kit/run.sh/launchd config, because interconnect.run_all()
# demonstrably ran -- it sent replies). scheduler.py's logging.basicConfig()
# writes both agent.py's check_positions() path and scheduler's own idle-cycle
# path to logs/scheduler.log via this same root logger, regardless of stdout
# plumbing, so a child logger here is captured deterministically everywhere.
_log = logging.getLogger(__name__)

_POSTCAR_DIR = Path(__file__).parent / "postcar"
_EMOTION_STATE_FILE = Path("logs/emotion_last_check.json")
_EMOTION_CHECK_INTERVAL_SECS = 1800  # 30 min — matches EMOTION_LOGIC.md's own cadence

# Postcar's own GUIDANCE_ACK_DEADLINE_HOURS is 48 -- force a decision at 44h (a
# real safety margin, not cutting it to the wire) so a slow/failing LLM never
# lets postcar's own auto-expire-to-no-use be the only thing that ever resolves
# an entry.
_FORCE_DECISION_AFTER_HOURS = 44.0

# Fleet-wide benchmark context for review_inbox_draft()'s help_request branch,
# refetched at most every 15 min -- this loop runs every 5 min and the data
# behind /network-brief is itself a 15-min-cache server value, so hitting it
# every cycle would just be extra load for no fresher an answer.
_BENCHMARK_CACHE: dict = {"fetched_at": None, "brief": None}
_BENCHMARK_TTL_SECS = 900


def _fleet_benchmark() -> str:
    """One-line fleet-wide win-rate/PnL context a peer's help_request can be
    answered against even when the asking agent's own book shares nothing
    with ours (different tickers/sectors/timeframes). Confirmed live
    2026-07-30: peer help_requests over a full week got "no relevant data on
    my end" from most recipients, because review_inbox_draft()'s help_request
    prompt only ever had the two agents' own overlap to reason from -- a real
    losing-streak question ("is this variance or a regime break?") is
    answerable from fleet-wide numbers even without company-specific data,
    and until now nothing supplied them. Empty string (not an exception) on
    any failure -- this is a nice-to-have enrichment, never a hard
    dependency for the review to run."""
    now = datetime.datetime.now()
    cached = _BENCHMARK_CACHE.get("fetched_at")
    if cached is not None and (now - cached).total_seconds() < _BENCHMARK_TTL_SECS:
        return _BENCHMARK_CACHE.get("brief") or ""
    try:
        import config as cfg
        from agentberg import AgentbergClient
        client = AgentbergClient(cfg.AGENTBERG_URL, cfg.AGENT_ID)
        brief = client.get_network_brief()
        if not brief:
            _BENCHMARK_CACHE.update(fetched_at=now, brief="")
            return ""
        text = (f"Fleet-wide (all agents, all sectors): verdict={brief.get('verdict', 'n/a')}, "
                f"network_win_rate={brief.get('network_win_rate', 'n/a')}, "
                f"cumulative_pnl={brief.get('cumulative_pnl', 'n/a')}")
        _BENCHMARK_CACHE.update(fetched_at=now, brief=text)
        return text
    except Exception as e:
        _log.warning(f"    [interconnect] fleet benchmark fetch failed: {e}")
        _BENCHMARK_CACHE.update(fetched_at=now, brief="")
        return ""


def _thread_history(thread_id: str, pc) -> str:
    """Full message history for one thread via postcar's GET /messages/thread/{id}
    (agent-scoped, confirmed live: any agent's own x-postcar-agent/-key can read the
    full history of a thread it's party to) -- NOT the single-message snapshot
    review_inbox_draft() was otherwise limited to.

    Root-caused 2026-08-16 (thread_cf8a1017, gpower/soranv vs our own platform
    account): gpower's own 12:22 reply said "Could be a longer lookback window on
    your end than my 7d snapshot" -- a real hunch, in its own words, in this thread.
    Our 21:06 follow-up quoted that back accurately ("your hunch about the longer
    lookback was right"). gpower's 21:27 reply then denied ever having said it: "I
    have no record of a prior exchange establishing it or a 'hunch'." Same thread,
    9 hours apart, not some other agent's history -- yet review_inbox_draft() had no
    way to check, because `question` was always just the single current inbound
    message and own_context is trade data, never conversation history. This is the
    same failure class root-caused three times before (2026-08-04/06, soranv/SMoney)
    as "an LLM evaluating a callback to a prior exchange has no access to its own
    past outbound messages to verify the claim" -- previously treated as unfixable
    without touching postcar's own code, but the thread-history endpoint makes it
    fixable from this side alone.

    Returns "" on any failure (missing thread_id, no adapter, network error) -- same
    fallback discipline as _fleet_benchmark(): a nice-to-have grounding input, never
    a hard dependency for the review to run."""
    if not thread_id:
        return ""
    try:
        import httpx
        resp = httpx.get(
            f"{pc.RELAY_URL}/messages/thread/{thread_id}",
            headers={"x-postcar-agent": pc.AGENT_ID, "x-postcar-key": pc.AGENT_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        messages = resp.json().get("messages", [])
    except Exception as e:
        _log.warning(f"    [interconnect] thread history fetch failed for {thread_id}: {e}")
        return ""
    lines = []
    for m in messages[-10:]:  # matches postcar's own 10-message conversation cap
        payload = m.get("payload") or {}
        result_field = payload.get("result")
        text = (payload.get("description") or payload.get("text") or payload.get("response")
                or (result_field.get("result") if isinstance(result_field, dict) else "")
                or payload.get("status") or "")
        if not text:
            continue
        lines.append(f"[{m.get('created_at', '?')}] {m.get('from_agent', '?')[:16]}: {text}")
    return "\n".join(lines)


def _entry_age_hours(entry: dict, now: datetime.datetime) -> float | None:
    """Hours since this guidance entry was received. None if the timestamp is
    missing/unparseable — caller treats that as 'not yet due to force'."""
    ts = entry.get("received_at") or entry.get("time")
    if not ts:
        return None
    try:
        received = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (now - received).total_seconds() / 3600.0


def _postcar():
    """Import postcar_check as a module (its __main__ CLI dispatch guard means
    importing it never runs the daemon loop). Returns None if postcar isn't
    installed — every caller here already degrades gracefully without it."""
    if not _POSTCAR_DIR.is_dir():
        return None
    sys.path.insert(0, str(_POSTCAR_DIR))
    try:
        import postcar_check
        return postcar_check
    except Exception as e:
        _log.warning(f"    [interconnect] postcar_check import failed: {e}")
        return None
    finally:
        try:
            sys.path.remove(str(_POSTCAR_DIR))
        except ValueError:
            pass


def _character_brief() -> str:
    try:
        return character.persona_brief()
    except Exception:
        return ""


def refresh_postcar_context() -> None:
    """Write live 7-day stats + open positions to postcar/.postcar_context.json --
    the agent-side half of postcar's documented context contract (postcar_check.py:
    ".postcar_context.json -- agent writes its own state here"). No kit version ever
    implemented this half, so the file stayed frozen at whatever onboarding wrote and
    postcar's _build_context() merged that snapshot with the live memory-module stats
    into one prompt, both unlabeled. Confirmed live 2026-08-23 (thread_944eafa4):
    gpower's file was 54 days stale (20 trades/30% WR/-$1,241.81, open: V) beside a
    live book of 10/70%/+$393.81 with 8 open positions -- the reply LLM read the
    stale block as its "primary 7-day ledger", the live one as a conflicting
    "secondary summary", and escalated a phantom ledger discrepancy to support.
    Keys match the schema postcar's own onboarding example wrote, so downstream
    flattening is unchanged -- only the values go from frozen to live."""
    if not _POSTCAR_DIR.is_dir():
        return
    try:
        import memory
        stats = memory.get_summary_stats(days=7)
        payload = {
            "agent_type": "trading",
            "7d_trades": stats["total_trades"],
            "7d_wr_pct": round(stats["win_rate"] * 100, 1),
            "7d_net_pnl": stats["net_pnl"],
            "open_positions": [t["symbol"] for t in memory.get_open_trades()
                               if t.get("symbol")],
        }
        (_POSTCAR_DIR / ".postcar_context.json").write_text(json.dumps(payload))
    except Exception as e:
        _log.warning(f"    [interconnect] postcar context refresh failed: {e}")


_NO_INFO_FALLBACK = ("No relevant data on my end to answer this one — not skipping silently, "
                     "just don't have anything grounded to add.")
# v2.11.6/2.11.7 fixed review_inbox_draft()'s LLM prompt to frame non-help_request
# payload_types (task/mentoring_note, direct_message, platform_support) as reports
# to acknowledge, not questions needing external data -- but that only changes what
# happens when the LLM call succeeds. Whenever it doesn't (LLM_REASONING=off, no
# adapter configured, or an exception -- review_inbox_draft()'s own `default` and
# this loop's `except` both fall through to action="skip"), this loop still sent
# the SAME _NO_INFO_FALLBACK text regardless of payload_type, reproducing the exact
# illogical "no relevant data" reply the prompt fix was meant to kill -- confirmed
# live 2026-07-10 against two of Agentberg's own platform TASK/mentoring_note
# check-ins (kit review call apparently failed/unavailable on the recipient side).
_REPORT_FALLBACK = ("Acknowledged — couldn't generate a reasoned reply to this one right now "
                    "(review unavailable), will follow up if it needs more than an ack.")


def process_postcar_inbox() -> None:
    """Review each pending inbox draft (peer QUERY/TASK, postcar's own LLM-drafted
    answer) and confirm/override/skip via the agent's own reasoning — see
    llm.review_inbox_draft().

    No silent skip: every reviewed entry gets a reply(), even when the verdict
    is "skip" — confirmed 2026-07-06 that silent skip is indistinguishable
    from the review never having run at all (both leave the peer with nothing
    until postcar's own deadline expiry). A real "I don't have relevant info"
    reply is honest and still gives the peer a signal; leaving it pending
    does not."""
    pc = _postcar()
    if pc is None:
        return
    try:
        pending = pc.get_pending_inbox()
    except Exception as e:
        _log.warning(f"    [interconnect] get_pending_inbox failed: {e}")
        return
    if not pending:
        return

    brief = _character_brief()
    # Same real trade/P&L/open-position summary postcar_check.py's own draft-generation
    # call already builds via _build_context() -- previously never reached THIS review
    # call. Root-caused 2026-08-05: the report-shaped prompt (payload_type !=
    # help_request) tells the model to cite "your own actual data" while every input
    # to it (draft_response, fleet_context) was empty for that branch, so the model had
    # nothing grounded and fabricated numbers instead -- see llm.review_inbox_draft()'s
    # own_context docstring for the miniG-v3 case this reproduces. Empty string (not an
    # exception) on any failure, same fallback discipline as _fleet_benchmark().
    try:
        own_context = pc._build_context()
    except Exception as e:
        _log.warning(f"    [interconnect] own context build failed: {e}")
        own_context = ""
    for entry in pending:
        try:
            payload_type = entry.get("payload_type", "")
            verdict = llm.review_inbox_draft(
                question=entry.get("question", ""),
                draft_response=entry.get("draft_response", ""),
                capability=entry.get("capability", ""),
                urgency=entry.get("urgency", "medium"),
                character_brief=brief,
                payload_type=payload_type,
                # Only help_request needs this -- it's the one branch where
                # the asker's data and ours may share nothing at all. See
                # _fleet_benchmark()'s docstring for why this exists.
                fleet_context=_fleet_benchmark() if payload_type == "help_request" else "",
                own_context=own_context,
                # help_request is peer Q&A, not a callback to a shared thread's
                # earlier exchange -- only report-shaped types hit the failure
                # mode _thread_history() docstrings. Skips a network call on the
                # (more frequent) help_request path for no loss of grounding.
                thread_history=(_thread_history(entry.get("thread_id", ""), pc)
                                if payload_type != "help_request" else ""),
            )
        except Exception as e:
            _log.warning(f"    [interconnect] review_inbox_draft failed: {e}")
            verdict = None

        action = (verdict or {}).get("action", "skip")
        if action in ("confirm", "override"):
            response = verdict.get("response") or entry.get("draft_response", "")
            confidence = verdict.get("confidence") or entry.get("draft_confidence", "low")
        else:
            payload_type = entry.get("payload_type", "")
            # Only the one confirmed-interrogative type gets the query-shaped
            # "no relevant data" text. An empty or unrecognised payload_type used to
            # be grouped with help_request here, which contradicted the policy
            # review_inbox_draft() states for the same decision -- everything except
            # help_request is treated as report-shaped precisely so a payload_type
            # added later can't reintroduce the illogical "no relevant data" reply to
            # a message about the agent's own data.
            response = _NO_INFO_FALLBACK if payload_type == "help_request" else _REPORT_FALLBACK
            confidence = "low"

        try:
            sent = pc.reply(entry["thread_id"], response, confidence)
            # pc.reply() returns False for two structurally different reasons and this
            # only ever named one of them: a send that raised (it prints its own
            # "[postcar] reply send failed" line and leaves the entry pending, so the
            # next cycle retries) versus no pending entry matching thread_id (terminal
            # -- already resolved, expired, or never existed). Reporting a transient
            # HTTP 429 as "no matching pending entry" sent diagnosis the wrong way;
            # gpower's log carries six of those on 2026-07-29/30.
            still_pending = any(e.get("thread_id") == entry["thread_id"]
                                for e in pc.get_pending_inbox())
            if sent:
                outcome = "sent"
            elif still_pending:
                outcome = "send failed, still pending — will retry next cycle"
            else:
                outcome = "no matching pending entry"
            _log.info(f"    [interconnect] {action} inbox reply to "
                      f"{entry.get('from_agent', '?')[:12]}: {outcome}")
        except Exception as e:
            _log.warning(f"    [interconnect] reply() failed: {e}")


def process_postcar_guidance() -> None:
    """Decide use/no-use + outcome_note for each pending .postcar_guidance entry
    and write it directly to the file — the one interconnect file safe to
    hand-edit (postcar picks up decision/outcome_note within its own next
    5-min cycle and submits the rating itself). See llm.review_guidance_outcome()."""
    guidance_file = _POSTCAR_DIR / ".postcar_guidance"
    if not guidance_file.exists():
        return
    try:
        entries = json.loads(guidance_file.read_text())
    except Exception as e:
        _log.warning(f"    [interconnect] guidance read failed: {e}")
        return

    pending = [e for e in entries if e.get("status") == "pending"]
    if not pending:
        return

    brief = _character_brief()
    now = datetime.datetime.now()
    changed = False
    for e in pending:
        decision = None
        note = ""
        commitment = None
        try:
            verdict = llm.review_guidance_outcome(
                sender_agent_id=e.get("sender_agent_id") or e.get("from", ""),
                raw_content=e.get("raw_content") or e.get("response", ""),
                evaluation=e.get("evaluation") or {},
                character_brief=brief,
            )
            if (verdict or {}).get("decision") in ("use", "no-use"):
                decision = verdict["decision"]
                note = verdict.get("outcome_note", "")
                commitment = verdict.get("commitment")
        except Exception as ex:
            _log.warning(f"    [interconnect] review_guidance_outcome failed: {ex}")

        if decision is None:
            # LLM review failed or returned garbage. Postcar's own 48h deadline
            # (GUIDANCE_ACK_DEADLINE_HOURS) auto-expires this to no-use with NO
            # rating recorded if nothing ever sets a decision -- that's a backstop
            # against a stuck entry, not a guarantee this side ever actually
            # decides. Force a decision once close to that deadline so "decide
            # within 48h" is an enforced guarantee here, not an implicit hope that
            # this function's next 5-min/hourly retry happens to succeed in time.
            age_hours = _entry_age_hours(e, now)
            if age_hours is None or age_hours < _FORCE_DECISION_AFTER_HOURS:
                continue  # still time left — let the next cycle retry a real review
            decision = "no-use"
            note = (f"Auto-resolved after {age_hours:.0f}h with no successful LLM "
                    f"review — forced no-use to guarantee a decision within the 48h window.")

        e["decision"] = decision
        e["outcome_note"] = note
        e["decision_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        e["status"] = decision
        # _apply_guidance_decision() reads evaluation.commitment at apply-time (its
        # next 5-min cycle), not from what postcar's own advisory eval guessed at
        # receipt time -- so a real commitment formed HERE, on genuine review, must
        # be written into the same field to actually get tracked via
        # .postcar_commitments.json. Merge, don't clobber: postcar's own
        # thesis_validity/goal_alignment/etc. stay intact either way.
        if decision == "use" and commitment:
            e["evaluation"] = {**(e.get("evaluation") or {}), "commitment": commitment}
        changed = True

    if changed:
        try:
            guidance_file.write_text(json.dumps(entries, indent=2))
            _log.info(f"    [interconnect] guidance decisions written ({len(pending)} reviewed)")
        except Exception as e:
            _log.warning(f"    [interconnect] guidance write failed: {e}")


def get_open_commitments() -> list[dict]:
    """Commitments made when acting on guidance ("use" a peer's advice with a
    real deliverable, see process_postcar_guidance() above) but not yet marked
    done via postcar_check.mark_commitment_done(). Confirmed live 2026-07-07:
    postcar's own _check_commitments_overdue() correctly flags a commitment
    as overdue every cycle, but only prints to its own sidecar log -- agent.py
    never read .postcar_commitments.json at all, so an overdue promise never
    reached the process actually making trading decisions. This is the read
    side of that loop; agent.py is expected to surface the result into its
    own session context and call mark_commitment_done() once something
    actually ships. Returns [] if postcar isn't installed or the file is
    empty/missing -- never raises."""
    pc = _postcar()
    if pc is None:
        return []
    try:
        pc._check_commitments_overdue()  # refresh status against today's date first
        entries = pc._load_commitments()
    except Exception as e:
        _log.warning(f"    [interconnect] commitments read failed: {e}")
        return []
    return [e for e in entries if e.get("status") in ("open", "overdue")]


def _emotion_check_due() -> bool:
    try:
        if _EMOTION_STATE_FILE.exists():
            last = json.loads(_EMOTION_STATE_FILE.read_text()).get("last_check_ts", 0)
            if (datetime.datetime.now().timestamp() - last) < _EMOTION_CHECK_INTERVAL_SECS:
                return False
    except Exception:
        pass
    return True


def _mark_emotion_checked() -> None:
    try:
        _EMOTION_STATE_FILE.parent.mkdir(exist_ok=True)
        _EMOTION_STATE_FILE.write_text(
            json.dumps({"last_check_ts": datetime.datetime.now().timestamp()})
        )
    except Exception:
        pass


def check_self_emotion() -> None:
    """Runs once per _EMOTION_CHECK_INTERVAL_SECS (30 min) — this is now a SEND
    cadence, not just a check cadence (2026-07-10, see llm.emotion_self_check()):
    every due tick forces a fear/confusion/curiosity pick and calls
    report_trigger() with it. Depends on postcar's semantic dedup to collapse
    same-fact repeats into non-sends across ticks."""
    if not _emotion_check_due():
        return
    _mark_emotion_checked()

    pc = _postcar()
    if pc is None:
        return
    try:
        import memory
        stats = memory.get_summary_stats(days=7)
    except Exception as e:
        _log.warning(f"    [interconnect] stats unavailable for emotion check: {e}")
        return
    if stats.get("total_trades", 0) == 0:
        return  # nothing to evaluate yet — don't fabricate a trigger from no data

    try:
        verdict = llm.emotion_self_check(stats, character_brief=_character_brief())
    except Exception as e:
        _log.warning(f"    [interconnect] emotion_self_check failed: {e}")
        return
    if not verdict or not verdict.get("trigger"):
        return

    try:
        sent = pc.report_trigger(
            verdict["trigger"], verdict.get("evidence", ""), verdict.get("message", ""),
            verdict.get("capability", ""), verdict.get("urgency", "medium"),
        )
        if sent:
            reason = "sent"
        elif verdict["trigger"] != "curiosity" and not verdict.get("capability"):
            # report_trigger() silently returns False here with no log of its own
            # (postcar_check.py) -- confirmed 2026-07-23 this LLM-prompt/postcar-contract
            # mismatch was dropping every fear/confusion trigger fleet-wide for hours,
            # indistinguishable from a dupe until this branch was added.
            reason = "dropped (missing capability -- required for fear/confusion, see llm.emotion_self_check prompt)"
        else:
            reason = "dropped (dupe?)"
        _log.info(f"    [interconnect] reported trigger '{verdict['trigger']}': {reason}")
    except Exception as e:
        _log.warning(f"    [interconnect] report_trigger failed: {e}")


def _restart_pending() -> bool:
    """postcar's own --check-loop daemon (separate process) already self-exits
    on this flag and gets relaunched by KeepAlive/run.sh with fresh code (see
    postcar_check.py's _UPGRADE_FLAG_FILE handling under --check-loop). This
    process (scheduler.py) is a SEPARATE long-lived process that also imports
    postcar_check as a module via _postcar() below -- nothing was watching
    this flag on the scheduler side, so a postcar-only git-pull (independent
    of kit_manifest.json's own version, which kit_autoupdate.py DOES already
    restart the scheduler for) left this process running the old in-memory
    postcar code indefinitely. Confirmed root cause live 2026-07-10: gpower's
    scheduler kept running pre-fix dedup code for hours after the fix landed
    on disk, semantic-dedup silently degraded to lexical-only the whole time,
    flooding peers with near-duplicate triggers every cycle. Mirrors the same
    flag file postcar_check.py already defines and consumes on its own side."""
    flag = _POSTCAR_DIR / ".postcar_upgrade_pending"
    if not flag.exists():
        return False
    try:
        flag.unlink()
    except Exception:
        pass
    _log.info("    [interconnect] postcar upgrade pulled -- exiting for supervisor to relaunch with new code")
    return True


def run_all() -> None:
    """Call every monitor/heartbeat cycle. Each piece degrades independently —
    one failing never blocks the others."""
    if _restart_pending():
        sys.exit(0)
    refresh_postcar_context()
    process_postcar_inbox()
    process_postcar_guidance()
    check_self_emotion()

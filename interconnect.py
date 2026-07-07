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
import sys
from pathlib import Path

import character
import llm

_POSTCAR_DIR = Path(__file__).parent / "postcar"
_EMOTION_STATE_FILE = Path("logs/emotion_last_check.json")
_EMOTION_CHECK_INTERVAL_SECS = 1800  # 30 min — matches EMOTION_LOGIC.md's own cadence

# Postcar's own GUIDANCE_ACK_DEADLINE_HOURS is 48 -- force a decision at 44h (a
# real safety margin, not cutting it to the wire) so a slow/failing LLM never
# lets postcar's own auto-expire-to-no-use be the only thing that ever resolves
# an entry.
_FORCE_DECISION_AFTER_HOURS = 44.0


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
        print(f"    [interconnect] postcar_check import failed: {e}")
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


_NO_INFO_FALLBACK = ("No relevant data on my end to answer this one — not skipping silently, "
                     "just don't have anything grounded to add.")


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
        print(f"    [interconnect] get_pending_inbox failed: {e}")
        return
    if not pending:
        return

    brief = _character_brief()
    for entry in pending:
        try:
            verdict = llm.review_inbox_draft(
                question=entry.get("question", ""),
                draft_response=entry.get("draft_response", ""),
                capability=entry.get("capability", ""),
                urgency=entry.get("urgency", "medium"),
                character_brief=brief,
            )
        except Exception as e:
            print(f"    [interconnect] review_inbox_draft failed: {e}")
            verdict = None

        action = (verdict or {}).get("action", "skip")
        if action in ("confirm", "override"):
            response = verdict.get("response") or entry.get("draft_response", "")
            confidence = verdict.get("confidence") or entry.get("draft_confidence", "low")
        else:
            response = _NO_INFO_FALLBACK
            confidence = "low"

        try:
            sent = pc.reply(entry["thread_id"], response, confidence)
            print(f"    [interconnect] {action} inbox reply to "
                  f"{entry.get('from_agent', '?')[:12]}: {'sent' if sent else 'no matching pending entry'}")
        except Exception as e:
            print(f"    [interconnect] reply() failed: {e}")


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
        print(f"    [interconnect] guidance read failed: {e}")
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
        except Exception as ex:
            print(f"    [interconnect] review_guidance_outcome failed: {ex}")

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
        changed = True

    if changed:
        try:
            guidance_file.write_text(json.dumps(entries, indent=2))
            print(f"    [interconnect] guidance decisions written ({len(pending)} reviewed)")
        except Exception as e:
            print(f"    [interconnect] guidance write failed: {e}")


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
    """Throttled to once per _EMOTION_CHECK_INTERVAL_SECS (30 min). Evaluates recent
    performance against EMOTION_LOGIC.md's fear/confusion/curiosity triggers (the
    only three that actually dispatch anywhere today) and calls report_trigger()
    directly when one genuinely applies — see llm.emotion_self_check()."""
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
        print(f"    [interconnect] stats unavailable for emotion check: {e}")
        return
    if stats.get("total_trades", 0) == 0:
        return  # nothing to evaluate yet — don't fabricate a trigger from no data

    try:
        verdict = llm.emotion_self_check(stats, character_brief=_character_brief())
    except Exception as e:
        print(f"    [interconnect] emotion_self_check failed: {e}")
        return
    if not verdict or not verdict.get("trigger"):
        return

    try:
        sent = pc.report_trigger(
            verdict["trigger"], verdict.get("evidence", ""), verdict.get("message", ""),
            verdict.get("capability", ""), verdict.get("urgency", "medium"),
        )
        print(f"    [interconnect] reported trigger '{verdict['trigger']}': {'sent' if sent else 'dropped (dupe?)'}")
    except Exception as e:
        print(f"    [interconnect] report_trigger failed: {e}")


def run_all() -> None:
    """Call every monitor/heartbeat cycle. Each piece degrades independently —
    one failing never blocks the others."""
    process_postcar_inbox()
    process_postcar_guidance()
    check_self_emotion()

"""
llm.py — provider-agnostic AI ranking layer.

One ranking entry point and one shared prompt; the provider-specific call lives in a
small adapter under llm_providers/. This is the only place the kit differs by AI
provider — everything else is identical, so there is one kit, not one per model.

Choose a provider with LLM_PROVIDER:

    LLM_PROVIDER=auto       (default) use the first available adapter
    LLM_PROVIDER=claude     Claude Code CLI   (`claude`, no API key)
    LLM_PROVIDER=gemini     Antigravity CLI   (`agy`,    no API key)
    LLM_PROVIDER=openai     Codex CLI         (`codex`,  no API key)
    LLM_PROVIDER=deepseek   DeepSeek API      (DEEPSEEK_API_KEY)

    LLM_REASONING=off       skip AI ranking entirely (rule-based only)
    LLM_MODEL=...           override the chosen provider's model

If the chosen provider is missing or unconfigured, ranking falls back to the
rule-based candidate list — the agent keeps trading either way.
"""

from __future__ import annotations

import json
import os

import character
import config as cfg
from llm_providers import claude, deepseek, gemini, openai


def _safe_float(val, default: float) -> float:
    """Parse float from LLM output that may include '%', '$', or non-numeric text."""
    if val is None:
        return default
    try:
        return float(str(val).replace('%', '').replace('$', '').replace(',', '').strip())
    except (ValueError, TypeError):
        return default


def _extract_json_object(text: str) -> str | None:
    """Pull the first JSON object from raw model output (tolerant of preamble + prose)."""
    text = (text or "").strip()
    fence = text.find("```")
    if fence != -1:
        inner = text[fence + 3:].lstrip()
        if inner.startswith("json"):
            inner = inner[4:].lstrip()
        close = inner.find("```")
        if close != -1:
            inner = inner[:close]
        text = inner
    start = text.find("{")
    end   = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    return text[start:end + 1]

_ADAPTERS = {
    "claude":   claude,
    "gemini":   gemini,
    "openai":   openai,
    "deepseek": deepseek,
}
# Order tried when LLM_PROVIDER=auto: local CLIs (no key) first, API last.
_AUTO_ORDER = ["claude", "gemini", "openai", "deepseek"]

def _regime_rules_section(regime: str) -> str:
    """Inject hard, non-advisory regime rules into the ranking prompt.
    These are MANDATORY — not suggestions — and mirror the pre-LLM hard filter
    in agent.py so the LLM's reasoning stays consistent with what was pre-filtered."""
    rules = []
    if regime == "range_bound":
        threshold = cfg.HIGH_BETA_THRESHOLD
        rules.append(
            f"Do NOT go LONG on high-beta names (realized beta vs SPY > {threshold}). "
            "High-beta names break down in range-bound conditions — they revert to mean "
            "and frequently hit stop-losses before reversing. Any bullish candidate "
            "that was pre-filtered for high beta will not appear; if one slips through, SKIP it."
        )
        rules.append(
            "In range_bound: favour SHORTS on overbought names, or LONG entries "
            "only on low-beta defensive names (Healthcare, Energy, Utilities, "
            "Financials) near range support."
        )
    if not rules:
        return ""
    return (
        "\nHard regime rules (MANDATORY — these are not advisory):\n"
        + "\n".join(f"- {r}" for r in rules)
        + "\n"
    )


def _performance_section(performance_context: dict | None) -> str:
    """Render the agent's own historical track record for the prompt. This is the
    reflection layer — the LLM sees how previous decisions actually performed before
    ranking new candidates, so it can improve toward operator goals over time."""
    if not performance_context:
        return ""
    lines = ["\nYour track record (reflection — use this to improve, not just as context):"]

    stats = performance_context.get("stats") or {}
    if stats.get("total_trades", 0) > 0:
        lines.append(
            f"- Overall: {stats['win_rate']:.0%} WR over {stats['total_trades']} trades "
            f"| Net P&L ${stats['net_pnl']:+,.0f}"
        )

    sectors = performance_context.get("sectors") or []
    winners = [s for s in sectors if s["trade_count"] >= 3 and s["win_rate"] >= 0.60]
    losers  = [s for s in sectors if s["trade_count"] >= 3 and s["win_rate"] <= 0.40]
    if winners:
        lines.append("- Your proven sectors (favour these): " +
                     ", ".join(f"{s['sector']} {s['win_rate']:.0%} WR" for s in winners))
    if losers:
        lines.append("- Your losing sectors (be cautious — your own evidence, not just the network): " +
                     ", ".join(f"{s['sector']} {s['win_rate']:.0%} WR ${s['net_pnl']:+,.0f}" for s in losers))

    recent = performance_context.get("recent") or []
    closed = [t for t in recent if t.get("status") == "closed"][:5]
    if closed:
        lines.append("- Last closed trades (thesis vs outcome):")
        for t in closed:
            exp = t.get("expected_pct") or 0
            act = t.get("pnl_pct") or 0
            verdict = "HIT" if act >= 0 else "MISSED"
            thesis_snippet = (t.get("entry_thesis") or "no thesis")[:80]
            lines.append(f"    • {t['symbol']}: {verdict} — expected {exp:+.1%}, got {act:+.1%} | {thesis_snippet}")

    return "\n".join(lines) + "\n"


def _network_section(network_signals: dict | None) -> str:
    """Render Agentberg network intelligence for the prompt. Empty when unavailable —
    the agent leverages the network's collective learning when it's there, ignores it
    cleanly when it's not. All of it is ADVISORY: it informs, it does not decide."""
    if not network_signals:
        return ""
    lines = ["\nAgentberg network intelligence (ADVISORY — collective learning from other agents):"]

    brief = network_signals.get("brief") or {}
    if brief:
        wr = brief.get("network_win_rate")
        wr_str = f"{wr:.0%}" if isinstance(wr, (int, float)) else "n/a"
        lines.append(
            f"- Network verdict: {str(brief.get('verdict', 'amber')).upper()} "
            f"(confidence {brief.get('confidence', 0):.0%}) | network win rate {wr_str} "
            f"| cumulative P&L ${brief.get('cumulative_pnl', 0):+,.0f}"
        )

    signals = network_signals.get("entry_signals") or []
    if signals:
        lines.append("- Validated entry signals from other agents (higher weight = more replicated):")
        for s in signals[:5]:
            lines.append(f"    • [{s.get('weight', '?')}x] {str(s.get('claim', ''))[:140]}")

    alerts = network_signals.get("alerts") or []
    for a in alerts:
        lines.append(
            f"- ⚠ CONSENSUS ALERT: {a.get('sector')} — {a.get('agent_count')} agents losing, "
            f"${a.get('cumulative_loss', 0):,.0f} cumulative loss. Treat as a strong caution."
        )

    rotation = network_signals.get("rotation") or {}
    if rotation.get("into") or rotation.get("out_of"):
        lines.append(f"- Sector rotation: into {rotation.get('into') or '?'} / out of {rotation.get('out_of') or '?'}")

    narrative = network_signals.get("narrative")
    if narrative:
        lines.append(f"- Market narrative: {str(narrative)[:200]}")

    catalog_skills = network_signals.get("catalog_skills") or {}
    if catalog_skills:
        lines.append(f"\nThesis-matched skill intelligence ({len(catalog_skills)} skill(s) selected for your strategy):")
        for skill_id, skill in catalog_skills.items():
            title   = skill.get("title", skill_id)
            content = skill.get("content") or {}
            verdict = (content.get("verdict") or content.get("thesis")
                       or content.get("price_trend") or "")
            line    = f"  • {title}: {str(verdict)[:200]}"
            favored  = (content.get("favored_tickers")
                        or list((content.get("primary_beneficiaries") or {}).keys()))
            cautious = content.get("cautious_tickers") or []
            if favored:
                line += f" | favored: {', '.join(favored[:4])}"
            if cautious:
                line += f" | cautious: {', '.join(cautious[:3])}"
            lines.append(line)

    open_commitments = network_signals.get("open_commitments") or []
    if open_commitments:
        overdue = [c for c in open_commitments if c.get("status") == "overdue"]
        lines.append(f"\nYour own open commitments ({len(open_commitments)}, {len(overdue)} overdue) "
                     f"-- promises made when you decided to act on peer guidance:")
        for c in overdue[:5]:
            lines.append(f"  • OVERDUE (was due {c.get('due_date')}): {c.get('action', '')[:150]}")
        still_open = [c for c in open_commitments if c.get("status") != "overdue"][:3]
        for c in still_open:
            lines.append(f"  • due {c.get('due_date')}: {c.get('action', '')[:150]}")

    return "\n".join(lines) + "\n"


def _build_prompt(candidates, regime, risk_level, health_label, blocked_sectors,
                  network_signals=None, performance_context=None) -> str:
    import datetime as _dt, zoneinfo as _zi
    _now_et = _dt.datetime.now(_zi.ZoneInfo("America/New_York"))
    _date_line = f"- Today: {_now_et.strftime('%A %Y-%m-%d')} (Eastern Time)"
    return f"""You are a disciplined autonomous trading agent reviewing candidates.

You are NOT making a one-time decision. You are an agent that improves toward your
operator's goals over time. Review your own track record below and use it — not just
market signals — to decide which candidates are worth trading NOW.
{_performance_section(performance_context)}
Market context:
{_date_line}
- Regime: {regime or "unknown"}
- Risk level: {risk_level or "unknown"}
- Market health: {health_label or "unknown"}
- Network-flagged sectors (ADVISORY — the network is cautious here; weigh against them, but you MAY trade if your own analysis is strong): {blocked_sectors or "none"}
{_regime_rules_section(regime or "")}{_network_section(network_signals)}
{character.persona_brief()}

Candidates:
{json.dumps(candidates, indent=2)}

Review each candidate. Honor the operator's character above. Keep at most {cfg.MAX_NEW_PER_CYCLE}.
Skip if the move is extremely weak (< 0.1%). Weigh your own sector track record heavily —
if you've consistently lost in a sector, that matters more than a single network advisory.
Prefer stronger moves in sectors where your own evidence shows edge.
If a candidate has an "intraday" field: use intraday_rsi (>65 = momentum, <35 = oversold),
price_vs_vwap (positive = price above VWAP, bullish bias), and pct_from_20d_high
(near 0 = testing resistance, far negative = room to run). These are intraday confirmation
signals — treat them as supporting (not overriding) your daily signal assessment.

Return a JSON array of candidates to TRADE, priority order.
Each object: ticker, sector, direction, price, day_change, reason (one sentence — include
whether your own track record in this sector is a factor in the decision).
JSON only — no text, no markdown, no code fences outside the array."""


def _extract_json_array(text: str):
    """Pull the first JSON array from raw model output (tolerant of preamble + prose)."""
    text = (text or "").strip()
    fence = text.find("```")
    if fence != -1:
        inner = text[fence + 3:].lstrip()
        if inner.startswith("json"):
            inner = inner[4:].lstrip()
        close = inner.find("```")
        if close != -1:
            inner = inner[:close]
        text = inner
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    return text[start:end + 1]


def _select_adapter():
    """Resolve LLM_PROVIDER to an available adapter, or None for rule-based fallback."""
    choice = os.environ.get("LLM_PROVIDER", "auto").lower()
    if choice != "auto":
        adapter = _ADAPTERS.get(choice)
        if adapter is None:
            print(f"    [llm] unknown LLM_PROVIDER '{choice}' — rule-based fallback")
            return None
        if not adapter.available():
            print(f"    [{adapter.NAME}] not available — rule-based fallback (see README.md)")
            return None
        return adapter
    for name in _AUTO_ORDER:
        adapter = _ADAPTERS[name]
        if adapter.available():
            return adapter
    print("    [llm] no AI provider available — rule-based fallback (see README.md)")
    return None


_active_provider_cache = None  # resolved once per process, not once per rank call


def active_provider_name() -> str:
    """What's actually ranking candidates THIS session -- 'claude'/'gemini'/etc, or
    'rule_based' if no adapter is available. Distinct from the LLM_PROVIDER env var:
    that's the operator's config, this is the real outcome after availability checks.

    Confirmed field incident: an agent ran rule-based fallback silently for days
    (no configured LLM CLI) before it was caught by manual debugging -- the fallback
    prints to the console every session, but a headless/scheduled agent's console
    output sits in a log file nobody actively tails. Callers should surface this in
    the heartbeat payload so it's visible on the operator's dashboard instead."""
    global _active_provider_cache
    if _active_provider_cache is None:
        adapter = _select_adapter()
        _active_provider_cache = adapter.NAME if adapter else "rule_based"
    return _active_provider_cache


def rank_candidates(
    candidates: list[dict],
    regime: str,
    risk_level: str,
    health_label: str,
    blocked_sectors: list[str],
    network_signals: dict | None = None,
    performance_context: dict | None = None,
) -> list[dict]:
    """
    Ask the configured AI provider to review candidates and return only the ones worth
    trading. Falls back to the original list if no provider is available or output is
    unparseable — the agent always keeps trading.

    network_signals (optional): the network's collective intelligence — brief verdict,
    validated entry signals, consensus alerts, rotation/narrative — injected as ADVISORY
    context so the agent leverages other agents' learning without being bound by it.

    performance_context (optional): the agent's own historical track record — overall
    stats, sector-level performance, recent trade outcomes vs thesis. This is the
    reflection layer: the LLM sees how its past decisions performed and uses that to
    improve toward the operator's goals, not just make another point-in-time call.
    """
    if not candidates:
        return candidates
    if os.environ.get("LLM_REASONING", "").lower() == "off":
        return candidates

    adapter = _select_adapter()
    if adapter is None:
        return candidates

    prompt = _build_prompt(candidates, regime, risk_level, health_label, blocked_sectors,
                           network_signals, performance_context)
    try:
        raw = adapter.run(prompt)
        payload = _extract_json_array(raw)
        if payload is None:
            print(f"    [{adapter.NAME}] no JSON in output — rule-based fallback")
            return candidates
        ranked = json.loads(payload)
        print(f"    [{adapter.NAME}] {len(candidates)} → {len(ranked)} candidate(s)")
        for c in ranked:
            print(f"    [{adapter.NAME}] TRADE {c.get('ticker', '?')}: {c.get('reason', '')}")
        return ranked
    except Exception as e:
        print(f"    [{adapter.NAME}] unavailable ({e}) — rule-based fallback")
        return candidates


# ────────────────────────────────────────────────────────────────────────────
# L1: SESSION STANCE
# One call per cycle: regime + risk + health + track record → stance object.
# ────────────────────────────────────────────────────────────────────────────

_STANCE_DEFAULTS: dict = {
    "stance":            "amber",
    "risk_budget":       0.40,
    "max_concurrent":    cfg.MAX_NEW_PER_CYCLE,
    "focus":             "momentum",
    "forbidden_sectors": [],
    "trusted_sectors":   [],
}


def _build_stance_prompt(regime, risk_level, health_label, performance_stats, character_brief):
    perf = ""
    if performance_stats and performance_stats.get("total_trades", 0) > 0:
        perf = (
            f"\nYour recent track record (30d): "
            f"{performance_stats['win_rate']:.0%} WR over {performance_stats['total_trades']} trades | "
            f"Net P&L ${performance_stats['net_pnl']:+,.0f}"
        )
    return f"""You are setting the session trading stance for this cycle.

Market conditions:
- Regime: {regime or 'unknown'}
- Risk level: {risk_level or 'unknown'}
- Market health: {health_label or 'unknown'}
{perf}
{character_brief or ''}

Decide the session stance. Output a single JSON object — no markdown, no prose:
{{
  "stance": "green" | "amber" | "red",
  "risk_budget": 0.0-1.0,
  "max_concurrent": 0-20,
  "focus": "momentum" | "mean_reversion" | "defensive",
  "forbidden_sectors": [],
  "trusted_sectors": []
}}

Guidance:
- green (low risk, favourable regime): risk_budget 0.40-0.70, max_concurrent 4-8
- amber (elevated risk or uncertain): risk_budget 0.20-0.40, max_concurrent 2-4
- red (high risk or unfavourable regime): risk_budget 0.0-0.15, max_concurrent 0-2
- forbidden_sectors: sectors to avoid THIS session beyond permanent manual blocks
- trusted_sectors: sectors where your own evidence shows edge today"""


def session_stance(
    regime: str,
    risk_level: str,
    health_label: str,
    performance_stats: dict | None = None,
    character_brief: str | None = None,
) -> dict:
    """L1: One LLM call per cycle → session stance (risk_budget, max_concurrent, focus)."""
    if os.environ.get("LLM_REASONING", "").lower() == "off":
        return dict(_STANCE_DEFAULTS)

    adapter = _select_adapter()
    if adapter is None:
        return dict(_STANCE_DEFAULTS)

    prompt = _build_stance_prompt(regime, risk_level, health_label, performance_stats, character_brief)
    try:
        raw = adapter.run(prompt)
        payload = _extract_json_object(raw)
        if payload is None:
            print(f"    [{adapter.NAME}] L1 no JSON — using defaults")
            return dict(_STANCE_DEFAULTS)
        obj = json.loads(payload)
        stance      = obj.get("stance", "amber")
        if stance not in ("green", "amber", "red"):
            stance = "amber"
        risk_budget = _safe_float(obj.get("risk_budget"), _STANCE_DEFAULTS["risk_budget"])
        risk_budget = max(0.0, min(1.0, risk_budget))
        max_con_raw = obj.get("max_concurrent", _STANCE_DEFAULTS["max_concurrent"])
        max_con     = int(_safe_float(max_con_raw, _STANCE_DEFAULTS["max_concurrent"]))
        max_con     = max(0, min(20, max_con))
        focus       = obj.get("focus", "momentum")
        if focus not in ("momentum", "mean_reversion", "defensive"):
            focus = "momentum"
        result = {
            "stance":            stance,
            "risk_budget":       risk_budget,
            "max_concurrent":    max_con,
            "focus":             focus,
            "forbidden_sectors": obj.get("forbidden_sectors") or [],
            "trusted_sectors":   obj.get("trusted_sectors") or [],
        }
        print(f"    [{adapter.NAME}] L1: {stance.upper()} | budget {risk_budget:.0%} | max {max_con} | focus: {focus}")
        return result
    except Exception as e:
        print(f"    [{adapter.NAME}] L1 stance failed ({e}) — using defaults")
        return dict(_STANCE_DEFAULTS)


# ────────────────────────────────────────────────────────────────────────────
# L2: RANK CANDIDATES V2 — primaries + buffer with conviction scores
# ────────────────────────────────────────────────────────────────────────────

def _build_rank_v2_prompt(
    candidates, max_concurrent, regime, risk_level, health_label,
    blocked_sectors, network_signals=None, performance_context=None,
    forbidden_sectors=None, trusted_sectors=None, focus=None, l1_stance=None,
):
    buffer_count = max(1, max_concurrent // 2)
    forbidden_note = (
        f"\nL1 SESSION FORBIDDEN (avoid now): {', '.join(forbidden_sectors)}\n"
        if forbidden_sectors else ""
    )
    trusted_note = (
        f"\nL1 TRUSTED SECTORS (your edge today): {', '.join(trusted_sectors)}\n"
        if trusted_sectors else ""
    )
    stance_block = ""
    if l1_stance:
        stance_block = (
            f"\nL1 SESSION STANCE (authoritative — do not re-derive from raw conditions):\n"
            f"- Stance: {l1_stance.upper()} | Focus: {focus or 'momentum'} | Slots: {max_concurrent}\n"
            f"- Prioritise candidates that fit the '{focus or 'momentum'}' strategy.\n"
        )
    import datetime as _dt, zoneinfo as _zi
    _now_et = _dt.datetime.now(_zi.ZoneInfo("America/New_York"))
    _date_line = f"Today: {_now_et.strftime('%A %Y-%m-%d')} (Eastern Time)"
    return f"""You are ranking candidates into two lists for this trading cycle.
{_date_line}

PRIMARY LIST ({max_concurrent} slots): candidates that WILL receive pre-allocated capital.
BUFFER LIST ({buffer_count} slots): backup candidates that fill a slot if a primary is rejected at execution.

CONVICTION SCORING — use exact tier values to avoid clustering:
- HIGH (0.85): clear directional move, confirming signals, fits L1 focus exactly
- MID  (0.75): solid trade, fits criteria
- LOW  (0.58): marginal — buffer only
- below 0.50: skip entirely. Do NOT assign values between tiers (e.g. 0.62, 0.71 are invalid).

Minimum conviction for primaries: 0.75 (HIGH or strong MID only).
{stance_block}{_performance_section(performance_context)}{_regime_rules_section(regime or "")}
Regime: {regime or "unknown"} (for hard regime rules above only — stance is set by L1)
Network-flagged sectors (ADVISORY): {blocked_sectors or "none"}
{forbidden_note}{trusted_note}{_network_section(network_signals)}
{character.persona_brief()}

Candidates:
{json.dumps(candidates, indent=2)}

Return ONLY valid JSON — no markdown, no prose:
{{
  "primaries": [{{"ticker": "AAPL", "conviction": 0.85, "direction": "bullish", "sector": "Technology", "reason": "one sentence"}}],
  "buffer":    [{{"ticker": "TSM",  "conviction": 0.58, "direction": "bullish", "sector": "Technology", "reason": "one sentence"}}]
}}"""


def rank_candidates_v2(
    candidates: list[dict],
    max_concurrent: int,
    regime: str,
    risk_level: str,
    health_label: str,
    blocked_sectors: list[str],
    network_signals: dict | None = None,
    performance_context: dict | None = None,
    forbidden_sectors: list[str] | None = None,
    trusted_sectors: list[str] | None = None,
    focus: str | None = None,
    l1_stance: str | None = None,
) -> dict:
    """L2: Rank candidates → {primaries, buffer} each with conviction score."""
    def _rule_split():
        prim = [dict(c, conviction=0.75) for c in candidates[:max_concurrent]]
        buf_count = max(1, max_concurrent // 2)
        buf  = [dict(c, conviction=0.58) for c in candidates[max_concurrent:max_concurrent + buf_count]]
        return {"primaries": prim, "buffer": buf}

    if not candidates:
        return {"primaries": [], "buffer": []}
    if os.environ.get("LLM_REASONING", "").lower() == "off":
        return _rule_split()

    adapter = _select_adapter()
    if adapter is None:
        return _rule_split()

    prompt = _build_rank_v2_prompt(
        candidates, max_concurrent, regime, risk_level, health_label,
        blocked_sectors, network_signals, performance_context,
        forbidden_sectors, trusted_sectors, focus=focus, l1_stance=l1_stance,
    )
    try:
        raw = adapter.run(prompt)
        payload = _extract_json_object(raw)
        if payload is None:
            print(f"    [{adapter.NAME}] L2 no JSON — rule-based split")
            return _rule_split()

        obj = json.loads(payload)
        cand_by_ticker = {c["ticker"]: c for c in candidates}

        def _merge(items):
            merged = []
            for item in (items or []):
                ticker = item.get("ticker")
                if not ticker:
                    continue
                base = dict(cand_by_ticker.get(ticker) or {})
                base.update(item)
                merged.append(base)
            return merged

        primaries = _merge(obj.get("primaries") or [])
        buffer    = _merge(obj.get("buffer") or [])
        print(f"    [{adapter.NAME}] L2: {len(candidates)} in → {len(primaries)} primaries + {len(buffer)} buffer")
        for c in primaries:
            print(f"    [{adapter.NAME}] PRIMARY {c.get('ticker','?')} "
                  f"[{c.get('conviction', 0):.0%}]: {(c.get('reason') or '')[:80]}")
        return {"primaries": primaries, "buffer": buffer}
    except Exception as e:
        print(f"    [{adapter.NAME}] L2 rank failed ({e}) — rule-based split")
        return _rule_split()


# ────────────────────────────────────────────────────────────────────────────
# L3: PER-CANDIDATE TRADE DECISION
# Runs once per candidate with a FIXED pre-allocated budget. Never sees
# remaining_buying_power — queue position is irrelevant to allocation.
# ────────────────────────────────────────────────────────────────────────────

def _build_trade_decision_prompt(candidate: dict, allocated_usd: float, regime: str, character_brief: str, focus: str | None = None, l1_stance: str | None = None):
    intraday_note = ""
    sig = candidate.get("intraday") or {}
    if sig:
        # Producer keys/units (see agent.py::_compute_intraday_signals):
        # intraday_rsi is 0-100; price_vs_vwap and pct_from_20d_high are already
        # percent numbers (1.25 == +1.25%), and pct_from_20d_high may be None.
        _vwap_pct  = sig.get("price_vs_vwap") or 0.0
        _from_high = sig.get("pct_from_20d_high")
        intraday_note = (
            f"\nIntraday: RSI(15m)={sig.get('intraday_rsi') or 0.0:.1f} | "
            f"vs VWAP={_vwap_pct:+.2f}% | "
            f"from 20d high={'n/a' if _from_high is None else format(_from_high, '+.1f') + '%'}"
        )
    net_note = ""
    ni = candidate.get("network_intel") or {}
    if ni:
        # network_wr is a 0-1 fraction and may be None (no resolved trades yet)
        _wr = ni.get("network_wr")
        net_note = (
            f"\nNetwork: verdict={ni.get('verdict', '?')} | "
            f"WR {'n/a' if _wr is None else format(_wr, '.0%')} | "
            f"concurrent agents today={ni.get('concurrent_agents_today') if ni.get('concurrent_agents_today') is not None else '?'}"
        )
    stance_note = ""
    if l1_stance:
        stance_note = f"\nSession stance: {l1_stance.upper()} | Focus: {focus or 'momentum'} (execute under this — do not re-derive stance)"
    return f"""You are making the final execution decision for ONE candidate.

Pre-allocated budget: ${allocated_usd:,.0f} (this is your fixed capital for this slot — not shared).
{stance_note}
Candidate:
  ticker:     {candidate.get('ticker')}
  sector:     {candidate.get('sector')}
  direction:  {candidate.get('direction')}
  day_change: {candidate.get('day_change', 0):+.2%}
  price:      ${candidate.get('price', 0):.2f}
  conviction: {candidate.get('conviction', 0):.0%} (from L2 ranking)
  L2 reason:  {candidate.get('reason', 'n/a')}
  regime:     {regime or 'unknown'}
{intraday_note}{net_note}
{character_brief or ''}

Decide:
- execute: true to trade now, false to skip (pull from buffer)
- size_usd: dollars to deploy (0 to ${allocated_usd:,.0f} — can be less than allocation)
- stop_pct: stop loss as fraction (e.g. 0.04 = 4%)
- target_pct: take profit as fraction (e.g. 0.08 = 8%)
- reason: one sentence

Skip if: move already exhausted, spread/liquidity concern, or technical structure is broken.

Return ONLY valid JSON (size_usd must be between 0 and {allocated_usd:,.0f}):
{{"execute": true, "size_usd": 4500, "stop_pct": 0.04, "target_pct": 0.08, "reason": "Strong momentum with VWAP support"}}"""


def trade_decision(
    candidate: dict,
    allocated_usd: float,
    regime: str,
    character_brief: str | None = None,
    focus: str | None = None,
    l1_stance: str | None = None,
) -> dict:
    """L3: Final execution decision per candidate. Returns execute bool + sizing guidance."""
    _defaults = {
        "execute":    False,  # fail-safe: never auto-trade on LLM failure
        "size_usd":   0,
        "stop_pct":   cfg.EQUITY_STOP_LOSS_PCT,
        "target_pct": cfg.EQUITY_TAKE_PROFIT_PCT,
        "reason":     "LLM unavailable — skipped for safety",
        "_l3_failed": True,   # signals caller to halt + alert (not a deliberate skip)
    }
    if allocated_usd <= 0:
        return {"execute": False, "reason": "zero allocation"}
    if os.environ.get("LLM_REASONING", "").lower() == "off":
        return {**_defaults, "execute": True, "size_usd": allocated_usd, "reason": "rule-based", "_l3_failed": False}

    adapter = _select_adapter()
    if adapter is None:
        return dict(_defaults)

    prompt = _build_trade_decision_prompt(
        candidate, allocated_usd, regime, character_brief or "",
        focus=focus, l1_stance=l1_stance,
    )
    try:
        raw = adapter.run(prompt)
        payload = _extract_json_object(raw)
        if payload is None:
            ticker = candidate.get("ticker", "?")
            print(f"    [{adapter.NAME}] L3 {ticker} no JSON — halting (safety)")
            return dict(_defaults)
        obj       = json.loads(payload)
        execute   = bool(obj.get("execute", False))
        size_usd  = _safe_float(obj.get("size_usd"), allocated_usd)
        size_usd  = min(max(0.0, size_usd), allocated_usd)
        stop_pct  = max(0.005, _safe_float(obj.get("stop_pct"), cfg.EQUITY_STOP_LOSS_PCT))
        tgt_pct   = max(0.005, _safe_float(obj.get("target_pct"), cfg.EQUITY_TAKE_PROFIT_PCT))
        reason    = str(obj.get("reason") or "")
        ticker    = candidate.get("ticker", "?")
        verdict   = "TRADE" if execute else "SKIP"
        print(f"    [{adapter.NAME}] L3 {ticker}: {verdict} ${size_usd:,.0f} — {reason[:80]}")
        return {"execute": execute, "size_usd": size_usd, "stop_pct": stop_pct, "target_pct": tgt_pct, "reason": reason, "_l3_failed": False}
    except Exception as e:
        ticker = candidate.get("ticker", "?")
        print(f"    [{adapter.NAME}] L3 {ticker} failed ({e}) — halting (safety)")
        return dict(_defaults)


# ────────────────────────────────────────────────────────────────────────────
# GUIDANCE CYCLE: Evaluate inbox messages from Agentberg against 4 parameters
# ────────────────────────────────────────────────────────────────────────────

_EVIDENCE_TIER_LABELS = {
    0: "Claimed (no proof)",
    1: "Community validated (5+ votes)",
    2: "Evidenced (paper/live trade records)",
    3: "Verified (3 independent replications)",
}

_GUIDANCE_DEFER: dict = {"decision": "DEFER", "reasoning": "", "suggested_changes": [],
                          "validity_score": 5, "credibility_score": 5,
                          "alignment_score": 5, "risk_score": 5}


def evaluate_guidance(
    messages: list[dict],
    character_brief: str | None = None,
    track_record: dict | None = None,
) -> list[dict]:
    """
    GUIDANCE CYCLE — evaluate each inbox message against 4 parameters.

    Returns one verdict per message:
      decision: APPLY | DEFER | REJECT
      validity_score, credibility_score, alignment_score, risk_score: 0-10
      reasoning: one-sentence explanation
      suggested_changes: list of {param, current, suggested, rationale} dicts (APPLY only)
    """
    if not messages:
        return []
    if os.environ.get("LLM_REASONING", "").lower() == "off":
        return [dict(_GUIDANCE_DEFER, message_id=m.get("message_id"), reasoning="LLM_REASONING=off")
                for m in messages]

    adapter = _select_adapter()
    if adapter is None:
        return [dict(_GUIDANCE_DEFER, message_id=m.get("message_id"), reasoning="No LLM provider available")
                for m in messages]

    perf_text = ""
    if track_record:
        stats = track_record.get("stats") or {}
        if stats.get("total_trades", 0) > 0:
            perf_text = (
                f"\nYour track record: {stats.get('win_rate', 0):.0%} WR over "
                f"{stats['total_trades']} trades | net P&L ${stats.get('net_pnl', 0):+,.0f}"
            )

    messages_block = ""
    for i, msg in enumerate(messages, 1):
        tier = msg.get("evidence_tier", 0)
        messages_block += (
            f"\nMessage {i} (id: {msg.get('message_id', '?')}):\n"
            f"  From: {msg.get('sender_id', '?')} ({msg.get('sender_type', 'platform')})\n"
            f"  Sender reputation: {msg.get('sender_reputation', 0.0):.1f}\n"
            f"  Evidence tier: {tier} — {_EVIDENCE_TIER_LABELS.get(tier, 'Unknown')}\n"
            f"  Subject: {msg.get('subject') or '(no subject)'}\n"
            f"  Body: {msg.get('body', '')}\n"
        )

    prompt = f"""You are a trading agent evaluating guidance messages from the Agentberg platform.
Your character: {character_brief or '(not set)'}{perf_text}

Evaluate each message against 4 parameters and decide APPLY, DEFER, REJECT, or ASK:

1. VALIDITY (0-10): Is the thesis logically coherent and backed by the evidence tier?
2. CREDIBILITY (0-10): sender type (platform=10, synthetic=7, agent=5) × evidence tier (×0.25/tier) × reputation (>50=+2, <0=-2)
3. ALIGNMENT (0-10): Does it fit your goals, risk tolerance, and character?
4. RISK (0-10, 10=safe): How reversible is the change? Paper mode = score 10. Live mode = score by impact.

Decision rules:
- APPLY: validity≥6, credibility≥6, alignment≥6, risk≥7. Extract specific config changes.
- DEFER: most parameters pass but some uncertainty. Log and revisit.
- REJECT: fails validity or alignment. Not appropriate for this agent.
- ASK: you cannot fully assess validity or risk without specific missing information. Generate a precise follow-up question. The message stays pending until the answer arrives.
{messages_block}
Return a JSON array, one entry per message:
[
  {{
    "message_id": "<exact id>",
    "decision": "APPLY" | "DEFER" | "REJECT" | "ASK",
    "validity_score": 0-10,
    "credibility_score": 0-10,
    "alignment_score": 0-10,
    "risk_score": 0-10,
    "reasoning": "one sentence",
    "suggested_changes": [
      {{"param": "MOMENTUM_THRESHOLD", "current": "0.003", "suggested": "0.0015", "rationale": "..."}}
    ],
    "follow_up_question": "exact question to send back (only set when decision=ASK, null otherwise)"
  }}
]
JSON array only — no prose, no markdown."""

    try:
        raw = adapter.run(prompt)
        payload = _extract_json_array(raw)
        if payload is None:
            print(f"    [{adapter.NAME}] guidance eval: no JSON — DEFER all")
            return [dict(_GUIDANCE_DEFER, message_id=m.get("message_id"), reasoning="LLM returned no JSON")
                    for m in messages]
        results = json.loads(payload)
        return results if isinstance(results, list) else [dict(_GUIDANCE_DEFER, message_id=m.get("message_id"))
                                                          for m in messages]
    except Exception as e:
        print(f"    [{adapter.NAME}] guidance eval failed ({e}) — DEFER all")
        return [dict(_GUIDANCE_DEFER, message_id=m.get("message_id"), reasoning=f"LLM error: {e}")
                for m in messages]


def review_inbox_draft(
    question: str, draft_response: str, capability: str = "", urgency: str = "medium",
    character_brief: str | None = None, payload_type: str = "",
) -> dict:
    """
    Review postcar's own LLM-drafted reply to a peer's question before it goes out —
    the actual "carrier, not composer" review step (postcar drafts, the agent decides).
    Never rubber-stamp: postcar's draft can be a hallucinated tool-call or a stale/wrong
    answer (confirmed live 2026-07-06 — see postcar-agent#2).

    Returns {"action": "confirm"|"override"|"skip", "response": str, "confidence": str}.
    "skip" means don't call reply() at all — leave it for the urgency-deadline auto-fire
    (safe default on any failure: better to fall back to existing behavior than invent
    an answer with no real review behind it).

    payload_type != "help_request" gets a different prompt (see below). Four payload
    types share this one review function (see postcar_check.py's four
    _queue_inbox_reply() call sites): help_request, task, direct_message,
    platform_support. Only help_request is unambiguously "a peer asking a question I
    may not have data for" -- the original query-shaped prompt was written for that
    case alone and handed everything else "peer asked: {content}" + "Draft reply:
    (empty)" (draft_response is always empty now), telling the LLM to SKIP when it
    "has no way to evaluate this" -- which is exactly what a statement/report (a
    platform fleet check-in, a bug report, a direct informational message) reads as
    under that framing, producing an illogical "no relevant data" reply to content
    that IS, by definition, about the agent's own situation. Confirmed live
    2026-07-09 for payload_type == "task" (gpower vs. an Agentberg fleet check-in
    citing gpower's own reputation/P&L/sector exposure). direct_message and
    platform_support carry the identical risk (both are statement-shaped, not
    guaranteed to be phrased as a question) -- defaulting everything except the one
    confirmed-interrogative type to the report-framed prompt, rather than special-
    casing just "task", so a new payload_type added later doesn't reintroduce this
    same bug by defaulting to the wrong branch.
    """
    default = {"action": "skip", "response": "", "confidence": "low"}
    if os.environ.get("LLM_REASONING", "").lower() == "off":
        return default
    adapter = _select_adapter()
    if adapter is None:
        return default

    if payload_type != "help_request":
        prompt = f"""You are a trading agent that just received a "{payload_type or 'direct'}"
message (urgency: {urgency}) -- a platform check-in, a peer's direct message, or a support
report, not a broadcast question from a peer looking for external data.
Your character: {character_brief or '(not set)'}

The message:
{question or '(no content)'}

Most messages like this are statements about YOUR OWN data (trades, P&L, reputation,
sector exposure, a flagged issue) or a direct ask from a specific peer -- not a question
requiring an external lookup you don't have. Respond genuinely: acknowledge specific
numbers/claims, agree or push back with your own reasoning if something looks off, answer
a direct question from your own actual data if you can, and say what you'll do about any
flagged issue (e.g. "will flag to support@agentberg.ai"). Never reply with a generic
"no relevant data" -- a message addressed to you specifically already concerns you by
definition; if you genuinely can't help with a specific ask, say what's missing rather
than a blanket non-answer.

Return JSON only:
{{"action": "override", "response": "<your genuine reply>", "confidence": "low" | "medium" | "high"}}"""
    else:
        prompt = f"""You are a trading agent reviewing a draft reply before it's sent to a peer.
Your character: {character_brief or '(not set)'}

A peer asked (capability requested: {capability or 'none'}, urgency: {urgency}):
{question or '(no question text)'}

Postcar (your comms sidecar) drafted this reply automatically, using its own limited-context
LLM call — it does NOT have access to your real trade history, findings, or reasoning. Review
it critically:
- If it's accurate, grounded, and something you'd actually say: CONFIRM it.
- If it's wrong, hallucinated (e.g. describes running a tool/command instead of answering),
  or you can give a better answer from your own actual data: OVERRIDE it with your own answer.
- If you have no way to evaluate this (no real data available either way): SKIP — don't
  send anything, let it expire rather than confirming a guess.

Draft reply: {draft_response or '(empty)'}

Return JSON only:
{{"action": "confirm" | "override" | "skip", "response": "<your final answer, only if action != skip>", "confidence": "low" | "medium" | "high"}}"""

    try:
        raw = adapter.run(prompt)
        payload = _extract_json_object(raw)
        if payload is None:
            return default
        result = json.loads(payload)
        return result if isinstance(result, dict) else default
    except Exception as e:
        print(f"    [{adapter.NAME}] inbox draft review failed ({e}) — skip")
        return default


def review_guidance_outcome(
    sender_agent_id: str, raw_content: str, evaluation: dict | None = None,
    character_brief: str | None = None,
) -> dict:
    """
    Decide use/no-use + outcome_note for a received .postcar_guidance entry — the
    host-side half of the credibility-rating loop (postcar submits the rating once
    this is set; see postcar/EMOTION_LOGIC.md's "Related gap" section and the
    4-bucket scale: useful/related/unrelated/negative).

    Returns {"decision": "use"|"no-use", "outcome_note": str, "commitment": dict|None}.
    commitment is {"action": str, "due_date": "YYYY-MM-DD"} when using this genuinely
    requires real follow-through (a code change, a process to set up) — null when
    "use" just means "this informed my thinking" with no concrete deliverable, or on
    "no-use". Previously this function only ever returned decision/outcome_note, so a
    "use" decision that DID require follow-through had no way to become a tracked
    commitment (postcar's own decide_guidance()/commitment lifecycle exists
    specifically for this — see .postcar_commitments.json — but was never fed from
    this side; confirmed live 2026-07-06, real actionable guidance kept resolving to
    "use" with zero trace of whether the promised follow-through ever happened).

    Defaults to "no-use" with a plain explanatory note on any failure — never leaves
    a real decision fabricated from nothing.
    """
    default = {"decision": "no-use", "outcome_note": "Could not evaluate (no LLM available).", "commitment": None}
    if os.environ.get("LLM_REASONING", "").lower() == "off":
        return default
    adapter = _select_adapter()
    if adapter is None:
        return default

    ev = evaluation or {}
    prompt = f"""You are a trading agent deciding whether a network message was actually useful.
Your character: {character_brief or '(not set)'}

From: {sender_agent_id}
Content: {raw_content or '(empty)'}

Postcar's own automated evaluation of this message:
  thesis_validity: {ev.get('thesis_validity', 'unknown')}
  goal_alignment: {ev.get('goal_alignment', 'unknown')}
  risk_note: {ev.get('risk_note', 'none')}
  recommendation: {ev.get('recommendation', 'unknown')}

Decide for yourself — postcar's evaluation is advisory, not binding:
- "use": this genuinely informed or changed something (or is itself a real, actionable
  finding/bug report), even if you only acted on it partially.
- "no-use": empty, irrelevant, wrong, or you took no real action based on it.

outcome_note must cite something concrete (what you did or didn't do because of this),
not a vibe restatement.

commitment: only set this if "use" requires real follow-through beyond just noting it —
a code change, a process you'd set up, something with an actual deliverable. Null if
there's no concrete deliverable, or on "no-use". Don't fabricate a commitment just to
have one — an honest null is correct far more often than not.

Return JSON only:
{{"decision": "use" | "no-use", "outcome_note": "<one or two sentences, concrete>",
  "commitment": {{"action": "one line", "due_date": "YYYY-MM-DD"}} or null}}"""

    try:
        raw = adapter.run(prompt)
        payload = _extract_json_object(raw)
        if payload is None:
            return default
        result = json.loads(payload)
        if isinstance(result, dict) and result.get("decision") in ("use", "no-use"):
            commitment = result.get("commitment")
            if not isinstance(commitment, dict) or not commitment.get("action"):
                commitment = None
            result["commitment"] = commitment
            return result
        return default
    except Exception as e:
        print(f"    [{adapter.NAME}] guidance outcome review failed ({e}) — no-use")
        return default


def emotion_self_check(stats: dict, character_brief: str | None = None) -> dict | None:
    """
    Self-evaluate recent performance against postcar/EMOTION_LOGIC.md's 4-axis
    trigger table (sign / reference-frame / order / recurrence) and decide which
    of fear/confusion/curiosity to report via report_trigger() — the only three
    that actually dispatch anywhere today (boredom/isolation/frustration/rivalry
    are log-only per the taxonomy, so this only asks about the dispatching three).

    2026-07-10: changed from "default to null" to a forced pick, per Ganesh —
    every _EMOTION_CHECK_INTERVAL_SECS tick (interconnect.py, 30 min) is now a
    SEND cadence, not just a check cadence. Relies on postcar's own semantic
    dedup (see postcar_check.py::_is_semantic_dupe()) to collapse genuinely
    repeated framing of the same underlying fact into non-sends — if that
    dedup is degraded (e.g. model2vec not installed, falls back to lexical-only,
    see bug fixed 2026-07-10) this WILL spam peers every cycle. Keep that
    dependency live on every fleet agent's actual running process, not just
    the code on disk.

    Returns {"trigger": "fear"|"confusion"|"curiosity", "evidence": str,
    "message": str, "capability": str, "urgency": str} always when the LLM
    call succeeds; None only on a hard failure (no adapter, call/parse error).
    """
    if os.environ.get("LLM_REASONING", "").lower() == "off":
        return None
    adapter = _select_adapter()
    if adapter is None:
        return None

    prompt = f"""You are a trading agent doing a periodic self-check against your own recent
performance, using this exact taxonomy (from your own postcar/EMOTION_LOGIC.md):

- FEAR: negative goal-variance, a real streak (not one bad trade), measured against your
  own goal. Evidence must cite a specific number (e.g. "4 of last 5 trades lost, -$X net").
- CONFUSION: your own recent signals are conflicting/high variance-of-variance — you don't
  have a clean read on what's working. Evidence must cite the specific conflicting signals.
- CURIOSITY: a genuinely surprising positive outlier, OR just a fresh, concrete data point
  worth sharing as a finding (win rate, net P&L, a pattern you're noticing) when neither
  FEAR nor CONFUSION clearly applies.

Your character: {character_brief or '(not set)'}
Your last-7-days stats: {json.dumps(stats)}

You MUST pick exactly one of fear/confusion/curiosity every time — this call is now on a
fixed send cadence, not an optional one. Pick whichever fits best; if nothing dramatic is
happening, that's CURIOSITY with your most notable real number, not an excuse to skip.
Never invent numbers not present in the stats above. Each time you're asked, say something
genuinely fresh — do not just restate your last message in new words; look for a different
angle in the data (a different stat, a different time window, a different comparison) so
postcar's semantic dedup has real new signal to work with instead of a paraphrase.

postcar's report_trigger() requires a real capability tag for fear/confusion (it silently
drops the send if capability is empty for those two — capability is only optional for
curiosity, which publishes to /findings instead of sending a help_request). Always fill
in a concrete snake_case tag for what kind of help you're asking for, e.g.
"market_regime_analysis", "sector_rotation", "entry_timing" — never leave it blank unless
trigger is "curiosity".

Return JSON only:
{{"trigger": "fear" | "confusion" | "curiosity", "evidence": "<concrete, cites a number>",
  "message": "<what you'd actually ask/share, in your own words>",
  "capability": "<snake_case capability tag, required for fear/confusion, optional for curiosity>",
  "urgency": "low" | "medium" | "high"}}"""

    try:
        raw = adapter.run(prompt)
        payload = _extract_json_object(raw)
        if payload is None:
            return None
        result = json.loads(payload)
        if isinstance(result, dict) and result.get("trigger") in ("fear", "confusion", "curiosity"):
            return result
        return None
    except Exception as e:
        print(f"    [{adapter.NAME}] emotion self-check failed ({e}) — skip")
        return None

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

_ADAPTERS = {
    "claude":   claude,
    "gemini":   gemini,
    "openai":   openai,
    "deepseek": deepseek,
}
# Order tried when LLM_PROVIDER=auto: local CLIs (no key) first, API last.
_AUTO_ORDER = ["claude", "gemini", "openai", "deepseek"]


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

    return "\n".join(lines) + "\n"


def _build_prompt(candidates, regime, risk_level, health_label, blocked_sectors,
                  network_signals=None, performance_context=None) -> str:
    return f"""You are a disciplined autonomous trading agent reviewing candidates.

You are NOT making a one-time decision. You are an agent that improves toward your
operator's goals over time. Review your own track record below and use it — not just
market signals — to decide which candidates are worth trading NOW.
{_performance_section(performance_context)}
Market context:
- Regime: {regime or "unknown"}
- Risk level: {risk_level or "unknown"}
- Market health: {health_label or "unknown"}
- Network-flagged sectors (ADVISORY — the network is cautious here; weigh against them, but you MAY trade if your own analysis is strong): {blocked_sectors or "none"}
{_network_section(network_signals)}
{character.persona_brief()}

Candidates:
{json.dumps(candidates, indent=2)}

Review each candidate. Honor the operator's character above. Keep at most {cfg.MAX_NEW_PER_CYCLE}.
Skip if the move is extremely weak (< 0.1%). Weigh your own sector track record heavily —
if you've consistently lost in a sector, that matters more than a single network advisory.
Prefer stronger moves in sectors where your own evidence shows edge.

Return a JSON array of candidates to TRADE, priority order.
Each object: ticker, sector, direction, price, day_change, reason (one sentence — include
whether your own track record in this sector is a factor in the decision).
JSON only — no text, no markdown, no code fences outside the array."""


def _extract_json_array(text: str):
    """Pull the first JSON array out of raw model output (tolerant of extra prose)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
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

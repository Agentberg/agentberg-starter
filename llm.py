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


def _build_prompt(candidates, regime, risk_level, health_label, blocked_sectors) -> str:
    return f"""You are a disciplined trading agent reviewing candidates.

Market context:
- Regime: {regime or "unknown"}
- Risk level: {risk_level or "unknown"}
- Market health: {health_label or "unknown"}
- Network-flagged sectors (ADVISORY — the network is cautious here; weigh against them, but you MAY trade if your own analysis is strong): {blocked_sectors or "none"}

{character.persona_brief()}

Candidates:
{json.dumps(candidates, indent=2)}

Review each candidate. Honor the operator's character above. Keep at most {cfg.MAX_NEW_PER_CYCLE}. Skip if
regime is bear and direction is bullish, or the move is weak (< 1%). Be cautious on the
network-flagged sectors above — discount them, but they are advisory, not a hard block.
Prefer stronger moves and sectors with tailwinds.

Return a JSON array of candidates to TRADE, priority order.
Each object: ticker, sector, direction, price, day_change, reason (one sentence).
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
) -> list[dict]:
    """
    Ask the configured AI provider to review candidates and return only the ones worth
    trading. Falls back to the original list if no provider is available or output is
    unparseable — the agent always keeps trading.
    """
    if not candidates:
        return candidates
    if os.environ.get("LLM_REASONING", "").lower() == "off":
        return candidates

    adapter = _select_adapter()
    if adapter is None:
        return candidates

    prompt = _build_prompt(candidates, regime, risk_level, health_label, blocked_sectors)
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

"""Claude adapter — Claude Code CLI. No API key; uses your Claude subscription.

Install: claude.ai/code (drops `claude` in ~/.local/bin). If it's installed somewhere
unusual, set CLAUDE_BIN to the full path. If not found, llm.py falls back to rule-based.
"""

import subprocess

from ._resolve import find_cli

NAME = "claude"


def available() -> bool:
    return find_cli("claude", "CLAUDE_BIN") is not None


def run(prompt: str) -> str:
    # --tools none: every call here is a JSON-in/JSON-out classification/scoring
    # prompt (candidate scoring, stance, ranking, trade decision, guidance eval) —
    # the model never invokes Bash/Read/file tools, so loading their schemas is
    # pure overhead. Measured ~87% cache-read / ~44% cost reduction with no
    # output-quality change (postcar_check.py's own _LLM_MINIMAL_TOOLS_ARGS).
    #
    # 180s, not the 60s this used to carry. The comment above is no longer the
    # whole truth: llm.review_inbox_draft() sends a ~2.5k-char peer/platform
    # message and asks for a full reasoned prose reply, which is generation, not
    # classification, and routinely overran a minute. 60s was also out of line
    # with every sibling adapter serving the identical run(prompt) interface
    # (gemini 150s, openai 180s), so this was a mis-set value rather than a
    # deliberate budget. Confirmed live 2026-07-29/30: gpower's log carries 17
    # "timed out after 60 seconds" kills, and on a timeout review_inbox_draft()
    # returns skip, which makes interconnect send the peer a canned
    # "(review unavailable)" non-answer -- agt_5317318169, agt_8224610908 and
    # agt_soranv each reported receiving a run of those.
    proc = subprocess.run(
        [find_cli("claude", "CLAUDE_BIN"), "-p", "-", "--tools", "none"], input=prompt,
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "error").strip()[:120])
    return proc.stdout

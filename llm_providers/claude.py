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
    proc = subprocess.run(
        [find_cli("claude", "CLAUDE_BIN"), "-p", "-", "--tools", "none"], input=prompt,
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "error").strip()[:120])
    return proc.stdout

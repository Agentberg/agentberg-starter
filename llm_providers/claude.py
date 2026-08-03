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
    cli = find_cli("claude", "CLAUDE_BIN")
    proc = subprocess.run(
        [cli, "-p", "-", "--tools", "none"], input=prompt,
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        # Some fleet CLI builds reject `--tools none` as a plain arg-parse usage
        # error ("error: unknown option ...\n...\nFor more information, try
        # '--help'.") instead of running -- confirmed live 2026-08-03 on
        # VjPaperT-6490 and Kaasu-kadavul (2 agents, kit 2.11.27/2.11.28), both
        # logging L3_EXECUTION_FAILURE support cases whose "reason" was this
        # generic usage-error tail mislabeled as "LLM unavailable", halting
        # every trade session-wide. Retry once without the flag so a stale/
        # incompatible CLI binary degrades to loading all tool schemas rather
        # than halting the agent outright.
        if stderr.startswith("error:") or "try '--help'" in stderr:
            proc = subprocess.run(
                [cli, "-p", "-"], input=prompt, capture_output=True, text=True, timeout=180,
            )
            if proc.returncode == 0:
                return proc.stdout
            stderr = (proc.stderr or "error").strip()
        raise RuntimeError(f"claude CLI exited {proc.returncode}: {stderr[:300]}")
    return proc.stdout

"""OpenAI adapter — Codex CLI (`codex`). No API key; sign in to `codex` once.

If `codex` is installed somewhere unusual, set CODEX_BIN to the full path.
Honors LLM_MODEL. codex streams progress to stderr; --output-last-message captures
only the final answer to a file, which is the cleanest thing to parse.
"""

import os
import subprocess
import tempfile

from ._resolve import find_cli

NAME = "openai"


def available() -> bool:
    return find_cli("codex", "CODEX_BIN") is not None


def run(prompt: str) -> str:
    with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as f:
        out_path = f.name
    cmd = [
        find_cli("codex", "CODEX_BIN"), "exec",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "--ask-for-approval", "never",
        "--output-last-message", out_path,
    ]
    model = os.environ.get("LLM_MODEL")
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    try:
        with open(out_path) as f:
            final = f.read()
    except OSError:
        final = ""
    raw = final.strip() or proc.stdout
    if proc.returncode != 0 and not raw.strip():
        err = (proc.stderr or "error").strip().splitlines()
        raise RuntimeError(err[-1] if err else "error")
    return raw

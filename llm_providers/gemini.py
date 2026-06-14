"""Gemini adapter — Antigravity CLI (`agy`). No API key; sign in to `agy` once.

If `agy` is installed somewhere unusual, set AGY_BIN to the full path.
Honors LLM_MODEL to override the agy model.
"""

import os
import subprocess

from ._resolve import find_cli

NAME = "gemini"


def available() -> bool:
    return find_cli("agy", "AGY_BIN") is not None


def run(prompt: str) -> str:
    cmd = [find_cli("agy", "AGY_BIN"), "-p", prompt, "--print-timeout", "120s"]
    model = os.environ.get("LLM_MODEL")
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "error").strip().splitlines()
        raise RuntimeError(err[-1] if err else "error")
    return proc.stdout

"""DeepSeek adapter — API (~$0.001/cycle). The one provider that uses an API key.

Set DEEPSEEK_API_KEY (or LLM_API_KEY) and `pip install openai`. Free key at
platform.deepseek.com. If unset/uninstalled, llm.py falls back to rule-based.
Honors LLM_MODEL (default: deepseek-chat).
"""

from __future__ import annotations

import os

NAME = "deepseek"


def _key() -> str | None:
    return os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY")


def available() -> bool:
    if not _key():
        return False
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return True


def run(prompt: str) -> str:
    from openai import OpenAI
    model = os.environ.get("LLM_MODEL", "deepseek-chat")
    client = OpenAI(api_key=_key(), base_url="https://api.deepseek.com")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.2,
    )
    return resp.choices[0].message.content

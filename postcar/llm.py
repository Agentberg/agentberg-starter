"""
LLM provider detection and call utilities for postcar-agent.
"""

import os
import shutil
import subprocess


def detect_provider() -> str | None:
    """Detect the available LLM provider.

    Detection order:
      1. shutil.which("claude") -> "claude-cli"
      2. shutil.which("gemini") -> "gemini-cli"
      3. shutil.which("codex")  -> "codex-cli"
      4. ANTHROPIC_API_KEY env  -> "claude-api"
      5. GOOGLE_API_KEY env     -> "gemini-api"
      6. OPENAI_API_KEY env     -> "openai-api"
      7. None
    """
    if shutil.which("claude"):
        return "claude-cli"
    if shutil.which("gemini"):
        return "gemini-cli"
    if shutil.which("codex"):
        return "codex-cli"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude-api"
    if os.environ.get("GOOGLE_API_KEY"):
        return "gemini-api"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai-api"
    return None


def call_llm(prompt: str, system: str = "", expect_json: bool = False) -> str:
    """Call the detected LLM provider and return the response text.

    Never raises exceptions — returns "" on any error or when no provider found.
    """
    try:
        provider = detect_provider()

        if provider == "claude-cli":
            return _call_claude_cli(prompt)

        if provider == "gemini-cli":
            return _call_gemini_cli(prompt)

        if provider == "codex-cli":
            return _call_codex_cli(prompt)

        if provider == "claude-api":
            return _call_claude_api(prompt, system)

        if provider == "gemini-api":
            return _call_gemini_api(prompt, system)

        if provider == "openai-api":
            return _call_openai_api(prompt, system)

    except Exception:
        pass

    return ""


def make_template_query(topic: str, indicators: str) -> str:
    """Build a standard guidance query string."""
    return f"I need guidance on: {topic}. My current stress indicators: {indicators}."


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _call_claude_cli(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--no-memory"],
            timeout=60,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        # Fallback without --no-memory
        result = subprocess.run(
            ["claude", "-p", prompt],
            timeout=60,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _call_gemini_cli(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["gemini", "-p", prompt],
            timeout=60,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _call_codex_cli(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["codex", prompt],
            timeout=60,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _call_claude_api(prompt: str, system: str = "") -> str:
    try:
        import anthropic  # type: ignore

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        messages = [{"role": "user", "content": prompt}]
        kwargs = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        response = client.messages.create(**kwargs)
        return response.content[0].text.strip()
    except Exception:
        pass
    return ""


def _call_gemini_api(prompt: str, system: str = "") -> str:
    try:
        import google.generativeai as genai  # type: ignore

        genai.configure(api_key=os.environ.get("GOOGLE_API_KEY"))
        model = genai.GenerativeModel("gemini-pro")
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        response = model.generate_content(full_prompt)
        return response.text.strip()
    except Exception:
        pass
    return ""


def _call_openai_api(prompt: str, system: str = "") -> str:
    try:
        import openai  # type: ignore

        client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=1024,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        pass
    return ""

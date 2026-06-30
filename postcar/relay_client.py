"""
relay_client.py — HTTP wrapper for all PostCar relay API calls.

Usage:
    client = PostCarClient.from_env()
    client.heartbeat(stress=0.2, version="0.2.0")
    qid = client.send_query(tags=["trading"], question="What is the trend?")
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional


def load_env(env_dir: str = ".") -> Dict[str, str]:
    """Load .env file from env_dir and return a dict of key=value pairs.

    Tries python-dotenv first; falls back to manual parsing.
    Does not modify os.environ.
    """
    env_path = os.path.join(env_dir, ".env")
    result: Dict[str, str] = {}

    try:
        from dotenv import dotenv_values  # type: ignore
        loaded = dotenv_values(env_path)
        result = {k: v for k, v in loaded.items() if v is not None}
        return result
    except ImportError:
        pass

    # Manual parse fallback
    try:
        with open(env_path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Strip inline comments
                if " #" in value:
                    value = value[: value.index(" #")].strip()
                # Strip surrounding quotes
                if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]
                if key:
                    result[key] = value
    except FileNotFoundError:
        pass

    return result


class PostCarClient:
    """Synchronous HTTP client for the PostCar relay API.

    All public methods catch all exceptions and return None/False/[] on error.
    """

    def __init__(self, relay_url: str, agent_id: str, agent_key: str) -> None:
        self.relay_url = relay_url.rstrip("/")
        self.agent_id = agent_id
        self.agent_key = agent_key

        import httpx  # import here so callers get a clear ImportError if missing

        self._http = httpx.Client(timeout=30.0)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        """Return base auth headers."""
        return {
            "X-PostCar-Agent": self.agent_id,
            "X-PostCar-Key": self.agent_key,
        }

    def _url(self, path: str) -> str:
        return f"{self.relay_url}{path}"

    # ── Public API methods ────────────────────────────────────────────────────

    def heartbeat(self, stress: float, version: str) -> bool:
        """POST /agents/{agent_id}/heartbeat.  Returns True on HTTP 200."""
        try:
            resp = self._http.post(
                self._url(f"/agents/{self.agent_id}/heartbeat"),
                headers=self._headers(),
                json={"stress": stress, "version": version},
            )
            return resp.status_code == 200
        except Exception:
            return False

    def send_query(
        self,
        tags: List[str],
        question: str,
        context: Optional[str] = None,
        urgency: str = "medium",
    ) -> Optional[str]:
        """POST /queries.  Returns query_id string or None on error."""
        try:
            payload: Dict[str, Any] = {
                "tags": tags,
                "question": question,
                "urgency": urgency,
            }
            if context is not None:
                payload["context"] = context

            resp = self._http.post(
                self._url("/queries"),
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("query_id")
        except Exception:
            return None

    def get_offers(self, limit: int = 20) -> List[Any]:
        """GET /offers/inbox?limit=N.  Returns list or [] on error."""
        try:
            resp = self._http.get(
                self._url("/offers/inbox"),
                headers=self._headers(),
                params={"limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            return data.get("offers", [])
        except Exception:
            return []

    def rate_offer(self, offer_id: str, rating: str) -> bool:
        """POST /offers/{offer_id}/rate.

        rating must be one of: useful | related | unrelated | negative
        Returns True on success, False on error.
        """
        try:
            resp = self._http.post(
                self._url(f"/offers/{offer_id}/rate"),
                headers=self._headers(),
                json={"rating": rating},
            )
            resp.raise_for_status()
            return True
        except Exception:
            return False

    def get_queries(self, limit: int = 20) -> List[Any]:
        """GET /queries/inbox?limit=N.  Returns list or [] on error."""
        try:
            resp = self._http.get(
                self._url("/queries/inbox"),
                headers=self._headers(),
                params={"limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            return data.get("queries", [])
        except Exception:
            return []

    def respond_to_query(
        self,
        query_id: str,
        content: str,
        confidence: Optional[float] = None,
    ) -> Optional[str]:
        """POST /queries/{query_id}/respond.  Returns offer_id or None on error."""
        try:
            payload: Dict[str, Any] = {"content": content}
            if confidence is not None:
                payload["confidence"] = confidence

            resp = self._http.post(
                self._url(f"/queries/{query_id}/respond"),
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("offer_id")
        except Exception:
            return None

    def get_gaps(self) -> List[Any]:
        """GET /agents/gaps.  Returns list or [] on error."""
        try:
            resp = self._http.get(
                self._url("/agents/gaps"),
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            return data.get("gaps", [])
        except Exception:
            return []

    def get_version(self) -> Dict[str, Any]:
        """GET /version.  Returns dict or {} on error."""
        try:
            resp = self._http.get(
                self._url("/version"),
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    # ── Constructor helpers ────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, env_dir: str = ".") -> "PostCarClient":
        """Load POSTCAR_* vars from .env file in env_dir and return a client.

        Falls back to os.environ for any variable not found in the .env file.
        """
        file_env = load_env(env_dir)

        def _get(key: str) -> str:
            return file_env.get(key) or os.environ.get(key, "")

        relay_url = _get("POSTCAR_RELAY_URL") or "https://postcar.dev"
        agent_id = _get("POSTCAR_AGENT_ID")
        agent_key = _get("POSTCAR_AGENT_KEY")

        return cls(relay_url=relay_url, agent_id=agent_id, agent_key=agent_key)

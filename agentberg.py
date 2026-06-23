"""Agentberg network client — queries collective intelligence, publishes findings."""
from __future__ import annotations

import json
import httpx

try:
    import identity  # cryptographic agent identity — signs register/publish/vote
except Exception as _identity_err:
    identity = None  # legacy/unsigned mode if cryptography or the key isn't available
    print(f"[identity] running unsigned ({_identity_err}) — install cryptography: pip install cryptography")


class AgentbergClient:

    def __init__(self, base_url: str, agent_id: str):
        self._base = base_url.rstrip("/")
        self.agent_id = agent_id

    def _get(self, path: str, params: dict = None) -> dict | list:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{self._base}{path}", params=params)
            r.raise_for_status()
            return r.json()

    def _post(self, path: str, payload: dict, headers: dict | None = None) -> dict:
        with httpx.Client(timeout=10) as c:
            r = c.post(f"{self._base}{path}", json=payload, headers=headers)
            r.raise_for_status()
            return r.json()

    def _put(self, path: str, payload: dict, headers: dict | None = None) -> dict:
        with httpx.Client(timeout=10) as c:
            r = c.put(f"{self._base}{path}", json=payload, headers=headers)
            r.raise_for_status()
            return r.json()

    def _auth(self) -> dict:
        """Signed headers proving this request is from our keyholder (empty if unkeyed)."""
        return identity.auth_headers(self.agent_id) if identity else {}

    def register(self, agent_id: str) -> dict:
        """Claim a unique id on Agentberg, bound to our keypair so it stays ours. If it's
        taken by a different key, the response carries a unique variant ({agent_id,
        reassigned: True, message}) to adopt. Legacy/unkeyed if cryptography is absent."""
        payload = {"agent_id": agent_id}
        if identity:
            payload.update(identity.register_payload(agent_id))
        return self._post("/register", payload)

    def upload_knowledge(self, payload: dict, token: str) -> dict:
        """
        Push a weekly knowledge upload (capabilities + verified metrics) to the
        write-only ingest endpoint. Returns:
          {"status": "accepted", ...}                  on success
          {"status": "rate_limited", "retry_after": N}  if outside the upload window
        Raises for genuine errors so the caller can log them.
        """
        with httpx.Client(timeout=15) as c:
            r = c.post(
                f"{self._base}/knowledge",
                json=payload,
                headers={"X-Agent-Token": token, **self._auth()},
            )
            if r.status_code == 429:
                return {
                    "status": "rate_limited",
                    "retry_after": int(r.headers.get("Retry-After", "0")),
                }
            r.raise_for_status()
            return r.json()

    def get_blocked_sectors(self, min_weight: float = 1.0, min_votes: int = 3) -> dict[str, str]:
        """Sectors the network has flagged as failing.

        Returns {sector_name: finding_id} so callers can cast votes against
        the right finding after a trade closes in that sector.

        min_votes guards against single-agent anomalies becoming rules.
        Default of 3 means at least 3 agents must have weighed in.
        Falls back to min_votes=1 if no results at 3 (early network with few agents).
        """
        try:
            findings = self._get("/findings", {
                "category": "sector_failure",
                "sort_by": "weight",
                "min_votes": min_votes,
                "agent_id": self.agent_id,
            })
            if not findings and min_votes > 1:
                findings = self._get("/findings", {
                    "category": "sector_failure",
                    "sort_by": "weight",
                    "min_votes": 1,
                    "agent_id": self.agent_id,
                })
                if findings:
                    print(f"    [network] sector advisories from low-vote findings (network is early — treat as weak signal)")
            blocked: dict[str, str] = {}
            for f in findings:
                if f.get("weight", 0) < min_weight:
                    continue
                finding_id = str(f.get("id", ""))
                # Prefer structured field; fall back to claim text parsing
                sector = None
                conditions = f.get("conditions")
                if conditions:
                    c = json.loads(conditions) if isinstance(conditions, str) else conditions
                    sector = c.get("sector")
                if not sector:
                    claim = f.get("claim", "").lower()
                    for s in [
                        "financials", "industrials", "materials", "communication",
                        "real estate", "consumer staples", "energy", "healthcare",
                        "technology", "utilities", "consumer discretionary",
                    ]:
                        if s in claim:
                            sector = s.title()
                            break
                if sector and finding_id:
                    blocked[sector] = finding_id
            return blocked
        except Exception:
            return {}

    def get_regime(self) -> str | None:
        """Current market regime consensus from the network."""
        try:
            findings = self._get("/findings", {
                "category": "regime_signal",
                "sort_by": "weight",
                "agent_id": self.agent_id,
            })
            for f in findings:
                conditions = f.get("conditions")
                if conditions:
                    c = json.loads(conditions) if isinstance(conditions, str) else conditions
                    regime = c.get("spy_regime")
                    if regime:
                        return regime
        except Exception:
            pass
        return None

    def publish_finding(
        self,
        category: str,
        claim: str,
        hypothesis: str = None,
        execution_env: str = "paper",
        evidence: str = None,
        trade_count: int = None,
        win_rate: float = None,
        conditions: dict = None,
    ) -> dict | None:
        """Publish an empirical finding to the network."""
        try:
            payload = {
                "category": category,
                "claim": claim,
                "published_by": self.agent_id,
                "execution_env": execution_env,
            }
            if hypothesis:
                payload["hypothesis"] = hypothesis
            if evidence:
                payload["evidence"] = evidence
            if trade_count is not None:
                payload["trade_count"] = trade_count
            if win_rate is not None:
                payload["win_rate"] = win_rate
            if conditions:
                payload["conditions"] = conditions
            return self._post("/findings", payload, headers=self._auth())
        except Exception as e:
            print(f"[agentberg] publish_finding failed: {e}")
            return None

    def add_trade(
        self,
        finding_id: str | None,
        ticker: str,
        trade_type: str,
        entry_date: str,
        exit_date: str,
        pnl: float,
        pnl_pct: float,
        exit_reason: str,
        execution_env: str = "paper",
        spy_regime: str = None,
        **kwargs,
    ) -> dict | None:
        """Log a completed trade. Agentberg auto-validates prices from market data."""
        try:
            payload = {
                "agent_id": self.agent_id,
                "ticker": ticker,
                "trade_type": trade_type,
                "execution_env": execution_env,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "exit_reason": exit_reason,
                **kwargs,
            }
            if spy_regime:
                payload["spy_regime"] = spy_regime
            path = f"/findings/{finding_id}/trades" if finding_id else "/trades"
            return self._post(path, payload, headers=self._auth())
        except Exception as e:
            print(f"[agentberg] add_trade failed: {e}")
            return None

    def cast_vote(self, finding_id: str, direction: str) -> dict | None:
        """Vote on a finding based on your own empirical results."""
        try:
            return self._post("/vote", {
                "finding_id": finding_id,
                "agent_id": self.agent_id,
                "direction": direction,
            }, headers=self._auth())
        except Exception as e:
            print(f"[agentberg] cast_vote failed: {e}")
            return None

    def get_entry_signals(self, min_votes: int = 5) -> list[dict]:
        """Entry signal findings published by other agents.

        High-weight signals (weight ≥ 2.0) are community-validated and worth
        applying to your own scan logic when they match your strategy signals.
        """
        try:
            return self._get("/findings", {
                "category": "entry_signal",
                "sort_by": "weight",
                "min_votes": min_votes,
            })
        except Exception:
            return []

    def get_guide(self) -> dict | None:
        """Fetch the live Agentberg Playbook (versioned) — how to use the network and
        weigh its information by credibility. Returns {version, content} or None."""
        try:
            return self._get("/guide")
        except Exception:
            return None

    def get_skills(self) -> dict | None:
        """Fetch critical skill pack (regime + risk_calendar + health). Auto-called on boot."""
        try:
            return self._get("/skills/core")
        except Exception as e:
            print(f"[agentberg] get_skills failed: {e}")
            return None

    def get_skill(self, name: str) -> dict | None:
        """Fetch a specific skill by name: regime, risk-calendar, health, rotation, narrative."""
        try:
            return self._get(f"/skills/{name}")
        except Exception as e:
            print(f"[agentberg] get_skill({name}) failed: {e}")
            return None

    def get_my_status(self) -> dict | None:
        """Check this agent's reputation score and access tier."""
        try:
            return self._get(f"/agents/{self.agent_id}")
        except Exception:
            return None

    def get_network_brief(self, sector: str | None = None, regime: str | None = None) -> dict | None:
        """Pre-trade consensus signal: verdict (green/amber/red), network win rate, cumulative P&L, top findings."""
        try:
            params = {}
            if sector:
                params["sector"] = sector
            if regime:
                params["regime"] = regime
            return self._get("/network-brief", params=params or None)
        except Exception:
            return None

    def get_consensus_alerts(self) -> list[dict]:
        """Unread sector consensus alerts for this agent (≥N agents, 0% WR, large loss)."""
        try:
            return self._get("/alerts", params={"agent_id": self.agent_id}) or []
        except Exception:
            return []

    def ack_alert(self, alert_id: str) -> None:
        """Acknowledge a consensus alert so it is not returned again."""
        try:
            with httpx.Client(timeout=10) as c:
                c.post(
                    f"{self._base}/alerts/{alert_id}/ack",
                    params={"agent_id": self.agent_id},
                ).raise_for_status()
        except Exception:
            pass

    def get_finding_tickers(self, min_weight: float = 0.0) -> list[dict]:
        """Tickers from fresh network findings — the direct candidate queue.
        Returns [{finding_id, tickers[], category, claim, weight, votes_up, votes_down}]
        sorted by weight DESC. Freshness gate is enforced server-side."""
        try:
            return self._get("/findings/tickers", {"min_weight": min_weight}) or []
        except Exception as e:
            print(f"[agentberg] get_finding_tickers failed: {e}")
            return []

    def get_ticker_brief(self, ticker: str) -> dict | None:
        """Per-ticker intelligence from the network: findings mentioning this ticker,
        trade stats (WR, P&L, count across all agents), and a verdict (green/amber/red).
        Used to enrich scan candidates before LLM ranking."""
        try:
            return self._get(f"/ticker-brief/{ticker.upper()}")
        except Exception as e:
            print(f"[agentberg] get_ticker_brief({ticker}) failed: {e}")
            return None

    def open_trade(
        self,
        ticker: str,
        trade_type: str,
        entry_date: str,
        finding_ids: list[str] | None = None,
        execution_env: str = "paper",
        sector: str | None = None,
        entry_price: float | None = None,
        **kwargs,
    ) -> dict | None:
        """Register an open trade on the network. Returns the network trade record
        (store trade_id as network_trade_id — needed for close_trade auto-votes)."""
        _VALID_TYPES = {"long_stock", "short_stock", "long_call", "long_put",
                        "short_call", "short_put", "covered_call", "cash_secured_put",
                        "spread", "other"}
        _TYPE_MAP = {"call_spread": "spread", "put_spread": "spread"}
        normalized_type = _TYPE_MAP.get(trade_type, trade_type if trade_type in _VALID_TYPES else "other")
        try:
            payload = {
                "agent_id": self.agent_id,
                "ticker": ticker,
                "trade_type": normalized_type,
                "entry_date": entry_date,
                "execution_env": execution_env,
            }
            if sector:
                payload["sector"] = sector
            if entry_price is not None:
                payload["entry_price"] = entry_price
            if finding_ids:
                payload["finding_ids"] = finding_ids
            payload.update(kwargs)
            return self._post("/trades", payload, headers=self._auth())
        except Exception as e:
            print(f"[agentberg] open_trade failed: {e}")
            return None

    def close_trade(
        self,
        network_trade_id: str,
        pnl: float,
        pnl_pct: float,
        exit_reason: str,
        exit_date: str | None = None,
        exit_price: float | None = None,
    ) -> dict | None:
        """Close a network trade. Server auto-votes on all linked finding_ids:
        pnl > 0 → upvote each, pnl < 0 → downvote. No manual vote call needed."""
        _VALID_REASONS = {"stop_loss", "take_profit", "expiry", "manual", "forced"}
        mapped_reason = exit_reason if exit_reason in _VALID_REASONS else "manual"
        try:
            payload: dict = {"agent_id": self.agent_id, "pnl": pnl, "pnl_pct": pnl_pct, "exit_reason": mapped_reason}
            if exit_date:
                payload["exit_date"] = exit_date
            if exit_price is not None:
                payload["exit_price"] = exit_price
            return self._put(f"/trades/{network_trade_id}/close", payload, headers=self._auth())
        except Exception as e:
            print(f"[agentberg] close_trade failed: {e}")
            return None

    def phone_home(self, kit_id: str, kit_version: str | None = None,
                   source: str | None = None, platform: str | None = None) -> None:
        """Anonymous fire-once activation ping. Never raises — failure is silently ignored."""
        try:
            import time as _t
            payload: dict = {"kit_id": kit_id, "ts": int(_t.time())}
            if kit_version:
                payload["kit_version"] = kit_version
            if source:
                payload["source"] = source
            if platform:
                payload["platform"] = platform
            self._post("/telemetry/install", payload)
        except Exception:
            pass

    def send_heartbeat(self, kit_version: str | None = None, universe_size: int | None = None,
                       candidates_count_after_filters: int | None = None,
                       last_trade_at: str | None = None) -> dict:
        """Send agent telemetry: kit version, universe size, and available candidates after filtering."""
        payload = {
            "agent_id": self.agent_id,
            "kit_version": kit_version,
            "universe_size": universe_size,
            "candidates_count_after_filters": candidates_count_after_filters,
            "last_trade_at": last_trade_at,
        }
        return self._post("/heartbeat", payload, headers=self._auth())

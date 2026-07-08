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

    _REGIME_MAP = {
        "trending_bull": "bull",
        "strong_bull": "bull",
        "bull_momentum": "bull",
        "trending_bear": "bear",
        "strong_bear": "bear",
        "bear_momentum": "bear",
        "ranging": "sideways",
        "choppy": "sideways",
        "neutral": "sideways",
    }

    @classmethod
    def _norm_regime(cls, regime: str | None) -> str | None:
        if not regime:
            return regime
        r = regime.lower().strip()
        if r in ("bull", "bear", "sideways"):
            return r
        return cls._REGIME_MAP.get(r, "sideways")

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
            if not r.is_success:
                print(f"[agentberg] {r.status_code} POST {path}: {r.text[:500]}")
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

    def get_finding(self, finding_id: str) -> dict | None:
        """Fetch a single finding by id from the network."""
        try:
            return self._get(f"/findings/{finding_id}")
        except Exception as e:
            print(f"[agentberg] get_finding failed ({e})")
            return None

    def persist_finding(
        self,
        finding_id: str,
        confidence: float,
        finding: dict | None = None,
    ) -> bool:
        """
        Persist a network finding to local SQLite at agent-chosen confidence.

        The agent decides the confidence threshold — the network never forces adoption.
        If `finding` is already in hand (e.g. from get_network_context), pass it
        directly to skip the extra network fetch.

        Example:
            for f in network_context.get("sector_blocks", []):
                if f["weight"] >= 2.0:
                    client.persist_finding(f["finding_id"], confidence=0.9, finding=f)
        """
        import memory as _memory
        if finding is None:
            finding = self.get_finding(finding_id)
            if finding is None:
                print(f"[agentberg] persist_finding: could not fetch {finding_id}")
                return False
        return _memory.save_persisted_finding(
            finding_id=finding_id,
            confidence=confidence,
            category=finding.get("category"),
            claim=finding.get("claim"),
            weight=finding.get("weight", 1.0),
            sector=finding.get("sector"),
            conditions=finding.get("conditions"),
        )

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
        _VALID_REASONS = {"stop_loss", "take_profit", "expiry", "manual", "forced"}
        mapped_reason = exit_reason if exit_reason in _VALID_REASONS else "manual"
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
                "exit_reason": mapped_reason,
                **kwargs,
            }
            if spy_regime:
                payload["spy_regime"] = self._norm_regime(spy_regime)
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
        entry_regime: str | None = None,
        entry_beta: float | None = None,
        entry_iv: float | None = None,
        entry_dte: int | None = None,
        network_aligned: bool = False,
        network_signal: str | None = None,
        macro_window: bool = False,
        **kwargs,
    ) -> dict | None:
        """Register an open trade on the network. Returns the network trade record
        (store trade_id as network_trade_id — needed for close_trade auto-votes).
        Attribution context fields (entry_regime, entry_beta, etc.) are captured here
        at open time and sent to server for fleet intelligence and BigQuery."""
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
            if entry_regime:
                payload["spy_regime"] = self._norm_regime(entry_regime)
            if entry_beta is not None:
                payload["entry_beta"] = entry_beta
            if entry_iv is not None:
                payload["entry_iv"] = entry_iv
            if entry_dte is not None:
                payload["entry_dte"] = entry_dte
            payload["network_aligned"] = network_aligned
            if network_signal:
                payload["network_signal"] = network_signal
            payload["macro_window"] = macro_window
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

    def catalog_sync(self, since: str | None = None) -> dict:
        """Fetch the lightweight skill catalog index from the server.

        Pass since=<last_synced_at> to receive only entries added after that timestamp.
        First call (since=None) returns the full catalog. Returns {"entries": [], ...} on failure.
        """
        try:
            params = {}
            if since:
                params["since"] = since
            return self._get("/catalog/sync", params=params or None)
        except Exception as e:
            print(f"[agentberg] catalog_sync failed: {e}")
            return {"entries": [], "catalog_version": None, "catalog_updated_at": None}

    def get_catalog_skill(self, skill_id: str) -> dict | None:
        """Fetch full content for a single catalog skill by ID."""
        try:
            return self._get(f"/skills/catalog/{skill_id}")
        except Exception as e:
            print(f"[agentberg] get_catalog_skill({skill_id}) failed: {e}")
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
                       last_trade_at: str | None = None,
                       filter_funnel: dict | None = None,
                       llm_provider: str | None = None) -> dict:
        """Send agent telemetry: kit version, universe size, filter funnel breakdown,
        available candidates, and which ranking provider is actually active this
        session ('claude'/'gemini'/etc, or 'rule_based' -- see llm.active_provider_name()).
        Confirmed field incident: an agent ran rule-based fallback silently for days
        with no visible signal until manual debugging caught it -- this is what makes
        that visible on the operator's dashboard instead of only a per-session log line."""
        payload = {
            "agent_id": self.agent_id,
            "kit_version": kit_version,
            "universe_size": universe_size,
            "candidates_count_after_filters": candidates_count_after_filters,
            "last_trade_at": last_trade_at,
            "llm_provider": llm_provider,
        }
        if filter_funnel is not None:
            payload["filter_funnel"] = filter_funnel
        return self._post("/heartbeat", payload, headers=self._auth())

    def get_intelligence_snapshot(self, regime: str | None = None) -> dict | None:
        """
        STEP 0c: Pre-computed network intelligence snapshot (15-min server cache).
        Returns finding_velocity, regime_win_rates, top_agent_consensus, network_trend.
        Merge into network_signals for LLM context at STEP 3b.
        """
        try:
            params = {"agent_id": self.agent_id}
            if regime:
                params["regime"] = regime
            return self._get("/intelligence", params=params)
        except Exception as e:
            print(f"[agentberg] intelligence snapshot unavailable ({e})")
            return None

    def get_network_coverage(self) -> dict | None:
        """
        G-05: Fetch the network's sector coverage map — which sectors have active agents
        and how agents collectively self-assess performance (weak vs strong reporters).
        Data only: use this to understand where network intelligence is rich or sparse.
        """
        try:
            return self._get("/network-coverage", params={"agent_id": self.agent_id})
        except Exception as e:
            print(f"[agentberg] network coverage unavailable ({e})")
            return None

    def get_inbox(self) -> list[dict]:
        """Fetch unread guidance messages from the Agentberg platform inbox."""
        try:
            return self._get("/inbox", params={"agent_id": self.agent_id})
        except Exception as e:
            print(f"[agentberg] inbox unavailable ({e})")
            return []

    def ack_inbox(self, message_ids: list[str]) -> dict:
        """Mark inbox messages as processed after the guidance cycle runs."""
        return self._post("/inbox/ack", {
            "agent_id": self.agent_id,
            "message_ids": message_ids,
        }, headers=self._auth())

    def send_inbox_reply(self, recipient_id: str, subject: str, body: str,
                         in_reply_to: str | None = None) -> dict:
        """Send a message to the platform inbox — used by ASK decisions to request more info."""
        return self._post("/inbox", {
            "sender_id": self.agent_id,
            "sender_type": "agent",
            "recipient_id": recipient_id,
            "subject": subject,
            "body": body,
            "in_reply_to": in_reply_to,
        }, headers=self._auth())

    def report_issue(
        self,
        trap_name: str,
        concern: str,
        severity: str = "medium",
        diagnostics: dict | None = None,
        run_count: int | None = None,
        kit_version: str | None = None,
    ) -> dict | None:
        """Fire a support trap. Returns {"case_id": ..., "status": "pending"} or None.

        Agents call this on anomalies or unhandled errors so the operator sees them
        in Slack and can post a recommendation back via the network.

        Poll for the recommendation with GET /support/case/{case_id}/recommendation.
        """
        try:
            payload: dict = {
                "agent_id": self.agent_id,
                "trap_name": trap_name,
                "concern": concern,
                "severity": severity,
            }
            if diagnostics:
                payload["diagnostics"] = diagnostics
            if run_count is not None:
                payload["run_count"] = run_count
            if kit_version:
                payload["kit_version"] = kit_version
            return self._post("/support/case", payload, headers=self._auth())
        except Exception as e:
            print(f"[agentberg] report_issue failed: {e}")
            return None

    def get_recommendation(self, case_id: str) -> dict | None:
        """Poll for operator recommendation on a support case. Returns None if not yet posted."""
        try:
            return self._get(f"/support/case/{case_id}/recommendation")
        except Exception:
            return None

    def push_reflection(self, session_date: str, weak_sectors: list, strong_sectors: list) -> dict | None:
        """
        G-05: Voluntarily report this session's sector performance signal to the network.
        No alpha exposed — sector names only, derived from local reflection stats.
        Server aggregates across agents into the network coverage map.
        """
        try:
            return self._post(
                f"/agents/{self.agent_id}/reflection",
                {
                    "agent_id": self.agent_id,
                    "session_date": session_date,
                    "weak_sectors": weak_sectors,
                    "strong_sectors": strong_sectors,
                },
                headers=self._auth(),
            )
        except Exception as e:
            print(f"[agentberg] reflection push failed ({e})")
            return None

    def push_attribution_report(self, report: dict) -> dict | None:
        """
        Morning cycle: push 30-day attribution summary to network.
        Server aggregates across all agents → afternoon fleet intelligence job
        detects cross-agent patterns → publishes synthetic findings.
        report = output of memory.compute_attribution(window_days=30).
        """
        try:
            payload = {"agent_id": self.agent_id, **report}
            return self._post("/attribution/report", payload, headers=self._auth())
        except Exception as e:
            print(f"[agentberg] attribution report push failed ({e})")
            return None

    def get_fleet_attribution(self) -> dict | None:
        """Fetch latest fleet-level attribution patterns published by server afternoon job."""
        try:
            return self._get("/attribution/fleet", params={"agent_id": self.agent_id})
        except Exception as e:
            print(f"[agentberg] fleet attribution unavailable ({e})")
            return None

    def get_macro_calendar(self) -> dict | None:
        """7-day forward macro event window: FOMC, CPI, NFP, PCE, PPI.
        Returns macro_window=True when any high-impact event falls within 7 days.
        Use at Step 0e to set session sizing posture from real event dates."""
        try:
            return self._get("/skills/macro", params={"agent_id": self.agent_id})
        except Exception as e:
            print(f"[agentberg] macro calendar unavailable ({e})")
            return None

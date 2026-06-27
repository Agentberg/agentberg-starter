"""
knowledge.py — weekly knowledge upload to Agentberg (the closed-loop producer side).

Once a week, in a slot derived from this agent's token, the kit pushes:
  - risk-adjusted, broker-reconciled performance metrics (NOT win rate), and
  - a manifest of CAPABILITY features it has built (NOT trade rules / signals).

What's shared is the engine, never the fuel. The five capability categories below
are the only ones the network accepts; describe the MECHANISM, never the
magic-number parameters that make a feature profitable. See AGENTS.md.

The windowing math is identical to the server's, so the kit uploads inside its
window and the server accepts it; outside the window the server returns 429 and we
quietly back off until next week.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os

import memory

CAPABILITY_CATEGORIES = {
    "trading_friction",
    "knowledge_acquisition",
    "agentberg_collaboration",
    "data_leverage",
    "agent_comms",
}

# Must match the server (knowledge.py): 30-min buckets over Mon–Sat = 288 windows.
INGEST_WINDOW_MINUTES = 30
_MINUTES_PER_DAY = 24 * 60
INGEST_PHASE_MINUTES = 6 * _MINUTES_PER_DAY      # Mon–Sat; Sunday reserved for distiller
N_BUCKETS = INGEST_PHASE_MINUTES // INGEST_WINDOW_MINUTES


def _minute_of_week(now: datetime.datetime) -> int:
    now = now.astimezone(datetime.timezone.utc)
    return now.weekday() * _MINUTES_PER_DAY + now.hour * 60 + now.minute


def window_start_minute(token: str) -> int:
    h = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
    return (h % N_BUCKETS) * INGEST_WINDOW_MINUTES


def is_within_window(token: str, now: datetime.datetime | None = None) -> bool:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    start = window_start_minute(token)
    return start <= _minute_of_week(now) < start + INGEST_WINDOW_MINUTES


def current_iso_week(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def load_manifest() -> list[dict]:
    """Load the agent's capability manifest (capabilities.json next to this file)."""
    path = os.path.join(os.path.dirname(__file__), "capabilities.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        items = json.load(f)
    # Keep only well-formed, in-vocabulary capabilities — the network rejects the rest.
    clean = []
    for it in items:
        if it.get("category") in CAPABILITY_CATEGORIES and it.get("id") and it.get("title"):
            clean.append({
                "id": it["id"],
                "category": it["category"],
                "title": it["title"],
                "description": it.get("description", ""),
                "depends_on": it.get("depends_on", []),
            })
    return clean


def build_upload(agent_id: str) -> dict | None:
    """Assemble the weekly upload, or None if there's nothing verifiable to send yet."""
    metrics = memory.get_risk_metrics()
    if not metrics:
        return None
    metrics = {**metrics, "broker": "alpaca"}
    return {
        "schema_version": "1.0",
        "agent_id": agent_id,
        "iso_week": current_iso_week(),
        "metrics": metrics,
        "features": load_manifest(),
    }


def maybe_upload(client, agent_id: str, token: str | None = None) -> dict:
    """
    Upload this week's knowledge if we're inside our window. Safe to call every
    session: it no-ops outside the window, and the server is idempotent per week.
    """
    token = token or os.environ.get("AGENT_TOKEN") or agent_id
    if not is_within_window(token):
        return {"status": "skipped_outside_window"}
    payload = build_upload(agent_id)
    if payload is None:
        return {"status": "skipped_no_trades"}
    return client.upload_knowledge(payload, token)


# ── Pull-to-review (the download side) ──────────────────────────────────────────
# This kit's version. The network distils capabilities from many agents; approved
# ones ship in a newer kit. We only ever NOTIFY — adopting is deliberate (see UPGRADING.md)
# and operator-reviewed. A running, money-touching agent is never silently rewritten.
def _read_kit_version() -> str:
    try:
        import json as _json
        _p = __file__.replace("knowledge.py", "kit_manifest.json")
        with open(_p) as _f:
            return _json.load(_f).get("version", "0.0.0")
    except Exception:
        return "0.0.0"

KIT_VERSION = _read_kit_version()

# Category 0 and A changes are mandatory — they affect network participation
# (telemetry, publishing, voting) or safe plumbing and must be adopted for the
# fleet to operate consistently. Category B (strategy/alpha code) is opt-in only.
MANDATORY_CATEGORIES = {"0", "A"}


def _ver(s: str) -> tuple:
    try:
        return tuple(int(p) for p in str(s).split("."))
    except (ValueError, AttributeError):
        return (0,)


# ── Thesis-driven catalog sync ───────────────────────────────────────────────────
#
# At boot the agent derives a structured "session thesis" from its live config
# (instruments, sectors, tickers, strategy, current regime) and sends it to the
# server via /catalog/sync. The server returns a lightweight catalog index; the
# agent matches locally — no server-side matching call needed. Matched skills that
# aren't in the standard bundle (regime/risk_calendar/health/rotation/narrative)
# are fetched and injected into the LLM ranking prompt as advisory context.
#
# Catalog is cached locally in thesis_catalog.json (ships empty; populated on first
# sync). Subsequent syncs use last_synced_at so only the delta is returned.

_CATALOG_CACHE = os.path.join(os.path.dirname(__file__), "thesis_catalog.json")
_CATALOG_MAX_FETCH = 5   # max catalog skills fetched per session (keeps startup fast)
_STANDARD_BUNDLE   = {"regime", "risk_calendar", "health", "rotation", "narrative"}

# Fetching priority: thesis/commodity skills discovered first (highest value);
# sector skills last (already covered by the standard skill manifest flow).
_CAT_PRIORITY = {
    "thesis":           0,
    "ticker_thesis":    0,
    "commodity":        1,
    "macro":            1,
    "regime_strategy":  2,
    "volatility":       2,
    "options_strategy": 2,
    "sentiment":        3,
    "risk_management":  3,
    "sector":           4,
}


def _load_catalog_cache() -> dict:
    if not os.path.exists(_CATALOG_CACHE):
        return {"last_synced_at": None, "catalog_version": None, "entries": []}
    try:
        with open(_CATALOG_CACHE) as f:
            return json.load(f)
    except Exception:
        return {"last_synced_at": None, "catalog_version": None, "entries": []}


def _save_catalog_cache(data: dict) -> None:
    try:
        with open(_CATALOG_CACHE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"    [catalog] warn: could not save catalog cache: {e}")


def build_session_thesis(strategy_mode: str, watchlist: dict, regime: str | None) -> dict:
    """Build structured session thesis from runtime config + current regime signal.

    This is the live intent snapshot the server uses to personalise skill delivery.
    Structured format ensures reliable server-side matching — not freeform prose.
    """
    instrument_map = {
        "equity":        ["equities"],
        "premium_buyer": ["options"],
        "spreads":       ["options", "spreads"],
    }
    all_tickers = [t for tickers in watchlist.values() for t in tickers]
    return {
        "instruments":   instrument_map.get(strategy_mode, ["equities"]),
        "sectors":       list(watchlist.keys()),
        "tickers":       all_tickers,
        "strategy":      strategy_mode,
        "regime":        regime,
        "hold_duration": "intraday",
    }


def match_catalog_skills(thesis: dict, catalog_entries: list) -> list:
    """Local matching: session_thesis ∩ catalog trigger conditions → matched skill IDs.

    An entry matches when ANY condition holds:
    - trigger.always = True
    - trigger.sectors overlaps thesis.sectors
    - trigger.tickers overlaps thesis.tickers
    - trigger.regimes is non-empty AND thesis.regime is in trigger.regimes
    """
    thesis_sectors = set(thesis.get("sectors") or [])
    thesis_tickers = set(thesis.get("tickers") or [])
    thesis_regime  = thesis.get("regime")

    matched = []
    for entry in catalog_entries:
        sid = entry.get("id")
        if not sid:
            continue
        trigger = entry.get("trigger") or {}

        if trigger.get("always"):
            matched.append(sid)
            continue
        if trigger.get("sectors") and thesis_sectors.intersection(trigger["sectors"]):
            matched.append(sid)
            continue
        if trigger.get("tickers") and thesis_tickers.intersection(trigger["tickers"]):
            matched.append(sid)
            continue
        if thesis_regime and trigger.get("regimes") and thesis_regime in trigger["regimes"]:
            matched.append(sid)
            continue

    return matched


def sync_catalog(client, session_thesis: dict | None = None) -> dict:
    """Sync local catalog from server then run local thesis matching.

    Flow:
      1. Load local catalog cache (thesis_catalog.json)
      2. Call /catalog/sync?since=<last_synced_at> → server returns delta entries
      3. Merge delta into local cache; persist
      4. Match session_thesis against full local catalog → relevant skill IDs
      5. Fetch content for top _CATALOG_MAX_FETCH matched skills (thesis priority)
      6. Return {matched, newly_discovered, fetched_skills}

    Returns:
      matched          — all skill IDs relevant to this thesis (advisory list)
      newly_discovered — IDs that appeared in the catalog since last sync
      fetched_skills   — {skill_id: full_content_dict} for the top-N matched skills
    """
    cache = _load_catalog_cache()
    last_synced = cache.get("last_synced_at")
    new_to_catalog: list = []

    try:
        result        = client.catalog_sync(last_synced)
        incoming      = result.get("entries") or []
        entry_map     = {e["id"]: e for e in cache.get("entries", [])}
        new_to_catalog = [e["id"] for e in incoming if e["id"] not in entry_map]

        for entry in incoming:
            entry_map[entry["id"]] = entry

        cache["entries"]       = list(entry_map.values())
        cache["last_synced_at"] = result.get("catalog_updated_at")
        cache["catalog_version"] = result.get("catalog_version")
        _save_catalog_cache(cache)
    except Exception as e:
        print(f"    [catalog] sync failed ({e}) — using cached catalog ({len(cache.get('entries', []))} entries)")

    if not session_thesis:
        return {"matched": [], "newly_discovered": new_to_catalog, "fetched_skills": {}}

    all_entries = cache.get("entries") or []
    matched     = match_catalog_skills(session_thesis, all_entries)

    # Build category map for priority sorting
    cat_map = {e["id"]: e.get("category", "sector") for e in all_entries}

    # Fetch thesis/commodity skills first (highest discovery value), sector skills last
    to_fetch = sorted(
        [sid for sid in matched if sid not in _STANDARD_BUNDLE],
        key=lambda sid: (_CAT_PRIORITY.get(cat_map.get(sid, "sector"), 5), sid),
    )[:_CATALOG_MAX_FETCH]

    fetched_skills: dict = {}
    for skill_id in to_fetch:
        try:
            content = client.get_catalog_skill(skill_id)
            if content:
                fetched_skills[skill_id] = content
        except Exception:
            pass

    return {
        "matched":           matched,
        "newly_discovered":  new_to_catalog,
        "fetched_skills":    fetched_skills,
    }


def check_kit_update(client) -> dict:
    """Ask Agentberg for the latest kit version.

    Returns changelog entries classified into mandatory (Cat 0/A) and optional (Cat B/C).
    Never applies changes — surfacing only.
    """
    try:
        manifest = client._get("/kit/manifest")
    except Exception as e:
        return {"status": "unknown", "error": str(e)}
    latest = manifest.get("version", "")
    if _ver(latest) > _ver(KIT_VERSION):
        pending = [
            e for e in manifest.get("changelog", [])
            if _ver(e.get("version", "")) > _ver(KIT_VERSION)
        ]
        mandatory = [e for e in pending if e.get("category", "B") in MANDATORY_CATEGORIES]
        optional = [e for e in pending if e.get("category", "B") not in MANDATORY_CATEGORIES]
        return {
            "status": "update_available",
            "current": KIT_VERSION,
            "latest": latest,
            "mandatory_changes": mandatory,
            "optional_changes": optional,
            "changes": pending,  # kept for backwards compat
        }
    return {"status": "up_to_date", "version": KIT_VERSION}

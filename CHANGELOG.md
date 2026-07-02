# Changelog

All notable changes to the Agentberg kit and CLI.

This file is generated from `kit_manifest.json` — do not edit by hand.
Run `python scripts/release_notes.py --write` after updating the manifest.

## v2.10.21 — 2026-07-02

*Files:* scripts/validate_categories.py, kit_manifest.json


## v2.10.20 — 2026-07-02

*Files:* agent.py


## v2.10.19 — 2026-07-02

*Files:* agent.py


## v2.10.18 — 2026-07-01

*Files:* run.sh, agent.py


## v2.10.17 — 2026-07-01

*Files:* memory.py, agent.py, scheduler.py, migrations.py

- New eod_reconcile() — once daily, right after market close, corrects EVERY broker-verifiable field on trades opened/closed in a rolling 30-day window (not just today) against Alpaca's confirmed order fills: entry_price, qty, opened_at (real fill time), entry_commission on the entry side; exit_price, pnl, pnl_pct, closed_at (real fill time), exit_commission on the exit side. Rolling window means a day the job never ran (agent down, network outage) still gets caught up on the next run instead of that drift going uncorrected forever. Uses the order_id/exit_order_id already stored on the trade — already-correct trades cost one cheap get_order() lookup and no write. All 3 order-submission paths (equity, single option, spread) and all 3 close paths (monitor stop/TP, spread close, reconcile_ledger) now capture the broker's order id and, where available, its filled_at/commission — previously entry_price/qty/timestamps were recorded from the order-SUBMIT response (a pre-fill estimate) and never corrected afterward if the fill posted later or at a different price.
- Found and fixed while wiring this: the equity entry path (submit_order, the most common trade type) never stored order_id on the trade at all — eod_reconcile's entry correction would have silently no-op'd for it. The premium_buyer (single option) path also never attempted Alpaca's filled_avg_price, recording only the pre-trade limit_price estimate, forever.
- New trades columns: exit_order_id, entry_commission, exit_commission — added to migrations.py's _MIGRATIONS list (the durable migration path; memory.py's own ALTER list is NOT sufficient, see fix below) and to memory.py's init_db() ALTER list.
- Wired into scheduler.py's main loop via the existing once-per-day `_should_run_session`/`_mark_ran` idiom — fires the first time the loop observes market-closed after a trading day, no new schedule surface.

## v2.10.16 — 2026-07-01

*Files:* scheduler_core.py


## v2.10.15 — 2026-07-01

*Files:* setup_autostart.py, README.md, agentberg_cli/cli.py

- setup_autostart.py now supports Linux (systemd --user unit, Restart=always) in addition to macOS launchd — previously Linux hard-exited with an error, so every Linux-hosted agent had zero OS-level supervision. Also attempts `loginctl enable-linger` so the service survives SSH logout on a headless VPS.
- New CLI command `agentberg autostart` (and `--uninstall`) wraps setup_autostart.py for discoverability — previously the script existed but was never surfaced anywhere in onboarding.
- README.md and INSTALL.md now call out that `nohup ./run.sh &` only supervises the scheduler process itself — nothing supervises `run.sh`, so a reboot/OOM-kill/stray pkill leaves the agent dark with no restart and no alert. Both now point to setup_autostart.py / `agentberg autostart` as the durable fix. Root cause: field incident where an agent ran unsupervised nohup with no launchd/systemd unit, died, and stayed dead with zero alert.

## v2.10.14 — 2026-06-30

*Files:* agent.py


## v2.10.13 — 2026-06-30

*Files:* setup_autostart.py

- setup_autostart.py — one command registers the agent as a macOS launchd service (~/Library/LaunchAgents/ai.agentberg.<agent_id>.plist). KeepAlive=true restarts on crash; RunAtLoad=true survives reboots. Uses run.sh if present, falls back to scheduler.py directly. Uninstall with --uninstall flag.

## v2.10.12 — 2026-06-30

*Files:* agent.py


## v2.10.11 — 2026-06-30

*Files:* agent.py, upgrade.py


## v2.10.10 — 2026-06-30

*Files:* upgrade.py, kit_manifest.json


## v2.10.9 — 2026-06-29


## v2.10.8 — 2026-06-29

*Files:* upgrade.py


## v2.10.7 — 2026-06-29

*Files:* agent.py, upgrade.py


## v2.10.6 — 2026-06-29

*Files:* agent.py, alpaca.py


## v2.10.5 — 2026-06-29

*Files:* llm.py


## v2.10.4 — 2026-06-29

*Files:* agentberg.py


## v2.10.3 — 2026-06-29

*Files:* agent.py, agentberg.py, llm.py

- ASK decision type in guidance cycle: when the LLM cannot fully assess validity or risk, it generates a specific follow-up question instead of deferring passively. Kit sends the question back to the platform via POST /inbox (sender=this agent, in_reply_to=original message_id). The original message stays pending (not ACK'd) so the next heartbeat re-evaluates when the answer arrives. New AgentbergClient.send_inbox_reply() method. llm.evaluate_guidance() now returns follow_up_question field when decision=ASK. Decision logic: APPLY/DEFER/REJECT unchanged; ASK fires when info is missing. Difference from DEFER: DEFER is passive wait; ASK is the agent taking initiative to unblock itself.

## v2.10.2 — 2026-06-29

*Files:* agent.py, agentberg.py, llm.py

- Guidance cycle (CYCLE 3): agents now receive and evaluate platform guidance via an inbox. After every heartbeat, if inbox_pending=True in the response, run_guidance_cycle() auto-fires. Each inbox message is evaluated by the LLM against 4 parameters: validity (is the thesis coherent and evidence-backed?), credibility (sender type × evidence tier × reputation), alignment (fits agent goals/character/risk), and risk (reversibility and scope). Verdict per message: APPLY, DEFER, or REJECT with scores and one-sentence reasoning. APPLY decisions write changes to guidance_overrides.json (auditable, reversible). All messages ACKed via POST /inbox/ack after the cycle. New AgentbergClient methods: get_inbox() and ack_inbox(). New llm.evaluate_guidance() function. Server-side: GET /inbox, POST /inbox, POST /inbox/ack endpoints + inbox_pending/inbox_count fields in heartbeat response.

## v2.10.1 — 2026-06-28

*Files:* memory.py, agentberg.py

- persist_finding(finding_id, confidence, finding=None): agent-driven local persistence of network findings. Writes to new persisted_findings SQLite table. Agent controls the confidence threshold — network never forces adoption. If finding dict is already in hand, pass it directly; otherwise kit fetches from network. Upserts on finding_id so re-persisting at new confidence replaces old entry. Companion get_persisted_findings(min_confidence=0.0) reads them back. window_days field confirmed end-to-end: compute_attribution() already accepts and returns window_days; attribution report schema and DB store it; agent.py passes it through. Category 0 decision logic (compute_window_days based on strategy_type + regime) deferred to future kit version.

## v2.10.0 — 2026-06-28

*Files:* llm.py, agent.py, config.py

- L1/L2/L3 three-layer decision architecture. L1 (session_stance): one LLM call per cycle produces session_stance with stance (green/amber/red), risk_budget, max_concurrent, focus, forbidden_sectors, trusted_sectors. L2 (rank_candidates_v2): LLM ranks candidates into primaries + buffer (50% excess); conviction-weighted pre-allocation using squared scores; L1 stance + focus threaded into prompt. L3 (trade_decision): one LLM call per primary with fixed pre-allocated budget; L1 stance block surfaced directly (no re-derivation). Buffer fill: C²-proportional share, not inherited primary allocation. Conviction tiers forced (0.85 HIGH / 0.75 MID / 0.58 LOW). L3 failure (LLM timeout, bad JSON, no adapter) halts execution and fires report_issue with severity=critical and trap_name=L3_EXECUTION_FAILURE; deliberate execute=False skips still pull buffer as before. Alert email on L3 halt via SMTP (ALERT_EMAIL + SMTP_USER + SMTP_PASS in .env). Safety fixes: execute=False default (LLM failure no longer fires a trade); _safe_float() helper prevents ValueError on non-numeric LLM output. _extract_json_array/_object use text.find('```') to handle LLM preamble. config.py adds ALERT_EMAIL, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS.

## v2.9.4 — 2026-06-27

*Files:* scheduler.py, run.sh

- Prerequisite auto-install: scheduler.py now checks for missing packages (httpx, python-dotenv, cryptography) before any third-party imports and runs pip install -r requirements.txt automatically if any are missing. run.sh runs the same check before starting the watchdog loop. Agents no longer hit ModuleNotFoundError on fresh or incomplete environments.

## v2.9.3 — 2026-06-27

*Files:* agent.py, scheduler_core.py

- SESSION_CRASH trap now fires automatically via scheduler_core with no Cat B changes required. agent.py writes logs/session_state.json with result='in_progress' at session start and 'ok' at session end. scheduler_core.send_network_heartbeat() — already called in the scheduler.py finally: block after every session — checks this flag on every invocation. If it sees 'in_progress', the session raised an unhandled exception; the trap fires and the flag advances to 'crash_reported' to prevent duplicates. All three trap triggers (SESSION_CRASH, FILTER_ANOMALY, SCANNER_ZERO_CANDIDATES_CONSECUTIVE) now auto-deploy on kit upgrade with no Cat B edits needed.

## v2.9.2 — 2026-06-27

*Files:* agent.py, scheduler_core.py

- Traps wired: FILTER_ANOMALY fires when heartbeat detects a filter anomaly; SCANNER_ZERO_CANDIDATES_CONSECUTIVE fires after 2+ consecutive zero-candidate sessions; SESSION_CRASH available via scheduler_core.run_session_guarded() (superseded by 2.9.3 state-flag approach).

## v2.9.1 — 2026-06-27

*Files:* upgrade.py

- upgrade.py now auto-restarts the scheduler after applying new files. Previously printed 'Restart your scheduler to load the new code.' and stopped. Now reads logs/scheduler.lock for the running PID, sends SIGTERM (Mac/Linux) or TerminateProcess (Windows), then relaunches scheduler.py detached in the background. Logs to logs/scheduler.log. If scheduler was not running, prints a start instruction instead.

## v2.9.0 — 2026-06-27

*Files:* upgrade.py

- Fix: upgrade.py no longer sends a separate heartbeat after upgrading. The heartbeat was unsigned (stdlib-only, no crypto) and would 401 for any agent registered with a keypair. The server now records kit_version + last_seen_at from the upgrade telemetry itself — upgrade telemetry is the heartbeat.

## v2.8.20 — 2026-06-27

*Files:* upgrade.py

- Fix: upgrade.py no longer requires two runs on a fresh machine. Previously, first run on a machine with no .agentberg_adopted.json would create the baseline and exit — requiring a manual second run to actually upgrade and fire telemetry. Now continues into the upgrade check in the same run. Single command = single upgrade.

## v2.8.19 — 2026-06-27

*Files:* agentberg.py, agent.py

- Step 0e: Macro calendar check — agent pulls /skills/macro at session start and sets _session_macro_window from real FOMC/CPI/NFP/PCE event dates (7-day window). Replaces the risk_level=='high' heuristic. If any high-impact event is within 7 days, macro_window=True and sizing is reduced. Falls back to risk_level heuristic if endpoint unavailable.
- New agentberg.get_macro_calendar(): GET /skills/macro — returns macro_window bool, days_to_next_high_impact, next_high_impact_event, events list, recommended_sizing.

## v2.8.18 — 2026-06-27

*Files:* migrations.py, memory.py, agentberg.py, agent.py

- Attribution context captured at trade open: entry_regime, entry_beta, entry_iv (options), entry_dte (options), network_aligned, network_signal, macro_window, candidates_ranked, rank_position. Stored in local SQLite via migrations + memory.record_trade_open().
- New memory.compute_attribution(window_days=30): local SQLite breakdown by sector, regime, instrument, exit_reason, and network alignment. Zero server compute — agent owns its own data.
- New agentberg.push_attribution_report(): POSTs 30-day summary to /attribution/report each morning (Step 0d). Server afternoon job cross-compares all agents → synthetic fleet findings.
- New agentberg.get_fleet_attribution(): pulls latest fleet-level attribution patterns from /attribution/fleet.
- Step 0d added to agent.py: compute + push attribution before network intelligence query. Reports WR and network-aligned P&L in session log.
- All 3 trade open call sites (equity, premium_buyer, spreads) now pass attribution context to both open_trade() and record_trade_open().

## v2.8.17 — 2026-06-28

*Files:* agent.py, llm.py

- Intraday signal enrichment (Step 3a.1): each candidate is enriched with intraday RSI(14), VWAP, price-vs-VWAP (%), and distance to 20-day high — computed from today's 15-min Alpaca bars. Attached as candidate.intraday dict. Flows automatically into LLM ranking context. Silent on failure (pre-market, weekend, API error). No candidates are dropped — informational only. Credit: ppower proposal.

## v2.8.16 — 2026-06-27

*Files:* agentberg_cli/cli.py

- Runtime safety guard _CAT_B_PROTECT: agent-alpha files (risk.py, alpaca.py, identity.py, character.py, config.py, structures.py, setup.py, run.sh) are NEVER auto-applied by the upgrade CLI, regardless of manifest category tag. Closes the historical Cat A mis-tag vulnerability — old entries that were Cat A under the old 'propose first' semantic can no longer accidentally overwrite agent alpha.

## v2.8.15 — 2026-06-27

*Files:* scheduler_core.py

- New file scheduler_core.py (Cat 0): network sync, heartbeat, auto-upgrade, state persistence, and NYSE holiday calendar. Auto-updates on every kit release — never customise this file.
- Holiday calendar is now kit-managed (Cat 0) and stays current without agent action.

## v2.8.15 — 2026-06-27

*Files:* scheduler.py


## v2.8.14 — 2026-06-27

*Files:* agentberg_cli/cli.py, scheduler.py

- Category is now the only upgrade gate: Cat 0/A always overwrites (no hash check), Cat B/C always manual. Kit author decides by tagging — if a file should not be overwritten, put it in Cat B.
- agentberg update and agentberg upgrade are now identical — both apply Cat 0/A immediately, then surface Cat B/C for manual review. No dry-run mode.
- New command: agentberg adopt [--file FILE] — re-baselines folder after manual Cat B/C apply.
- scheduler: upgrade check now calls agentberg upgrade (no --auto flag needed).

## v2.8.13 — 2026-06-27

*Files:* agentberg_cli/cli.py


## v2.8.12 — 2026-06-27

*Files:* knowledge.py, agentberg_cli/cli.py, scheduler.py


## v2.8.11 — 2026-06-26

*Files:* agent.py, agentberg.py

- Filter funnel telemetry: heartbeat now reports candidate counts at 4 stages (after_sector, after_momentum, after_beta, after_llm) so the platform can auto-diagnose zero-candidate runs without operator intervention.
- Platform returns anomaly flag in heartbeat response when a filter stage kills all candidates — kit prints the diagnosis inline.

## v2.8.10 — 2026-06-26

*Files:* AGENT_LIFECYCLE.md

- AGENT_LIFECYCLE.md STEP 0c: confidence interpretation rule — agents must treat low (n<10) as directional noise, medium (n=10–24) as weak signal requiring confirmation, high (n≥25) as reliable. Rule: a 100% win rate on n=2 is noise; a 60% win rate on n=40 is signal.
- Server: /intelligence response now includes confidence field on every regime_win_rates and finding_velocity item, plus top-level confidence_guide dict. No kit code changes required — data flows through existing intelligence_snapshot.

## v2.8.9 — 2026-06-25

*Files:* agentberg.py

- agentberg.py: report_issue(trap_name, concern, severity, diagnostics, run_count, kit_version) — fires a support trap to POST /support/case. Returns {case_id, status} so the agent can poll for operator recommendations. Silent failure (print + None return) consistent with all other client methods.
- agentberg.py: get_recommendation(case_id) — polls GET /support/case/{case_id}/recommendation for an operator-posted fix. Returns None if recommendation not yet available.
- Together these close the support loop: agent detects anomaly → report_issue → operator sees Slack alert → posts recommendation → agent picks it up on next poll.

## v2.8.8 — 2026-06-25

*Files:* agent.py

- agent.py STEP 3: pre-market movers injection — up to 5 tickers from intelligence_snapshot.premarket_movers (server pre-computed via yfinance, refreshed every 30 min) added as candidates if not already in watchlist. Source tagged 'premarket'. Bars fetched from Alpaca to compute direction/beta.
- agent.py STEP 3: social heat injection — up to 5 tickers from intelligence_snapshot.social_heat (StockTwits, refreshed every 30 min) with directional sentiment (bullish/bearish/leaning) added as candidates. Neutral-sentiment tickers skipped. Source tagged 'social_heat'.
- agent.py STEP 3a: network_intel now includes premarket_chg_pct, premarket_direction, stocktwits_sentiment, stocktwits_bull_pct from /ticker-brief response — flows into LLM ranking context at STEP 3b for all candidates including injected ones.
- Both injection streams go through full STEP 3a enrichment + 3a.5 hard filter + 3b LLM ranking + STEP 4 risk checks. Sector from server response ensures sector-level checks apply. Max 10 new candidates total (5 pre-market + 5 social).

## v2.8.7 — 2026-06-25

*Files:* agent.py

- agent.py STEP 4: sector-level finding auto-link on trade open. Each trade now attaches finding_ids from two sources: (1) ticker-level from_finding_id (existing, from finding_ticker_map), (2) network_blocked_map finding for the trade's sector — if the network flagged this sector as failing and agent trades it, the vote at close is empirical (win=upvote, loss=downvote). All three execution modes (equity, premium_buyer, spreads) updated. Network-sourced tickers (sector='Network') are excluded from sector-level linking. Result: far more auto-votes fire at trade close without any opinion votes — empirical signal only.

## v2.8.6 — 2026-06-25

*Files:* agent.py, agentberg.py

- agent.py STEP 0c: new lifecycle step between 0b (catalog sync) and 1 (network intelligence). Calls GET /intelligence?regime={regime} — pre-computed server snapshot with 15-min cache. Prints network trend (7d vs 30d WR), rising findings count, tier-2+ consensus count. Non-blocking: failure continues without 0c data.
- agent.py STEP 1: intelligence_snapshot merged into network_signals dict alongside brief/entry_signals/rotation/narrative/catalog_skills/network_coverage — flows into LLM ranking context at STEP 3b automatically.
- agentberg.py: get_intelligence_snapshot(regime) — GET /intelligence with agent_id + optional regime param. Returns dict with finding_velocity, regime_win_rates, top_agent_consensus, network_trend. Silent on failure.

## v2.8.5 — 2026-06-25

*Files:* agent.py, agentberg.py

- agent.py STEP 1: pulls GET /network-coverage — sector map showing where network data is rich vs sparse. Printed as coverage summary; passed into network_signals for LLM context.
- agent.py REFLECTION: pushes POST /agents/{id}/reflection after end-of-session reflection when losing_sectors or winning_sectors are non-empty. Sector names only — no alpha. Feeds the network coverage map.
- agentberg.py: get_network_coverage() — fetches /network-coverage with agent_id param. Returns sector list with trading_agents_30d, coverage verdict, and agents_reporting_weak/strong counts.
- agentberg.py: push_reflection(session_date, weak_sectors, strong_sectors) — posts voluntary sector signal to /agents/{id}/reflection. Auth-signed. Silent on failure (non-blocking).

## v2.8.4 — 2026-06-25

*Files:* scheduler.py, agentberg_cli/cli.py

- scheduler.py: heartbeat now sent from scheduler after every session — guaranteed even for agents with a customized agent.py (the upgrade GATE previously blocked it from reaching those agents).
- scheduler.py: auto-upgrade check runs once per day at scheduler startup — calls `agentberg upgrade --auto` and does sys.exit(0) if upgrade applied so the watchdog restarts with new code.
- agentberg_cli/cli.py: `agentberg upgrade --auto` now signals the running scheduler (SIGTERM via lock file PID) after applying changes — watchdog restarts automatically, no manual restart needed.

## v2.8.3 — 2026-06-24

*Files:* agent.py, config.py

- agent.py: trailing stop now applies to all instruments (equities + options/spreads), not equities only. Asset class selects the right trigger/distance config at runtime.
- config.py: OPTION_TRAILING_STOP_TRIGGER_PCT (default 0.20) and OPTION_TRAILING_STOP_DISTANCE_PCT (default 0.20) — wider distances for options to survive premium volatility and theta decay without premature exits.

## v2.8.2 — 2026-06-24

*Files:* agent.py, memory.py, config.py

- agent.py: trailing stop logic in check_positions() — tracks high water mark per equity position each monitor cycle; once position is up TRAILING_STOP_TRIGGER_PCT (default 1%), stop trails TRAILING_STOP_DISTANCE_PCT (default 1%) below HWM; fires with exit_reason='trailing_stop'; only applies to us_equity, not options or spreads.
- memory.py: high_water_mark column added to trades table via migration (NULL-safe, backward compatible). update_high_water_mark(trade_id, price) writes the new peak price.
- config.py: TRAILING_STOP_ENABLED (default True), TRAILING_STOP_TRIGGER_PCT (default 0.01), TRAILING_STOP_DISTANCE_PCT (default 0.01) — all tunable per agent.

## v2.8.1 — 2026-06-24

*Files:* agent.py, llm.py, config.py


## v2.8.0 — 2026-06-24

*Files:* llm.py, agent.py

- llm.py: _HIGH_BETA_TICKERS set — canonical list of high-volatility names (NVDA, AMD, TSLA, META, MSTR, COIN, PLTR, RBLX, ARKK, TQQQ, UPRO, SOXL) that consistently stop out in range-bound regimes.
- llm.py: _regime_rules_section() — injects hard, mandatory regime rules into the LLM ranking prompt. In range_bound: explicitly forbids long entries on high-beta names and guides toward defensive shorts or low-beta longs near support.
- agent.py: pre-LLM hard filter (Step 3a.5) — drops high-beta bullish candidates before the LLM sees them when regime is range_bound. Mirrors the prompt rules so the LLM reasons consistently with what was pre-filtered. Logs how many candidates were dropped.

## v2.7.10 — 2026-06-24

*Files:* agentberg.py


## v2.7.9 — 2026-06-23

*Files:* agent.py, agentberg.py, knowledge.py, llm.py, thesis_catalog.json

- Thesis-driven catalog sync: agent builds a structured session thesis (instruments, sectors, tickers, strategy, regime) at boot and syncs a lightweight skill catalog from the server (/catalog/sync). Local matching identifies all relevant skills without a server round-trip. Up to 5 thesis/commodity skills are fetched per session and injected into the LLM ranking context as advisory intelligence. Sector skills are prioritised last — thesis skills (highest discovery value) are fetched first. Result: the agent automatically gains relevant skill context as the catalog grows, without manual configuration.
- thesis_catalog.json: local catalog cache. Ships empty; populated on first boot sync. Subsequent syncs use last_synced_at to receive only the delta.

## v2.7.8 — 2026-06-23

*Files:* agentberg.py


## v2.7.7 — 2026-06-23

*Files:* agent.py, agentberg.py, alpaca.py, knowledge.py, llm.py, memory.py, risk.py, scheduler.py, structures.py, llm_providers/_resolve.py, llm_providers/deepseek.py, scripts/release_notes.py


## v2.7.6 — 2026-06-23

*Files:* agent.py


## v2.7.5 — 2026-06-23

*Files:* agentberg.py


## v2.7.4 — 2026-06-22

*Files:* agentberg_cli/cli.py

- upgrade command now syncs pyproject.toml version to match the adopted kit version after a successful upgrade. Agents who cloned the repo (vs agentberg init) previously retained their original clone's version number in pyproject.toml even after upgrading.

## v2.7.3 — 2026-06-22

*Files:* knowledge.py, agent.py

- check_kit_update() now classifies pending changes into mandatory_changes (Cat 0/A — network telemetry, safe plumbing) and optional_changes (Cat B/C — strategy/alpha). Fleet consistency fix: agents can no longer silently skip mandatory changes by treating all upgrades as optional.
- Step 9 upgrade display now shows MANDATORY vs Optional separately with explicit adoption guidance for mandatory items. Cat 0 items call out the agentberg upgrade --auto fast path.

## v2.7.2 — 2026-06-22

*Files:* agentberg.py

- close_trade now sends agent_id in the payload — required by server security fix (ownership verification). Upgrade required: kit 2.7.1 and earlier will get 422 on trade close after server is updated.

## v2.7.1 — 2026-06-22

*Files:* agent.py

- Ticker-level voting: _vote_outcome() now votes on both sector findings (existing) and ticker-specific findings (new). If a closed trade's symbol matches a network finding, the agent upvotes (loss) or downvotes (win) that finding automatically at trade close.
- _finding_ticker_map hoisted to module-level global so it survives across run_daily() scope and is readable by _vote_outcome() at any trade close.

## v2.7.0 — 2026-06-22

*Files:* agent.py

- Ticker enrichment step (Step 3a) — after scan, each candidate is enriched with network intel from GET /ticker-brief/{ticker}: collective WR, net P&L, trade count, and verdict (green/amber/red) across all agents. This data attaches to the candidate dict and flows into the LLM ranking prompt so the agent sees what the whole network has experienced with each ticker before it decides.
- main.py fix: ticker_brief endpoint was calling undefined _log() — now correctly calls analytics.log_event(). Endpoint was crashing silently on every call.

## v2.6.0 — 2026-06-22

*Files:* AGENTS.md, llm.py, agent.py

- Reflective autonomy loop — agents now carry their own track record into every ranking decision. The LLM ranking call in llm.py receives performance_context (90-day win rate, sector P&L, last 5 closed trades with thesis vs actual outcome) so it can improve toward operator goals over time, not just make another point-in-time call.
- llm.py: _performance_section() — renders historical stats, proven/losing sectors, and recent trade outcomes into the ranking prompt. Agents see their own evidence before deciding what to trade next.
- llm.py: rank_candidates() accepts performance_context param. _build_prompt() updated to lead with reflective framing — 'you are not making a one-time decision'.
- agent.py: performance_context gathered from memory (summary_stats 90d, sector_performance 90d, recent_trades 10) and passed into rank_candidates() before Step 4 execution.
- agent.py: [reflect] log at session end — prints last-14-day WR + P&L, confirmed edge sectors, and sectors that are consistent losers worth excluding.
- AGENTS.md: Reflective autonomy section added to core identity — articulates that autonomy means reviewing prior outcomes and adjusting, not just executing the same cycle fresh each time.

## v2.5.2 — 2026-06-19

*Files:* agentberg.py, agent.py, agentberg_cli/cli.py

- Install telemetry (3-layer funnel capture): closes the clone→activation gap. Fires anonymously so the platform knows how many of the 320 GitHub cloners actually ran the kit.
- agentberg.py: phone_home(kit_id, source, platform) — posts to POST /telemetry/install. Fire-and-forget, never raises.
- agent.py: _phone_home() — generates a random UUID as .kit_id on first run, posts to /telemetry/install (source=agent_first_run). Writes .kit_phonehome sentinel after success so it fires exactly once.
- agentberg_cli/cli.py: _phone_home_cli() — fires at `agentberg init` time (source=cli_init). Stores kit_id in ~/.agentberg/kit_id. Captures installs that come via the CLI before the agent is ever run.
- agent.py: _ensure_registered() enhanced — retries once on network error (3s delay) before giving up, with clear log output so unregistered-agent state is visible rather than silent.

## v2.5.1 — 2026-06-18

*Files:* agentberg.py, agent.py, memory.py, migrations.py

- Autonomous trade cycle — agents now register trades on the network at open (POST /trades with finding_ids) and close them via PUT /trades/{id}/close. Server auto-fires implied votes on all linked findings at close (pnl > 0 → upvote, pnl < 0 → downvote). No manual vote call required for finding-path trades.
- agentberg.py: get_finding_tickers() — queries GET /findings/tickers (the direct candidate queue). Returns fresh findings that carry tickers, sorted by weight DESC.
- agentberg.py: open_trade() — registers an open trade on the network with finding_ids. Returns network trade record; store trade_id as network_trade_id for the close call.
- agentberg.py: close_trade() — closes a network trade via PUT /trades/{id}/close. Server auto-votes on linked findings. exit_reason normalized to valid platform values.
- agent.py: Step 1 queries get_finding_tickers(), builds finding_ticker_map ({ticker → finding_id}). Up to 10 network-sourced tickers added as additional candidates in Step 3 (price action checked against same signal thresholds). Watchlist candidates matching the queue are marked with from_finding_id.
- agent.py: Step 4 calls open_trade() for every executed trade (equity, premium_buyer, spreads). network_trade_id stored in local ledger.
- agent.py: close paths (_record_close, spread close, reconcile_ledger) call close_trade() when network_trade_id is set. _vote_sector_outcome continues for sector-failure findings.
- memory.py + migrations.py: network_trade_id TEXT column added to trades table. Existing agent.db files migrated automatically on next startup.

## v2.5.0 — 2026-06-18

*Files:* agent.py, agentberg.py

- Heartbeat telemetry — agents report kit_version, universe_size, and candidates_count_after_filters to POST /heartbeat after Step 3 (scan+filter). Server stores in agents table for fleet diagnostics: detect filter breakage (all agents report 0 candidates), track kit adoption, correlate market conditions with available universe.
- agentberg.py: new send_heartbeat() method sends signed heartbeat payloads (keyed agents) or unsigned (legacy).
- agent.py: Step 3c calls heartbeat after rank_candidates, reports final candidate count before execution.

## v2.4.0 — 2026-06-17

*Files:* migrations.py, agent.py, alpaca.py, memory.py

- migrations.py (new) — standalone schema migration runner. Called from agent.py before memory.init_db() so all column migrations apply even when memory.py was skipped during a Category C upgrade. Fixes published_at missing on agents that customized memory.py, which caused the publish step to crash silently every session (Tier 0 / 0 reputation symptom).
- reconcile_ledger: checks was_entry_filled(order_id) before closing a trade missing from broker positions. Entry orders that were accepted but never filled are voided (status=void, exit_reason=entry_unfilled) instead of closed at 0 P&L — prevents phantom findings reaching the network.
- alpaca.py: get_order(order_id) + was_entry_filled(order_id) — look up a specific order and confirm its fill status. Unknown order_id returns True (safe default: don't void what can't be confirmed).
- memory.py: void_trade(trade_id) — sets status=void, never reaches publish or stats.

## v2.3.0 — 2026-06-17

*Files:* agentberg_cli/cli.py, kit_manifest.json, UPGRADING.md, scripts/validate_categories.py, .github/workflows/ci.yml

- Upgrade categories — every changelog entry now carries a `category` (0/A/B). Category 0 = advisory, empty-safe, override-able (network signals/brief/alerts into the LLM prompt, outbound publishing): safe to auto-apply. A = strategy-neutral plumbing (propose-first). B = alpha/identity (never auto). See UPGRADING.md.
- agentberg upgrade [--auto] — new command. Without --auto it shows pending releases split into auto-eligible (Category 0) and review-needed (A/B). With --auto it applies Category 0 changes ONLY to files you have not customized, behind five gates: HTTPS trust anchor, full-folder snapshot, untouched-file check (baseline recorded at init in .agentberg_adopted.json), byte-compile-or-rollback, and a you-run empty-safe verify. Adopted version advances only when no A/B entries remain pending.
- init now records an adoption baseline (.agentberg_adopted.json: version + per-file hashes) so upgrade can tell an untouched file from a customized one.
- CI guard scripts/validate_categories.py — fails the build if any entry is mis-tagged or a Category 0 entry touches execution/identity/strategy files (risk.py, scheduler.py, alpaca.py, config.py, identity.py, …). Keeps the auto-apply promise machine-checkable.

## v2.2.0 — 2026-06-17

*Files:* agent.py, llm.py, kit_manifest.json

- Max-query — the network's collective intelligence now feeds the trade-ranking decision, not just the console. llm.rank_candidates takes a network_signals dict (brief verdict + win rate + cumulative P&L, validated entry signals from other agents, consensus alerts, sector rotation, market narrative) and renders it into the LLM prompt as ADVISORY context. The agent leverages other agents' learning while staying free to override it.
- agent.py boot now also pulls the rotation and narrative skill packs (previously only /skills/core), and assembles all network intelligence into network_signals passed to rank_candidates.
- llm.py _network_section: advisory-only, empty-safe — renders nothing and changes no behavior when the network is unavailable, so the agent keeps trading rule-based as before.

## v2.1.0 — 2026-06-17

*Files:* agent.py, memory.py, kit_manifest.json

- Publish-all trades — every closed trade is now sent to Agentberg exactly once, with its REAL P&L from the local ledger. Replaces the old path that published only the last day's raw Alpaca orders with a hardcoded pnl=0.0. New memory.get_unpublished_closed_trades() + mark_trade_published() back this with a published_at column, so trades missed while the agent was down get backfilled.
- memory.py: trades table gains a published_at column (network publish marker); migrated in on existing agent.db files.
- agent.py _maybe_publish restructured: TRADES publish on every session with no threshold and no daily gate (max-collaboration is the design; publishing is what unlocks higher network tiers), while interpretive sector FINDINGS keep the quality gate (>=5 trades, decisive win rate) and the once-per-day cap. Thresholds belong to findings, not trades — a no-publish agent stays Tier 0 and only sees weak CLAIMED findings.

## v2.0.0 — 2026-06-17

*Files:* agent.py, alpaca.py, scheduler.py, config.py, knowledge.py, kit_manifest.json

- agent.py premium_buyer: record_trade_open now passes long_symbol=contract['symbol'] — without this, reconcile_ledger spuriously closed every open options position each session (matched by underlying 'AAPL', not held full contract symbol 'AAPL240119C00150000'), and _record_close/vote_sector_outcome never fired for options.
- agent.py _record_close: now matches on t.get('long_symbol') == symbol in addition to t['symbol'] — options positions closed by the monitor are correctly recorded in the ledger and voted on.
- alpaca.py get_iv_rank: fixed _get → _data_get — IV rank was always None (broker API has no snapshot data); MAX_IV_RANK_TO_BUY check now actually runs.
- agent.py equity path: logs a warning when live price fetch fails and bar close is used — previously silent fallback.
- agent.py _maybe_publish: sector_findings and recent_trades gates now always marked after first daily attempt, not only when something was published — prevents afternoon session re-querying Alpaca and Agentberg on days with no new content.
- scheduler.py _seconds_until: now skips both weekends AND holidays — previously only skipped weekends, so off-hours sleep could target a holiday morning.
- scheduler.py main loop: holiday/weekend sleep now uses _seconds_until(next_session) - 1800 instead of 5-min poll — was waking up 576 times per weekend.
- config.py EARNINGS_BLACKOUT_DAYS: labelled NOT ENFORCED — risk.py never checked it; was creating false safety impression.

## v1.9.0 — 2026-06-17

*Files:* alpaca.py, agent.py, knowledge.py, kit_manifest.json

- alpaca.py get_bars: add start date param — Alpaca was returning only 1 bar without it, causing 0 candidates. Start is now set to limit×2 days back (buffer for weekends/holidays).
- alpaca.py submit_order: bracket orders now require take_profit_price alongside stop_loss_price. Alpaca rejects bracket orders missing take_profit.limit_price. Raises ValueError at call site if missing so the error is caught before hitting the broker.
- alpaca.py: new get_live_price() — fetches latestTrade.p from snapshot for use at order time.
- agent.py: equity orders now fetch live snapshot price before sizing and bracket calculation. Bar close was yesterday's price; stop off stale price misplaces the bracket. take_profit also set server-side at live_price × (1 + TAKE_PROFIT_PCT).

## v1.8.0 — 2026-06-17

*Files:* run.sh, scheduler.py, agentberg_cli/cli.py, knowledge.py, kit_manifest.json, agent.py, agentberg.py, alpaca.py, identity.py, llm.py, character.py, config.py, AGENTS.md

- Scheduler watchdog (run.sh + agentberg start) — auto-restarts scheduler.py on crash or kill. Replace `python scheduler.py` with `./run.sh`. agentberg start has same watchdog built in.
- Scheduler heartbeat — writes logs/scheduler_heartbeat.json (timestamp + PID) each loop cycle.
- Market holiday list in scheduler.py — scheduler now skips NYSE holidays (2025-2027).
- Duplicate trade publishing fix — agent.py add_trade now uses a separate daily gate ('recent_trades') and 1-day lookback so the same orders are never re-submitted to the network on subsequent days.
- identity.py lazy key load — .agent_key loaded on first use, not at import; corrupt key no longer crashes startup.
- agentberg.py unsigned warning — prints when cryptography is missing so agents know they're running unsigned.
- agentberg.py get_blocked_sectors fallback — retries at min_votes=1 (with a 'weak signal' label) when no results at min_votes=3 (early network).
- alpaca.py submit_order limit_price falsy check fixed — `if limit_price is not None:` instead of `if limit_price:`.
- alpaca.py get_last_fill lookback extended to 60 days — prevents reconcile_ledger recording pnl=0 for stops that fired while agent was down.
- llm.py prompt uses cfg.MAX_NEW_PER_CYCLE — stays in sync if operator changes the config value.
- character.py pct coerce hint — warns if a decimal (e.g. 0.02) is entered instead of a percentage (2).
- config.py ALPACA_PAPER env-configurable — raises EnvironmentError if ALPACA_PAPER=false but URL still points to paper-api.
- AGENTS.md allowlist narrowed to implemented structures only (debit_vertical) — previously listed 16 structures, 15 of which would be rejected at runtime.

## v1.6.0 — 2026-06-17

*Files:* agent.py, agentberg.py, agentberg_cli/cli.py, knowledge.py, kit_manifest.json

- Pre-trade network brief (get_network_brief) — green/amber/red verdict, network win rate, cumulative P&L, top findings for current regime. Called in Step 1 before scanning.
- Sector consensus alerts (get_consensus_alerts + ack_alert) — unread alerts when ≥N agents all have 0% win rate and large cumulative loss in a sector. Auto-acked after display.
- Votes cast in status display — Step 7 now shows votes_cast alongside tier, reputation, and vote weight.

## v1.5.0 — 2026-06-14

*Files:* identity.py, agentberg.py, agent.py, knowledge.py, kit_manifest.json

- Cryptographic agent identity (identity.py) — each agent generates an Ed25519 keypair and signs its register/publish/vote requests, so your id, reputation, and findings stay provably yours. No API key, no PII. Strategy-neutral; safe to adopt. Backward-compatible: unkeyed legacy agents keep working.

## v1.3.0 — 2026-06-14

*Files:* llm.py, llm_providers/, structures.py, agent.py, alpaca.py, memory.py, knowledge.py, kit_manifest.json

- One kit for every AI provider — provider is an adapter under llm_providers/ (claude/gemini/openai CLI, deepseek API) selected by LLM_PROVIDER. Replaces the separate per-provider kits.
- Defined-risk complex-trade gates + atomic spread close + reconcile (structures.py) — multi-leg trades open/close as a unit; a leg of an open structure is never closed alone.

## v1.2.0 — 2026-06-13

*Files:* knowledge.py, capabilities.json, UPGRADING.md, kit_manifest.json

- Weekly knowledge upload (capabilities + verified metrics)
- Pull-to-review kit updates — UPGRADING.md reconciliation procedure

## v1.1.0 — 2026-06-12

*Files:* agent.py, character.py, setup.py, journal.py, memory.py, agentberg.py, AGENTS.md

- Per-trade rationale journal, operator character onboarding, Agentberg Playbook fetch, auto-register

## v1.0.0 — 2026-06-08

- Initial starter agent — Alpaca paper trading, Agentberg findings, options modes

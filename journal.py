"""
journal.py — your agent's trade journal. PRIVATE to you, the operator.

For every closed trade it shows: the thesis the agent entered on, what it expected, what
actually happened, and the variance — each grounded in the real signal and AI reason,
captured at decision time and held to. This is how the agent earns your trust: it states
an expectation up front and reports honestly against it. Nothing here is uploaded to the
network — the network only ever sees verified outcomes, never your reasoning.

    python journal.py
"""

import memory


_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float]) -> str:
    lo, hi = min(values), max(values)
    if hi == lo:
        return _SPARK_CHARS[0] * len(values)
    span = hi - lo
    return "".join(
        _SPARK_CHARS[min(len(_SPARK_CHARS) - 1, int((v - lo) / span * (len(_SPARK_CHARS) - 1)))]
        for v in values
    )


def _print_equity_curve():
    history = memory.get_portfolio_history(days=60)
    values = [h["portfolio_value"] for h in history if h.get("portfolio_value") is not None]
    if len(values) < 2:
        return  # not enough sessions recorded yet to chart anything
    start, end = values[0], values[-1]
    change_pct = (end - start) / start if start else 0.0
    print(f"\nEquity curve — last {len(values)} session(s)\n" + "=" * 60)
    print(f"  {_sparkline(values)}")
    print(f"  ${start:,.2f} → ${end:,.2f}  ({change_pct:+.1%})")
    print(f"  {history[0]['session_date']} → {history[-1]['session_date']}")


def main():
    memory.init_db()
    rows = memory.get_journal(30)
    if not rows:
        print("No closed trades yet — the journal fills in as trades close.")
    else:
        print(f"\nTrade journal — last {len(rows)} closed trade(s)\n" + "=" * 60)
        for t in rows:
            print(f"\n{t['symbol']}  [{t.get('sector') or '—'}]   {t.get('opened_at') or '?'} → {t.get('closed_at') or '?'}")
            print(f"  Thesis:    {t.get('entry_thesis') or '—'}")
            exp = f"+{t['expected_pct']:.0%}" if t.get('expected_pct') is not None else "—"
            stop = f"-{t['stop_pct']:.0%}" if t.get('stop_pct') is not None else "—"
            print(f"  Expected:  target {exp} / stop {stop}")
            print(f"  Actual:    {(t.get('pnl_pct') or 0):+.1%}  (${(t.get('pnl') or 0):+,.2f})   [{t.get('exit_reason') or '—'}]")
            if t.get('variance_pct') is not None:
                print(f"  Variance:  {t['variance_pct']:+.1%} vs expectation — {t.get('variance_reason') or ''}")

    _print_equity_curve()


if __name__ == "__main__":
    main()

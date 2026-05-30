"""
Producer test suite. No network: a FakeFetcher is injected at the seam, so
every policy case (omit-NaN, pence normalisation, drift correction, split
rebuild, calendar derivation, artifact shapes) is deterministic.

Run: python test_producer.py
"""

import json
import math
import sys
import tempfile
from pathlib import Path

import producer as P


# ---------------------------------------------------------------------------
# Fake fetcher
# ---------------------------------------------------------------------------
class FakeFetcher:
    def __init__(self):
        self.t: dict[str, dict] = {}

    def add(self, ticker, full=None, drift=None, divs=None, snapshot=None,
            fail_bars=False, fail_snap=False):
        self.t[ticker] = dict(full=full or [], drift=drift, divs=divs or [],
                              snapshot=snapshot, fail_bars=fail_bars, fail_snap=fail_snap)

    def fetch_bars(self, ticker, period):
        e = self.t[ticker]
        if e["fail_bars"]:
            raise ValueError("simulated bars failure")
        if period == P.BACKFILL_PERIOD:
            return list(e["full"])
        return list(e["drift"]) if e["drift"] is not None else list(e["full"][-5:])

    def fetch_dividends(self, ticker):
        return list(self.t[ticker]["divs"])

    def fetch_snapshot(self, ticker):
        e = self.t[ticker]
        if e["fail_snap"] or e["snapshot"] is None:
            raise ValueError("simulated snapshot failure")
        return dict(e["snapshot"])


def fresh_db():
    conn = P.connect(":memory:")
    P.init_schema(conn)
    return conn


def seed_one(conn, ticker, pence=0, exchange="US", currency="USD",
             category="us_stock", tradable=1, name="Test"):
    now = P._utc_now()
    conn.execute(
        "INSERT INTO tickers (ticker, name, currency, exchange, category, tradable, "
        "pence_denominated, added_at, backfilled, version, delisted_at, last_modified) "
        "VALUES (?,?,?,?,?,?,?,?,0,0,NULL,?)",
        (ticker, name, currency, exchange, category, tradable, pence, now, now))
    conn.commit()
    return conn.execute("SELECT * FROM tickers WHERE ticker = ?", (ticker,)).fetchone()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
TESTS = []
def test(fn): TESTS.append(fn); return fn


@test
def test_schema_and_seed_idempotent():
    conn = fresh_db()
    added1 = P.seed_tickers(conn, P.build_seed())
    assert added1 == 45, f"expected 45 seeded, got {added1}"
    added2 = P.seed_tickers(conn, P.build_seed())
    assert added2 == 0, f"second seed should add 0, got {added2}"
    # pence flags correct
    lloy = conn.execute("SELECT pence_denominated FROM tickers WHERE ticker='LLOY.L'").fetchone()
    eqqq = conn.execute("SELECT pence_denominated FROM tickers WHERE ticker='EQQQ.L'").fetchone()
    vwrp = conn.execute("SELECT pence_denominated FROM tickers WHERE ticker='VWRP.L'").fetchone()
    assert lloy["pence_denominated"] == 1
    assert eqqq["pence_denominated"] == 1
    assert vwrp["pence_denominated"] == 0, "VWRP.L should NOT be pence"
    # tradability
    idx = conn.execute("SELECT tradable FROM tickers WHERE ticker='^FTSE'").fetchone()
    assert idx["tradable"] == 0, "indices are watch-only"
    # ^FTAW probe is NOT seeded
    assert conn.execute("SELECT 1 FROM tickers WHERE ticker='^FTAW'").fetchone() is None


@test
def test_backfill_writes_bars_and_divs():
    conn = fresh_db()
    row = seed_one(conn, "AAPL")
    f = FakeFetcher()
    f.add("AAPL", full=[("2024-01-02", 185.0), ("2024-01-03", 186.5)],
          divs=[("2024-01-02", 0.24)])
    P.backfill_ticker(conn, f, row)
    bars = conn.execute("SELECT date, close FROM bars WHERE ticker='AAPL' ORDER BY date").fetchall()
    assert [(b["date"], b["close"]) for b in bars] == [("2024-01-02", 185.0), ("2024-01-03", 186.5)]
    r = conn.execute("SELECT backfilled, version FROM tickers WHERE ticker='AAPL'").fetchone()
    assert r["backfilled"] == 1 and r["version"] == 1


@test
def test_nan_close_is_omitted():
    conn = fresh_db()
    row = seed_one(conn, "AAPL")
    f = FakeFetcher()
    f.add("AAPL", full=[("2024-01-02", 185.0), ("2024-01-03", float("nan")),
                        ("2024-01-04", 187.0)])
    P.backfill_ticker(conn, f, row)
    dates = [b["date"] for b in conn.execute("SELECT date FROM bars WHERE ticker='AAPL'")]
    assert dates == ["2024-01-02", "2024-01-04"], f"NaN bar should be omitted, got {dates}"
    # and the published artifact must be valid JSON (allow_nan=False would raise otherwise)
    art = P.generate_history_json(conn, "AAPL")
    json.dumps(art, allow_nan=False)  # must not raise


@test
def test_pence_normalisation():
    conn = fresh_db()
    row = seed_one(conn, "LLOY.L", pence=1, exchange="LSE", currency="GBP", category="uk_stock")
    f = FakeFetcher()
    f.add("LLOY.L", full=[("2024-01-02", 99.03)], divs=[("2024-01-02", 5.0)])
    P.backfill_ticker(conn, f, row)
    bar = conn.execute("SELECT close FROM bars WHERE ticker='LLOY.L'").fetchone()
    assert abs(bar["close"] - 0.9903) < 1e-9, f"pence->pounds: got {bar['close']}"
    div = conn.execute("SELECT amount FROM dividends WHERE ticker='LLOY.L'").fetchone()
    assert abs(div["amount"] - 0.05) < 1e-9, f"div pence->pounds: got {div['amount']}"


@test
def test_daily_appends_new_bar_and_bumps_version():
    conn = fresh_db()
    row = seed_one(conn, "AAPL")
    f = FakeFetcher()
    f.add("AAPL", full=[("2024-01-02", 185.0)])
    P.backfill_ticker(conn, f, row)
    v0 = conn.execute("SELECT version FROM tickers WHERE ticker='AAPL'").fetchone()["version"]
    # drift window now reports a new day
    f.t["AAPL"]["drift"] = [("2024-01-02", 185.0), ("2024-01-03", 188.0)]
    row = conn.execute("SELECT * FROM tickers WHERE ticker='AAPL'").fetchone()
    changed, split = P.daily_update_ticker(conn, f, row)
    assert changed and not split
    v1 = conn.execute("SELECT version FROM tickers WHERE ticker='AAPL'").fetchone()["version"]
    assert v1 == v0 + 1, "version should bump on append"
    assert conn.execute("SELECT close FROM bars WHERE ticker='AAPL' AND date='2024-01-03'").fetchone()["close"] == 188.0


@test
def test_drift_corrects_changed_close():
    conn = fresh_db()
    row = seed_one(conn, "AAPL")
    f = FakeFetcher()
    f.add("AAPL", full=[("2024-01-02", 185.0), ("2024-01-03", 186.0)])
    P.backfill_ticker(conn, f, row)
    # a stored close is later corrected by ~1% (below split threshold)
    f.t["AAPL"]["drift"] = [("2024-01-02", 185.0), ("2024-01-03", 187.8)]
    row = conn.execute("SELECT * FROM tickers WHERE ticker='AAPL'").fetchone()
    changed, split = P.daily_update_ticker(conn, f, row)
    assert changed and not split
    c = conn.execute("SELECT close FROM bars WHERE ticker='AAPL' AND date='2024-01-03'").fetchone()["close"]
    assert c == 187.8, f"drift should overwrite, got {c}"


@test
def test_no_change_no_version_bump():
    conn = fresh_db()
    row = seed_one(conn, "AAPL")
    f = FakeFetcher()
    f.add("AAPL", full=[("2024-01-02", 185.0), ("2024-01-03", 186.0)])
    P.backfill_ticker(conn, f, row)
    v0 = conn.execute("SELECT version FROM tickers WHERE ticker='AAPL'").fetchone()["version"]
    f.t["AAPL"]["drift"] = [("2024-01-02", 185.0), ("2024-01-03", 186.0)]  # identical
    row = conn.execute("SELECT * FROM tickers WHERE ticker='AAPL'").fetchone()
    changed, split = P.daily_update_ticker(conn, f, row)
    assert not changed and not split
    v1 = conn.execute("SELECT version FROM tickers WHERE ticker='AAPL'").fetchone()["version"]
    assert v1 == v0, "no change => no version bump"


@test
def test_split_triggers_full_rebuild():
    conn = fresh_db()
    row = seed_one(conn, "NVDA")
    f = FakeFetcher()
    # pre-split history around 1200
    f.add("NVDA", full=[("2024-06-05", 1200.0), ("2024-06-06", 1210.0)])
    P.backfill_ticker(conn, f, row)
    v0 = conn.execute("SELECT version FROM tickers WHERE ticker='NVDA'").fetchone()["version"]
    # after a 10:1 split yfinance reports ~120; the full (backfill) series is now adjusted
    f.t["NVDA"]["full"] = [("2024-06-05", 120.0), ("2024-06-06", 121.0), ("2024-06-07", 122.0)]
    f.t["NVDA"]["drift"] = [("2024-06-06", 121.0), ("2024-06-07", 122.0)]  # 1210 -> 121 = >25%
    row = conn.execute("SELECT * FROM tickers WHERE ticker='NVDA'").fetchone()
    changed, split = P.daily_update_ticker(conn, f, row)
    assert changed and split, "big shift w/o dividend should rebuild"
    closes = {b["date"]: b["close"] for b in conn.execute("SELECT date, close FROM bars WHERE ticker='NVDA'")}
    assert closes == {"2024-06-05": 120.0, "2024-06-06": 121.0, "2024-06-07": 122.0}, \
        "rebuild should replace all bars with adjusted series"
    v1 = conn.execute("SELECT version FROM tickers WHERE ticker='NVDA'").fetchone()["version"]
    assert v1 > v0


@test
def test_split_guard_when_dividend_present():
    conn = fresh_db()
    row = seed_one(conn, "TST")
    f = FakeFetcher()
    f.add("TST", full=[("2024-01-02", 100.0)])
    P.backfill_ticker(conn, f, row)
    # large move but explained by a same-day dividend -> NOT a split rebuild
    f.t["TST"]["drift"] = [("2024-01-02", 60.0)]
    f.t["TST"]["divs"] = [("2024-01-02", 1.0)]
    row = conn.execute("SELECT * FROM tickers WHERE ticker='TST'").fetchone()
    changed, split = P.daily_update_ticker(conn, f, row)
    assert changed and not split, "dividend present should suppress split rebuild"


@test
def test_calendar_derivation_per_exchange():
    conn = fresh_db()
    a = seed_one(conn, "AAPL", exchange="US")
    l = seed_one(conn, "LLOY.L", pence=1, exchange="LSE", currency="GBP", category="uk_stock")
    f = FakeFetcher()
    f.add("AAPL", full=[("2024-01-02", 185.0), ("2024-01-03", 186.0)])
    f.add("LLOY.L", full=[("2024-01-02", 99.0), ("2024-01-04", 100.0)])
    P.backfill_ticker(conn, f, a)
    P.backfill_ticker(conn, f, l)
    bumped = P.rebuild_trading_days(conn)
    assert bumped
    cal = P.generate_calendar_json(conn)
    assert set(cal["exchanges"].keys()) == {"US", "LSE"}
    assert cal["exchanges"]["US"]["days"] == ["2024-01-02", "2024-01-03"]
    assert cal["exchanges"]["LSE"]["days"] == ["2024-01-02", "2024-01-04"]
    assert cal["exchanges"]["LSE"]["first"] == "2024-01-02"
    assert cal["exchanges"]["LSE"]["last"] == "2024-01-04"
    # second rebuild with no change => no bump
    v0 = P._calendar_version(conn)
    assert not P.rebuild_trading_days(conn)
    assert P._calendar_version(conn) == v0


@test
def test_universe_json_shape():
    conn = fresh_db()
    row = seed_one(conn, "AAPL")
    f = FakeFetcher()
    f.add("AAPL", full=[("2024-01-02", 185.0), ("2024-01-03", 186.0)])
    P.backfill_ticker(conn, f, row)
    # an un-backfilled ticker must be excluded from the index
    seed_one(conn, "ZZZZ")
    uni = P.generate_universe_json(conn)
    assert uni["schema_version"] == 1
    assert "AAPL" in uni["tickers"] and "ZZZZ" not in uni["tickers"]
    e = uni["tickers"]["AAPL"]
    assert set(e.keys()) == {"name", "currency", "exchange", "category", "tradable",
                             "pence_denominated", "delisted_at", "version", "last_date", "file"}
    assert e["last_date"] == "2024-01-03"
    assert e["file"] == "history/AAPL.json"
    assert e["delisted_at"] is None
    json.dumps(uni, allow_nan=False)


@test
def test_history_json_is_close_only():
    conn = fresh_db()
    row = seed_one(conn, "AAPL")
    f = FakeFetcher()
    f.add("AAPL", full=[("2024-01-02", 185.0)], divs=[("2024-01-02", 0.24)])
    P.backfill_ticker(conn, f, row)
    h = P.generate_history_json(conn, "AAPL")
    assert h["adjusted"] == "split_and_div"
    assert h["bars"] == [{"d": "2024-01-02", "c": 185.0}], "bars must be close-only {d,c}"
    # explicitly assert NO ohlcv keys leaked in
    for bar in h["bars"]:
        assert set(bar.keys()) == {"d", "c"}, f"unexpected bar keys: {bar.keys()}"
    assert h["dividends"] == [{"ex_date": "2024-01-02", "amount": 0.24}]


@test
def test_prices_json_status_and_errors():
    conn = fresh_db()
    a = seed_one(conn, "AAPL")
    b = seed_one(conn, "LLOY.L", pence=1, exchange="LSE", currency="GBP", category="uk_stock")
    bad = seed_one(conn, "BAD")
    f = FakeFetcher()
    f.add("AAPL", full=[("2024-01-02", 185.0)],
          snapshot=dict(last_price=187.0, open=185.0, high=188.0, low=184.0,
                        close=185.0, volume=1000, last_date="2024-01-02"))
    f.add("LLOY.L", full=[("2024-01-02", 99.0)],
          snapshot=dict(last_price=99.50, open=99.0, high=100.0, low=98.0,
                        close=99.03, volume=500, last_date="2024-01-02"))
    f.add("BAD", full=[("2024-01-02", 1.0)], fail_snap=True)
    for r in (a, b, bad):
        P.backfill_ticker(conn, f, conn.execute("SELECT * FROM tickers WHERE ticker=?", (r["ticker"],)).fetchone())
    out = Path(tempfile.mkdtemp())
    summary = P.run_prices(conn, f, out)
    assert summary["status"] == "degraded", summary
    pj = json.loads((out / "prices.json").read_text())
    assert pj["status"] == "degraded"
    assert "BAD" in pj["errors"]
    # pence snapshot normalised
    assert abs(pj["prices"]["LLOY.L"]["last_price"] - 0.995) < 1e-9
    assert abs(pj["prices"]["LLOY.L"]["close"] - 0.9903) < 1e-9
    # non-pence untouched
    assert pj["prices"]["AAPL"]["last_price"] == 187.0


@test
def test_run_daily_end_to_end_partial_failure():
    conn = fresh_db()
    P.seed_tickers(conn, [dict(ticker="AAPL", name="Apple", currency="USD", exchange="US",
                               category="us_stock", tradable=1, pence=0),
                          dict(ticker="BAD", name="Bad", currency="USD", exchange="US",
                               category="us_stock", tradable=1, pence=0)])
    f = FakeFetcher()
    f.add("AAPL", full=[("2024-01-02", 185.0), ("2024-01-03", 186.0)])
    f.add("BAD", fail_bars=True)
    out = Path(tempfile.mkdtemp())
    summary = P.run_daily(conn, f, out)
    assert summary["processed"] == 1, summary
    assert "BAD" in summary["errors"]
    # artifacts written for the good ticker + index + calendar
    assert (out / "universe.json").exists()
    assert (out / "calendar.json").exists()
    assert (out / "history" / "AAPL.json").exists()
    assert not (out / "history" / "BAD.json").exists()
    # run logged
    run = conn.execute("SELECT * FROM producer_runs WHERE kind='daily'").fetchone()
    assert run["tickers_processed"] == 1 and run["errors"] == 1


# ---------------------------------------------------------------------------
# Stateless / hydration tests (the Actions-runner model)
# ---------------------------------------------------------------------------
@test
def test_cold_start_no_artifacts():
    conn = fresh_db()
    out = Path(tempfile.mkdtemp())
    hydrated = P.hydrate_from_artifacts(conn, out, with_history=True)
    assert hydrated is False, "no universe.json => cold start"


@test
def test_hydration_roundtrip():
    # First run: cold start, backfill, write artifacts.
    conn1 = fresh_db()
    P.seed_tickers(conn1, [dict(ticker="AAPL", name="Apple", currency="USD", exchange="US",
                                category="us_stock", tradable=1, pence=0),
                           dict(ticker="LLOY.L", name="Lloyds", currency="GBP", exchange="LSE",
                                category="uk_stock", tradable=1, pence=1)])
    f = FakeFetcher()
    f.add("AAPL", full=[("2024-01-02", 185.0), ("2024-01-03", 186.0)], divs=[("2024-01-03", 0.24)])
    f.add("LLOY.L", full=[("2024-01-02", 99.0)])
    out = Path(tempfile.mkdtemp())
    P.run_daily(conn1, f, out)

    # Second process: fresh empty DB, hydrate purely from the written artifacts.
    conn2 = fresh_db()
    assert P.hydrate_from_artifacts(conn2, out, with_history=True) is True
    bars = {b["date"]: b["close"]
            for b in conn2.execute("SELECT date, close FROM bars WHERE ticker='AAPL'")}
    assert bars == {"2024-01-02": 185.0, "2024-01-03": 186.0}, bars
    div = conn2.execute("SELECT amount FROM dividends WHERE ticker='AAPL'").fetchone()
    assert div["amount"] == 0.24
    r = conn2.execute("SELECT backfilled, version, pence_denominated FROM tickers WHERE ticker='LLOY.L'").fetchone()
    assert r["backfilled"] == 1 and r["version"] >= 1 and r["pence_denominated"] == 1
    # calendar hydrated too
    days = [t["date"] for t in conn2.execute("SELECT date FROM trading_days WHERE exchange='US' ORDER BY date")]
    assert days == ["2024-01-02", "2024-01-03"]


@test
def test_hydration_preserves_version_continuity():
    # Cold start at version 1, write artifacts.
    conn1 = fresh_db()
    P.seed_tickers(conn1, [dict(ticker="AAPL", name="Apple", currency="USD", exchange="US",
                                category="us_stock", tradable=1, pence=0)])
    f = FakeFetcher()
    f.add("AAPL", full=[("2024-01-02", 185.0)])
    out = Path(tempfile.mkdtemp())
    P.run_daily(conn1, f, out)
    uni1 = json.loads((out / "universe.json").read_text())
    assert uni1["tickers"]["AAPL"]["version"] == 1

    # New "process": hydrate, then a new day arrives. Version must go 1 -> 2,
    # NOT reset to 1 -- proving drift propagation survives a stateless restart.
    conn2 = fresh_db()
    P.hydrate_from_artifacts(conn2, out, with_history=True)
    P.seed_tickers(conn2, P.build_seed())  # mimic main(): seed runs after hydrate
    f.t["AAPL"]["drift"] = [("2024-01-02", 185.0), ("2024-01-03", 188.0)]
    P.run_daily(conn2, f, out)
    uni2 = json.loads((out / "universe.json").read_text())
    assert uni2["tickers"]["AAPL"]["version"] == 2, \
        f"version should continue 1->2 across restart, got {uni2['tickers']['AAPL']['version']}"
    hist = json.loads((out / "history" / "AAPL.json").read_text())
    assert {b["d"] for b in hist["bars"]} == {"2024-01-02", "2024-01-03"}


@test
def test_hydration_self_heal_missing_history():
    conn1 = fresh_db()
    P.seed_tickers(conn1, [dict(ticker="AAPL", name="Apple", currency="USD", exchange="US",
                                category="us_stock", tradable=1, pence=0)])
    f = FakeFetcher()
    f.add("AAPL", full=[("2024-01-02", 185.0)])
    out = Path(tempfile.mkdtemp())
    P.run_daily(conn1, f, out)
    # Corrupt: delete the history file but leave it referenced in the index.
    (out / "history" / "AAPL.json").unlink()
    conn2 = fresh_db()
    P.hydrate_from_artifacts(conn2, out, with_history=True)
    r = conn2.execute("SELECT backfilled FROM tickers WHERE ticker='AAPL'").fetchone()
    assert r["backfilled"] == 0, "missing history file should mark ticker for re-backfill"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main() -> int:
    passed = failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed ({len(TESTS)} total)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

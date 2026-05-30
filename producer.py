"""
Sim-trader producer (universe-expansion rework).

Replaces the old fetch_history.py / fetch_prices.py "re-download everything"
model with an append-only SQLite working store (universe.db) plus published
static artifacts matching the FROZEN v1 contract in
ARCHITECTURE_universe_expansion.md:

    universe.json          index + per-ticker version
    prices.json            latest snapshot (status + errors)
    calendar.json          per-exchange trading days
    history/<ticker>.json  close-only bars + dividends, version-stamped

Design notes
------------
- universe.db is the producer's PRIVATE store. It is never served to clients.
- yfinance lives behind the `Fetcher` seam so all DB / drift / artifact logic
  is testable against mocks with no network. The real `YFinanceFetcher`
  lazy-imports yfinance, so this module imports fine without it installed.
- Bars are CLOSE-ONLY by design: the app draws line charts only (SPEC rules
  candlesticks out of scope) and the engine fills on close. Storing OHLCV
  would ~triple the hot-path artifact for fields nothing reads, and would make
  drift fire on OHLCV wobble the consumer can't see.
- Pence->pounds normalisation is driven by the tickers.pence_denominated
  column (seeded once), not hard-coded in the fetch path.
- Missing / NaN closes are OMITTED, never written or forward-filled. A NaN
  therefore never reaches an artifact (kills the historical ^N225 bug at
  source). calendar.json is the authority on which days should have traded.
- Splits are handled adjusted-at-rest: a >25% single-bar shift in the 30-day
  drift window that isn't explained by a same-day dividend triggers a full
  5y rebuild of that ticker (and a version bump). Rare but dramatic; cheap to
  handle this way.

Entry points (argparse subcommands):
    python producer.py init     # create schema + seed tickers (idempotent)
    python producer.py daily     # backfill new tickers, append, drift, artifacts
    python producer.py prices    # refresh snapshot -> prices.json only

CT's machine runs `daily` once/day and `prices` every ~30 min, same cadence
as the two old crons.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable, Optional, Protocol

# ---------------------------------------------------------------------------
# Universe seed
# ---------------------------------------------------------------------------
# The 45-ticker starting universe. This used to be implicit in the two fetch
# scripts; it now seeds the `tickers` table so adding a ticker is a data
# change (a row), not a code change -- which is the whole point of the rework.
#
# `exchange` doubles as the calendar key: every ticker on the same exchange
# shares a trading-day calendar derived from the union of its tickers' actual
# bar dates. US equities + US-listed ETFs + US indices share "US" (NYSE and
# NASDAQ keep the same holiday calendar). ^FTAW (the FTSE All-World index
# probe in the old scripts) is intentionally NOT seeded -- it's a probe, not a
# universe member, and reliably errors via yfinance.
#
# Fields: ticker, name, currency, exchange, category, tradable, pence
_UK_STOCKS = [
    ("LLOY.L", "Lloyds Banking Group"),
    ("SHEL.L", "Shell"),
    ("AZN.L", "AstraZeneca"),
    ("ULVR.L", "Unilever"),
    ("BARC.L", "Barclays"),
    ("GSK.L", "GSK"),
    ("HSBA.L", "HSBC Holdings"),
    ("BP.L", "BP"),
    ("RIO.L", "Rio Tinto"),
    ("VOD.L", "Vodafone Group"),
    ("TSCO.L", "Tesco"),
    ("DGE.L", "Diageo"),
    ("NWG.L", "NatWest Group"),
    ("GLEN.L", "Glencore"),
    ("AAL.L", "Anglo American"),
]
_US_STOCKS = [
    ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("GOOGL", "Alphabet"),
    ("AMZN", "Amazon"), ("NVDA", "NVIDIA"), ("META", "Meta Platforms"),
    ("TSLA", "Tesla"), ("NFLX", "Netflix"), ("AMD", "AMD"),
    ("PLTR", "Palantir"), ("COIN", "Coinbase"),
]
_LSE_ETFS = [
    ("VWRP.L", "Vanguard FTSE All-World UCITS ETF"),
    ("VUSA.L", "Vanguard S&P 500 UCITS ETF"),
    ("VUKE.L", "Vanguard FTSE 100 UCITS ETF"),
    ("VAGP.L", "Vanguard Global Aggregate Bond UCITS ETF"),
    ("IGLN.L", "iShares Physical Gold ETC"),
    ("VFEM.L", "Vanguard FTSE Emerging Markets UCITS ETF"),
    ("EQQQ.L", "Invesco EQQQ Nasdaq-100 UCITS ETF"),
]
_US_ETFS = [
    ("SPY", "SPDR S&P 500 ETF Trust"), ("QQQ", "Invesco QQQ Trust"),
    ("VOO", "Vanguard S&P 500 ETF"), ("VTI", "Vanguard Total Stock Market ETF"),
]
_INDICES = [
    ("^FTSE", "FTSE 100", "LSE"), ("^GSPC", "S&P 500", "US"),
    ("^NDX", "Nasdaq 100", "US"), ("^DJI", "Dow Jones Industrial Average", "US"),
    ("^N225", "Nikkei 225", "JPX"), ("^FCHI", "CAC 40", "EURONEXT"),
]
_COMMODITIES = [
    ("GC=F", "Gold Futures", "CME"), ("CL=F", "Crude Oil WTI Futures", "CME"),
]

# Pence-denominated LSE tickers: yfinance returns these in pence (GBX), so the
# producer divides prices AND dividends by 100. EQQQ.L is the odd ETF in the
# set; the other LSE ETFs come back in pounds already.
_PENCE = {t for t, _ in _UK_STOCKS} | {"EQQQ.L"}


def build_seed() -> list[dict]:
    seed: list[dict] = []
    for t, name in _UK_STOCKS:
        seed.append(dict(ticker=t, name=name, currency="GBP", exchange="LSE",
                         category="uk_stock", tradable=1, pence=1))
    for t, name in _US_STOCKS:
        seed.append(dict(ticker=t, name=name, currency="USD", exchange="US",
                         category="us_stock", tradable=1, pence=0))
    for t, name in _LSE_ETFS:
        seed.append(dict(ticker=t, name=name, currency="GBP", exchange="LSE",
                         category="etf", tradable=1, pence=1 if t in _PENCE else 0))
    for t, name in _US_ETFS:
        seed.append(dict(ticker=t, name=name, currency="USD", exchange="US",
                         category="etf", tradable=1, pence=0))
    for t, name, exch in _INDICES:
        # Indices are quoted in points; treat as the listing currency for
        # display, never pence-normalised, never tradable.
        cur = "GBP" if exch == "LSE" else ("JPY" if exch == "JPX"
                                           else ("EUR" if exch == "EURONEXT" else "USD"))
        seed.append(dict(ticker=t, name=name, currency=cur, exchange=exch,
                         category="index", tradable=0, pence=0))
    for t, name, exch in _COMMODITIES:
        seed.append(dict(ticker=t, name=name, currency="USD", exchange=exch,
                         category="commodity", tradable=0, pence=0))
    return seed


# ---------------------------------------------------------------------------
# Numeric guards (carried over from the old scripts)
# ---------------------------------------------------------------------------
def safe_num(x) -> Optional[float]:
    """float(x) unless x is NaN/unconvertible, else None.

    NaN is the only float that fails equality with itself, so `f == f` is the
    NaN test. Returning None means "omit" everywhere downstream -- a NaN never
    survives to an artifact.
    """
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def normalise_price(pence_denominated: bool, x, ndigits: int = 4) -> Optional[float]:
    s = safe_num(x)
    if s is None:
        return None
    if pence_denominated:
        s = s / 100.0
    return round(s, ndigits)


# ---------------------------------------------------------------------------
# Fetch seam
# ---------------------------------------------------------------------------
class Fetcher(Protocol):
    """The only thing that talks to yfinance. Tests inject a fake."""

    def fetch_bars(self, ticker: str, period: str) -> list[tuple[str, float]]:
        """Return [(YYYY-MM-DD, raw_close), ...] ascending. Raw = pre-pence,
        as yfinance gives it. Raise on hard failure (empty/no data)."""

    def fetch_dividends(self, ticker: str) -> list[tuple[str, float]]:
        """Return [(YYYY-MM-DD ex_date, raw_amount), ...]. Empty is fine."""

    def fetch_snapshot(self, ticker: str) -> dict:
        """Return latest snapshot dict with raw (pre-pence) numbers:
        {last_price, open, high, low, close, volume, last_date}. Raise on
        no usable last_price."""


class YFinanceFetcher:
    """Real fetcher. Lazy-imports yfinance so the module loads without it.

    Mirrors the behaviour of the old scripts: auto_adjust=False (real prices,
    dividends tracked separately as cash events), fast_info for the live tick
    with a fall back to the last daily close.
    """

    def _yf(self):
        import yfinance as yf  # lazy
        return yf

    def fetch_bars(self, ticker: str, period: str) -> list[tuple[str, float]]:
        yf = self._yf()
        hist = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
        if hist.empty:
            raise ValueError(f"no daily history returned for {ticker}")
        out = []
        for idx, row in hist.iterrows():
            out.append((idx.date().isoformat(), row["Close"]))
        return out

    def fetch_dividends(self, ticker: str) -> list[tuple[str, float]]:
        yf = self._yf()
        try:
            divs = yf.Ticker(ticker).dividends
        except Exception:
            return []
        return [(idx.date().isoformat(), amt) for idx, amt in divs.items()]

    def fetch_snapshot(self, ticker: str) -> dict:
        yf = self._yf()
        t = yf.Ticker(ticker)
        hist = t.history(period="5d", interval="1d", auto_adjust=False)
        if hist.empty:
            raise ValueError(f"no daily history returned for {ticker}")
        last = hist.iloc[-1]
        last_date = hist.index[-1].date().isoformat()
        lp = None
        try:
            lp = safe_num(t.fast_info["last_price"])
        except Exception:
            pass
        if lp is None:
            lp = safe_num(last["Close"])
        if lp is None:
            raise ValueError(f"no usable last_price for {ticker}")
        vol = last["Volume"]
        return {
            "last_price": lp, "open": last["Open"], "high": last["High"],
            "low": last["Low"], "close": last["Close"],
            "volume": int(vol) if safe_num(vol) is not None else 0,
            "last_date": last_date,
        }


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS tickers (
    ticker            TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    currency          TEXT NOT NULL,
    exchange          TEXT NOT NULL,
    category          TEXT NOT NULL,
    tradable          INTEGER NOT NULL,
    pence_denominated INTEGER NOT NULL,
    added_at          TEXT NOT NULL,
    backfilled        INTEGER NOT NULL DEFAULT 0,
    version           INTEGER NOT NULL DEFAULT 0,
    delisted_at       TEXT,
    last_modified     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bars (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,
    close  REAL NOT NULL,
    PRIMARY KEY (ticker, date)
);
CREATE TABLE IF NOT EXISTS dividends (
    ticker  TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    amount  REAL NOT NULL,
    PRIMARY KEY (ticker, ex_date)
);
CREATE TABLE IF NOT EXISTS prices_snapshot (
    ticker     TEXT PRIMARY KEY,
    last_price REAL NOT NULL,
    open       REAL, high REAL, low REAL, close REAL, volume INTEGER,
    last_date  TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trading_days (
    exchange TEXT NOT NULL,
    date     TEXT NOT NULL,
    PRIMARY KEY (exchange, date)
);
CREATE TABLE IF NOT EXISTS calendar_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS producer_runs (
    started_at        TEXT NOT NULL,
    kind              TEXT NOT NULL,
    tickers_processed INTEGER NOT NULL,
    drift_corrections INTEGER NOT NULL,
    errors            INTEGER NOT NULL
);
"""

SPLIT_THRESHOLD = 0.25       # >25% single-bar move not explained by a dividend
DRIFT_PERIOD = "35d"         # re-fetch window for daily drift check
BACKFILL_PERIOD = "5y"
PRICE_EPSILON = 1e-6         # closes within this are "equal" (float noise)


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO calendar_meta (id, version) VALUES (1, 0)")
    conn.commit()


def seed_tickers(conn: sqlite3.Connection, seed: list[dict]) -> int:
    """Insert any seed tickers not already present. Idempotent; never clobbers
    existing rows (which may carry accumulated version/backfill state)."""
    now = _utc_now()
    added = 0
    for r in seed:
        cur = conn.execute("SELECT 1 FROM tickers WHERE ticker = ?", (r["ticker"],))
        if cur.fetchone():
            continue
        conn.execute(
            "INSERT INTO tickers (ticker, name, currency, exchange, category, "
            "tradable, pence_denominated, added_at, backfilled, version, "
            "delisted_at, last_modified) VALUES (?,?,?,?,?,?,?,?,0,0,NULL,?)",
            (r["ticker"], r["name"], r["currency"], r["exchange"], r["category"],
             r["tradable"], r["pence"], now, now),
        )
        added += 1
    conn.commit()
    return added


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_ticker_rows(conn: sqlite3.Connection, include_delisted: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM tickers"
    if not include_delisted:
        q += " WHERE delisted_at IS NULL"
    q += " ORDER BY ticker"
    return list(conn.execute(q))


def _bump_version(conn: sqlite3.Connection, ticker: str) -> None:
    conn.execute(
        "UPDATE tickers SET version = version + 1, last_modified = ? WHERE ticker = ?",
        (_utc_now(), ticker),
    )


# ---------------------------------------------------------------------------
# Bar / dividend application (transform + store, with omit + pence + drift)
# ---------------------------------------------------------------------------
def _transform_bars(pence: bool, raw: Iterable[tuple[str, float]]) -> dict[str, float]:
    """raw (date, raw_close) -> {date: pounds_close}, omitting NaN/None."""
    out: dict[str, float] = {}
    for d, rc in raw:
        c = normalise_price(pence, rc, 4)
        if c is None:
            continue  # OMIT policy -- no NaN ever stored
        out[d] = c
    return out


def _transform_divs(pence: bool, raw: Iterable[tuple[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for d, ra in raw:
        a = normalise_price(pence, ra, 6)
        if a is None:
            continue
        out[d] = a
    return out


def _stored_closes(conn: sqlite3.Connection, ticker: str) -> dict[str, float]:
    return {r["date"]: r["close"]
            for r in conn.execute("SELECT date, close FROM bars WHERE ticker = ?", (ticker,))}


def _looks_like_split(old: float, new: float, has_div_today: bool) -> bool:
    """A >25% single-bar move with no same-day dividend smells like a split
    (or a corporate action) that retroactively shifts all prior closes."""
    if has_div_today or old <= 0:
        return False
    return abs(new - old) / old > SPLIT_THRESHOLD


def backfill_ticker(conn: sqlite3.Connection, fetcher: Fetcher, row: sqlite3.Row) -> bool:
    """Full 5y fetch for a new (or split-triggered) ticker. Returns True if any
    data was written. Bumps version. Sets backfilled = 1."""
    ticker, pence = row["ticker"], bool(row["pence_denominated"])
    bars = _transform_bars(pence, fetcher.fetch_bars(ticker, BACKFILL_PERIOD))
    if not bars:
        raise ValueError(f"no usable bars for {ticker} (all closes NaN/missing)")
    divs = _transform_divs(pence, fetcher.fetch_dividends(ticker))
    first, last = min(bars), max(bars)
    conn.execute("DELETE FROM bars WHERE ticker = ?", (ticker,))
    conn.execute("DELETE FROM dividends WHERE ticker = ?", (ticker,))
    conn.executemany("INSERT INTO bars (ticker, date, close) VALUES (?,?,?)",
                     [(ticker, d, c) for d, c in sorted(bars.items())])
    conn.executemany("INSERT INTO dividends (ticker, ex_date, amount) VALUES (?,?,?)",
                     [(ticker, d, a) for d, a in sorted(divs.items()) if first <= d <= last])
    conn.execute("UPDATE tickers SET backfilled = 1 WHERE ticker = ?", (ticker,))
    _bump_version(conn, ticker)
    return True


def daily_update_ticker(conn: sqlite3.Connection, fetcher: Fetcher,
                        row: sqlite3.Row) -> tuple[bool, bool]:
    """Append + 30-day drift re-fetch for one already-backfilled ticker.

    Returns (changed, split_rebuilt). `changed` => version was bumped and the
    history artifact should be regenerated. A detected split triggers a full
    rebuild instead (which itself bumps version)."""
    ticker, pence = row["ticker"], bool(row["pence_denominated"])
    fresh = _transform_bars(pence, fetcher.fetch_bars(ticker, DRIFT_PERIOD))
    if not fresh:
        return (False, False)
    fresh_divs = _transform_divs(pence, fetcher.fetch_dividends(ticker))
    stored = _stored_closes(conn, ticker)

    # Split / corporate-action detection across the overlap window.
    for d, new_c in fresh.items():
        old_c = stored.get(d)
        if old_c is not None and _looks_like_split(old_c, new_c, d in fresh_divs):
            backfill_ticker(conn, fetcher, row)  # adjusted-at-rest full rebuild
            return (True, True)

    changed = False
    for d, new_c in sorted(fresh.items()):
        old_c = stored.get(d)
        if old_c is None:
            conn.execute("INSERT INTO bars (ticker, date, close) VALUES (?,?,?)",
                         (ticker, d, new_c))
            changed = True
        elif abs(old_c - new_c) > PRICE_EPSILON:
            conn.execute("UPDATE bars SET close = ? WHERE ticker = ? AND date = ?",
                         (new_c, ticker, d))
            changed = True

    # New dividends landing in the window.
    first, last = min(fresh), max(fresh)
    for d, a in fresh_divs.items():
        if not (first <= d <= last):
            continue
        cur = conn.execute("SELECT amount FROM dividends WHERE ticker = ? AND ex_date = ?",
                           (ticker, d)).fetchone()
        if cur is None:
            conn.execute("INSERT INTO dividends (ticker, ex_date, amount) VALUES (?,?,?)",
                         (ticker, d, a))
            changed = True
        elif abs(cur["amount"] - a) > PRICE_EPSILON:
            conn.execute("UPDATE dividends SET amount = ? WHERE ticker = ? AND ex_date = ?",
                         (a, ticker, d))
            changed = True

    if changed:
        _bump_version(conn, ticker)
    return (changed, False)


# ---------------------------------------------------------------------------
# Trading calendar (derived from observed bar dates, per exchange)
# ---------------------------------------------------------------------------
def rebuild_trading_days(conn: sqlite3.Connection) -> bool:
    """Recompute trading_days as the union of bar dates per exchange. Returns
    True if the set changed (=> calendar version bumped)."""
    rows = conn.execute(
        "SELECT t.exchange AS exch, b.date AS date "
        "FROM bars b JOIN tickers t ON t.ticker = b.ticker "
        "GROUP BY t.exchange, b.date"
    )
    fresh: set[tuple[str, str]] = {(r["exch"], r["date"]) for r in rows}
    existing: set[tuple[str, str]] = {
        (r["exchange"], r["date"]) for r in conn.execute("SELECT exchange, date FROM trading_days")
    }
    if fresh == existing:
        return False
    conn.execute("DELETE FROM trading_days")
    conn.executemany("INSERT INTO trading_days (exchange, date) VALUES (?,?)", sorted(fresh))
    conn.execute("UPDATE calendar_meta SET version = version + 1 WHERE id = 1")
    return True


def _calendar_version(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT version FROM calendar_meta WHERE id = 1").fetchone()["version"]


# ---------------------------------------------------------------------------
# Artifact generation (the frozen v1 contract)
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1


def _safe_filename(ticker: str) -> str:
    """history/<safe>.json. Symbols can contain ^ = . -- keep them but they're
    URL-safe enough for raw.githubusercontent paths; the consumer reads the
    `file` field rather than reconstructing this, so the exact scheme is ours."""
    return f"history/{ticker}.json"


def generate_universe_json(conn: sqlite3.Connection) -> dict:
    tickers = {}
    for r in get_ticker_rows(conn):
        if not r["backfilled"]:
            continue
        last = conn.execute("SELECT MAX(date) AS d FROM bars WHERE ticker = ?",
                            (r["ticker"],)).fetchone()["d"]
        tickers[r["ticker"]] = {
            "name": r["name"], "currency": r["currency"], "exchange": r["exchange"],
            "category": r["category"], "tradable": bool(r["tradable"]),
            "pence_denominated": bool(r["pence_denominated"]),
            "delisted_at": r["delisted_at"], "version": r["version"],
            "last_date": last, "file": _safe_filename(r["ticker"]),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "calendar_version": _calendar_version(conn),
        "tickers": tickers,
    }


def generate_history_json(conn: sqlite3.Connection, ticker: str) -> dict:
    r = conn.execute("SELECT * FROM tickers WHERE ticker = ?", (ticker,)).fetchone()
    bars = [{"d": b["date"], "c": b["close"]}
            for b in conn.execute("SELECT date, close FROM bars WHERE ticker = ? ORDER BY date",
                                  (ticker,))]
    divs = [{"ex_date": d["ex_date"], "amount": d["amount"]}
            for d in conn.execute(
                "SELECT ex_date, amount FROM dividends WHERE ticker = ? ORDER BY ex_date",
                (ticker,))]
    return {
        "ticker": ticker, "version": r["version"], "currency": r["currency"],
        "pence_denominated": bool(r["pence_denominated"]),
        "adjusted": "split_and_div", "bars": bars, "dividends": divs,
    }


def generate_calendar_json(conn: sqlite3.Connection) -> dict:
    exchanges: dict[str, dict] = {}
    rows = conn.execute("SELECT exchange, date FROM trading_days ORDER BY exchange, date")
    for r in rows:
        exchanges.setdefault(r["exchange"], {"days": []})["days"].append(r["date"])
    for exch, blob in exchanges.items():
        days = blob["days"]
        blob["first"], blob["last"] = days[0], days[-1]
        # reorder keys for readability: first, last, days
        exchanges[exch] = {"first": days[0], "last": days[-1], "days": days}
    return {
        "schema_version": SCHEMA_VERSION,
        "version": _calendar_version(conn),
        "exchanges": exchanges,
    }


def generate_prices_json(snapshots: dict[str, dict], errors: dict[str, str],
                        fetched_at: str) -> dict:
    status = "healthy" if not errors else ("degraded" if snapshots else "stale")
    return {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": fetched_at,
        "status": status,
        "prices": snapshots,
        "errors": errors,
    }


def _write_json(path: Path, obj: dict, compact: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(obj, separators=(",", ":"), allow_nan=False)
    else:
        text = json.dumps(obj, indent=2, allow_nan=False)
    path.write_text(text)


# ---------------------------------------------------------------------------
# Hydration (stateless operation)
# ---------------------------------------------------------------------------
# GitHub Actions runners are ephemeral -- universe.db cannot persist between
# runs. So the PUBLISHED ARTIFACTS are the source of truth, and the DB is
# rebuilt from them at the start of every run: read universe.json + history/*
# + calendar.json back into the tables, do the incremental append/drift, then
# rewrite the changed artifacts. No binary state is ever committed.
#
# Version continuity is preserved because each ticker's `version` is read back
# from universe.json, so drift-propagation keeps incrementing correctly across
# runs. Audit fields not carried by the contract (added_at, last_modified) are
# defaulted on hydration -- they're producer-internal niceties, not load-
# bearing; the durable audit trail is the Actions run log. The producer_runs
# table is likewise per-run only in this mode.
def hydrate_from_artifacts(conn: sqlite3.Connection, out_dir: Path,
                           with_history: bool = True) -> bool:
    """Rebuild DB state from committed artifacts in `out_dir`. Returns False on
    a cold start (no universe.json present), in which case the seed + a full
    backfill will populate everything. Idempotent (INSERT OR REPLACE)."""
    uni_path = out_dir / "universe.json"
    if not uni_path.exists():
        return False
    uni = json.loads(uni_path.read_text())
    now = _utc_now()

    for ticker, e in uni["tickers"].items():
        conn.execute(
            "INSERT OR REPLACE INTO tickers (ticker, name, currency, exchange, "
            "category, tradable, pence_denominated, added_at, backfilled, version, "
            "delisted_at, last_modified) VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
            (ticker, e["name"], e["currency"], e["exchange"], e["category"],
             1 if e["tradable"] else 0, 1 if e["pence_denominated"] else 0,
             now, e["version"], e.get("delisted_at"), now),
        )

    cal_path = out_dir / "calendar.json"
    if cal_path.exists():
        cal = json.loads(cal_path.read_text())
        conn.execute("UPDATE calendar_meta SET version = ? WHERE id = 1", (cal["version"],))
        for exch, blob in cal["exchanges"].items():
            conn.executemany("INSERT OR REPLACE INTO trading_days (exchange, date) VALUES (?,?)",
                             [(exch, d) for d in blob["days"]])

    if with_history:
        for ticker, e in uni["tickers"].items():
            hp = out_dir / e["file"]
            if not hp.exists():
                # Self-heal: index references a history file that's gone. Mark
                # for re-backfill rather than carrying a ticker with no bars.
                conn.execute("UPDATE tickers SET backfilled = 0 WHERE ticker = ?", (ticker,))
                print(f"WARN hydrate: missing {e['file']} for {ticker}; will re-backfill",
                      file=sys.stderr)
                continue
            h = json.loads(hp.read_text())
            conn.executemany("INSERT OR REPLACE INTO bars (ticker, date, close) VALUES (?,?,?)",
                             [(ticker, b["d"], b["c"]) for b in h["bars"]])
            conn.executemany(
                "INSERT OR REPLACE INTO dividends (ticker, ex_date, amount) VALUES (?,?,?)",
                [(ticker, d["ex_date"], d["amount"]) for d in h["dividends"]])
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Run orchestration
# ---------------------------------------------------------------------------
def _record_run(conn: sqlite3.Connection, kind: str, started: str,
                processed: int, drift: int, errors: int) -> None:
    conn.execute(
        "INSERT INTO producer_runs (started_at, kind, tickers_processed, "
        "drift_corrections, errors) VALUES (?,?,?,?,?)",
        (started, kind, processed, drift, errors),
    )
    conn.commit()


def run_daily(conn: sqlite3.Connection, fetcher: Fetcher, out_dir: Path) -> dict:
    """Backfill new tickers, append + drift existing ones, regenerate changed
    history artifacts + always-regen universe/calendar. Returns a summary."""
    started = _utc_now()
    processed = drift = errs = 0
    changed_tickers: list[str] = []
    errors: dict[str, str] = {}

    for row in get_ticker_rows(conn, include_delisted=False):
        ticker = row["ticker"]
        try:
            if not row["backfilled"]:
                backfill_ticker(conn, fetcher, row)
                changed_tickers.append(ticker)
            else:
                changed, split = daily_update_ticker(conn, fetcher, row)
                if changed:
                    changed_tickers.append(ticker)
                    drift += 1
            processed += 1
        except Exception as e:  # partial failure tolerated, named in run log
            errors[ticker] = str(e)
            errs += 1
            print(f"FAIL {ticker}: {e}", file=sys.stderr)
    conn.commit()

    rebuild_trading_days(conn)
    conn.commit()

    # Regenerate artifacts. history/*.json only for changed tickers; the index
    # and calendar always (cheap, and versions inside drive consumer refetch).
    for ticker in changed_tickers:
        # re-fetch the row to read the post-bump version
        r = conn.execute("SELECT ticker FROM tickers WHERE ticker = ? AND backfilled = 1",
                         (ticker,)).fetchone()
        if r:
            _write_json(out_dir / _safe_filename(ticker),
                        generate_history_json(conn, ticker), compact=True)
    _write_json(out_dir / "universe.json", generate_universe_json(conn), compact=True)
    _write_json(out_dir / "calendar.json", generate_calendar_json(conn), compact=True)

    _record_run(conn, "daily", started, processed, drift, errs)
    return {"processed": processed, "changed": len(changed_tickers),
            "drift_corrections": drift, "errors": errors}


def run_prices(conn: sqlite3.Connection, fetcher: Fetcher, out_dir: Path) -> dict:
    """Refresh the live snapshot for every backfilled ticker -> prices.json."""
    started = _utc_now()
    fetched_at = _utc_now()
    snapshots: dict[str, dict] = {}
    errors: dict[str, str] = {}

    for row in get_ticker_rows(conn, include_delisted=False):
        if not row["backfilled"]:
            continue
        ticker, pence = row["ticker"], bool(row["pence_denominated"])
        try:
            raw = fetcher.fetch_snapshot(ticker)
            snap = {
                "last_price": normalise_price(pence, raw["last_price"], 4),
                "open": normalise_price(pence, raw["open"], 4),
                "high": normalise_price(pence, raw["high"], 4),
                "low": normalise_price(pence, raw["low"], 4),
                "close": normalise_price(pence, raw["close"], 4),
                "volume": int(raw.get("volume") or 0),
                "last_date": raw["last_date"],
            }
            if snap["last_price"] is None:
                raise ValueError("no usable last_price after normalisation")
            snapshots[ticker] = snap
            conn.execute(
                "INSERT OR REPLACE INTO prices_snapshot (ticker, last_price, open, "
                "high, low, close, volume, last_date, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ticker, snap["last_price"], snap["open"], snap["high"], snap["low"],
                 snap["close"], snap["volume"], snap["last_date"], fetched_at),
            )
        except Exception as e:
            errors[ticker] = str(e)
            print(f"FAIL {ticker}: {e}", file=sys.stderr)
    conn.commit()

    _write_json(out_dir / "prices.json",
                generate_prices_json(snapshots, errors, fetched_at), compact=False)
    _record_run(conn, "prices", started, len(snapshots), 0, len(errors))
    return {"ok": len(snapshots), "errors": errors,
            "status": "healthy" if not errors else ("degraded" if snapshots else "stale")}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Sim-trader producer (SQLite + artifacts)")
    p.add_argument("command", choices=["init", "daily", "prices"])
    p.add_argument("--db", default=":memory:",
                   help="SQLite working store. Default ':memory:' (stateless: DB is "
                        "rebuilt from artifacts each run, nothing binary persists). "
                        "Pass a path for a persistent local store.")
    p.add_argument("--out", default=".", help="dir holding/receiving published artifacts")
    args = p.parse_args(argv)

    out_dir = Path(args.out)
    conn = connect(args.db)
    init_schema(conn)

    # Stateless rebuild: load prior state from committed artifacts before doing
    # anything. Cold start (no artifacts) -> hydrated is False -> seed + backfill.
    hydrated = hydrate_from_artifacts(conn, out_dir, with_history=(args.command == "daily"))
    if hydrated:
        print(f"Hydrated from artifacts in {out_dir}")

    added = seed_tickers(conn, build_seed())
    if added:
        print(f"Seeded {added} new tickers")

    if args.command == "init":
        print(f"Initialised ({len(get_ticker_rows(conn))} tickers, "
              f"{'hydrated' if hydrated else 'cold start'})")
        return 0
    if args.command == "daily":
        summary = run_daily(conn, YFinanceFetcher(), out_dir)
        print(f"daily: {summary['processed']} processed, {summary['changed']} changed, "
              f"{summary['drift_corrections']} drift, {len(summary['errors'])} errors")
        return 0 if summary["processed"] else 1
    if args.command == "prices":
        summary = run_prices(conn, YFinanceFetcher(), out_dir)
        print(f"prices: {summary['ok']} ok, {len(summary['errors'])} errors "
              f"({summary['status']})")
        return 0 if summary["ok"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""
Fetch 5 years of daily bars + dividend events for the sim-trader universe.
Writes results to history.json in the repo root.

Runs in GitHub Actions once a day after US market close. Output structure:
each ticker gets a self-contained object with its bars and dividends.

Honesty notes:
- Uses unadjusted close (auto_adjust=False). Dividends are tracked separately
  as cash events, the way they actually happen for a real holder.
- If a ticker can't be fetched, it goes in 'errors' rather than being faked.
- Partial data beats no data: workflow only fails if literally everything errored.
- We only store date + close for each bar. Spec is line charts only, no
  candlesticks; OHLC + volume would roughly triple the file size for no benefit.
- Days where the close is NaN (missing data) are skipped entirely. Browsers
  reject NaN as invalid JSON, and a "missing close" day is meaningless to the
  sim engine anyway — the trading-day calendar is derived from the union of
  bar dates across tickers, so a skipped row is the right semantics.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

# Same universe as fetch_prices.py. Keep these in sync; see SPEC for the
# longer-term plan to grow the universe.
UNIVERSE = [
    # UK large caps (FTSE 100 leaders)
    "LLOY.L", "SHEL.L", "AZN.L", "ULVR.L", "BARC.L", "GSK.L",
    "HSBA.L", "BP.L", "RIO.L", "VOD.L", "TSCO.L", "DGE.L",
    "NWG.L", "GLEN.L", "AAL.L",

    # US mega caps (mag 7 + a few popular extras)
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "NFLX", "AMD", "PLTR", "COIN",

    # Popular ETFs (UK-listed where possible for ISA realism)
    "VWRP.L",  # Vanguard FTSE All-World — our benchmark
    "VUSA.L",  # Vanguard S&P 500
    "VUKE.L",  # Vanguard FTSE 100
    "VAGP.L",  # Vanguard Global Aggregate Bond
    "IGLN.L",  # iShares Physical Gold
    "VFEM.L",  # Vanguard FTSE Emerging Markets
    "EQQQ.L",  # Invesco Nasdaq-100

    # Popular US-listed ETFs
    "SPY", "QQQ", "VOO", "VTI",

    # Indices and commodities (watch-only — not tradable in the sim)
    "^FTSE", "^GSPC", "^NDX", "^DJI", "^N225", "^FCHI",
    "GC=F",  # Gold futures (proxy for gold spot)
    "CL=F",  # Crude oil futures
]


def _safe_num(x):
    """Return float(x) unless x is NaN or unconvertible, in which case None.

    NaN-checking via the 'x != x' identity — NaN is the only float for which
    equality with itself fails. The consumer is JavaScript JSON.parse, which
    rejects NaN as invalid JSON. None becomes null on the wire.
    """
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def fetch_one(ticker: str) -> dict:
    """
    Fetch 5 years of daily bars and dividend events for one ticker.
    Returns a dict with bars and dividends.
    """
    t = yf.Ticker(ticker)

    # Unadjusted close — we want real prices, with dividends tracked as
    # separate events. auto_adjust=False is the key flag here.
    hist = t.history(period="5y", interval="1d", auto_adjust=False)
    if hist.empty:
        raise ValueError(f"no daily history returned for {ticker}")

    # Build the bars list. Each row becomes a small dict, keyed by date.
    # We only keep date + close — that's all the chart and engine actually
    # need. Open/high/low/volume removed to keep the file size manageable.
    # Skip any rows where close is NaN — invalid JSON if we emit it, and a
    # missing close is meaningless for the sim anyway.
    bars = []
    for idx, row in hist.iterrows():
        close = _safe_num(row["Close"])
        if close is None:
            continue
        date_str = idx.date().isoformat()
        bars.append({
            "date": date_str,
            "close": round(close, 4),
        })

    if not bars:
        raise ValueError(f"no usable bars for {ticker} (all closes were NaN)")

    # Dividends: yfinance returns a pandas Series indexed by date, with the
    # dividend amount per share. Empty series for tickers that don't pay.
    dividends = []
    try:
        divs = t.dividends
        for idx, amount in divs.items():
            div_amount = _safe_num(amount)
            if div_amount is None:
                continue
            div_date = idx.date().isoformat()
            # Only include dividends that fall within our bar window — the
            # 5y history is the relevant horizon for the sim.
            if bars[0]["date"] <= div_date <= bars[-1]["date"]:
                dividends.append({
                    "date": div_date,
                    "amount": round(div_amount, 6),
                })
    except Exception:
        # Some tickers (indices, futures) don't have dividends and yfinance
        # can be inconsistent about how it signals that. Empty list is fine.
        pass

    return {
        "ticker": ticker,
        "bar_count": len(bars),
        "dividend_count": len(dividends),
        "first_date": bars[0]["date"],
        "last_date": bars[-1]["date"],
        "bars": bars,
        "dividends": dividends,
    }


def main() -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    history = {}
    errors = {}

    for ticker in UNIVERSE:
        try:
            history[ticker] = fetch_one(ticker)
            h = history[ticker]
            print(
                f"OK {ticker}: {h['bar_count']} bars, "
                f"{h['dividend_count']} divs, {h['first_date']} -> {h['last_date']}"
            )
        except Exception as e:
            errors[ticker] = str(e)
            print(f"FAIL {ticker}: {e}", file=sys.stderr)

    output = {
        "fetched_at": fetched_at,
        "universe_size": len(UNIVERSE),
        "ok_count": len(history),
        "error_count": len(errors),
        "history": history,
        "errors": errors,
    }

    # Compact JSON — no whitespace. The HTML doesn't care about readability,
    # and this roughly halves the file size vs indent=2.
    # allow_nan=False makes json.dumps raise if any NaN sneaks through despite
    # our guards — better to fail the workflow than silently write invalid
    # JSON that breaks the browser app.
    Path("history.json").write_text(
        json.dumps(output, separators=(",", ":"), allow_nan=False)
    )
    print(f"\nWrote history.json: {len(history)} ok, {len(errors)} errors")

    if not history:
        print("ERROR: no tickers fetched successfully", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

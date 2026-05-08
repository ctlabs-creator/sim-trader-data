"""
Fetch end-of-day and recent intraday prices for the sim-trader universe.
Writes results to prices.json in the repo root.

Runs in GitHub Actions on a schedule. Designed to be honest about failures:
if a ticker can't be fetched, it goes into the 'errors' section of the output
rather than silently being dropped or faked.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf


# Starting universe: 50 tickers covering the most likely things friends will
# search for. Curated, not exhaustive. Easy to grow later by appending.
UNIVERSE = [
    # UK large caps (FTSE 100 leaders)
    "LLOY.L", "SHEL.L", "AZN.L", "ULVR.L", "BARC.L", "GSK.L",
    "HSBA.L", "BP.L", "RIO.L", "VOD.L", "TSCO.L", "DGE.L",
    "NWG.L", "GLEN.L", "AAL.L",
    # US mega caps (mag 7 + a few popular extras)
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "NFLX", "AMD", "PLTR", "COIN",
    # Popular ETFs (UK-listed where possible for ISA realism)
    "VWRP.L",   # Vanguard FTSE All-World — our benchmark
    "VUSA.L",   # Vanguard S&P 500
    "VUKE.L",   # Vanguard FTSE 100
    "VAGP.L",   # Vanguard Global Aggregate Bond
    "IGLN.L",   # iShares Physical Gold
    "VFEM.L",   # Vanguard FTSE Emerging Markets
    "EQQQ.L",   # Invesco Nasdaq-100
    # Popular US-listed ETFs (in case people search them by US ticker)
    "SPY", "QQQ", "VOO", "VTI",
    # Indices and commodities (watch-only — not tradable in the sim)
    "^FTSE", "^GSPC", "^NDX", "^DJI", "^N225", "^FCHI",
    "GC=F",    # Gold futures (proxy for gold spot)
    "CL=F",    # Crude oil futures
]


def fetch_one(ticker: str) -> dict:
    """
    Fetch what we need for one ticker. Returns a dict with the price info,
    or raises if the fetch failed in a way we couldn't paper over.
    """
    t = yf.Ticker(ticker)

    # Get the last few days of daily history. Use 5 days to ride over weekends
    # and one-off market closures.
    hist = t.history(period="5d", interval="1d", auto_adjust=False)
    if hist.empty:
        raise ValueError(f"no daily history returned for {ticker}")

    last_row = hist.iloc[-1]
    last_date = hist.index[-1].date().isoformat()

    # Try to get a more recent intraday price. yfinance's "fast_info" sometimes
    # has it; fall back gracefully if it doesn't.
    try:
        fast = t.fast_info
        last_price = float(fast["last_price"])
    except Exception:
        last_price = float(last_row["Close"])

    return {
        "ticker": ticker,
        "last_price": round(last_price, 4),
        "last_date": last_date,
        "open": round(float(last_row["Open"]), 4),
        "high": round(float(last_row["High"]), 4),
        "low": round(float(last_row["Low"]), 4),
        "close": round(float(last_row["Close"]), 4),
        "volume": int(last_row["Volume"]) if last_row["Volume"] == last_row["Volume"] else 0,
    }


def main() -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    prices = {}
    errors = {}

    for ticker in UNIVERSE:
        try:
            prices[ticker] = fetch_one(ticker)
            print(f"OK   {ticker}: {prices[ticker]['last_price']}")
        except Exception as e:
            errors[ticker] = str(e)
            print(f"FAIL {ticker}: {e}", file=sys.stderr)

    output = {
        "fetched_at": fetched_at,
        "universe_size": len(UNIVERSE),
        "ok_count": len(prices),
        "error_count": len(errors),
        "prices": prices,
        "errors": errors,
    }

    Path("prices.json").write_text(json.dumps(output, indent=2))
    print(f"\nWrote prices.json: {len(prices)} ok, {len(errors)} errors")

    # Don't fail the workflow just because some tickers errored — partial data
    # is more useful than no data. We'd only fail if literally everything broke.
    if not prices:
        print("ERROR: no tickers fetched successfully", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

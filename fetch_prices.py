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

# Starting universe: 45 + 1 (probe) tickers covering the most likely things
# friends will search for. Curated, not exhaustive. Easy to grow by appending.
UNIVERSE = [
    # UK large caps (FTSE 100 leaders)
    "LLOY.L", "SHEL.L", "AZN.L", "ULVR.L", "BARC.L", "GSK.L",
    "HSBA.L", "BP.L", "RIO.L", "VOD.L", "TSCO.L", "DGE.L",
    "NWG.L", "GLEN.L", "AAL.L",

    # US mega caps (mag 7 + a few popular extras)
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "NFLX", "AMD", "PLTR", "COIN",

    # Popular ETFs (UK-listed where possible for ISA realism)
    "VWRP.L",  # Vanguard FTSE All-World ETF (current benchmark fallback)
    "VUSA.L",  # Vanguard S&P 500
    "VUKE.L",  # Vanguard FTSE 100
    "VAGP.L",  # Vanguard Global Aggregate Bond
    "IGLN.L",  # iShares Physical Gold
    "VFEM.L",  # Vanguard FTSE Emerging Markets
    "EQQQ.L",  # Invesco Nasdaq-100

    # Popular US-listed ETFs (in case people search them by US ticker)
    "SPY", "QQQ", "VOO", "VTI",

    # Indices and commodities (watch-only — not tradable in the sim)
    "^FTSE", "^GSPC", "^NDX", "^DJI", "^N225", "^FCHI",
    "GC=F",  # Gold futures (proxy for gold spot)
    "CL=F",  # Crude oil futures

    # PROBE: FTSE All-World index (preferred benchmark per spec). If
    # yfinance returns clean data we'll switch BENCHMARK to this; if not,
    # it'll land in 'errors' and we stick with VWRP.L.
    "^FTAW",
]

# Tickers whose prices yfinance returns in pence (GBX) rather than pounds.
# Everything in this set is divided by 100 before being written to JSON, so
# downstream consumers see a single canonical unit (GBP) for every ticker
# tagged 'GBP' in the engine. Without this, portfolio totals that mix UK
# stocks with UK ETFs come out as nonsense — pence and pounds added together.
#
# yfinance is inconsistent across LSE listings:
#   - Individual stocks (.L) → pence
#   - Most Vanguard ETFs (VWRP, VUSA, VUKE, VAGP, VFEM, IGLN) → pounds
#   - EQQQ.L → pence (the odd one out)
# This list is hand-curated based on inspection of the live yfinance output
# (LLOY 99.03 ≈ 99p, SHEL 3103 ≈ £31, EQQQ 52153 ≈ £521).
PENCE_TICKERS = {
    "LLOY.L", "SHEL.L", "AZN.L", "ULVR.L", "BARC.L", "GSK.L",
    "HSBA.L", "BP.L", "RIO.L", "VOD.L", "TSCO.L", "DGE.L",
    "NWG.L", "GLEN.L", "AAL.L",
    "EQQQ.L",
}


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


def _gbp_round(ticker, x, ndigits=4):
    """Convert pence to pounds for pence-denominated tickers, then round.

    Returns None if x is NaN/missing. For non-pence tickers, just rounds.
    Used for every currency-bearing field (prices, opens, etc.) so the
    consumer sees a single canonical unit per currency tag.
    """
    s = _safe_num(x)
    if s is None:
        return None
    if ticker in PENCE_TICKERS:
        s = s / 100
    return round(s, ndigits)


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
    last_price_raw = None
    try:
        fast = t.fast_info
        last_price_raw = _safe_num(fast["last_price"])
    except Exception:
        pass

    # Fall back to the daily close if fast_info didn't yield a usable number.
    if last_price_raw is None:
        last_price_raw = _safe_num(last_row["Close"])

    # If we still don't have a price, refuse — fills must have a real number
    # to honour. Better to put this ticker in 'errors' than emit NaN that
    # breaks the JSON or, worse, fills at NaN.
    if last_price_raw is None:
        raise ValueError(f"no usable last_price for {ticker}")

    # Apply pence-to-pounds normalisation if applicable.
    if ticker in PENCE_TICKERS:
        last_price = last_price_raw / 100
    else:
        last_price = last_price_raw

    return {
        "ticker": ticker,
        "last_price": round(last_price, 4),
        "last_date": last_date,
        "open":  _gbp_round(ticker, last_row["Open"]),
        "high":  _gbp_round(ticker, last_row["High"]),
        "low":   _gbp_round(ticker, last_row["Low"]),
        "close": _gbp_round(ticker, last_row["Close"]),
        "volume": int(last_row["Volume"]) if last_row["Volume"] == last_row["Volume"] else 0,
    }


def main() -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    prices = {}
    errors = {}

    for ticker in UNIVERSE:
        try:
            prices[ticker] = fetch_one(ticker)
            print(f"OK {ticker}: {prices[ticker]['last_price']}")
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

    # allow_nan=False makes json.dumps raise if any NaN sneaks through despite
    # our guards — better to fail the workflow than silently write invalid
    # JSON that breaks the browser app.
    Path("prices.json").write_text(json.dumps(output, indent=2, allow_nan=False))
    print(f"\nWrote prices.json: {len(prices)} ok, {len(errors)} errors")

    # Don't fail the workflow just because some tickers errored — partial data
    # is more useful than no data. We'd only fail if literally everything broke.
    if not prices:
        print("ERROR: no tickers fetched successfully", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Validate a directory of produced sim-trader artifacts and print a readable
report. Used by the non-destructive validation workflow so the Actions log
itself tells you whether the producer output is sane -- no need to download
and eyeball JSON on an iPad.

Usage: python validate_artifacts.py <dir>

Exit code:
  0  artifacts are structurally sound (yfinance fetch errors are reported as
     warnings, not failures -- partial data is acceptable).
  1  a hard problem was found (missing/zero/corrupt data, schema mismatch).

Stdlib only -- runs anywhere Python does, no install step.
"""

import json
import math
import sys
from pathlib import Path

SCHEMA_VERSION = 1


class Report:
    def __init__(self):
        self.fails: list[str] = []
        self.warns: list[str] = []
        self.lines: list[str] = []

    def fail(self, msg): self.fails.append(msg); self.lines.append(f"  FAIL  {msg}")
    def warn(self, msg): self.warns.append(msg); self.lines.append(f"  warn  {msg}")
    def info(self, msg): self.lines.append(f"        {msg}")
    def head(self, msg): self.lines.append(f"\n{msg}")


def _is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _load(path: Path, r: Report):
    """json.load raises on NaN/Infinity, so a clean load already proves no
    non-finite literals slipped into the file."""
    try:
        return json.loads(path.read_text())
    except Exception as e:
        r.fail(f"{path.name}: could not parse ({e})")
        return None


def validate(dir_path: Path) -> int:
    r = Report()
    r.head(f"Validating artifacts in {dir_path}")

    uni_p = dir_path / "universe.json"
    prices_p = dir_path / "prices.json"
    cal_p = dir_path / "calendar.json"
    for p in (uni_p, prices_p, cal_p):
        if not p.exists():
            r.fail(f"missing {p.name}")
    if r.fails:
        return _finish(r)

    uni = _load(uni_p, r)
    prices = _load(prices_p, r)
    cal = _load(cal_p, r)
    if uni is None or prices is None or cal is None:
        return _finish(r)

    # ---- universe.json + per-ticker history -------------------------------
    r.head("universe.json")
    if uni.get("schema_version") != SCHEMA_VERSION:
        r.fail(f"schema_version is {uni.get('schema_version')}, expected {SCHEMA_VERSION}")
    tickers = uni.get("tickers", {})
    r.info(f"{len(tickers)} tickers, calendar_version={uni.get('calendar_version')}, "
           f"generated_at={uni.get('generated_at')}")
    if not tickers:
        r.fail("universe has no tickers")
        return _finish(r)

    pence_spotcheck: list[tuple[str, float]] = []
    total_bars = 0
    for ticker, e in sorted(tickers.items()):
        hp = dir_path / e.get("file", "")
        if not e.get("file") or not hp.exists():
            r.fail(f"{ticker}: history file '{e.get('file')}' missing")
            continue
        h = _load(hp, r)
        if h is None:
            continue
        if h.get("version") != e.get("version"):
            r.warn(f"{ticker}: version mismatch index={e.get('version')} file={h.get('version')}")
        bars = h.get("bars", [])
        if not bars:
            r.fail(f"{ticker}: zero bars")
            continue
        # close-only shape + finiteness + ascending dates
        prev = ""
        bad_shape = False
        for b in bars:
            if set(b.keys()) != {"d", "c"}:
                bad_shape = True
            if not _is_finite_number(b.get("c")):
                r.fail(f"{ticker}: non-finite close at {b.get('d')}")
            if b.get("d", "") <= prev:
                r.fail(f"{ticker}: dates not strictly ascending near {b.get('d')}")
            prev = b.get("d", "")
        if bad_shape:
            r.fail(f"{ticker}: bars are not close-only {{d,c}}")
        for d in h.get("dividends", []):
            if not _is_finite_number(d.get("amount")):
                r.fail(f"{ticker}: non-finite dividend at {d.get('ex_date')}")
        total_bars += len(bars)
        last_c = bars[-1]["c"]
        if e.get("pence_denominated"):
            pence_spotcheck.append((ticker, last_c))
        # cross-check last_date in index vs file
        if e.get("last_date") != bars[-1]["d"]:
            r.warn(f"{ticker}: index last_date={e.get('last_date')} != last bar {bars[-1]['d']}")
    r.info(f"{total_bars} total bars across {len(tickers)} tickers")

    # Pence spot-check: informational. Pence->pounds is right when individual
    # LSE names land in single/low-double-digit pounds (e.g. Lloyds ~£0.99).
    # We can't auto-decide (AZN legitimately ~£100, EQQQ ~£500), so we PRINT
    # them for a human eyeball and only hard-flag absurd values.
    if pence_spotcheck:
        r.head("pence-normalised tickers (eyeball these look like POUNDS, not pence)")
        for t, c in sorted(pence_spotcheck):
            flag = "  <-- SUSPICIOUS (looks un-normalised)" if c > 5000 else ""
            if c > 5000:
                r.fail(f"{t}: last close £{c:,.2f} -- likely still in pence{flag}")
            else:
                r.info(f"{t}: £{c:,.4f}")

    # ---- prices.json ------------------------------------------------------
    r.head("prices.json")
    if prices.get("schema_version") != SCHEMA_VERSION:
        r.fail(f"schema_version is {prices.get('schema_version')}")
    r.info(f"status={prices.get('status')}, fetched_at={prices.get('fetched_at')}")
    pmap = prices.get("prices", {})
    perr = prices.get("errors", {})
    r.info(f"{len(pmap)} priced, {len(perr)} errors")
    for t, snap in pmap.items():
        if not _is_finite_number(snap.get("last_price")):
            r.fail(f"prices[{t}]: non-finite last_price")
    if perr:
        r.head("prices.json fetch errors (warnings -- partial data is acceptable)")
        for t, msg in sorted(perr.items()):
            r.warn(f"{t}: {msg}")

    # ---- calendar.json ----------------------------------------------------
    r.head("calendar.json")
    if cal.get("schema_version") != SCHEMA_VERSION:
        r.fail(f"schema_version is {cal.get('schema_version')}")
    exchanges = cal.get("exchanges", {})
    r.info(f"version={cal.get('version')}, {len(exchanges)} exchanges")
    for exch, blob in sorted(exchanges.items()):
        days = blob.get("days", [])
        if not days:
            r.warn(f"{exch}: no trading days")
            continue
        if list(days) != sorted(days):
            r.fail(f"{exch}: trading days not sorted")
        if len(set(days)) != len(days):
            r.fail(f"{exch}: duplicate trading days")
        r.info(f"{exch}: {len(days)} days, {days[0]} -> {days[-1]}")

    # ---- cross-checks -----------------------------------------------------
    r.head("cross-checks")
    indexed = set(tickers)
    priced = set(pmap) | set(perr)
    missing_price = indexed - priced
    if missing_price:
        r.warn(f"{len(missing_price)} indexed tickers absent from prices.json "
               f"(consumer falls back to last bar): {sorted(missing_price)[:8]}"
               f"{' ...' if len(missing_price) > 8 else ''}")

    return _finish(r)


def _finish(r: Report) -> int:
    print("\n".join(r.lines))
    print("\n" + "=" * 60)
    if r.fails:
        print(f"RESULT: FAIL  ({len(r.fails)} hard problems, {len(r.warns)} warnings)")
        return 1
    print(f"RESULT: PASS  ({len(r.warns)} warnings)")
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print("usage: python validate_artifacts.py <dir>", file=sys.stderr)
        return 2
    return validate(Path(argv[0]))


if __name__ == "__main__":
    sys.exit(main())

"""Read-only V40 executed-entry API support audit against CURRENT MEXC country contract data.

Default: $2,000 V40 HYBRID, S100/CAP5 (~7.24x historical exported result).
No private GET, no API keys, no POST, no orders, no trading config changes.
Current public API eligibility is NOT proof of historical eligibility or an
account-specific order permit. Report only aggregate counts, never symbols.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass
import io
import json
from pathlib import Path
import re
from zipfile import ZipFile

from app.t100_live_api_eligibility_census import _parse_data, _digest
from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)

COUNTRY = "/api/v1/contract/detail/country"
_SYMBOL = re.compile(r"[A-Z0-9]+_USDT\Z")
MODES = ("HYBRID", "REFERENCE", "KNOWN_ONLY")
_CAPITALS = (2000, 10000, 100000)
_COSTS = (100, 200)


@dataclass(frozen=True)
class HistoricalEntries:
    capital: int
    cost_bps: int
    mode: str
    scenario: str
    final_equity: float
    symbols: tuple[str, ...]


def scenario_label(cost_bps: int, mode: str) -> str:
    if cost_bps not in _COSTS or mode not in MODES:
        raise ValueError("UNSUPPORTED_SCENARIO")
    base = f"V32_VOL30_SKIP_H4_S{cost_bps}_CAP5"
    return base if mode == "REFERENCE" else f"{base}_V39_{mode}"


def _csv_from_zip(z: ZipFile, path: str) -> list[dict[str, str]]:
    info = z.getinfo(path)
    if info.file_size > 8_000_000:
        raise ValueError("CSV_TOO_LARGE")
    return list(csv.DictReader(io.StringIO(z.read(path).decode("utf-8-sig"))))


def read_entries(path: Path, capital: int = 2000, cost_bps: int = 100,
                 mode: str = "HYBRID") -> HistoricalEntries:
    if capital not in _CAPITALS:
        raise ValueError("UNSUPPORTED_CAPITAL")
    scenario = scenario_label(cost_bps, mode)
    with ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        if (manifest.get("type") != "READ_ONLY_SOURCE_PARITY_SAMPLE"
                or manifest.get("candidate_count") != 146):
            raise ValueError("UNEXPECTED_V40_ARCHIVE")
        rows = _csv_from_zip(z, f"capital_{capital}/entries.csv")
        metrics = _csv_from_zip(z, f"capital_{capital}/metrics.csv")
    selected = [r for r in rows if r.get("scenario") == scenario]
    metric = [r for r in metrics if r.get("scenario") == scenario]
    if not selected or len(metric) != 1:
        raise ValueError("SCENARIO_ENTRIES_OR_METRICS_MISSING")
    m = metric[0]
    if len(selected) != int(m["entered"]):
        raise ValueError("BACKTEST_ENTRY_COUNT_MISMATCH")
    symbols = tuple(r["symbol"] for r in selected)
    if any(not _SYMBOL.fullmatch(s) for s in symbols):
        raise ValueError("INVALID_ENTRY_SYMBOL")
    equity = float(m["final_equity"])
    if not (0 < equity < 1e15):
        raise ValueError("INVALID_FINAL_EQUITY")
    return HistoricalEntries(capital, cost_bps, mode, scenario, equity, symbols)


def current_public_status(row: object) -> str:
    """Current public eligibility only, never private API-key permissions."""
    if row is None:
        return "UNLISTED_NOW"
    if type(row) is not dict:
        return "API_FLAG_UNKNOWN"
    api_allowed = row.get("apiAllowed")
    if api_allowed is False:
        return "API_DENIED_NOW"
    if api_allowed is not True:
        return "API_FLAG_UNKNOWN"
    if (type(row.get("state")) is not int or row["state"] != 0
            or type(row.get("futureType")) is not int
            or row["futureType"] != 1
            or type(row.get("positionOpenType")) is not int
            or row["positionOpenType"] not in (2, 3)
            or row.get("quoteCoin") != "USDT"
            or row.get("settleCoin") != "USDT"):
        return "API_ALLOWED_OTHER_RESTRICTION"
    return "CURRENT_PUBLIC_ELIGIBLE"


def compare_current_support(entries: HistoricalEntries, first: object,
                            second: object) -> dict[str, object]:
    """Pure calculation; fail closed if either GET is incomplete or drifting."""
    rows_first = tuple(sorted(_parse_data(first), key=lambda r: r["symbol"]))
    rows_second = tuple(sorted(_parse_data(second), key=lambda r: r["symbol"]))
    if _digest(rows_first) != _digest(rows_second):
        raise ValueError("CURRENT_PUBLIC_METADATA_DRIFT")
    contract_by_symbol = {r["symbol"]: r for r in rows_second}

    def classify(symbol: str) -> str:
        return current_public_status(contract_by_symbol.get(symbol))

    unique = set(entries.symbols)
    status_by_symbol = {sym: classify(sym) for sym in unique}
    categories = (
        "CURRENT_PUBLIC_ELIGIBLE", "API_DENIED_NOW", "API_FLAG_UNKNOWN",
        "API_ALLOWED_OTHER_RESTRICTION", "UNLISTED_NOW",
    )
    unique_counts = {c: sum(v == c for v in status_by_symbol.values())
                     for c in categories}
    entry_counts = {c: sum(status_by_symbol[s] == c for s in entries.symbols)
                    for c in categories}
    eligible_unique = unique_counts["CURRENT_PUBLIC_ELIGIBLE"]
    eligible_entries = entry_counts["CURRENT_PUBLIC_ELIGIBLE"]
    allowed_flag_unique = eligible_unique + unique_counts["API_ALLOWED_OTHER_RESTRICTION"]
    allowed_flag_entries = eligible_entries + entry_counts["API_ALLOWED_OTHER_RESTRICTION"]
    return {
        "status": "CURRENT_BACKTEST_CONTRACT_API_COVERAGE_NONAUTHORIZING",
        "scenario": entries.scenario,
        "initial_capital_usdt": entries.capital,
        "saved_final_equity_usdt": round(entries.final_equity, 2),
        "saved_equity_multiple": round(entries.final_equity / entries.capital, 4),
        "backtest_entry_rows": len(entries.symbols),
        "unique_traded_contracts": len(unique),
        "api_allowed_flag_unique": allowed_flag_unique,
        "api_allowed_flag_unique_pct": round(100 * allowed_flag_unique / len(unique), 2),
        "api_allowed_flag_entries": allowed_flag_entries,
        "api_allowed_flag_entries_pct": round(100 * allowed_flag_entries / len(entries.symbols), 2),
        "currently_eligible_unique": eligible_unique,
        "currently_eligible_unique_pct": round(100 * eligible_unique / len(unique), 2),
        "currently_eligible_entries": eligible_entries,
        "currently_eligible_entries_pct": round(100 * eligible_entries / len(entries.symbols), 2),
        "unique_breakdown": unique_counts,
        "entry_breakdown": entry_counts,
        "returned_current_contract_rows": len(rows_second),
        "country_public_two_get_consistent": True,
        "current_public_metadata_only": True,
        "historical_api_permissions_certified": False,
        "account_specific_api_order_access_certified": False,
        "backtest_return_after_filter_replayed": False,
        "live_authorization": False,
        "entry_allowed": False,
        "mexc_orders_sent": False,
        "read_only": True,
    }


async def fetch_public_country_pair() -> tuple[object, object]:
    import httpx
    async with httpx.AsyncClient(
        base_url="https://api.mexc.com",
        timeout=httpx.Timeout(20.0),
        follow_redirects=False,
        headers={"User-Agent": "T100-V40-backtest-API-coverage-readonly/1.0"},
    ) as client:
        snapshots = []
        for _ in range(2):
            response = await client.get(COUNTRY, headers={"Cache-Control": "no-cache"})
            response.raise_for_status()
            snapshots.append(response.json())
        return snapshots[0], snapshots[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path,
                        help="Local v40_parity_for_live.zip, never uploaded")
    parser.add_argument("--capital", type=int, choices=_CAPITALS, default=2000)
    parser.add_argument("--cost-bps", type=int, choices=_COSTS, default=100)
    parser.add_argument("--mode", choices=MODES, default="HYBRID")
    args = parser.parse_args()
    import os
    try:
        require_readonly_controls(os.environ)
    except CrossRiskDiagnosticHalted:
        raise SystemExit("PAPER_MODE_KILL_SWITCH_REQUIRED") from None
    try:
        entries = read_entries(args.archive, args.capital, args.cost_bps, args.mode)
        a, b = asyncio.run(fetch_public_country_pair())
        report = compare_current_support(entries, a, b)
    except (OSError, ValueError, KeyError, IndexError, UnicodeError,
            json.JSONDecodeError, asyncio.TimeoutError) as exc:
        # Fixed categories only; no archive rows, HTTP payloads, URLs or symbols.
        safe = {"UNSUPPORTED_SCENARIO", "UNSUPPORTED_CAPITAL",
                "CSV_TOO_LARGE", "UNEXPECTED_V40_ARCHIVE",
                "SCENARIO_ENTRIES_OR_METRICS_MISSING", "BACKTEST_ENTRY_COUNT_MISMATCH",
                "INVALID_ENTRY_SYMBOL", "INVALID_FINAL_EQUITY",
                "CURRENT_PUBLIC_METADATA_DRIFT"}
        code = str(exc) if str(exc) in safe else "INPUT_OR_PUBLIC_READ_UNVERIFIED"
        print(json.dumps({"status": "BACKTEST_API_COVERAGE_UNVERIFIED",
                          "failure_code": code,
                          "live_authorization": False, "mexc_orders_sent": False},
                         indent=2, sort_keys=True))
        raise SystemExit(2) from None
    except Exception:
        print(json.dumps({"status": "BACKTEST_API_COVERAGE_UNVERIFIED",
                          "failure_code": "INPUT_OR_PUBLIC_READ_UNVERIFIED",
                          "live_authorization": False, "mexc_orders_sent": False},
                         indent=2, sort_keys=True))
        raise SystemExit(2) from None
    print(json.dumps(report, indent=2, sort_keys=True))
    print("PUBLIC GET ONLY. NO KEYS, NO ORDERS. HISTORICAL API ELIGIBILITY UNKNOWN.")


if __name__ == "__main__":
    main()

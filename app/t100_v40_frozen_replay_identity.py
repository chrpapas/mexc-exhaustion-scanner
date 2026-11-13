"""Authoritative immutable V40 7.238406x backtest identity, independently auditable.

This is NOT a trading release or proof of live performance. The SHA is for the
USER-SUPPLIED original V40 reference ZIP; never upload raw trades to CI/logs.

The HYBRID strategy had: 108 entries / 105 exits / three positions marked open
at the 2026 sample end, 2.0901x maximum actual gross exposure/equity,
23 liquidity-constrained partial exits, 16 delayed exits, and 10 entry
contracts with historical specification passthrough. Those cannot be treated
as exchange-verified native stop fills or as a 1x-capital strategy.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal as D
from hashlib import sha256
import csv
import io
import json
from pathlib import Path
import sys
from zipfile import ZipFile, BadZipFile

from app.t100_200_strategy import STOP_PCT,TRAIL_ACTIVATION_PCT,TRAIL_GAP_PCT
from app.t100_v40_admission import MAX_POSITIONS,VOL_SKIP_THRESHOLD

ZIP_SHA256="2918f4589e8d564ca55b6f24aa2e9a868b5d214653ad7d92acaf1729fd1e4cb5"
SCENARIO="V32_VOL30_SKIP_H4_S100_CAP5_V39_HYBRID"
EXPECTED_METRICS={
    "initial":"2000.0", "final_equity":"14476.81238416",
    "multiple":"7.238406","slots":"4","stop":"75.0",
    "activation":"14.0","gap":"0.5",
    "low_gross":"100.0","high_gross":"250.0",
    "candidate_count":"146","entered":"108","closed":"105",
    "open_positions":"3","nominal_leverage":"5.0",
    "entry_shock":"0.2","equity_reserve":"0.05",
    "peak_gross_to_equity":"2.0901",
    "high_envelope_dd_pct":"-54.8664",
    "close_dd_pct":"-50.0955",
    "high_im_violation_bars":"0",
    "high_shock_im_violation_bars":"0",
    "high_mmr_5pct_violation_bars":"0",
    "partial_exits":"23","completed_delayed_exits":"16",
    "max_exit_delay_bars":"3",
    "missing_spec_entries_passthrough":"10",
    "missing_spec_exit_fills_passthrough":"13",
    "unverified_symbol_count":"9",
    "child_orders_extra":"45",
    "pending_mandatory_exits":"0",
    "unknown_funding_entries":"0",
    "chronology_complete":"True","research_data_complete":"True",
    "complete_for_ranking":"True",
    "historical_venue_executable_certified":"False",
    "cost_bps":"100.0",
    "lot_mode":"HYBRID",
    "exit_fill":"open_worst",
    "gate_profile":"VOL30_SKIP",
}


class V40FrozenArchiveHalted(ValueError):
    """No filenames, symbols, exchange details or SQL values in error."""


@dataclass(frozen=True)
class V40FrozenIdentity:
    status:str="V40_ORIGINAL_FROZEN_ARCHIVE_MATCH_NONAUTHORIZING"
    archive_sha256:str=ZIP_SHA256
    scenario:str=SCENARIO
    final_equity_usdt:D=D("14476.81238416")
    capital_multiple:D=D("7.238406")
    initial_usdt:D=D("2000")
    actual_peak_gross_to_equity:D=D("2.0901")
    modeled_margin_leverage_proxy:int=5
    simultaneous_positions:int=4
    open_at_horizon:int=3
    entry_count:int=108
    closed_count:int=105
    exit_partial_fills:int=23
    delayed_exits:int=16
    max_delay_bars:int=3
    missing_spec_passthrough_entries:int=10
    sl75_pct:D=D("75")
    trail_arm_pct:D=D("14")
    trail_gap_percentage_points:D=D("0.5")
    exchange_margin_and_liquidation_certified:bool=False
    historical_funding_exit_and_venue_replayed_independently:bool=False
    full_live_execution_authorized:bool=False
    exchange_posts:int=0

    def public_summary(self)->dict:
        a=asdict(self)
        for k,v in a.items():
            if type(v) is D:a[k]=str(v)
        return a


def _csv(z:ZipFile,name:str)->list[dict[str,str]]:
    info=z.getinfo(name)
    if info.file_size>5_000_000 or info.file_size<=0:
        raise V40FrozenArchiveHalted("V40_FROZEN_ARCHIVE_MEMBER_SIZE_HALT")
    content=z.read(info).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(content)))


def verify_original_v40_archive(path:Path)->V40FrozenIdentity:
    """Checks exact immutable original file and saved V40 HYBRID scenario.

    Full portfolio and exchange liquidation are NOT certified by this
    export, even when the archive matches its signed SHA-256 fingerprint.
    """
    if not isinstance(path, Path) or not path.is_file() or path.stat().st_size>10_000_000:
        raise V40FrozenArchiveHalted("V40_FROZEN_ARCHIVE_UNAVAILABLE")
    if sha256(path.read_bytes()).hexdigest()!=ZIP_SHA256:
        raise V40FrozenArchiveHalted("V40_FROZEN_ARCHIVE_HASH_MISMATCH")
    if not (
        MAX_POSITIONS==4 and VOL_SKIP_THRESHOLD==30.
        and D(str(STOP_PCT))==D("75")
        and D(str(TRAIL_ACTIVATION_PCT))==D("14")
        and D(str(TRAIL_GAP_PCT))==D("0.5")
    ):
        raise V40FrozenArchiveHalted("V40_FROZEN_CURRENT_POLICY_DRIFT")
    try:
        with ZipFile(path) as z:
            expected_files={
                "manifest.json","candidates.csv","prior49_bars.csv",
                "capital_2000/metrics.csv","capital_2000/entries.csv",
                "capital_2000/gate_decisions.csv",
                "capital_10000/metrics.csv","capital_10000/entries.csv",
                "capital_10000/gate_decisions.csv",
                "capital_100000/metrics.csv","capital_100000/entries.csv",
                "capital_100000/gate_decisions.csv",
            }
            if set(z.namelist())!=expected_files:
                raise V40FrozenArchiveHalted("V40_FROZEN_ARCHIVE_MEMBERS_MISMATCH")
            manifest=json.loads(z.read("manifest.json"))
            if (
                manifest.get("candidate_count")!=146
                or manifest.get("preceding_candles_exported")!=7154
                or manifest.get("no_exchange_keys_or_orders") is not True
            ):
                raise V40FrozenArchiveHalted("V40_FROZEN_ARCHIVE_MANIFEST_DRIFT")
            metrics=[m for m in _csv(z,"capital_2000/metrics.csv")
                     if m.get("scenario")==SCENARIO]
            entries=[e for e in _csv(z,"capital_2000/entries.csv")
                     if e.get("scenario")==SCENARIO]
            gates=[g for g in _csv(z,"capital_2000/gate_decisions.csv")
                   if g.get("scenario")==SCENARIO]
    except (BadZipFile,KeyError,UnicodeError,ValueError,TypeError,OverflowError) as err:
        if type(err) is V40FrozenArchiveHalted:raise
        raise V40FrozenArchiveHalted("V40_FROZEN_ARCHIVE_CONTENT_INVALID") from None
    if len(metrics)!=1 or len(entries)!=108 or len(gates)!=132:
        raise V40FrozenArchiveHalted("V40_FROZEN_ARCHIVE_COUNTS_MISMATCH")
    row=metrics[0]
    for col,ref in EXPECTED_METRICS.items():
        val=row.get(col)
        try:
            same=(D(val)==D(ref)) if (
                val is not None and ref not in ("True","False","HYBRID",
                                               "VOL30_SKIP","open_worst")
            ) else val==ref
        except Exception:
            same=False
        if not same:
            raise V40FrozenArchiveHalted("V40_FROZEN_ARCHIVE_METRIC_DRIFT")
    return V40FrozenIdentity()


def _main()->int:
    if len(sys.argv)!=2:
        print(json.dumps({"status":"V40_FROZEN_ARCHIVE_ARGUMENT_REQUIRED"}))
        return 2
    try:
        out=verify_original_v40_archive(Path(sys.argv[1]))
    except V40FrozenArchiveHalted as exc:
        print(json.dumps({"status":str(exc),"live_authorization":False}))
        return 2
    print(json.dumps(out.public_summary(),sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(_main())

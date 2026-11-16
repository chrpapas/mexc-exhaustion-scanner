"""V40 offline independent exit-quantity hypothesis replay (NO orders).

Compute candle stop triggers and greedy 5%-prior-30m exit capacity without
looking at exported exits. Compare modeled events to historical exits only
after generation. Integer-lot modeling uses CURRENT public spec snapshots:
these cannot establish actual historical rules or executable market fills.
No exchange/API, mutable DB, credentials, network, or live permissions.

A mismatched event is evidence to investigate, not a reason to loosen checks.
S200 may include a prescribed portfolio risk trim; trimmed positions are
explicitly EXCLUDED, never silently treated as independently generated.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal as D, ROUND_FLOOR, InvalidOperation
from pathlib import Path

COSTS=(100,200)
END_UTC="2026-09-25T08:30:00+00:00"
BAR_SECONDS=1800
TOL=D("0.001")


class ExitQuantityAuditHalted(ValueError):
    """Never include exchange credentials or source paths in messages."""


def number(s):
    try:
        v=D(str(s))
        if not v.is_finite() or v<=0:
            raise ValueError()
        return v
    except (InvalidOperation, TypeError, ValueError):
        raise ExitQuantityAuditHalted("NONPOSITIVE_OR_INVALID_NUMBER") from None


def time_of(s):
    try:
        obj=datetime.fromisoformat(s.replace("Z","+00:00"))
        if obj.tzinfo is None or obj.utcoffset() is None:
            raise ValueError()
        t=int(obj.timestamp())
        if float(obj.timestamp())!=t or t%BAR_SECONDS:
            raise ValueError()
        return t
    except (ValueError, TypeError, OverflowError):
        raise ExitQuantityAuditHalted("INVALID_OR_UNALIGNED_TIMESTAMP") from None


def open_offline_db(path,immutable=False):
    path=Path(path).expanduser().resolve()
    if not path.is_file():
        raise ExitQuantityAuditHalted("CANDLE_DATABASE_MISSING")
    if immutable and any(Path(str(path)+suffix).exists()
                         for suffix in ("-wal","-journal")):
        raise ExitQuantityAuditHalted("CANDLE_DB_HAS_PENDING_JOURNAL")
    db=sqlite3.connect(path.as_uri()+("?mode=ro&immutable=1" if immutable
                                     else "?mode=ro"),uri=True)
    db.execute("PRAGMA query_only=ON")
    return db


@dataclass(frozen=True)
class Lot:
    size: D
    step: D
    minimum: D


def lot_from_metadata(meta):
    if meta is None:
        return None
    size,step,minimum=(number(meta[k]) for k in ("contractSize","volUnit","minVol"))
    if minimum%step:
        raise ExitQuantityAuditHalted("LOT_MINIMUM_NOT_MULTIPLE_OF_STEP")
    return Lot(size,step,minimum)


def quote_capacity(prior_amount):
    return number(prior_amount)*D(".05")


def allocate(remaining, entry, price, quota, *, lot=None, remaining_units=None):
    """Greedy historical reference model: independent fill amount.

    remaining is ORIGINAL entry-price notional; quota is quote-USDT
    capacity; price is modeled stop or delayed open. With lot, use verified
    entry contract units (not any exported exit contract units).
    """
    remaining,entry,price,quota=(number(x) for x in
                                 (remaining,entry,price,quota))
    if lot is None:
        basis=min(remaining,quota*entry/price)
        quote=basis*price/entry
        return basis,quote,None
    if remaining_units is None or remaining_units<=0:
        raise ExitQuantityAuditHalted("MISSING_ENTRY_LOT_INVENTORY")
    available=(quota/(price*lot.size*lot.step)).to_integral_value(
        rounding=ROUND_FLOOR)*lot.step
    units=min(remaining_units,available)
    if units<lot.minimum:
        return D(0),D(0),remaining_units
    residue=remaining_units-units
    if 0<residue<lot.minimum:
        # No unclosable dust. Leave one minimum tradeable lot if a full
        # close cannot be fitted into this bar's 5% turnover quota.
        units=max(D(0),remaining_units-lot.minimum)
        units=(units/lot.step).to_integral_value(rounding=ROUND_FLOOR)*lot.step
    if units<lot.minimum:
        return D(0),D(0),remaining_units
    quote=units*lot.size*price
    basis=units*lot.size*entry
    if basis>remaining+TOL or quote>quota+TOL:
        raise ExitQuantityAuditHalted("MODELED_LOT_EXCEEDS_INVENTORY_OR_QUOTA")
    return basis,quote,remaining_units-units


def generate_position(*, entry_ts,entry_price,entry_notional,entry_quote,
                      candle_rows,lot=None,entry_units=None):
    """Generate exits without any access to exported exit records.

    candle_rows is a contiguous sequence (open_ts,open,high,low,close,
    previous_30m_turnover). The previous bar is strictly causal.
    """
    if not candle_rows or candle_rows[0][0]!=entry_ts:
        raise ExitQuantityAuditHalted("ENTRY_CANDLE_MISSING")
    ep=number(entry_price)
    original=number(entry_notional)
    entry_quote=number(entry_quote)
    remaining=original
    units=entry_units
    if lot is not None:
        if units is None or units%lot.step or units<lot.minimum:
            raise ExitQuantityAuditHalted("ENTRY_LOT_INVALID")
        if abs(units*lot.size*ep-original)>TOL:
            raise ExitQuantityAuditHalted("ENTRY_LOT_NOTIONAL_DRIFT")
    armed=False
    best=D(0)
    pending=False
    events=[]
    previous=None
    triggers=0
    for t,op,hi,lo,cl,prior in candle_rows:
        if previous is not None and t!=previous+BAR_SECONDS:
            raise ExitQuantityAuditHalted("HISTORICAL_CANDLE_GAP")
        previous=t
        op,hi,lo,cl=(number(x) for x in (op,hi,lo,cl))
        if not (lo<=min(op,cl)<=max(op,cl)<=hi):
            raise ExitQuantityAuditHalted("INVALID_CANDLE_OHLC")
        quota=quote_capacity(prior)
        if t==entry_ts:
            # No quota double-spend: entry already consumed the same
            # contract's prior-30m 5% budget before a same-bar stop.
            if entry_quote>quota+TOL:
                raise ExitQuantityAuditHalted("ENTRY_EXCEEDS_PRIOR30_QUOTA")
            quota=max(D(0),quota-entry_quote)
        if pending:
            basis,quote,newunits=(
                allocate(remaining,ep,op,quota,lot=lot,remaining_units=units)
                if quota>0 else (D(0),D(0),units)
            )
            if basis>0:
                remaining-=basis
                if lot is not None:
                    units=newunits
                    if units==0:
                        remaining=D(0)
                if abs(remaining)<=TOL:
                    remaining=D(0)
                events.append({
                    "time":t,"kind":"delayed_exit",
                    "price":op,"notional":basis,
                    "reason":("delayed_forced_exit_partial" if remaining>0
                              else "delayed_forced_exit"),
                    "quota":quota,"quote":quote,
                })
            pending=remaining>0
            if not pending:
                break
            continue

        stop=ep*(D(1)-max(D(0),best-D(".5"))/D(100)) if armed else ep*D("1.75")
        if hi>=stop:
            triggers+=1
            price=max(op,stop)
            basis,quote,newunits=(
                allocate(remaining,ep,price,quota,lot=lot,remaining_units=units)
                if quota>0 else (D(0),D(0),units)
            )
            if basis>0:
                remaining-=basis
                if lot is not None:
                    units=newunits
                    if units==0:
                        remaining=D(0)
                if abs(remaining)<=TOL:
                    remaining=D(0)
                events.append({
                    "time":t,"kind":"normal_exit",
                    "price":price,"notional":basis,
                    "reason":(("trail" if armed else "pretrail_stop")+
                              ("_partial" if remaining>0 else "")),
                    "quota":quota,"quote":quote,
                })
            pending=remaining>0
            if not pending:
                break
        else:
            profit=(D(1)-lo/ep)*D(100)
            if profit>=14 or armed:
                armed=True
                best=max(best,profit)
    return {
        "events":events, "triggers":triggers, "pending":pending,
        "remaining_notional":remaining,
    }


def csv_rows(path):
    if not path.is_file() or path.stat().st_size>20_000_000:
        raise ExitQuantityAuditHalted("REFERENCE_CSV_MISSING_OR_OVERSIZED")
    with path.open(newline="",encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def compare_generated(generated,observed):
    """Compare only AFTER model has generated its own event sequence."""
    observed=sorted(observed,key=lambda x:(time_of(x["exit_time"]),
                                          0 if x["kind"]=="delayed_exit" else 1))
    result=Counter()
    result["observed_rows"]=len(observed)
    result["modeled_rows"]=len(generated)
    if len(generated)!=len(observed):
        result["event_count_mismatch"]=1
    for expected,actual in zip(generated,observed):
        if (expected["time"]!=time_of(actual["exit_time"]) or
            expected["kind"]!=actual["kind"]):
            result["event_identity_mismatches"]+=1
            continue
        price=number(actual["exit"])
        if abs(price-expected["price"])>max(D("1e-8"),price*D("1e-10")):
            result["price_mismatches"]+=1
        actual_basis=number(actual["notional"])
        if abs(actual_basis-expected["notional"])>TOL:
            result["quantity_mismatches"]+=1
        if actual.get("reason")!=expected["reason"]:
            result["reason_mismatches"]+=1
    return result


def audit(folder,candles_path,snapshot_path,*,end_ts=None,immutable=False):
    folder=Path(folder).expanduser()
    payload=json.loads(Path(snapshot_path).expanduser().read_text())
    if (payload.get("NOT_HISTORICAL") is not True or
        payload.get("purpose")!="CURRENT_CONTRACT_SPEC_REFERENCE_ONLY" or
        not isinstance(payload.get("specifications"),dict)):
        raise ExitQuantityAuditHalted("CURRENT_SNAPSHOT_PROVENANCE_UNVERIFIED")
    specs=payload["specifications"]
    entries=csv_rows(folder/"entries.csv")
    exits=csv_rows(folder/"exits.csv")
    fills=csv_rows(folder/"fills.csv")
    end_ts=time_of(END_UTC) if end_ts is None else end_ts
    db=open_offline_db(candles_path,immutable=immutable)
    db.execute("BEGIN")
    reports={}
    try:
        for cost in COSTS:
            label=f"V32_VOL30_SKIP_H4_S{cost}_CAP5_V39_HYBRID"
            opening=[r for r in entries if r.get("scenario")==label]
            closing=[r for r in exits if r.get("scenario")==label]
            entry_fills=[r for r in fills if r.get("scenario")==label
                         and r.get("kind")=="entry"]
            if len(opening)!=108 or len(entry_fills)!=108:
                raise ExitQuantityAuditHalted("FROZEN_ENTRY_COUNT_UNVERIFIED")
            by_fill=defaultdict(list)
            for r in entry_fills:
                by_fill[r["symbol"],time_of(r["time"])].append(r)
            by_exit=defaultdict(list)
            for r in closing:
                by_exit[r["symbol"],time_of(r["entry_time"])].append(r)
            seen_keys=set()
            counters=Counter()
            discrepancies=Counter()
            examples=[]
            for e in opening:
                symbol,t=e["symbol"],time_of(e["time"])
                key=symbol,t
                if key in seen_keys or len(by_fill[key])!=1:
                    raise ExitQuantityAuditHalted("ENTRY_OR_FILL_IDENTITY_NOT_UNIQUE")
                seen_keys.add(key)
                rows=by_exit.pop(key,[])
                if any(x["kind"]=="trim" for x in rows):
                    counters["portfolio_trim_positions_not_modeled"]+=1
                    continue
                fill=by_fill[key][0]
                ep=number(fill["price"])
                n=number(e["notional"])
                spec=specs.get(symbol)
                lot=lot_from_metadata(spec)
                if lot is None:
                    counters["reference_passthrough_positions"]+=1
                    units=None
                else:
                    counters["current_spec_lot_positions"]+=1
                    units=number(fill["current_contract_units"])
                # Include the one STRICTLY PRIOR candle for entry-bar quota.
                # It is needed even if a trigger fires on the entry candle.
                candles=list(db.execute(
                    "SELECT open_ts,open,high,low,close,amount FROM candles "
                    "WHERE symbol=? AND interval='Min30' AND open_ts>=? AND open_ts<=? "
                    "ORDER BY open_ts",(symbol,t-BAR_SECONDS,end_ts)))
                if not candles or candles[0][0]!=t-BAR_SECONDS:
                    raise ExitQuantityAuditHalted("PRIOR_CANDLE_MISSING")
                modeled=[]
                previous_ts=None
                previous_amount=None
                for ts,op,hi,lo,cl,amount in candles:
                    if previous_ts is not None and ts!=previous_ts+BAR_SECONDS:
                        raise ExitQuantityAuditHalted("HISTORICAL_CANDLE_GAP")
                    if ts>=t:
                        modeled.append((ts,op,hi,lo,cl,previous_amount))
                    previous_ts=ts
                    previous_amount=amount
                result=generate_position(
                    entry_ts=t,entry_price=ep,entry_notional=n,
                    entry_quote=number(fill.get("filled_usdt",e["notional"])),
                    candle_rows=modeled,lot=lot,entry_units=units,
                )
                observed=sorted(rows,key=lambda r:time_of(r["exit_time"]))
                compared=compare_generated(result["events"],observed)
                counters["positions_modeled"]+=1
                counters["modeled_trigger_count"]+=result["triggers"]
                counters["modeled_normal_exit_rows"]+=sum(
                    x["kind"]=="normal_exit" for x in result["events"])
                counters["modeled_delayed_exit_rows"]+=sum(
                    x["kind"]=="delayed_exit" for x in result["events"])
                counters["model_pending_positions"]+=int(result["pending"])
                for metric,value in compared.items():
                    discrepancies[metric]+=value
                if (compared.get("event_count_mismatch") or
                    compared.get("event_identity_mismatches") or
                    compared.get("price_mismatches") or
                    compared.get("quantity_mismatches") or
                    compared.get("reason_mismatches")):
                    if len(examples)<12:
                        examples.append({"symbol":symbol,
                                         "entry_ts":t,
                                         "modeled_rows":compared["modeled_rows"],
                                         "observed_rows":compared["observed_rows"],
                                         "event_count_mismatch":compared.get("event_count_mismatch",0),
                                         "event_identity_mismatches":compared.get("event_identity_mismatches",0),
                                         "quantity_mismatches":compared.get("quantity_mismatches",0),
                                         "reason_mismatches":compared.get("reason_mismatches",0)})
            if by_exit and any(by_exit.values()):
                raise ExitQuantityAuditHalted("ORPHAN_HISTORICAL_EXIT")
            counters["exported_exit_rows"]=len(closing)
            counters["compared_exported_exit_rows"]=discrepancies["observed_rows"]
            counters["compared_modeled_exit_rows"]=discrepancies["modeled_rows"]
            issue_count=sum(discrepancies[k] for k in (
                "event_count_mismatch","event_identity_mismatches",
                "price_mismatches","quantity_mismatches","reason_mismatches"))
            reports[str(cost)]={
                "status":("V40_EXIT_QUANTITY_REFERENCE_MODEL_MATCH"
                          if issue_count==0 and not counters["portfolio_trim_positions_not_modeled"]
                          else "V40_EXIT_QUANTITY_MODEL_REVIEW_REQUIRED"),
                **dict(counters),
                "differences":{
                    name:discrepancies[name] for name in (
                        "event_count_mismatch","event_identity_mismatches",
                        "price_mismatches","quantity_mismatches","reason_mismatches")},
                "first_discrepancies":examples,
                "reference_fill_amounts_regenerated_without_exported_exit_quantities":True,
                "original_historical_lot_specifications_verified":False,
                "original_liquidity_model_market_executable_verified":False,
                "independent_risk_trim_fills_verified":False,
                "full_strategy_parity_certified":False,
                "venue_execution_certified":False,
                "entry_allowed":False,
                "live_authorization":False,
                "mexc_posts":0,
            }
    finally:
        db.close()
    return reports


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder",type=Path,required=True)
    parser.add_argument("--candles",type=Path,required=True)
    parser.add_argument("--snapshot",type=Path,required=True)
    parser.add_argument("--immutable",action="store_true")
    args=parser.parse_args()
    try:
        result=audit(args.folder,args.candles,args.snapshot,
                     immutable=args.immutable)
        print(json.dumps(result,indent=2,sort_keys=True))
    except (ExitQuantityAuditHalted,ValueError,KeyError,IndexError,
            sqlite3.DatabaseError,ArithmeticError) as exc:
        code=(str(exc) if type(exc) is ExitQuantityAuditHalted
              else "HISTORICAL_INPUT_OR_MODEL_INVALID")
        print(json.dumps({
            "status":"V40_EXIT_QUANTITY_AUDIT_HALTED",
            "failure_code":code,
            "entry_allowed":False,"live_authorization":False,"mexc_posts":0,
        },indent=2))
        raise SystemExit(2)


if __name__=="__main__":
    main()

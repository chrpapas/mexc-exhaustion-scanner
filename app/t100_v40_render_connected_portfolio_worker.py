"""Real Render full-lifecycle V40 wiring, strictly GET/SELECT-only.

Uses actual Stage2 scanner and checkpoint SQL roles, authenticates the live
MEXC account and instantiates *all* real independent account/stop/cursor/
native-close/financial-settlement/slot-release components. A real STOP/CLOSE
must be monitored even when no NEW Stage2 exists. The idle graph has no
entry adapter and intentionally does not invent a source row or CROSS plan.

CONNECTIVITY mode is NOT live authorization: all three PostgreSQL pools
enforce default_transaction_read_only=on; the injected signer is physically
incapable of POST; the supervisor receives no permit or close-only leader.
It cannot place an order, amend a native SL, persist accounting or recycle a
slot. To execute money, a separate signal-bound and release-gated worker
must recreate an explicitly write-capable pool and real MEXC POST signer.
This module is a live-data first-stage integration, not a go-live switch.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict,dataclass
from datetime import datetime,timezone
from decimal import Decimal as D
from hashlib import sha256
import json
import re
import time
from typing import Any,Mapping

FLAG="T100_V40_FULL_PORTFOLIO_WIRING_ENABLED"
SOURCE_DSN="T100_V40_SIGNAL_READER_DSN"
RUNTIME_DSN="T100_V40_EXECUTOR_RUNTIME_DSN"


@dataclass(frozen=True)
class V40ConnectedPortfolioReport:
    status:str
    source_connected:bool=False
    account_inventory_verified:bool=False
    guardian_connected:bool=False
    complete_exit_ports_constructed:bool=False
    native_stop_management_port_constructed:bool=False
    financial_settlement_port_constructed:bool=False
    source_candidate_count:int|None=None
    observed_slots:int|None=None
    guardian_status:str="NOT_OBSERVED"
    supervisor_status:str="NOT_RUN"
    real_signal_entry_context_bound:bool=False
    source_signal_identity_bound:bool=False
    source_bound_signal_id:int|None=None
    source_bound_symbol:str|None=None
    source_bound_tier:str|None=None
    source_bound_vol30_passed:bool|None=None
    exchange_posts:int=0
    database_writes:int=0
    live_authorization:bool=False
    entry_allowed:bool=False


class V40NoPostMexcClient:
    """Only signer provided to full idle graph; POST is impossible.

    This is NOT the production executor. A real money worker must inject
    a separately reviewed actual order and stop transport; this wrapper
    cannot be upgraded by changing an environment variable.
    """
    def __init__(self,signed:Any)->None:
        if not callable(getattr(signed,"_private_get",None)):
            raise ValueError("V40_GET_ONLY_SIGNER_REQUIRED")
        self._signed=signed

    async def _private_get(self,path:str,params:dict|None=None)->Any:
        return await self._signed._private_get(path,params)

    async def _private_post(self,*args:Any,**kwargs:Any)->Any:
        raise RuntimeError("V40_FULL_PORTFOLIO_CONNECTIVITY_POST_PHYSICALLY_DISABLED")


def validate_config(env:Mapping[str,str])->bool:
    from app.t100_v40_render_executor_disabled_worker import require_disabled_worker
    if not (
        env.get(FLAG)=="true"
        and require_disabled_worker(env)
        and env.get("T100_V40_FIRST_CANARY_ORDER_WORKER_ENABLED")!="true"
        and env.get("T100_V40_EMERGENCY_EXIT_ENABLED")!="true"
        and env.get("T100_V40_FOUR_SLOT_READONLY_WIRING_ENABLED")!="true"
        and env.get("T100_V40_ACCOUNT_PROFILE")=="DEDICATED_WEB_PILOT"
    ):
        return False
    for key in (SOURCE_DSN,RUNTIME_DSN):
        value=env.get(key)
        if (type(value) is not str
            or not value.startswith(("postgresql://","postgres://"))
            or len(value)>3000):
            return False
    if env[SOURCE_DSN]==env[RUNTIME_DSN]:
        return False
    key=env.get("MEXC_API_KEY")
    secret=env.get("MEXC_API_SECRET")
    return (
        type(key) is str and len(key)>=12 and key==key.strip()
        and type(secret) is str and len(secret)>=16 and secret==secret.strip()
    )


async def observe_connected_portfolio_once(
    env:Mapping[str,str],
)->V40ConnectedPortfolioReport:
    """One bounded live-data pass; never calls any mutation provider."""
    if not validate_config(env):
        return V40ConnectedPortfolioReport(
            "V40_CONNECTED_PORTFOLIO_PAPER_ROLE_OR_ACCOUNT_CONFIG_HALT"
        )
    # Deferred imports keep all clients unavailable on config failure.
    import asyncpg
    import httpx
    from app.mexc_trade import MexcTradeClient
    from app.t100_live_readonly_probe import ReadOnlyMexcProbe
    from app.t100_live_execution import LiveCaps
    from app.t100_v40_concrete_idle_portfolio_factory import (
        assemble_concrete_v40_idle_portfolio_graph,
    )
    from app.t100_v40_live_portfolio_supervisor import (
        V40PortfolioCycleFrame,
    )
    from app.t100_v40_portfolio_signal_binding import (
        bind_first_unheld_stage2_identity,
    )
    from app.t100_v40_native_close_mismatch_recovery import (
        V40NativeCloseMismatchEvidence,
    )

    source_pool=ledger_pool=writer_pool=None
    signed=None
    try:
        source_pool=await asyncpg.create_pool(
            dsn=env[SOURCE_DSN],ssl="require",
            min_size=1,max_size=1,timeout=12,command_timeout=15,
            server_settings={"default_transaction_read_only":"on"},
        )
        ledger_pool=await asyncpg.create_pool(
            dsn=env[RUNTIME_DSN],ssl="require",
            min_size=1,max_size=1,timeout=12,command_timeout=15,
            server_settings={"default_transaction_read_only":"on"},
        )
        # Separate physical pool, SAME dedicated runtime role, but
        # read-only even if the DB role otherwise has INSERT grants.
        writer_pool=await asyncpg.create_pool(
            dsn=env[RUNTIME_DSN],ssl="require",
            min_size=1,max_size=1,timeout=12,command_timeout=15,
            server_settings={"default_transaction_read_only":"on"},
        )
        if len({id(source_pool),id(ledger_pool),id(writer_pool)})!=3:
            return V40ConnectedPortfolioReport(
                "V40_CONNECTED_PORTFOLIO_SQL_ROLE_POOL_COLLISION_HALT"
            )
        # Signed same API key is the only identity used by ALL venue GETs.
        # This SHA is a non-secret local signer correlation marker; it
        # DOES NOT certify a venue account ID or grant any permission.
        signer_scope=sha256(
            b"T100_V40_READONLY_SIGNER_CORRELATION_V1\x00"+
            env["MEXC_API_KEY"].encode()
        ).hexdigest()
        signed=MexcTradeClient(
            "https://api.mexc.com",
            api_key=env["MEXC_API_KEY"],
            api_secret=env["MEXC_API_SECRET"],
        )
        no_post=V40NoPostMexcClient(signed)
        async with httpx.AsyncClient(
            base_url="https://api.mexc.com",
            trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(10.0),
        ) as public_http:
            # Caps are syntactic, nonauthorizing placeholders ONLY for
            # the supervisor class's typed signature. No permit can ever
            # reach the unbound entry port in this worker.
            observer_caps=LiveCaps(
                max_order_usdt=D("1"),max_portfolio_usdt=D("1"),
                max_daily_loss_usdt=D("1"),
                risk_profile="T100_V40_FOUR_SLOT_PRODUCTION_TRIAL",
            )
            graph=assemble_concrete_v40_idle_portfolio_graph(
                source_pool=source_pool,ledger_pool=ledger_pool,
                writer_pool=writer_pool,client=no_post,
                probe=ReadOnlyMexcProbe(no_post),public_get=public_http,
                account_scope_sha256=signer_scope,
                caps=observer_caps,parity_report={},
                holder_id="v40-idle-connected-GET-only",
                now_ms=lambda:int(time.time()*1000),
            )
            # Explicitly exercise the REAL typed account, guardian,
            # signal and completed-bar projection. No fake frame or source.
            frame=await asyncio.wait_for(graph.ports.observe(),timeout=65)
            if type(frame) is V40PortfolioCycleFrame:
                # Exactly the immutable Stage2 identity that the full
                # four-slot coordinator would independently recheck.
                # This does NOT construct a CROSS lot/margin/POST context
                # and cannot grant the separate production release permit.
                identity=bind_first_unheld_stage2_identity(
                    frame.source,frame.snapshot,
                )
                observation=await graph.supervisor.run_one(permit=None)
                if (
                    observation.entries_allowed is not False
                    or observation.live_authorization is not False
                ):
                    return V40ConnectedPortfolioReport(
                        "V40_CONNECTED_PORTFOLIO_UNEXPECTED_AUTHORIZATION_HALT"
                    )
                return V40ConnectedPortfolioReport(
                    status="V40_CONNECTED_LIFECYCLE_PORTS_GET_ONLY_"+observation.status,
                    source_connected=True,account_inventory_verified=True,
                    guardian_connected=True,
                    complete_exit_ports_constructed=True,
                    native_stop_management_port_constructed=True,
                    financial_settlement_port_constructed=True,
                    source_candidate_count=len(frame.source.candidates),
                    observed_slots=frame.snapshot.occupied_slots,
                    guardian_status=frame.guardian.status,
                    supervisor_status=observation.status,
                    source_signal_identity_bound=identity is not None,
                    source_bound_signal_id=(
                        identity.signal_id if identity is not None else None
                    ),
                    source_bound_symbol=(
                        identity.symbol if identity is not None else None
                    ),
                    source_bound_tier=(
                        identity.tier if identity is not None else None
                    ),
                    source_bound_vol30_passed=(
                        identity.vol30_passed if identity is not None else None
                    ),
                )
            if type(frame) is V40NativeCloseMismatchEvidence:
                # A native stop may have already closed while SQL remains
                # OPEN. Without a write lease, explicitly report pending
                # financial settlement; never pretend that slot is free.
                return V40ConnectedPortfolioReport(
                    "V40_CONNECTED_NATIVE_CLOSE_POSITIVE_SETTLEMENT_PENDING_GET_ONLY",
                    source_connected=True,account_inventory_verified=True,
                    complete_exit_ports_constructed=True,
                    native_stop_management_port_constructed=True,
                    financial_settlement_port_constructed=True,
                )
            return V40ConnectedPortfolioReport(
                "V40_CONNECTED_PORTFOLIO_FRAME_TYPE_INVALID_HALT"
            )
    except BaseException:
        # Prevent leaking signed requests, DSNs, SQL and account details
        # into the public Render log. No retries after an unknown state.
        return V40ConnectedPortfolioReport(
            "V40_CONNECTED_PORTFOLIO_SIGNED_GET_OR_SQL_UNKNOWN_HALT"
        )
    finally:
        if signed is not None:
            await signed.close()
        for pool in (source_pool,ledger_pool,writer_pool):
            if pool is not None:
                await pool.close()


async def run_connected_monitor(
    env:Mapping[str,str], *,
    max_cycles:int|None=None,
    interval_seconds:int=120,
)->int:
    if (
        not validate_config(env)
        or type(interval_seconds) is not int
        or not 15<=interval_seconds<=300
        or max_cycles is not None
        and (type(max_cycles) is not int or not 1<=max_cycles<=100000)
    ):
        print(json.dumps(asdict(V40ConnectedPortfolioReport(
            "V40_CONNECTED_PORTFOLIO_PAPER_ROLE_OR_ACCOUNT_CONFIG_HALT"
        )),sort_keys=True),flush=True)
        return 2
    cycles=0
    repeat={
        "V40_SUPERVISOR_WAIT_GENUINE_STAGE2",
        "V40_SUPERVISOR_ENTRY_PERMIT_UNAVAILABLE",
        "V40_SUPERVISOR_DUPLICATE_SYMBOL_SKIP",
        "V40_SUPERVISOR_FOUR_SLOTS_OCCUPIED",
    }
    while True:
        report=await observe_connected_portfolio_once(env)
        print(json.dumps(asdict(report),sort_keys=True),flush=True)
        cycles+=1
        # Critical guardian, signed-account or SQL ambiguity is terminal.
        if not (
            report.complete_exit_ports_constructed is True
            and report.account_inventory_verified is True
            and report.entry_allowed is False
            and report.live_authorization is False
            and report.exchange_posts==report.database_writes==0
            and report.supervisor_status in repeat
        ):
            return 2
        if max_cycles is not None and cycles>=max_cycles:
            return 0
        await asyncio.sleep(float(interval_seconds))

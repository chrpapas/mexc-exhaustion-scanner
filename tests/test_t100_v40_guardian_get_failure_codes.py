"""V40 guardian GET failure classification: redacted, sticky, no trade permit."""
from __future__ import annotations

from dataclasses import dataclass
import httpx
import pytest

from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport, GuardianOnceHalted
from app.t100_live_readonly_probe import ReadOnlyMexcProbe, VenueReadHalted
from app.t100_v40_guardian_get_failure_codes import (
    classify_mode_api_rejection, classify_mode_transport_failure,
    guardian_mode_halt_from_venue_error, safe_wrapped_mode_reason,
    SAFE_GUARDIAN_MODE_HALTS,
)
from app.t100_v40_canary_durable_guardian import (
    _safe_observer_detail, _status_for_outbox,
)
from app.t100_v40_guardian_account_and_owned import (
    observe_v40_account_and_owned,
)
from app.t100_v40_guardian_ledger_reader import PostgresV40GuardianLedgerReader

KEY="a"*16
SECRET="b"*32
ENDPOINT="/api/v1/private/position/position_mode"


class Client:
    def __init__(self, *, status=200, payload=None, failure=None):
        self.status=status
        self.payload=payload if payload is not None else {"success":True,"code":0,"data":1}
        self.failure=failure
        self.calls=0
    async def get(self, path, **kwargs):
        self.calls+=1
        if self.failure is not None:
            raise self.failure
        return httpx.Response(
            self.status,
            json=self.payload,
            request=httpx.Request("GET","https://api.mexc.com"+path),
        )


def transport(**changes):
    client=Client(**changes)
    return GetOnlyMexcTransport(client,api_key=KEY,api_secret=SECRET),client


@pytest.mark.parametrize("status,expected",[
    (401,"POSITION_MODE_HTTP_401"),
    (403,"POSITION_MODE_HTTP_403"),
    (429,"POSITION_MODE_HTTP_429"),
    (500,"POSITION_MODE_HTTP_5XX"),
    (503,"POSITION_MODE_HTTP_5XX"),
    (418,"POSITION_MODE_HTTP_OTHER"),
])
@pytest.mark.asyncio
async def test_http_failure_classification_has_no_raw_data(status,expected):
    tr,client=transport(status=status,payload={"msg":"SECRET DO NOT LOG"})
    with pytest.raises(GuardianOnceHalted,match="^"+expected+"$") as exc:
        await tr._private_get(ENDPOINT)
    assert "SECRET" not in str(exc.value)
    assert client.calls==1
    assert safe_wrapped_mode_reason(exc.value)==expected


@pytest.mark.parametrize("code,expected",[
    (510,"POSITION_MODE_API_RATE_LIMIT"),
    (701,"POSITION_MODE_API_PERMISSION"),
    (602,"POSITION_MODE_API_AUTH"),
    (501,"POSITION_MODE_API_SERVER"),
    (9999,"POSITION_MODE_API_REJECTION"),
])
@pytest.mark.asyncio
async def test_api_failure_codes_are_bounded_and_redacted(code,expected):
    tr,_=transport(payload={"success":False,"code":code,"msg":"SECRET IN EXCHANGE BODY"})
    with pytest.raises(GuardianOnceHalted,match="^"+expected+"$") as exc:
        await tr._private_get(ENDPOINT)
    assert "SECRET" not in str(exc.value)
    assert classify_mode_api_rejection({"code":code})==expected


@pytest.mark.asyncio
async def test_network_timeout_is_distinguished_without_exception_text():
    req=httpx.Request("GET","https://api.mexc.com"+ENDPOINT)
    tr,_=transport(failure=httpx.ReadTimeout("PRIVATE URL AND TOKEN",request=req))
    with pytest.raises(GuardianOnceHalted,match="^POSITION_MODE_TIMEOUT$"):
        await tr._private_get(ENDPOINT)


@pytest.mark.asyncio
async def test_signed_get_probe_preserves_only_safe_fixed_codes():
    tr,_=transport(status=429)
    with pytest.raises(VenueReadHalted,match="^POSITION_MODE_HTTP_429$") as exc:
        await ReadOnlyMexcProbe(tr).raw_position_mode()
    code=guardian_mode_halt_from_venue_error(exc.value)
    assert code=="V40_GUARDIAN_HEDGE_POSITION_MODE_HTTP_429_HALT"
    assert code in SAFE_GUARDIAN_MODE_HALTS
    assert safe_wrapped_mode_reason(ValueError("POSITION_MODE_HTTP_429")) is None
    assert guardian_mode_halt_from_venue_error(ValueError("my secret"))==(
        "V40_GUARDIAN_HEDGE_MODE_SIGNED_GET_FAILED_HALT"
    )


@pytest.mark.asyncio
async def test_guardian_alerts_and_stays_non_authorizing(monkeypatch):
    from app import t100_v40_guardian_account_and_owned as module
    # The failure is before any ledger inventory call. No MEXC POST/DB mutation.
    monkeypatch.setattr(module,"_components",lambda get: (object(),object()))
    ledger=PostgresV40GuardianLedgerReader.__new__(PostgresV40GuardianLedgerReader)
    tr,client=transport(status=429)
    heartbeat=await observe_v40_account_and_owned(ledger=ledger,signed_get=tr)
    assert heartbeat.status=="V40_GUARDIAN_HEDGE_POSITION_MODE_HTTP_429_HALT"
    assert _status_for_outbox(heartbeat)==(
        "OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID"
    )
    assert _safe_observer_detail(heartbeat)==heartbeat.status
    assert not heartbeat.monitored_account_flat
    assert client.calls==1


@pytest.mark.asyncio
async def test_other_endpoint_keeps_historical_generic_redaction():
    tr,_=transport(status=429)
    with pytest.raises(GuardianOnceHalted,match="MEXC_SIGNED_GET_TRANSPORT_FAILED"):
        await tr._private_get("/api/v1/private/account/assets")

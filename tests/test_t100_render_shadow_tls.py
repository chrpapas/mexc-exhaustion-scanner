"""Production connector must verify TLS regardless of DSN SSL options."""
import os
import ssl
from unittest.mock import AsyncMock, patch

import pytest

from app.t100_render_shadow_witness import ShadowWitnessFailure, connect_render_witness


@pytest.mark.asyncio
async def test_tls_required_even_when_dsn_requests_disable():
    connect = AsyncMock(return_value=object())
    with patch('asyncpg.connect', connect):
        await connect_render_witness('postgresql://test:fake@localhost/test?sslmode=disable')
    context = connect.call_args.kwargs['ssl']
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.mark.asyncio
async def test_tls_error_is_redacted_without_plaintext_retry():
    connect = AsyncMock(side_effect=ssl.SSLError('PRIVATE_DSN'))
    with patch('asyncpg.connect', connect), pytest.raises(
        ShadowWitnessFailure, match='^SHADOW_WITNESS_CONNECT_FAILED$'
    ):
        await connect_render_witness('postgresql://test:fake@localhost/test')
    assert connect.await_count == 1


@pytest.mark.asyncio
async def test_pinned_private_render_host_uses_required_encrypted_tls():
    connect = AsyncMock(return_value=object())
    dsn = (
        "postgresql://t100_shadow_sentinel_outbox:dummy_value_for_test@"
        "dpg-d9kb9djm8hqs73c3c39g-a:5432/mexc_exhaustion"
    )
    with patch.dict(os.environ, {"T100_RENDER_DB_TLS_MODE": "private_required"}):
        with patch("asyncpg.connect", connect):
            await connect_render_witness(dsn)
    assert connect.await_count == 1
    assert connect.call_args.kwargs["ssl"] == "require"


@pytest.mark.asyncio
@pytest.mark.parametrize("dsn", [
    "postgresql://t100_shadow_sentinel_outbox:test@evil.example.com/mexc_exhaustion",
    "postgresql://mexc_scanner:test@dpg-d9kb9djm8hqs73c3c39g-a/mexc_exhaustion",
    "postgresql://t100_shadow_sentinel_outbox:test@dpg-d9kb9djm8hqs73c3c39g-a/other",
    "postgresql://t100_shadow_sentinel_outbox:test@dpg-d9kb9djm8hqs73c3c39g-a:6432/mexc_exhaustion",
    "postgresql://t100_shadow_sentinel_outbox@dpg-d9kb9djm8hqs73c3c39g-a/mexc_exhaustion",
])
async def test_private_mode_rejects_bad_hosts_roles_ports_before_connect(dsn):
    connect = AsyncMock(return_value=object())
    with patch.dict(os.environ, {"T100_RENDER_DB_TLS_MODE": "private_required"}):
        with patch("asyncpg.connect", connect):
            with pytest.raises(ShadowWitnessFailure, match="PRIVATE_HOST_INVALID"):
                await connect_render_witness(dsn)
    assert connect.await_count == 0


@pytest.mark.asyncio
async def test_private_mode_never_retries_plaintext_on_tls_error():
    connect = AsyncMock(side_effect=ssl.SSLError("do_not_leak"))
    dsn = (
        "postgresql://t100_shadow_witness_writer:dummy_value_for_test@"
        "dpg-d9kb9djm8hqs73c3c39g-a/mexc_exhaustion"
    )
    with patch.dict(os.environ, {"T100_RENDER_DB_TLS_MODE": "private_required"}):
        with patch("asyncpg.connect", connect):
            with pytest.raises(ShadowWitnessFailure, match="^SHADOW_WITNESS_CONNECT_FAILED$"):
                await connect_render_witness(dsn)
    assert connect.await_count == 1
    assert connect.call_args.kwargs["ssl"] == "require"


@pytest.mark.asyncio
async def test_unknown_tls_mode_rejected_before_connect():
    connect = AsyncMock(return_value=object())
    with patch.dict(os.environ, {"T100_RENDER_DB_TLS_MODE": "disabled"}):
        with patch("asyncpg.connect", connect):
            with pytest.raises(ShadowWitnessFailure, match="TLS_MODE_INVALID"):
                await connect_render_witness("postgresql://u:p@localhost/db")
    assert connect.await_count == 0

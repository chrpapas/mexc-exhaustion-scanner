"""Production connector must verify TLS regardless of DSN SSL options."""
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

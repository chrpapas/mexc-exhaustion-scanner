"""Offline-only local Keychain prerequisite reporting: no secret disclosure."""
from __future__ import annotations

from scripts.t100_v40_watchdog_local_prerequisites import (
    inspect_local_prerequisites,
)
from app.t100_v40_cross_watchdog_service import (
    KEYCHAIN_READONLY,KEYCHAIN_DISCORD,DISCORD_ACCOUNT,
)
from app.t100_v40_cross_watchdog_sentinel_service import SENTINEL_KEYCHAIN
from scripts.t100_v40_pilot_get_only import KEY_ACCOUNT,SECRET_ACCOUNT


def test_preflight_all_items_are_present_but_not_live_authorization():
    secrets={
        (KEYCHAIN_READONLY,KEY_ACCOUNT):"test-public-api-key-123456",
        (KEYCHAIN_READONLY,SECRET_ACCOUNT):"test-private-api-secret-123456",
        (KEYCHAIN_DISCORD,DISCORD_ACCOUNT):
            "https://discord.com/api/webhooks/123456/abcdEFG",
        (SENTINEL_KEYCHAIN,DISCORD_ACCOUNT):
            "https://discord.com/api/webhooks/234567/efghXYZ",
    }
    calls=[]
    def read(service,account):
        calls.append((service,account))
        return secrets[(service,account)]
    result=inspect_local_prerequisites(read)
    assert result.status=="V40_WATCHDOG_LOCAL_SECRETS_PRESENT_NONAUTHORIZING"
    assert result.readonly_api_key_and_secret_in_keychain
    assert result.primary_discord_webhook_in_keychain
    assert result.sentinel_discord_webhook_in_keychain
    assert result.distinct_valid_discord_webhooks
    assert not result.exchange_side_readonly_permission_verified
    assert not result.account_subaccount_identity_independently_verified
    assert not result.primary_watchdog_running
    assert not result.sentinel_running
    assert not result.live_authorization and not result.entry_allowed
    assert result.network_requests==0 and result.mexc_posts==0
    assert result.database_writes==0
    assert len(calls)==4
    assert all(secret not in str(result) for secret in secrets.values())


def test_absent_or_same_webhooks_require_setup_not_false_pass():
    def absent(*args):raise RuntimeError("SECRET_KEYCHAIN_ERROR")
    result=inspect_local_prerequisites(absent)
    assert result.status=="V40_WATCHDOG_LOCAL_SECRETS_SETUP_REQUIRED"
    assert "SECRET" not in str(result)
    same="https://discord.com/api/webhooks/123456/abcdEFG"
    def duplicate(service,account):
        return ("test-api-secret-or-key-12345"
                if service==KEYCHAIN_READONLY else same)
    result=inspect_local_prerequisites(duplicate)
    assert not result.distinct_valid_discord_webhooks
    assert not result.entry_allowed

"""One-command LOCAL Mac Keychain check for isolated V40 watchdog prerequisites.

No network, no MEXC access, no DB, no secret or webhook output, no mutation.
A stored key is NOT proof of exchange-side READ-ONLY permissions, correct
MEXC subaccount or live monitoring readiness.
"""
from __future__ import annotations

import json
import re
from typing import Callable
from dataclasses import dataclass,asdict

from app.t100_v40_cross_watchdog_service import (
    KEYCHAIN_READONLY,KEYCHAIN_DISCORD,DISCORD_ACCOUNT,
)
from app.t100_v40_cross_watchdog_sentinel_service import SENTINEL_KEYCHAIN
from scripts.t100_v40_pilot_get_only import (
    KEY_ACCOUNT,SECRET_ACCOUNT,_from_keychain,
)


@dataclass(frozen=True)
class V40WatchLocalPrerequisites:
    status:str
    readonly_api_key_and_secret_in_keychain:bool=False
    primary_discord_webhook_in_keychain:bool=False
    sentinel_discord_webhook_in_keychain:bool=False
    distinct_valid_discord_webhooks:bool=False
    exchange_side_readonly_permission_verified:bool=False
    account_subaccount_identity_independently_verified:bool=False
    primary_watchdog_running:bool=False
    sentinel_running:bool=False
    live_authorization:bool=False
    entry_allowed:bool=False
    mexc_posts:int=0
    database_writes:int=0
    network_requests:int=0


def inspect_local_prerequisites(
    read:Callable[[str,str],str]=_from_keychain,
)->V40WatchLocalPrerequisites:
    def get(service,account):
        try:
            val=read(service,account)
        except Exception:
            return None
        return val if type(val) is str and val else None

    key=get(KEYCHAIN_READONLY,KEY_ACCOUNT)
    secret=get(KEYCHAIN_READONLY,SECRET_ACCOUNT)
    primary=get(KEYCHAIN_DISCORD,DISCORD_ACCOUNT)
    sentinel=get(SENTINEL_KEYCHAIN,DISCORD_ACCOUNT)
    keypair=bool(key and secret and len(key)>=12 and len(secret)>=16)
    pattern=r"https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+"
    p=bool(primary and re.fullmatch(pattern,primary))
    s=bool(sentinel and re.fullmatch(pattern,sentinel))
    distinct=bool(p and s and primary!=sentinel)
    return V40WatchLocalPrerequisites(
        status=(
            "V40_WATCHDOG_LOCAL_SECRETS_PRESENT_NONAUTHORIZING"
            if keypair and distinct
            else "V40_WATCHDOG_LOCAL_SECRETS_SETUP_REQUIRED"
        ),
        readonly_api_key_and_secret_in_keychain=keypair,
        primary_discord_webhook_in_keychain=p,
        sentinel_discord_webhook_in_keychain=s,
        distinct_valid_discord_webhooks=distinct,
    )


def main():
    report=inspect_local_prerequisites()
    print(json.dumps(asdict(report),sort_keys=True))
    print("LOCAL_KEYCHAIN_ONLY NO_NETWORK NO_MEXC_POST NO_DB_WRITES")


if __name__=="__main__":
    main()

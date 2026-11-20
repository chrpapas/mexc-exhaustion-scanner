"""Guided macOS Keychain setup for the isolated V40 GET-only watchdog.

Never send secret values via chat, command-line arguments, environment
variables, shell history, git or stdout. macOS /usr/bin/security supports
'-w' AS THE FINAL ARGUMENT to ask interactively for a password.
No MEXC API or Discord network request, no order, no DB mutation.
Default mode is a read-only presence report; --install is explicit and
never replaces any existing item or touches the old trading Keychain.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

from app.t100_v40_cross_watchdog_service import (
    KEYCHAIN_READONLY,KEYCHAIN_DISCORD,DISCORD_ACCOUNT,
)
from app.t100_v40_cross_watchdog_sentinel_service import SENTINEL_KEYCHAIN
from scripts.t100_v40_pilot_get_only import KEY_ACCOUNT,SECRET_ACCOUNT
from scripts.t100_v40_watchdog_local_prerequisites import (
    inspect_local_prerequisites,
)

SECURITY="/usr/bin/security"
ITEMS=(
    (KEYCHAIN_READONLY,KEY_ACCOUNT,"MEXC T100 WEB pilot READ-ONLY API Access Key"),
    (KEYCHAIN_READONLY,SECRET_ACCOUNT,"MEXC T100 WEB pilot READ-ONLY API Secret Key"),
    (KEYCHAIN_DISCORD,DISCORD_ACCOUNT,"PRIMARY T100 V40 watchdog Discord webhook URL"),
    (SENTINEL_KEYCHAIN,DISCORD_ACCOUNT,"SECOND, DIFFERENT sentinel Discord webhook URL"),
)


def _item_exists(service:str,account:str,*,run=subprocess.run)->bool:
    try:
        result=run(
            [SECURITY,"find-generic-password","-s",service,"-a",account],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            timeout=12,check=False,
        )
        return result.returncode==0
    except Exception:
        # Unknown is not permission to overwrite or claim item absence.
        raise RuntimeError("WATCH_KEYCHAIN_EXISTENCE_UNVERIFIED") from None


def install_items(
    *,run=subprocess.run,
    exists=None,
    output=print,
)->int:
    """Prompt via native macOS Keychain tool; never accept value as argv.

    Preserve existing entries strictly. Native security uses macOS's
    interactive password prompt when -w is LAST and has no value.
    """
    exists=exists or (lambda service,account:_item_exists(
        service,account,run=run,
    ))
    saved=0
    for service,account,label in ITEMS:
        if exists(service,account):
            output("ALREADY_PRESENT: "+label+" (not changed)")
            continue
        output("PROMPT_FOR: "+label)
        try:
            process=run(
                [SECURITY,"add-generic-password",
                 "-s",service,"-a",account,"-w"],
                stdout=subprocess.DEVNULL,
                # stderr is inherited ONLY for the native secure prompt,
                # not captured/logged or persisted in our report.
                timeout=180,check=False,
            )
        except Exception:
            output("KEYCHAIN_ADD_UNKNOWN_HALT")
            return 2
        if process.returncode!=0:
            output("KEYCHAIN_ADD_REJECTED_HALT")
            return 2
        saved+=1
    output("KEYCHAIN_PROMPTS_FINISHED: "+str(saved)+" new item(s)")
    return 0


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install",action="store_true",
        help="Explicitly enter missing credentials through native Keychain prompts",
    )
    args=parser.parse_args()
    if sys.platform!="darwin":
        print("V40_WATCHDOG_MACOS_ONLY")
        return 2
    if args.install:
        if not sys.stdin.isatty():
            print("V40_WATCHDOG_INTERACTIVE_TERMINAL_REQUIRED")
            return 2
        print("This will prompt for ONLY missing dedicated watchdog items.")
        print("Use a NEW read-only MEXC key and TWO different Discord webhooks.")
        print("Never paste secrets into ChatGPT; this script does not print them.")
        answer=input("Install missing Keychain items? Type YES: ")
        if answer!="YES":
            print("V40_WATCHDOG_NO_CHANGES")
            return 2
        if install_items()!=0:
            return 2
    outcome=inspect_local_prerequisites()
    from dataclasses import asdict
    print(json.dumps(asdict(outcome),sort_keys=True))
    print("KEYCHAIN_ONLY NO_NETWORK NO_MEXC_ORDER NO_DB_WRITE")
    return 0 if (
        outcome.status=="V40_WATCHDOG_LOCAL_SECRETS_PRESENT_NONAUTHORIZING"
    ) else 2


if __name__=="__main__":
    raise SystemExit(main())

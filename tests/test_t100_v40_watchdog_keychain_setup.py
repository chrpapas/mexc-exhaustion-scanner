"""Offline native Keychain prompt orchestration; no credentials or network."""
from __future__ import annotations

from dataclasses import dataclass

from scripts.t100_v40_watchdog_keychain_setup import (
    ITEMS,SECURITY,install_items,_item_exists,
)


@dataclass
class Process:
    returncode:int=0


def test_never_passes_secrets_through_command_line_or_overwrites():
    calls=[]
    visible=[]
    existing={(ITEMS[0][0],ITEMS[0][1])}
    def exists(service,account):
        return (service,account) in existing
    def fake_run(args,**kwargs):
        calls.append((args,kwargs))
        return Process()
    status=install_items(run=fake_run,exists=exists,output=visible.append)
    assert status==0
    assert len(calls)==3
    assert all(args[0]==SECURITY and
               args[1]=="add-generic-password" and
               args[-1]=="-w" and len(args)==7
               for args,_ in calls)
    assert all("-U" not in args and "-A" not in args for args,_ in calls)
    assert all("SENSITIVE" not in str(args) for args,_ in calls)
    assert all(kwargs.get("check") is False for _,kwargs in calls)
    assert "ALREADY_PRESENT" in visible[0]
    assert visible[-1].endswith("3 new item(s)")


def test_existing_items_skip_every_password_prompt():
    calls=[]
    out=[]
    status=install_items(
        run=lambda *a,**k:(calls.append(a) or Process()),
        exists=lambda *a:True,output=out.append,
    )
    assert status==0
    assert calls==[]
    assert out[-1].endswith("0 new item(s)")


def test_failed_add_stops_before_next_missing_item_and_never_retries():
    calls=[]
    def reject(args,**kwargs):
        calls.append(args)
        return Process(1)
    result=install_items(
        run=reject,exists=lambda *a:False,
        output=lambda _:None,
    )
    assert result==2
    assert len(calls)==1
    assert calls[0][-1]=="-w"


def test_existing_lookup_does_not_read_password_or_log_raw_output():
    seen=[]
    def fake_run(args,**kwargs):
        seen.append((args,kwargs))
        return Process(0)
    assert _item_exists("dedicated-service","MEXC_API_KEY",run=fake_run)
    args,kwargs=seen[0]
    assert args==[
        SECURITY,"find-generic-password","-s","dedicated-service",
        "-a","MEXC_API_KEY",
    ]
    assert "-w" not in args
    assert kwargs["check"] is False


def test_failed_existence_check_is_not_authorized_to_overwrite():
    def boom(*a,**k):raise RuntimeError("SENSITIVE_KEYCHAIN_ERROR")
    raised=False
    try:
        _item_exists("svc","acct",run=boom)
    except RuntimeError as exc:
        raised=True
        assert str(exc)=="WATCH_KEYCHAIN_EXISTENCE_UNVERIFIED"
        assert "SENSITIVE" not in str(exc)
    assert raised

"""Offline tests for fifth one-shot scope and inherited pilot guards."""
import asyncio

import pytest

from app import t100_web_pilot_fifth_native_stop_trigger_cli as fifth
from app import t100_web_pilot_tight_stop_trigger_cli as pilot


def _fixtures(monkeypatch, tmp_path, *, previous_ok=True, flat=True, bp="5"):
    monkeypatch.setattr(fifth, "FIFTH_JOURNAL", tmp_path / "fifth_test.json")

    async def prior(_client, _fp):
        return {
            "status": (
                fifth.PREVIOUS_STATUS
                if previous_ok else "FOURTH_CLOSE_AUDIT_NOT_CERTIFIED"
            ),
            "fourth_entry_filled_verified": True,
            "fourth_amendment_ack_journaled": True,
            "manual_market_close_exactly_verified": True,
            "finished_stop_exact_identity_verified": True,
            "finished_stop_price_matches_amended_target": True,
            "finished_stop_canceled_not_triggered": True,
            "account_bookended_flat_verified": True,
            "live_authorization": False,
            "mexc_orders_sent": 0,
        }

    async def inventory(*, client):
        return "flat" if flat else "not-flat"

    async def preview(_client):
        return {
            "status": "TIGHT_STOP_GET_ONLY_PREVIEW_NOT_AUTHORIZED",
            "stop_bps": bp, "contracts": 1, "margin": "ISOLATED",
            "leverage": "1x", "max_notional_usdt": "12.50",
            "live_authorization": False, "mexc_orders_sent": 0,
        }

    monkeypatch.setattr(fifth.fourth_audit, "audit_fourth_pilot", prior)
    monkeypatch.setattr(fifth, "inventory_run", inventory)
    monkeypatch.setattr(fifth, "_is_flat", lambda state: state == "flat")
    monkeypatch.setattr(fifth.pilot, "_preview", preview)


def test_fifth_check_requires_exact_fourth_close(monkeypatch, tmp_path):
    _fixtures(monkeypatch, tmp_path)
    result = asyncio.run(fifth._prior_and_preview(object(), "fp"))
    assert result["status"] == (
        "FIFTH_NATIVE_TRIGGER_ONE_SHOT_GET_ONLY_PREFLIGHT_READY"
    )
    assert result["current_subaccount_stably_flat"] is True
    assert result["entry_allowed"] is False
    assert result["live_authorization"] is False
    assert result["mexc_orders_sent"] == 0


@pytest.mark.parametrize(
    "previous_ok,flat,bp,expected",
    [
        (False, True, "5", "FOURTH_EXACT_EXIT_NOT_RECONFIRMED"),
        (True, False, "5", "FIFTH_PILOT_ACCOUNT_NOT_STABLY_FLAT"),
        (True, True, "50", "FIFTH_PILOT_TIGHT_STOP_PREVIEW_INVALID"),
    ],
)
def test_preflight_fails_closed(
    monkeypatch, tmp_path, previous_ok, flat, bp, expected,
):
    _fixtures(monkeypatch, tmp_path, previous_ok=previous_ok, flat=flat, bp=bp)
    with pytest.raises(pilot.TriggerPilotHalted, match=expected):
        asyncio.run(fifth._prior_and_preview(object(), "fp"))


def test_existing_journal_fences_new_entry(monkeypatch, tmp_path):
    _fixtures(monkeypatch, tmp_path)
    fifth.FIFTH_JOURNAL.write_text("synthetic journal already exists")
    with pytest.raises(
        pilot.TriggerPilotHalted, match="FIFTH_PILOT_JOURNAL_EXISTS_NO_NEW_ENTRY",
    ):
        asyncio.run(fifth._prior_and_preview(object(), "fp"))


def test_fifth_journal_is_separate_from_prior_pilots():
    assert fifth.FIFTH_JOURNAL.name != pilot.JOURNAL.name
    from app import t100_web_pilot_fourth_supervised_amendment_cli as fourth
    assert fifth.FIFTH_JOURNAL.name != fourth.FOURTH_JOURNAL.name

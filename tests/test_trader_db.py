import pytest

from app.json_utils import json_object


def test_json_object_accepts_dict():
    assert json_object({"risk_tier": "STANDARD"}) == {"risk_tier": "STANDARD"}


def test_json_object_decodes_json_text_from_asyncpg():
    assert json_object('{"risk_tier":"STANDARD","retest_close":0.15195}') == {
        "risk_tier": "STANDARD",
        "retest_close": 0.15195,
    }


def test_json_object_decodes_json_bytes():
    assert json_object(b'{"source":"paper"}') == {"source": "paper"}


def test_json_object_handles_null_and_empty_text():
    assert json_object(None) == {}
    assert json_object("") == {}


def test_json_object_rejects_non_object_json():
    with pytest.raises(ValueError):
        json_object('["not", "an", "object"]')

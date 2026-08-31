"""
Guards the Twilio webhook response fix.

Twilio webhooks must return raw TwiML XML. A FastAPI route that returns a plain
`str` is JSON-encoded (Content-Type: application/json, body wrapped in quotes),
which Twilio cannot parse -- the caller hears "an application error has
occurred". These tests assert the TwiML routes render raw XML and that the
JSON-returning status callback is left alone.
"""

import importlib
import sys

import pytest


@pytest.fixture
def main_module(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.delenv("APP_ENV", raising=False)
    sys.modules.pop("main", None)
    module = importlib.import_module("main")
    yield module
    sys.modules.pop("main", None)


def test_twiml_response_is_raw_xml(main_module):
    r = main_module.TwiMLResponse(content="<Response><Say>hi</Say></Response>")
    assert r.media_type == "application/xml"
    # Raw bytes -- NOT a JSON-encoded (quote-wrapped, escaped) string.
    assert r.body == b"<Response><Say>hi</Say></Response>"


def test_twiml_routes_use_xml_response_class(main_module):
    xml_paths = {
        "/webhooks/twilio/inbound",
        "/webhooks/twilio/post-triage",
        "/webhooks/twilio/whisper",
        "/webhooks/twilio/gather-bridge",
    }
    by_path = {
        r.path: r.response_class
        for r in main_module.app.routes
        if getattr(r, "path", None) in xml_paths
    }
    for path in xml_paths:
        assert by_path.get(path) is main_module.TwiMLResponse, f"{path} not XML"


def test_status_callback_stays_json(main_module):
    # /contractor-complete returns JSON dicts (it's a status callback, not TwiML).
    for r in main_module.app.routes:
        if getattr(r, "path", None) == "/webhooks/twilio/contractor-complete":
            assert r.response_class is not main_module.TwiMLResponse
            return
    pytest.fail("contractor-complete route not found")

"""
Guards the Retell SIP dial format.

The SIP URI must carry transport=tcp -- over UDP the initial SDP can be dropped
by Retell's SBC, so signaling connects and the agent runs but no audio flows.
"""

from integrations import retell_calls


class _FakeResp:
    status_code = 201
    text = ""

    def json(self):
        return {"call_id": "abc123"}


def _register(monkeypatch, sip_domain):
    monkeypatch.setattr(retell_calls.requests, "post", lambda *a, **k: _FakeResp())
    return retell_calls.register_phone_call(
        api_key="k", agent_id="agent", from_number="+15551110000",
        to_number="+15552220000", sip_domain=sip_domain,
    )


def test_sip_uri_defaults_to_tcp_transport(monkeypatch):
    reg = _register(monkeypatch, "sip.retellai.com")
    assert reg.call_id == "abc123"
    assert reg.sip_uri == "sip:abc123@sip.retellai.com;transport=tcp"


def test_sip_uri_respects_explicit_transport(monkeypatch):
    # If the domain already pins a transport, don't double-append.
    reg = _register(monkeypatch, "sip.retellai.com;transport=tls")
    assert reg.sip_uri == "sip:abc123@sip.retellai.com;transport=tls"

"""Phase 1 of the WhatsApp bot: any inbound message -> this week's digest.

Offline like the rest of the suite — `httpx.post` is monkeypatched in both
directions (Turso read, Cloud API send), so nothing here touches the network.
"""

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from bot import app, store, whatsapp

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
DIGEST = "*Islamabad — week of 31 Aug*\n\n• *Talk*\n🕒 7pm"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", VERIFY_TOKEN)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "111")
    monkeypatch.setenv("WHATSAPP_TOKEN", "graph-token")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://isb-events-test.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "turso-token")


def _signed(payload: dict) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode()
    mac = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, f"sha256={mac}"


def _message_payload(text="what's on", sender="923001234567") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.TEST",
                                    "from": sender,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ]
                        },
                    }
                ]
            }
        ],
    }


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound Cloud API sends."""
    outbox = []
    monkeypatch.setattr(whatsapp, "send_text", lambda to, body: outbox.append((to, body)))
    return outbox


@pytest.fixture
def stored_digest(monkeypatch):
    """Serve a digest from the store without touching Turso."""

    def _set(text):
        monkeypatch.setattr(store, "latest_digest", lambda: (text, "2026-08-31"))

    _set(DIGEST)
    return _set


# -- the store, over Turso's HTTP API ----------------------------------------


def test_libsql_url_is_rewritten_to_https():
    assert store._http_url("libsql://isb-events-x.turso.io") == "https://isb-events-x.turso.io"
    assert store._http_url("https://already.turso.io") == "https://already.turso.io"


def test_query_decodes_typed_cells_and_nulls(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [
                    {
                        "type": "ok",
                        "response": {
                            "result": {
                                "rows": [
                                    [
                                        {"type": "text", "value": "digest text"},
                                        {"type": "null"},
                                    ]
                                ]
                            }
                        },
                    }
                ]
            }

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["auth"] = kwargs["headers"]["Authorization"]
        return _Resp()

    monkeypatch.setattr(store.httpx, "post", fake_post)
    assert store.query("SELECT 1") == [["digest text", None]]
    assert captured["url"] == "https://isb-events-test.turso.io/v2/pipeline"
    assert captured["auth"] == "Bearer turso-token"


def test_query_raises_on_a_turso_error(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"type": "error", "error": {"message": "no such table"}}]}

    monkeypatch.setattr(store.httpx, "post", lambda url, **kw: _Resp())
    with pytest.raises(RuntimeError, match="no such table"):
        store.query("SELECT 1")


def test_digest_messages_splits_on_the_pipeline_separator(monkeypatch):
    monkeypatch.setattr(
        store, "latest_digest", lambda: (f"one{store.MESSAGE_SEPARATOR}two", "2026-08-31")
    )
    assert store.digest_messages() == ["one", "two"]


def test_digest_messages_is_empty_when_nothing_is_stored(monkeypatch):
    monkeypatch.setattr(store, "latest_digest", lambda: None)
    assert store.digest_messages() == []


def test_message_separator_matches_the_pipeline():
    """The bot cannot import isb_events, so the constant is duplicated. Pin it."""
    cli_source = Path(__file__).resolve().parent.parent / "isb_events" / "cli.py"
    assert repr(store.MESSAGE_SEPARATOR)[1:-1] in cli_source.read_text()


# -- Meta's handshake and signature ------------------------------------------


def test_verify_challenge_echoes_when_the_token_matches():
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": VERIFY_TOKEN,
        "hub.challenge": "1158201444",
    }
    assert app.handle_verify(params) == (200, "1158201444")


def test_verify_challenge_rejects_a_wrong_token():
    params = {"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "x"}
    assert app.handle_verify(params)[0] == 403


def test_signature_accepts_a_correct_hmac():
    raw, sig = _signed(_message_payload())
    assert whatsapp.verify_signature(raw, sig) is True


def test_signature_rejects_tampering_and_absence():
    raw, sig = _signed(_message_payload())
    assert whatsapp.verify_signature(raw + b" ", sig) is False
    assert whatsapp.verify_signature(raw, None) is False
    assert whatsapp.verify_signature(raw, "sha256=deadbeef") is False


def test_signature_rejects_when_the_app_secret_is_unset(monkeypatch):
    """A misconfigured deploy must reject, not wave everything through."""
    raw, sig = _signed(_message_payload())
    monkeypatch.delenv("WHATSAPP_APP_SECRET")
    assert whatsapp.verify_signature(raw, sig) is False


# -- inbound events ----------------------------------------------------------


def test_unsigned_event_is_rejected_without_sending(sent, stored_digest):
    status, _ = app.handle_event(json.dumps(_message_payload()).encode(), None)
    assert status == 403
    assert sent == []


def test_message_gets_the_stored_digest(sent, stored_digest):
    raw, sig = _signed(_message_payload())
    assert app.handle_event(raw, sig) == (200, "ok")
    assert sent == [("923001234567", DIGEST)]


def test_multi_part_digest_is_sent_as_separate_messages(sent, stored_digest):
    stored_digest(f"part one{store.MESSAGE_SEPARATOR}part two")
    raw, sig = _signed(_message_payload())
    app.handle_event(raw, sig)
    assert [body for _, body in sent] == ["part one", "part two"]


def test_any_text_gets_the_digest(sent, stored_digest):
    """Phase 1 does no parsing — 'hi' works as well as 'what's on'."""
    raw, sig = _signed(_message_payload(text="hi"))
    app.handle_event(raw, sig)
    assert len(sent) == 1


def test_status_callbacks_are_ignored(sent, stored_digest):
    """Delivery receipts arrive on the same webhook; replying would loop."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {"statuses": [{"id": "wamid.X", "status": "delivered"}]},
                    }
                ]
            }
        ]
    }
    raw, sig = _signed(payload)
    assert app.handle_event(raw, sig) == (200, "no messages")
    assert sent == []


def test_missing_digest_replies_with_an_explanation(sent, monkeypatch):
    monkeypatch.setattr(store, "latest_digest", lambda: None)
    raw, sig = _signed(_message_payload())
    app.handle_event(raw, sig)
    assert sent == [("923001234567", app.NO_DIGEST_REPLY)]


def test_a_failing_send_still_returns_200(monkeypatch, stored_digest):
    """Meta retries non-2xx and disables webhooks that keep failing."""

    def boom(to, body):
        raise RuntimeError("graph api down")

    monkeypatch.setattr(whatsapp, "send_text", boom)
    raw, sig = _signed(_message_payload())
    assert app.handle_event(raw, sig) == (200, "ok")


def test_non_json_body_is_ignored_not_retried(sent):
    raw = b"not json"
    mac = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert app.handle_event(raw, f"sha256={mac}") == (200, "ignored")
    assert sent == []


# -- subscribers and consent -------------------------------------------------


@pytest.fixture
def writes(monkeypatch):
    """Capture subscriber writes without touching Turso."""
    calls = []
    for name in ("record_contact", "opt_in", "opt_out"):
        monkeypatch.setattr(store, name, lambda wa_id, _n=name: calls.append((_n, wa_id)))
    return calls


def test_every_message_records_a_contact(sent, stored_digest, writes):
    raw, sig = _signed(_message_payload(text="hi"))
    app.handle_event(raw, sig)
    assert ("record_contact", "923001234567") in writes


def test_a_plain_message_is_not_treated_as_consent(sent, stored_digest, writes):
    """Meta requires explicit opt-in before any business-initiated message."""
    raw, sig = _signed(_message_payload(text="what's on"))
    app.handle_event(raw, sig)
    assert [name for name, _ in writes] == ["record_contact"]


def test_subscribe_opts_in_and_still_sends_the_digest(sent, stored_digest, writes):
    raw, sig = _signed(_message_payload(text="Subscribe"))
    app.handle_event(raw, sig)
    assert ("opt_in", "923001234567") in writes
    assert [body for _, body in sent] == [app.OPT_IN_REPLY, DIGEST]


def test_stop_opts_out_and_sends_no_digest(sent, stored_digest, writes):
    raw, sig = _signed(_message_payload(text="STOP"))
    app.handle_event(raw, sig)
    assert ("opt_out", "923001234567") in writes
    assert [body for _, body in sent] == [app.OPT_OUT_REPLY]


def test_stop_matching_is_exact_not_substring(sent, stored_digest, writes):
    """'Stop Commenting on My Body' is a real event in this week's digest."""
    raw, sig = _signed(_message_payload(text="tell me about Stop Commenting on My Body"))
    app.handle_event(raw, sig)
    assert [name for name, _ in writes] == ["record_contact"]
    assert [body for _, body in sent] == [DIGEST]


def test_a_failed_contact_write_does_not_cost_the_reply(sent, stored_digest, monkeypatch):
    def boom(wa_id):
        raise RuntimeError("turso down")

    monkeypatch.setattr(store, "record_contact", boom)
    raw, sig = _signed(_message_payload())
    app.handle_event(raw, sig)
    assert [body for _, body in sent] == [DIGEST]


def test_non_text_messages_still_get_the_digest(sent, stored_digest, writes):
    payload = _message_payload()
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "id": "wamid.IMG",
        "from": "923001234567",
        "type": "image",
        "image": {"id": "media-id"},
    }
    raw, sig = _signed(payload)
    app.handle_event(raw, sig)
    assert [body for _, body in sent] == [DIGEST]

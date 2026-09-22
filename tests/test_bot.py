"""Phase 1 of the WhatsApp bot: any inbound message -> this week's digest.

Offline like the rest of the suite — `httpx.post` is monkeypatched in both
directions (Turso read, Cloud API send), so nothing here touches the network.
"""

import hashlib
import hmac
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from bot import app, intent, store, whatsapp

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
DIGEST = "*Islamabad — week of 31 Aug*\n\n• *Talk*\n🕒 7pm"


# What `digest_events` hands back for the default reply: two days, one of them
# in the following calendar week, which is the case a week-shaped reply got wrong.
UPCOMING_ROWS = [
    ("2026-09-06", "Sun 6 Sep", "• *Open Mic*\n🕒 7pm"),
    ("2026-09-09", "Wed 9 Sep", "• *Naik Chor*\n🕒 6pm"),
]
UPCOMING_REPLY = (
    "*Islamabad — Sun 6 Sep onwards*\n\n"
    "*Sun 6 Sep*\n\n• *Open Mic*\n🕒 7pm\n\n"
    "*Wed 9 Sep*\n\n• *Naik Chor*\n🕒 6pm"
)


def _week(*parts: str) -> list[str]:
    """A fallback reply: the stored weekly text, then the button message."""
    return [*parts, app.PICK_BODY]


def _upcoming() -> list[str]:
    """The default reply, built from digest_events, then the button message."""
    return [UPCOMING_REPLY, app.PICK_BODY]


BUTTON_TITLES = ("Today", "Tomorrow", "This week")


class _Outbox(list):
    """Every send in order as `(to, body)`; button sends also land in `.buttons`."""

    def __init__(self):
        super().__init__()
        self.buttons = []


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
    """Capture outbound Cloud API sends, text and buttons alike."""
    outbox = _Outbox()
    monkeypatch.setattr(whatsapp, "send_text", lambda to, body: outbox.append((to, body)))

    def send_buttons(to, body, buttons=whatsapp.BUTTONS):
        outbox.append((to, body))
        outbox.buttons.append((to, body, tuple(title for _, title in buttons)))

    monkeypatch.setattr(whatsapp, "send_buttons", send_buttons)
    return outbox


@pytest.fixture
def stored_digest(monkeypatch):
    """Serve a digest from the store without touching Turso.

    Also stubs `store.query`, which the health check calls directly to probe
    the subscribers table — without this the suite would reach for the network.
    """

    def _set(text):
        monkeypatch.setattr(store, "current_digest", lambda: (text, "2026-08-31"))

    monkeypatch.setattr(store, "query", lambda sql, args=None: [[0]])
    # `?health=` asks Meta how long the token has left; that is a Graph call.
    monkeypatch.setattr(whatsapp, "token_status", lambda: "valid, never expires")
    # The default reply reads digest_events, not the stored weekly text. Without
    # this the rolling-window path would raise and silently fall back, and every
    # assertion below would pass while testing the wrong code.
    monkeypatch.setattr(store, "events_between", lambda a, b, c=None: UPCOMING_ROWS)
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
        store, "current_digest", lambda: (f"one{store.MESSAGE_SEPARATOR}two", "2026-08-31")
    )
    assert store.digest_messages() == ["one", "two"]


def test_digest_messages_is_empty_when_nothing_is_stored(monkeypatch):
    monkeypatch.setattr(store, "current_digest", lambda: None)
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
    assert sent == [("923001234567", body) for body in _upcoming()]


def test_multi_part_digest_is_sent_as_separate_messages(sent, stored_digest):
    stored_digest(f"part one{store.MESSAGE_SEPARATOR}part two")
    raw, sig = _signed(_message_payload())
    app.handle_event(raw, sig)
    assert [body for _, body in sent] == _upcoming()


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
    monkeypatch.setattr(store, "current_digest", lambda: None)
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
    assert [body for _, body in sent] == [app.OPT_IN_REPLY, *_upcoming()]


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
    # Not an opt-out, and no longer the whole digest either — it asks nothing
    # the bot recognises, so it gets the "didn't catch that" reply.
    assert [body for _, body in sent] == [app.NOT_UNDERSTOOD]


def test_a_failed_contact_write_does_not_cost_the_reply(sent, stored_digest, monkeypatch):
    def boom(wa_id):
        raise RuntimeError("turso down")

    monkeypatch.setattr(store, "record_contact", boom)
    raw, sig = _signed(_message_payload())
    app.handle_event(raw, sig)
    assert [body for _, body in sent] == _upcoming()


def test_non_text_messages_are_not_understood(sent, stored_digest, writes):
    payload = _message_payload()
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "id": "wamid.IMG",
        "from": "923001234567",
        "type": "image",
        "image": {"id": "media-id"},
    }
    raw, sig = _signed(payload)
    app.handle_event(raw, sig)
    # A sticker or photo asks nothing. Flyer intake is not built yet.
    assert [body for _, body in sent] == [app.NOT_UNDERSTOOD]


# -- the send endpoint -------------------------------------------------------


def test_send_text_returns_the_parsed_response(monkeypatch):
    """A 200 carries the resolved wa_id and message id; both are diagnostic."""
    body = {
        "messaging_product": "whatsapp",
        "contacts": [{"input": "+923236501038", "wa_id": "923236501038"}],
        "messages": [{"id": "wamid.ABC", "message_status": "accepted"}],
    }
    sent_payload = {}

    class _Resp:
        status_code = 200

        def json(self):
            return body

    def fake_post(url, **kwargs):
        sent_payload.update(kwargs["json"])
        return _Resp()

    monkeypatch.setattr(whatsapp.httpx, "post", fake_post)
    assert whatsapp.send_text("+923236501038", "hi") == body
    assert sent_payload["messaging_product"] == "whatsapp"
    assert sent_payload["type"] == "text"
    assert sent_payload["text"]["body"] == "hi"


def test_send_buttons_uses_the_interactive_shape(monkeypatch):
    sent_payload = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    monkeypatch.setattr(
        whatsapp.httpx,
        "post",
        lambda url, **kw: (sent_payload.update(kw["json"]), _Resp())[1],
    )
    whatsapp.send_buttons("923236501038", "Pick a view")
    assert sent_payload["type"] == "interactive"
    inter = sent_payload["interactive"]
    assert inter["type"] == "button"
    assert inter["body"] == {"text": "Pick a view"}
    assert inter["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "today", "title": "Today"}},
        {"type": "reply", "reply": {"id": "tomorrow", "title": "Tomorrow"}},
        {"type": "reply", "reply": {"id": "week", "title": "This week"}},
    ]
    assert all(len(title) <= 20 for _, title in whatsapp.BUTTONS)
    assert len(whatsapp.BUTTONS) <= 3


def test_send_template_uses_the_template_shape(monkeypatch):
    sent_payload = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    monkeypatch.setattr(
        whatsapp.httpx,
        "post",
        lambda url, **kw: (sent_payload.update(kw["json"]), _Resp())[1],
    )
    whatsapp.send_template("923236501038", "hello_world")
    assert sent_payload["type"] == "template"
    assert sent_payload["template"] == {"name": "hello_world", "language": {"code": "en_US"}}


def test_send_surfaces_the_graph_error_body(monkeypatch):
    class _Resp:
        status_code = 400
        text = '{"error":{"message":"(#131030) Recipient not in allowed list"}}'

    monkeypatch.setattr(whatsapp.httpx, "post", lambda url, **kw: _Resp())
    with pytest.raises(RuntimeError, match="Recipient not in allowed list"):
        whatsapp.send_text("923236501038", "hi")


# -- the WSGI entrypoint -----------------------------------------------------
#
# Vercel serves `api.webhook:app`, so the adapter is part of the contract:
# a mistake in header mangling or body reading breaks the webhook while every
# test above still passes.


def _wsgi_call(method="GET", query="", body=b"", headers=None):
    from io import BytesIO

    from api.webhook import app as wsgi_app

    environ = {
        "REQUEST_METHOD": method,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": BytesIO(body),
        **(headers or {}),
    }
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = dict(response_headers)

    chunks = wsgi_app(environ, start_response)
    return captured["status"], b"".join(chunks).decode()


def test_wsgi_get_echoes_the_challenge():
    status, body = _wsgi_call(
        query=f"hub.mode=subscribe&hub.verify_token={VERIFY_TOKEN}&hub.challenge=1158201444"
    )
    assert status.startswith("200")
    assert body == "1158201444"


def test_wsgi_get_rejects_a_wrong_token():
    status, _ = _wsgi_call(query="hub.mode=subscribe&hub.verify_token=nope&hub.challenge=x")
    assert status.startswith("403")


def test_wsgi_post_reads_the_signature_header(sent, stored_digest, writes):
    """WSGI mangles X-Hub-Signature-256 into HTTP_X_HUB_SIGNATURE_256."""
    raw, sig = _signed(_message_payload())
    status, body = _wsgi_call(method="POST", body=raw, headers={"HTTP_X_HUB_SIGNATURE_256": sig})
    assert status.startswith("200")
    assert body == "ok"
    assert [b for _, b in sent] == _upcoming()


def test_wsgi_post_without_a_signature_is_rejected(sent, stored_digest, writes):
    raw, _ = _signed(_message_payload())
    status, _ = _wsgi_call(method="POST", body=raw)
    assert status.startswith("403")
    assert sent == []


def test_wsgi_rejects_other_methods():
    status, _ = _wsgi_call(method="DELETE")
    assert status.startswith("405")


# -- the health check --------------------------------------------------------


def test_health_requires_the_verify_token(stored_digest):
    assert app.handle_health("wrong")[0] == 403
    assert app.handle_health(None)[0] == 403


def test_health_reports_a_reachable_digest(stored_digest):
    status, body = app.handle_health(VERIFY_TOKEN)
    assert status == 200
    assert "week of 2026-08-31" in body
    assert "MISSING" not in body


def test_health_distinguishes_an_empty_store_from_a_broken_one(monkeypatch):
    monkeypatch.setattr(whatsapp, "token_status", lambda: "valid, never expires")
    monkeypatch.setattr(store, "current_digest", lambda: None)
    assert "NO ROWS" in app.handle_health(VERIFY_TOKEN)[1]

    def boom():
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(store, "current_digest", boom)
    body = app.handle_health(VERIFY_TOKEN)[1]
    assert "UNREACHABLE" in body and "401 Unauthorized" in body


def test_health_never_prints_secret_values(stored_digest):
    body = app.handle_health(VERIFY_TOKEN)[1]
    for secret in (APP_SECRET, "graph-token", "turso-token", VERIFY_TOKEN):
        assert secret not in body


def test_health_is_reachable_over_wsgi(stored_digest):
    status, body = _wsgi_call(query=f"health={VERIFY_TOKEN}")
    assert status.startswith("200")
    assert "digest" in body


def test_health_reports_the_token_lifetime(stored_digest, monkeypatch):
    """`set` was true right up until the 24-hour token died. Now Meta is asked."""
    assert "wa token  : set — valid, never expires" in app.handle_health(VERIFY_TOKEN)[1]

    monkeypatch.setattr(whatsapp, "token_status", lambda: "EXPIRED at 2026-09-16 10:00 UTC")
    assert "wa token  : set — EXPIRED" in app.handle_health(VERIFY_TOKEN)[1]


def test_health_survives_meta_being_down(stored_digest, monkeypatch):
    """A Graph outage must not turn the health check red for the wrong reason."""

    def boom():
        raise TimeoutError("graph.facebook.com")

    monkeypatch.setattr(whatsapp, "token_status", boom)
    status, body = app.handle_health(VERIFY_TOKEN)
    assert status == 200
    assert "wa token  : set — could not check (TimeoutError)" in body
    assert "digest    : ok" in body


def test_health_does_not_ask_meta_about_a_missing_token(stored_digest, monkeypatch):
    monkeypatch.delenv("WHATSAPP_TOKEN")
    calls = []
    monkeypatch.setattr(whatsapp, "token_status", lambda: calls.append(1))
    assert "wa token  : MISSING" in app.handle_health(VERIFY_TOKEN)[1]
    assert calls == []


# -- the subscription diagnostic ---------------------------------------------


def test_graph_get_returns_the_error_body_rather_than_raising(monkeypatch):
    """A 400 from Graph explains itself; that body is the useful part."""

    class _Resp:
        status_code = 400

        def json(self):
            return {"error": {"message": "Unsupported get request"}}

    monkeypatch.setattr(whatsapp.httpx, "get", lambda url, **kw: _Resp())
    assert whatsapp.graph_get("123/subscribed_apps")["error"]["message"] == (
        "Unsupported get request"
    )


def test_graph_get_handles_a_non_json_body(monkeypatch):
    class _Resp:
        status_code = 502

        text = "<html>bad gateway</html>"

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(whatsapp.httpx, "get", lambda url, **kw: _Resp())
    assert "502" in whatsapp.graph_get("x")["error"]["message"]


def test_diagnose_reports_an_unsubscribed_account(monkeypatch, capsys):
    from bot import selftest

    monkeypatch.setenv("WHATSAPP_WABA_ID", "WABA9")
    monkeypatch.setattr(whatsapp, "graph_get", lambda path, params=None: {"data": []})
    assert selftest.diagnose() == 1
    assert "NONE" in capsys.readouterr().out


def test_health_flags_a_missing_subscribers_table(monkeypatch, stored_digest):
    """The bot writes subscribers but never creates them — the pipeline does."""

    def boom(sql, args=None):
        raise RuntimeError("SQLite error: no such table: subscribers")

    monkeypatch.setattr(store, "query", boom)
    body = app.handle_health(VERIFY_TOKEN)[1]
    assert "subscribers: MISSING" in body
    assert "no such table" in body


def test_health_reports_a_present_subscribers_table(monkeypatch, stored_digest):
    monkeypatch.setattr(store, "query", lambda sql, args=None: [[0]])
    assert "subscribers: table present" in app.handle_health(VERIFY_TOKEN)[1]


def test_subscribe_requires_a_waba_id(monkeypatch, capsys):
    from bot import selftest

    monkeypatch.delenv("WHATSAPP_WABA_ID", raising=False)
    assert selftest.subscribe() == 1
    assert "WHATSAPP_WABA_ID" in capsys.readouterr().out


def test_subscribe_posts_to_subscribed_apps(monkeypatch, capsys):
    from bot import selftest

    monkeypatch.setenv("WHATSAPP_WABA_ID", "WABA9")
    calls = []
    monkeypatch.setattr(
        whatsapp, "graph_post", lambda path: (calls.append(path), {"success": True})[1]
    )
    assert selftest.subscribe() == 0
    assert calls == ["WABA9/subscribed_apps"]


def test_subscribe_surfaces_a_graph_error(monkeypatch, capsys):
    from bot import selftest

    monkeypatch.setenv("WHATSAPP_WABA_ID", "WABA9")
    monkeypatch.setattr(
        whatsapp, "graph_post", lambda path: {"error": {"message": "(#200) Permissions"}}
    )
    assert selftest.subscribe() == 1
    assert "(#200) Permissions" in capsys.readouterr().out


def test_token_status_flags_a_temporary_token(monkeypatch):
    import datetime

    soon = int((datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=3)).timestamp())
    monkeypatch.setattr(whatsapp, "graph_get", lambda p, q=None: {"data": {"expires_at": soon}})
    status = whatsapp.token_status()
    assert "TEMPORARY" in status and "System User" in status


def test_token_status_recognises_a_permanent_token(monkeypatch):
    monkeypatch.setattr(whatsapp, "graph_get", lambda p, q=None: {"data": {"expires_at": 0}})
    assert "never expires" in whatsapp.token_status()


def test_token_status_reports_an_expired_token(monkeypatch):
    monkeypatch.setattr(whatsapp, "graph_get", lambda p, q=None: {"data": {"expires_at": 86400}})
    assert whatsapp.token_status().startswith("EXPIRED at 1970-01-02")


def test_token_status_reports_rejection(monkeypatch):
    monkeypatch.setattr(
        whatsapp, "graph_get", lambda p, q=None: {"error": {"message": "Session has expired"}}
    )
    assert "REJECTED" in whatsapp.token_status()


# -- what did they ask for? --------------------------------------------------

TODAY = date(2026, 9, 1)


@pytest.mark.parametrize(
    "text",
    ["what's on today", "TODAY", "anything on today?", "what's on tonight"],
)
def test_today_is_recognised(text):
    wanted = intent.parse(text, today=TODAY)
    assert (wanted.kind, wanted.day, wanted.label) == (intent.DAY, TODAY, "today")


@pytest.mark.parametrize("text", ["what's on tomorrow", "tomorrow?", "tmrw"])
def test_tomorrow_is_recognised(text):
    wanted = intent.parse(text, today=TODAY)
    assert (wanted.kind, wanted.day, wanted.label) == (intent.DAY, date(2026, 9, 2), "tomorrow")


@pytest.mark.parametrize("text", ["what's on this week", "whats on", "events", "week", "upcoming"])
def test_the_week_phrases_are_recognised(text):
    assert intent.parse(text, today=TODAY) == intent.WEEK_FILTER


@pytest.mark.parametrize("text", ["hi", "", "thanks!", "is this the pizza place", "ok"])
def test_anything_unrecognised_is_not_the_week(text):
    """The bot used to answer every message with fifteen events."""
    assert intent.parse(text, today=TODAY).kind == intent.UNKNOWN


def test_a_day_word_has_to_stand_alone():
    """'Tomorrowland' is a plausible event title; it must not narrow the digest."""
    assert intent.parse("tickets for Tomorrowland?", today=TODAY).kind == intent.UNKNOWN


def test_tomorrow_wins_over_today_when_both_appear():
    assert intent.parse("not today — tomorrow", today=TODAY).label == "tomorrow"


# -- categories, and the two axes --------------------------------------------


def test_a_category_and_a_day_are_read_independently():
    """The whole point of the restructure: a message can carry both."""
    wanted = intent.parse("sports events happening today", today=TODAY)
    assert (wanted.kind, wanted.day, wanted.category) == (intent.DAY, TODAY, "sports")


def test_a_week_word_loses_to_a_named_day():
    """ "events" is a week word, and "sports events ... today" contains one.

    A reader who named a day means that day. This falls out of the ordering in
    `_timeframe`, but it is pinned because the old early-return code had the
    same behaviour by accident and a reader would assume it was load-bearing.
    """
    assert intent.parse("events today", today=TODAY).kind == intent.DAY
    assert intent.parse("music events tomorrow", today=TODAY).day == date(2026, 9, 2)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("music", "music"),
        ("any gigs?", "music"),
        ("comedy tonight", "comedy"),
        ("stand up", "comedy"),
        ("any workshops this week", "workshops"),
        ("classes tomorrow", "workshops"),
        ("talks", "talks"),
        ("films", "theatre_and_film"),
        ("what sports are on", "sports"),
        ("meetups", "social"),
        ("surprise me", "mixed"),
        ("mixed bag", "mixed"),
    ],
)
def test_the_category_words_are_recognised(text, expected):
    assert intent.parse(text, today=TODAY).category == expected


def test_a_category_alone_means_the_whole_week_of_it():
    """Someone texting "music" wants the gigs, not a refusal for naming no day."""
    wanted = intent.parse("music", today=TODAY)
    assert (wanted.kind, wanted.category) == (intent.WEEK, "music")


def test_a_bare_week_filter_is_unchanged():
    """The canary: `Filter` gained a field, and this asserts whole-object equality."""
    assert intent.parse("whats on", today=TODAY) == intent.WEEK_FILTER
    assert intent.WEEK_FILTER.category is None


def test_anything_on_today_is_still_the_day_not_the_mixed_bag():
    """ "anything on ...?" is one of the project's own week phrases.

    Reading "anything" as a mixed-bag word silently narrowed a question people
    already ask — which is why it is deliberately absent from CATEGORY_WORDS.
    """
    wanted = intent.parse("anything on today?", today=TODAY)
    assert (wanted.kind, wanted.day, wanted.category) == (intent.DAY, TODAY, None)


def test_an_unrecognised_message_is_still_unknown():
    assert intent.parse("is this the pizza place", today=TODAY).kind == intent.UNKNOWN


def test_the_label_names_the_category_and_the_day():
    """The empty reply echoes the asker's own words back."""
    assert intent.parse("music today", today=TODAY).label == "music today"
    assert intent.parse("music", today=TODAY).label == "music"


def test_the_bots_categories_match_the_pipelines():
    """`bot/` cannot import `isb_events`, so the vocabulary exists twice.

    `vercel.json` keeps the package out of the function bundle. This is the
    same discipline as `test_the_bots_post_pattern_matches_the_pipelines`.
    """
    from isb_events.categories import CATEGORIES as pipeline_categories

    assert intent.CATEGORIES == frozenset(pipeline_categories)
    # Every category can be asked for, and named back to a reader.
    assert set(intent.CATEGORY_WORDS.values()) == intent.CATEGORIES
    assert set(intent.CATEGORY_LABELS) == intent.CATEGORIES


# -- day replies -------------------------------------------------------------


@pytest.fixture
def day_rows(monkeypatch):
    """Serve `digest_events` rows without touching Turso."""

    def _set(rows):
        monkeypatch.setattr(store, "day_events", lambda day, category=None: rows)

    _set([("Tue 1 Sep", "• *Talk*\n🕒 7pm")])
    return _set


def test_asking_for_today_gets_only_that_day(sent, stored_digest, day_rows):
    raw, sig = _signed(_message_payload(text="what's on today"))
    app.handle_event(raw, sig)
    assert sent == [
        ("923001234567", "*Islamabad — Tue 1 Sep*\n\n• *Talk*\n🕒 7pm"),
        ("923001234567", app.PICK_BODY),
    ]


def test_the_day_heading_comes_from_the_stored_label(sent, stored_digest, day_rows):
    """The bot never formats a date — the renderer's heading is stored and reused."""
    day_rows([("Wed 2 Sep", "• *Gig*\n🕒 9pm")])
    raw, sig = _signed(_message_payload(text="tomorrow"))
    app.handle_event(raw, sig)
    assert sent[0][1].startswith("*Islamabad — Wed 2 Sep*")


def test_blocks_are_ordered_as_the_query_returned_them(sent, stored_digest, day_rows):
    day_rows([("Tue 1 Sep", "• *Early*"), ("Tue 1 Sep", "• *Late*")])
    raw, sig = _signed(_message_payload(text="today"))
    app.handle_event(raw, sig)
    assert sent[0][1] == "*Islamabad — Tue 1 Sep*\n\n• *Early*\n\n• *Late*"


def test_an_empty_day_says_so_instead_of_sending_the_week(sent, stored_digest, day_rows):
    day_rows([])
    raw, sig = _signed(_message_payload(text="anything on tomorrow?"))
    app.handle_event(raw, sig)
    assert sent == [("923001234567", app.NOTHING_ON.format(when="tomorrow"))]


def test_a_missing_digest_events_table_falls_back_to_the_week(sent, stored_digest, monkeypatch):
    """Migrations run in the pipeline, so the bot can be newer than the schema."""

    def boom(day, category=None):
        raise RuntimeError("no such table: digest_events")

    monkeypatch.setattr(store, "day_events", boom)
    raw, sig = _signed(_message_payload(text="what's on today"))
    assert app.handle_event(raw, sig) == (200, "ok")
    bodies = [body for _, body in sent]
    assert bodies[0] == app.DAY_UNAVAILABLE_NOTE
    assert DIGEST in bodies[1]


def test_a_day_reply_splits_over_the_char_limit(sent, stored_digest, day_rows):
    day_rows([("Tue 1 Sep", "• *Gig*\n" + "x" * 2000) for _ in range(3)])
    raw, sig = _signed(_message_payload(text="today"))
    app.handle_event(raw, sig)
    assert len(sent) > 1
    assert all(len(body) <= app.WHATSAPP_LIMIT for _, body in sent)


def test_every_listing_reply_ends_with_the_buttons(sent, stored_digest, day_rows):
    """Nobody discovers a filter they were never shown; the buttons show it."""
    raw, sig = _signed(_message_payload(text="what's on"))
    app.handle_event(raw, sig)
    assert sent == [("923001234567", UPCOMING_REPLY), ("923001234567", app.PICK_BODY)]
    assert sent.buttons == [("923001234567", app.PICK_BODY, BUTTON_TITLES)]

    sent.clear(), sent.buttons.clear()
    raw, sig = _signed(_message_payload(text="today"))
    app.handle_event(raw, sig)
    assert sent[-1] == ("923001234567", app.PICK_BODY)
    assert len(sent.buttons) == 1


def test_the_buttons_are_a_separate_message_so_nothing_overflows(sent, stored_digest, monkeypatch):
    # A packed message exactly on the limit: the buttons must not be glued to it.
    overhead = len("*Islamabad — Sun 6 Sep onwards*\n\n*Sun 6 Sep*\n\n")
    big = "y" * (app.WHATSAPP_LIMIT - overhead)
    monkeypatch.setattr(
        store, "events_between", lambda a, b, c=None: [("2026-09-06", "Sun 6 Sep", big)]
    )
    raw, sig = _signed(_message_payload(text="what's on"))
    app.handle_event(raw, sig)
    assert len(sent) == 2
    assert len(sent[0][1]) == app.WHATSAPP_LIMIT
    assert sent[1][1] == app.PICK_BODY


def test_short_replies_carry_the_buttons_on_themselves(sent, stored_digest, monkeypatch):
    """A one-line reply and its buttons are one message, not two."""
    monkeypatch.setattr(store, "events_between", lambda a, b, c=None: [])
    raw, sig = _signed(_message_payload(text="what's on"))
    app.handle_event(raw, sig)
    assert len(sent) == 1
    assert sent.buttons == [
        ("923001234567", app.NOTHING_UPCOMING.format(days=app.UPCOMING_DAYS), BUTTON_TITLES)
    ]


def test_a_tapped_button_is_handled_like_the_typed_word(sent, stored_digest, day_rows):
    payload = _message_payload()
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "id": "wamid.TAP",
        "from": "923001234567",
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": "today", "title": "Today"}},
    }
    raw, sig = _signed(payload)
    app.handle_event(raw, sig)
    assert sent[0][1].startswith("*Islamabad — ")
    assert "Sun 6 Sep onwards" not in sent[0][1]  # a day, not the week


def test_the_button_titles_are_words_the_parser_knows():
    """The tap comes back as the title; if a title stops parsing, taps go to NOT_UNDERSTOOD."""
    today = date(2026, 9, 6)
    kinds = {title: intent.parse(title, today=today).kind for _, title in whatsapp.BUTTONS}
    assert kinds == {"Today": intent.DAY, "Tomorrow": intent.DAY, "This week": intent.WEEK}


# -- the first message gets the greeting -------------------------------------


@pytest.fixture
def first_contact(monkeypatch):
    monkeypatch.setattr(store, "record_contact", lambda wa_id: True)


def test_a_first_message_gets_the_greeting_and_nothing_else(sent, stored_digest, first_contact):
    raw, sig = _signed(_message_payload(text="what's on"))
    app.handle_event(raw, sig)
    assert sent.buttons == [("923001234567", app.GREETING, BUTTON_TITLES)]
    assert len(sent) == 1
    assert "Kya Scene Hai?" in app.GREETING
    assert len(app.GREETING) <= whatsapp.BUTTON_BODY_LIMIT


def test_the_second_message_is_answered_normally(sent, stored_digest, monkeypatch):
    monkeypatch.setattr(store, "record_contact", lambda wa_id: False)
    raw, sig = _signed(_message_payload(text="ksh"))
    app.handle_event(raw, sig)
    assert [b for _, b in sent] == _upcoming()


def test_stop_as_a_first_message_is_still_honoured(sent, stored_digest, first_contact, monkeypatch):
    monkeypatch.setattr(store, "opt_out", lambda wa_id: None)
    raw, sig = _signed(_message_payload(text="STOP"))
    app.handle_event(raw, sig)
    assert [b for _, b in sent] == [app.OPT_OUT_REPLY]


def test_a_failed_contact_write_means_not_first(sent, stored_digest, monkeypatch):
    """A Turso blip costs a greeting, never produces a duplicate one."""

    def boom(wa_id):
        raise RuntimeError("turso down")

    monkeypatch.setattr(store, "record_contact", boom)
    raw, sig = _signed(_message_payload(text="what's on"))
    app.handle_event(raw, sig)
    assert app.GREETING not in [b for _, b in sent]


def test_record_contact_reports_the_first_message(monkeypatch):
    monkeypatch.setattr(store, "query", lambda sql, args=None: [["1"]])
    assert store.record_contact("923001234567") is True
    monkeypatch.setattr(store, "query", lambda sql, args=None: [["7"]])
    assert store.record_contact("923001234567") is False
    assert "RETURNING message_count" in store.RECORD_CONTACT_SQL


def test_the_greeting_words_are_recognised():
    assert intent.parse("KSH", today=date(2026, 9, 6)).kind == intent.WEEK
    assert intent.parse("What's on tonight?", today=date(2026, 9, 6)).day == date(2026, 9, 6)


def test_day_queries_do_not_count_as_consent(sent, stored_digest, day_rows, writes):
    raw, sig = _signed(_message_payload(text="what's on today"))
    app.handle_event(raw, sig)
    assert [name for name, _ in writes] == ["record_contact"]


# -- which week does "what's on" serve? --------------------------------------
#
# Newest-row-wins was right only while the cron ran once a week. A daily cron
# writes next week's row days ahead, so the bot has to pick by date instead.


@pytest.fixture
def digest_rows(monkeypatch):
    """Answer the three week lookups out of a fake `digests` table."""

    def _set(rows: dict[str, str], today: date):
        monkeypatch.setattr(store, "datetime", _FrozenDatetime(today))

        def fake_query(sql, args=None):
            monday = args[0]
            if sql == store.WEEK_DIGEST_SQL:
                hit = [monday] if monday in rows else []
            elif sql == store.UPCOMING_DIGEST_SQL:
                hit = sorted(w for w in rows if w > monday)[:1]
            else:
                hit = sorted((w for w in rows if w < monday), reverse=True)[:1]
            return [[rows[w], w] for w in hit]

        monkeypatch.setattr(store, "query", fake_query)

    return _set


class _FrozenDatetime:
    def __init__(self, today):
        self._today = today

    def now(self, tz=None):
        return datetime(self._today.year, self._today.month, self._today.day, 12, 0, tzinfo=tz)


def test_the_week_containing_today_wins_over_a_newer_row(digest_rows):
    """Wednesday: next week's row already exists, and must not be served."""
    digest_rows({"2026-08-31": "this week", "2026-09-07": "next week"}, today=date(2026, 9, 2))
    assert store.current_digest() == ("this week", "2026-08-31")


def test_sunday_still_gets_the_week_it_is_in(digest_rows):
    digest_rows({"2026-08-31": "this week", "2026-09-07": "next week"}, today=date(2026, 9, 6))
    assert store.current_digest() == ("this week", "2026-08-31")


def test_the_new_week_takes_over_on_monday(digest_rows):
    digest_rows({"2026-08-31": "this week", "2026-09-07": "next week"}, today=date(2026, 9, 7))
    assert store.current_digest() == ("next week", "2026-09-07")


def test_falls_forward_when_this_week_was_never_rendered(digest_rows):
    digest_rows({"2026-09-07": "next week"}, today=date(2026, 9, 2))
    assert store.current_digest() == ("next week", "2026-09-07")


def test_falls_back_to_a_past_week_rather_than_the_placeholder(digest_rows):
    """Stale beats "no digest published yet" when a digest does exist."""
    digest_rows({"2026-08-24": "old week"}, today=date(2026, 9, 2))
    assert store.current_digest() == ("old week", "2026-08-24")


def test_no_rows_at_all_is_still_none(digest_rows):
    digest_rows({}, today=date(2026, 9, 2))
    assert store.current_digest() is None


# -- the default reply is a rolling window, not a calendar week ---------------
#
# A week is the wrong unit for "what's on". Serving the week containing today
# hides everything coming once the week is nearly over; serving the newest row
# instead shows a week that has not started. Both were shipped, in that order.
# These pin the rolling window that replaced them.


def test_the_default_reply_crosses_the_week_boundary(sent, stored_digest):
    """Sun 6 Sep and Wed 9 Sep are in different digest weeks, and both show."""
    raw, sig = _signed(_message_payload(text="what's on"))
    app.handle_event(raw, sig)
    body = sent[0][1]
    assert "*Sun 6 Sep*" in body and "*Wed 9 Sep*" in body


def test_the_default_reply_asks_the_store_from_today_onward(sent, stored_digest, monkeypatch):
    """Nothing before today: the old reply showed two days that had passed."""
    asked = {}

    def fake_between(start, end, category=None):
        asked["start"], asked["end"] = start, end
        return UPCOMING_ROWS

    monkeypatch.setattr(store, "events_between", fake_between)
    monkeypatch.setattr(app, "_today", lambda: date(2026, 9, 6))
    raw, sig = _signed(_message_payload(text="what's on"))
    app.handle_event(raw, sig)
    assert asked["start"] == date(2026, 9, 6)
    assert asked["end"] == date(2026, 9, 6) + timedelta(days=app.UPCOMING_DAYS - 1)


def test_events_on_one_day_share_a_single_heading(sent, stored_digest, monkeypatch):
    monkeypatch.setattr(
        store,
        "events_between",
        lambda a, b, c=None: [
            ("2026-09-06", "Sun 6 Sep", "• *Early*"),
            ("2026-09-06", "Sun 6 Sep", "• *Late*"),
            ("2026-09-07", "Mon 7 Sep", "• *Next day*"),
        ],
    )
    raw, sig = _signed(_message_payload(text="what's on"))
    app.handle_event(raw, sig)
    body = sent[0][1]
    assert body.count("*Sun 6 Sep*") == 1
    assert body.index("• *Early*") < body.index("• *Late*") < body.index("*Mon 7 Sep*")


def test_an_empty_window_does_not_claim_there_is_no_digest(sent, stored_digest, monkeypatch):
    monkeypatch.setattr(store, "events_between", lambda a, b, c=None: [])
    raw, sig = _signed(_message_payload(text="what's on"))
    app.handle_event(raw, sig)
    assert sent == [("923001234567", app.NOTHING_UPCOMING.format(days=app.UPCOMING_DAYS))]


def test_the_upcoming_reply_is_capped_with_a_note(sent, stored_digest, monkeypatch):
    rows = [(f"2026-09-{6 + n // 3:02d}", f"Day {n // 3}", f"• *Gig {n}*") for n in range(30)]
    monkeypatch.setattr(store, "events_between", lambda a, b, c=None: rows)
    raw, sig = _signed(_message_payload(text="what's on"))
    app.handle_event(raw, sig)
    body = "\n".join(b for _, b in sent)
    assert f"…and {30 - app.MAX_UPCOMING} more not shown." in body
    assert "• *Gig 0*" in body and f"• *Gig {app.MAX_UPCOMING}*" not in body


def test_a_missing_digest_events_table_falls_back_to_the_stored_week(
    sent, stored_digest, monkeypatch
):
    """Until the migration lands remotely, the weekly text is better than nothing."""

    def boom(start, end, category=None):
        raise RuntimeError("no such table: digest_events")

    monkeypatch.setattr(store, "events_between", boom)
    raw, sig = _signed(_message_payload(text="what's on"))
    assert app.handle_event(raw, sig) == (200, "ok")
    assert DIGEST in sent[0][1]


# -- curator intake ----------------------------------------------------------
#
# The allowlist is the only thing between a public WhatsApp number and a model
# reading whatever a stranger sends. These pin it fail-closed.

CURATOR = "923001234567"
LISTING = "/insert Chess N' Jams, Saturday 12th September, 5-9pm, Leafy Brew"


@pytest.fixture
def intake(monkeypatch):
    """Capture intake writes and control the pending count."""
    saved = []
    monkeypatch.setenv("CURATORS", CURATOR)
    monkeypatch.setattr(store, "record_intake", lambda s, b, **kw: saved.append((s, b)) or "id1")
    monkeypatch.setattr(store, "pending_intake", lambda: len(saved))
    monkeypatch.setattr(app.dispatch, "should_fire", lambda pending: False)
    return saved


def test_a_curator_can_submit_a_listing(sent, stored_digest, intake):
    raw, sig = _signed(_message_payload(text=LISTING, sender=CURATOR))
    app.handle_event(raw, sig)
    assert len(intake) == 1
    assert intake[0][0] == CURATOR
    # The prefix is stripped; the model gets the listing, not the command.
    assert intake[0][1].startswith("Chess N' Jams")
    assert "Saved" in sent[0][1]


def test_a_stranger_gets_the_ordinary_reply_and_stores_nothing(sent, stored_digest, intake):
    """Fail closed, and silently.

    Answering "you are not authorised" would teach a stranger that /insert does
    something. They get exactly what any other message gets.
    """
    raw, sig = _signed(_message_payload(text=LISTING, sender="923009999999"))
    app.handle_event(raw, sig)
    assert intake == []
    # Whatever any other sender would get for the same words — here, a listing
    # asks nothing, so it is the "didn't catch that" reply. The point is that
    # it reveals nothing about /insert.
    assert sent[0][1] == app.NOT_UNDERSTOOD


def test_an_unset_allowlist_means_nobody(sent, stored_digest, intake, monkeypatch):
    """An unset CURATORS must not mean "everyone"."""
    monkeypatch.delenv("CURATORS")
    raw, sig = _signed(_message_payload(text=LISTING, sender=CURATOR))
    app.handle_event(raw, sig)
    assert intake == []


def test_insert_with_no_text_asks_for_some(sent, stored_digest, intake):
    raw, sig = _signed(_message_payload(text="/insert", sender=CURATOR))
    app.handle_event(raw, sig)
    assert intake == []
    assert sent == [(CURATOR, app.INSERT_EMPTY)]


def test_a_failed_write_tells_the_curator(sent, stored_digest, intake, monkeypatch):
    """Silently losing a forwarded listing is the worst outcome here."""

    def boom(sender, body, **kw):
        raise RuntimeError("turso down")

    monkeypatch.setattr(store, "record_intake", boom)
    raw, sig = _signed(_message_payload(text=LISTING, sender=CURATOR))
    assert app.handle_event(raw, sig) == (200, "ok")
    assert sent == [(CURATOR, app.INSERT_FAILED)]


def test_a_curator_can_still_ask_what_is_on(sent, stored_digest, intake):
    """Curators are readers too; only the keyword routes to intake."""
    raw, sig = _signed(_message_payload(text="what's on", sender=CURATOR))
    app.handle_event(raw, sig)
    assert intake == []
    assert UPCOMING_REPLY in sent[0][1]


def test_the_very_first_listing_fires_a_run(sent, stored_digest, intake, monkeypatch):
    """Waiting for a fifth listing can mean publishing the first one too late.

    The run it asks for does not scrape (`skip_fetch`), so there is nothing to
    save up for.
    """
    fired = []
    # The `intake` fixture stubs this off; put the real threshold rule back.
    monkeypatch.setattr(
        app.dispatch,
        "should_fire",
        lambda pending: pending >= app.dispatch.INTAKE_TRIGGER_THRESHOLD,
    )
    monkeypatch.setattr(app.dispatch, "fire", lambda: fired.append(True) or True)
    raw, sig = _signed(_message_payload(text=LISTING, sender=CURATOR))
    app.handle_event(raw, sig)
    assert len(fired) == 1, "one forwarded listing is enough"
    assert sent[0][1] == app.INSERT_SOON


def test_a_listing_inside_the_cooldown_still_gets_saved(sent, stored_digest, intake, monkeypatch):
    """The run already in flight drains the whole queue, so nothing is lost."""
    monkeypatch.setattr(app.dispatch, "should_fire", lambda pending: False)
    raw, sig = _signed(_message_payload(text=LISTING, sender=CURATOR))
    app.handle_event(raw, sig)
    assert len(intake) == 1
    assert sent[0][1] == app.INSERT_SAVED


def test_a_failed_dispatch_still_confirms_the_save(sent, stored_digest, intake, monkeypatch):
    """Triggering early is a nicety; the listing is saved either way."""
    monkeypatch.setattr(app.dispatch, "should_fire", lambda pending: True)
    monkeypatch.setattr(app.dispatch, "fire", lambda: False)
    raw, sig = _signed(_message_payload(text=LISTING, sender=CURATOR))
    app.handle_event(raw, sig)
    assert len(intake) == 1
    assert "Saved" in sent[0][1]


# -- the dispatch guard ------------------------------------------------------


def test_dispatch_is_inert_without_configuration(monkeypatch):
    """No token means intake still works; listings just wait for the schedule."""
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    assert app.dispatch.configured() is False
    assert app.dispatch.should_fire(99) is False


def test_one_queued_listing_is_enough(monkeypatch):
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_REPO", "o/r")
    monkeypatch.setattr(app.dispatch, "_last_fired", None)
    assert app.dispatch.should_fire(0, now=10_000) is False
    assert app.dispatch.should_fire(1, now=10_000) is True


def test_the_dispatch_asks_for_a_run_that_does_not_scrape(monkeypatch):
    """A forward must not cost a scrape of every source — that is what batching was for."""
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_REPO", "o/r")
    monkeypatch.setattr(app.dispatch, "_last_fired", None)
    captured = {}

    class _Resp:
        status_code = 204

    def fake_post(url, **kw):
        captured["url"] = url
        captured["json"] = kw["json"]
        return _Resp()

    monkeypatch.setattr(app.dispatch.httpx, "post", fake_post)
    assert app.dispatch.fire(now=1.0) is True
    assert captured["url"].endswith("/actions/workflows/weekly-digest.yml/dispatches")
    assert captured["json"]["inputs"] == {"skip_fetch": "true"}
    # Strings only: the REST API rejects a JSON boolean for a workflow input.
    assert all(isinstance(v, str) for v in captured["json"]["inputs"].values())


def test_the_workflow_accepts_the_input_the_bot_sends(monkeypatch):
    """The bot names an input; the workflow has to declare it, or GitHub 422s."""
    import yaml

    workflow = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / ".github/workflows/weekly-digest.yml").read_text()
    )
    # `on:` parses as the boolean True in YAML 1.1, which is why this is not `["on"]`.
    triggers = workflow[True] if True in workflow else workflow["on"]
    declared = triggers["workflow_dispatch"]["inputs"]
    assert "skip_fetch" in declared
    assert declared["skip_fetch"]["default"] is False


def test_a_cold_container_dispatches_on_the_real_clock(monkeypatch):
    """The 2026-09-21 regression: a fresh container refused to fire.

    `_last_fired` was 0.0, and `time.monotonic()` counts from the container's
    own boot — so on a Vercel microVM a few seconds old, `now - 0.0` was well
    under the cooldown and a process that had never dispatched anything
    concluded it had just dispatched. Two real curator listings sat unprocessed.

    This deliberately does NOT pass `now=`: injecting a large timestamp is what
    hid the bug, because it silently supplied the epoch the code assumed.
    """
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_REPO", "o/r")
    monkeypatch.setattr(app.dispatch, "_last_fired", None)

    # A container that booted a quarter of a second ago, as a cold start is.
    monkeypatch.setattr(app.dispatch.time, "monotonic", lambda: 0.25)
    assert app.dispatch.should_fire(1) is True, "a cold container must dispatch"

    # And having fired, it still honours the cooldown against that same clock.
    monkeypatch.setattr(app.dispatch, "_last_fired", 0.25)
    monkeypatch.setattr(app.dispatch.time, "monotonic", lambda: 0.25 + 60)
    assert app.dispatch.should_fire(1) is False
    monkeypatch.setattr(
        app.dispatch.time, "monotonic", lambda: 0.25 + app.dispatch.COOLDOWN_SECONDS
    )
    assert app.dispatch.should_fire(1) is True


def test_the_health_line_says_whether_a_dispatch_would_fire(monkeypatch):
    """`configured` alone was green while nothing fired; name the cooldown too."""
    monkeypatch.setattr(app.dispatch, "_last_fired", None)
    assert "never fired" in app.dispatch.cooldown_state()

    monkeypatch.setattr(app.dispatch.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(app.dispatch, "_last_fired", 100.0)
    assert "cooling down" in app.dispatch.cooldown_state()


def test_dispatch_respects_the_cooldown(monkeypatch):
    """A webhook can be delivered twice, and curators can cross."""
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_REPO", "o/r")
    monkeypatch.setattr(app.dispatch, "_last_fired", 10_000.0)
    assert app.dispatch.should_fire(9, now=10_000 + 60) is False
    assert app.dispatch.should_fire(9, now=10_000 + app.dispatch.COOLDOWN_SECONDS) is True


@pytest.mark.parametrize(
    "configured",
    ["923001234567", "+923001234567", "+92 300 1234567", "92-300-1234567", " 923001234567 "],
)
def test_a_curator_number_is_matched_however_it_is_written(
    sent, stored_digest, intake, monkeypatch, configured
):
    """Meta's `from` is digits only; a `+` or spaces in CURATORS must still work.

    The failure is otherwise silent — the number never matches and /insert
    behaves as if the curator were a stranger, which looks like a broken bot.
    """
    monkeypatch.setenv("CURATORS", configured)
    raw, sig = _signed(_message_payload(text=LISTING, sender="923001234567"))
    app.handle_event(raw, sig)
    assert len(intake) == 1, f"CURATORS={configured!r} did not match"


def test_a_partial_number_is_not_a_match(sent, stored_digest, intake, monkeypatch):
    monkeypatch.setenv("CURATORS", "1234567")
    raw, sig = _signed(_message_payload(text=LISTING, sender="923001234567"))
    app.handle_event(raw, sig)
    assert intake == []


# -- a curator's forward is the submission -----------------------------------
#
# WhatsApp gives you no way to add a prefix to a forwarded message, so the
# gesture a curator reaches for first could not work. Meta marks forwards with
# `context.forwarded` and omits `context` entirely otherwise — confirmed
# against a real forward in production on 2026-09-17.


def _forwarded_payload(text, sender=CURATOR):
    payload = _message_payload(text=text, sender=sender)
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["context"] = {"forwarded": True}
    return payload


def test_a_curator_forward_is_queued_without_any_keyword(sent, stored_digest, intake):
    raw, sig = _signed(_forwarded_payload("Open Mic tonight at 8pm, The Black Hole"))
    app.handle_event(raw, sig)
    assert len(intake) == 1
    assert intake[0][1] == "Open Mic tonight at 8pm, The Black Hole"
    assert "Saved" in sent[0][1]


def test_a_forward_beats_the_day_words_inside_it(sent, stored_digest, intake):
    """A listing saying "tonight" must be stored, not answered as a day query.

    The forward check runs before intent parsing for exactly this reason.
    """
    raw, sig = _signed(_forwarded_payload("Gig tonight, 9pm"))
    app.handle_event(raw, sig)
    assert len(intake) == 1


def test_a_stranger_forward_is_not_queued(sent, stored_digest, intake):
    raw, sig = _signed(_forwarded_payload("Open Mic tonight at 8pm", sender="923009999999"))
    app.handle_event(raw, sig)
    assert intake == []


def test_a_curators_own_typing_is_still_a_question(sent, stored_digest, intake):
    """Only forwards bypass the keyword; a curator can still ask what's on."""
    raw, sig = _signed(_message_payload(text="what's on", sender=CURATOR))
    app.handle_event(raw, sig)
    assert intake == []
    assert UPCOMING_REPLY in sent[0][1]


def test_a_forwarded_photo_says_it_cannot_read_it(sent, stored_digest, intake):
    """Flyer intake needs media download at receipt; it is not built."""
    payload = _forwarded_payload("")
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "id": "wamid.IMG",
        "from": CURATOR,
        "type": "image",
        "image": {"id": "media-id"},
        "context": {"forwarded": True},
    }
    raw, sig = _signed(payload)
    app.handle_event(raw, sig)
    assert intake == []
    assert sent == [(CURATOR, app.INSERT_NO_TEXT)]


POST_LINK = "https://www.instagram.com/p/DcL_UC2inUx/?utm_source=ig_web_copy_link"


def test_a_curators_instagram_link_is_queued(sent, stored_digest, intake):
    """The share button sends plain text, not a forward, so the link is the signal."""
    raw, sig = _signed(_message_payload(text=f" {POST_LINK} ", sender=CURATOR))
    app.handle_event(raw, sig)
    assert intake == [(CURATOR, POST_LINK)]
    assert "Saved" in sent[0][1]


def test_a_strangers_instagram_link_is_not(sent, stored_digest, intake):
    raw, sig = _signed(_message_payload(text=POST_LINK, sender="923009999999"))
    app.handle_event(raw, sig)
    assert intake == []
    assert sent[0][1] == app.NOT_UNDERSTOOD


def test_a_link_inside_a_sentence_is_a_question_not_a_submission(sent, stored_digest, intake):
    raw, sig = _signed(_message_payload(text=f"is this on? {POST_LINK}", sender=CURATOR))
    app.handle_event(raw, sig)
    assert intake == []


def test_the_bots_post_pattern_matches_the_pipelines():
    """Two copies of one regex; the bot cannot import the pipeline's."""
    from isb_events.sources import instagram

    for url in (POST_LINK, "https://instagram.com/reel/abc-123/", "http://www.instagram.com/p/x"):
        assert app._is_post_link(url) == (instagram.canonical_url(url) is not None)
    for url in ("https://instagram.com/somebody/", "https://example.com/p/x/"):
        assert not app._is_post_link(url)
        assert instagram.canonical_url(url) is None


def test_a_message_with_no_context_is_not_a_forward():
    assert app._is_forwarded({"type": "text"}) is False
    assert app._is_forwarded({"type": "text", "context": {}}) is False
    assert app._is_forwarded({"type": "text", "context": {"forwarded": True}}) is True


# -- category filtering in the store -----------------------------------------


def _captured_query(monkeypatch):
    """Record the SQL and args the store would send, without a network call."""
    seen = {}

    def _query(sql, args=None):
        seen["sql"], seen["args"] = " ".join(sql.split()), args
        return []

    monkeypatch.setattr(store, "query", _query)
    return seen


def test_no_category_uses_the_plain_range_query(monkeypatch):
    seen = _captured_query(monkeypatch)
    store.events_between(date(2026, 9, 6), date(2026, 9, 12))
    assert "category" not in seen["sql"]
    assert seen["args"] == ["2026-09-06", "2026-09-12"]


def test_a_category_is_bound_not_interpolated(monkeypatch):
    """`query` binds every arg as text; a value spliced into the SQL is a bug."""
    seen = _captured_query(monkeypatch)
    store.events_between(date(2026, 9, 6), date(2026, 9, 12), "music")
    assert "category = ?" in seen["sql"]
    assert seen["args"] == ["2026-09-06", "2026-09-12", "music"]


def test_mixed_bag_also_returns_unclassified_events(monkeypatch):
    """The reason `mixed` is askable at all.

    An event the classifier could not label, or has not reached yet, must stay
    reachable by tapping — otherwise the bucket holding the classifier's
    failures is the one bucket nobody can open.
    """
    seen = _captured_query(monkeypatch)
    store.events_between(date(2026, 9, 6), date(2026, 9, 12), "mixed")
    assert "category = 'mixed' OR category IS NULL" in seen["sql"]
    # No third bind: the category is in the statement, not an argument.
    assert seen["args"] == ["2026-09-06", "2026-09-12"]


def test_a_day_query_passes_the_category_through(monkeypatch):
    seen = _captured_query(monkeypatch)
    store.day_events(date(2026, 9, 6), "comedy")
    assert seen["args"] == ["2026-09-06", "2026-09-06", "comedy"]


# -- category replies --------------------------------------------------------


def test_a_category_narrows_the_day(sent, stored_digest, monkeypatch):
    asked = {}

    def _day_events(day, category=None):
        asked["category"] = category
        return [("Tue 1 Sep", "• *Gig*\n🕒 8pm")]

    monkeypatch.setattr(store, "day_events", _day_events)
    raw, sig = _signed(_message_payload(text="music today"))
    app.handle_event(raw, sig)
    assert asked["category"] == "music"
    assert "Gig" in sent[0][1]


def test_an_empty_category_says_so_rather_than_widening(sent, stored_digest, monkeypatch):
    """Answering "sports today" with a comedy gig teaches that the filter is broken."""
    monkeypatch.setattr(store, "day_events", lambda day, category=None: [])
    raw, sig = _signed(_message_payload(text="sports today"))
    app.handle_event(raw, sig)
    assert sent == [("923001234567", app.NOTHING_ON.format(when="sports today"))]


def test_an_empty_category_week_offers_the_buttons(sent, stored_digest, monkeypatch):
    monkeypatch.setattr(store, "events_between", lambda a, b, c=None: [])
    raw, sig = _signed(_message_payload(text="comedy"))
    app.handle_event(raw, sig)
    assert "comedy" in sent[0][1]
    assert "Nothing listed" in sent[0][1]


def test_a_category_week_names_itself_in_the_header(sent, stored_digest, monkeypatch):
    monkeypatch.setattr(
        store,
        "events_between",
        lambda a, b, c=None: [("2026-09-06", "Sun 6 Sep", "• *Gig*")],
    )
    raw, sig = _signed(_message_payload(text="music this week"))
    app.handle_event(raw, sig)
    assert "music" in sent[0][1]

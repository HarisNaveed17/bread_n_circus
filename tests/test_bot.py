"""Phase 1 of the WhatsApp bot: any inbound message -> this week's digest.

Offline like the rest of the suite — `httpx.post` is monkeypatched in both
directions (Turso read, Cloud API send), so nothing here touches the network.
"""

import hashlib
import hmac
import json
from datetime import date
from pathlib import Path

import pytest

from bot import app, intent, store, whatsapp

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
DIGEST = "*Islamabad — week of 31 Aug*\n\n• *Talk*\n🕒 7pm"


def _week(*parts: str) -> list[str]:
    """A full-week reply: the stored messages, with the day-filter hint on the last."""
    return [*parts[:-1], f"{parts[-1]}\n\n{app.WEEK_HINT}"]


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
    """Serve a digest from the store without touching Turso.

    Also stubs `store.query`, which the health check calls directly to probe
    the subscribers table — without this the suite would reach for the network.
    """

    def _set(text):
        monkeypatch.setattr(store, "latest_digest", lambda: (text, "2026-08-31"))

    monkeypatch.setattr(store, "query", lambda sql, args=None: [[0]])
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
    assert sent == [("923001234567", *_week(DIGEST))]


def test_multi_part_digest_is_sent_as_separate_messages(sent, stored_digest):
    stored_digest(f"part one{store.MESSAGE_SEPARATOR}part two")
    raw, sig = _signed(_message_payload())
    app.handle_event(raw, sig)
    assert [body for _, body in sent] == _week("part one", "part two")


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
    assert [body for _, body in sent] == [app.OPT_IN_REPLY, *_week(DIGEST)]


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
    assert [body for _, body in sent] == _week(DIGEST)


def test_a_failed_contact_write_does_not_cost_the_reply(sent, stored_digest, monkeypatch):
    def boom(wa_id):
        raise RuntimeError("turso down")

    monkeypatch.setattr(store, "record_contact", boom)
    raw, sig = _signed(_message_payload())
    app.handle_event(raw, sig)
    assert [body for _, body in sent] == _week(DIGEST)


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
    assert [body for _, body in sent] == _week(DIGEST)


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
    assert [b for _, b in sent] == _week(DIGEST)


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
    monkeypatch.setattr(store, "latest_digest", lambda: None)
    assert "NO ROWS" in app.handle_health(VERIFY_TOKEN)[1]

    def boom():
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(store, "latest_digest", boom)
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

    from bot import selftest

    soon = int((datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=3)).timestamp())
    monkeypatch.setattr(whatsapp, "graph_get", lambda p, q=None: {"data": {"expires_at": soon}})
    status = selftest._token_status()
    assert "TEMPORARY" in status and "System User" in status


def test_token_status_recognises_a_permanent_token(monkeypatch):
    from bot import selftest

    monkeypatch.setattr(whatsapp, "graph_get", lambda p, q=None: {"data": {"expires_at": 0}})
    assert "never expires" in selftest._token_status()


def test_token_status_reports_rejection(monkeypatch):
    from bot import selftest

    monkeypatch.setattr(
        whatsapp, "graph_get", lambda p, q=None: {"error": {"message": "Session has expired"}}
    )
    assert "REJECTED" in selftest._token_status()


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


@pytest.mark.parametrize("text", ["what's on this week", "hi", "", "events"])
def test_everything_else_is_the_week(text):
    assert intent.parse(text, today=TODAY) == intent.WEEK_FILTER


def test_a_day_word_has_to_stand_alone():
    """'Tomorrowland' is a plausible event title; it must not narrow the digest."""
    assert intent.parse("tickets for Tomorrowland?", today=TODAY) == intent.WEEK_FILTER


def test_tomorrow_wins_over_today_when_both_appear():
    assert intent.parse("not today — tomorrow", today=TODAY).label == "tomorrow"


# -- day replies -------------------------------------------------------------


@pytest.fixture
def day_rows(monkeypatch):
    """Serve `digest_events` rows without touching Turso."""

    def _set(rows):
        monkeypatch.setattr(store, "day_events", lambda day: rows)

    _set([("Tue 1 Sep", "• *Talk*\n🕒 7pm")])
    return _set


def test_asking_for_today_gets_only_that_day(sent, stored_digest, day_rows):
    raw, sig = _signed(_message_payload(text="what's on today"))
    app.handle_event(raw, sig)
    assert sent == [("923001234567", "*Islamabad — Tue 1 Sep*\n\n• *Talk*\n🕒 7pm")]


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

    def boom(day):
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


def test_the_week_reply_hints_at_the_day_filter(sent, stored_digest):
    raw, sig = _signed(_message_payload(text="what's on"))
    app.handle_event(raw, sig)
    assert sent[0][1] == f"{DIGEST}\n\n{app.WEEK_HINT}"


def test_the_hint_is_dropped_rather_than_overflowing_a_message(sent, stored_digest):
    stored_digest("y" * (app.WHATSAPP_LIMIT - 2))
    raw, sig = _signed(_message_payload(text="what's on"))
    app.handle_event(raw, sig)
    assert sent[0][1] == "y" * (app.WHATSAPP_LIMIT - 2)


def test_day_queries_do_not_count_as_consent(sent, stored_digest, day_rows, writes):
    raw, sig = _signed(_message_payload(text="what's on today"))
    app.handle_event(raw, sig)
    assert [name for name, _ in writes] == ["record_contact"]

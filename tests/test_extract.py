"""One extractor for every intake path, and the Instagram adapter that feeds it.

No test calls the model. `extract()` is exercised with a stubbed client, and
everything that can be checked without one — the Instagram fixtures, the
`Extraction` -> `Event` conversion, the refusals — is checked directly, which
is where the rules that matter actually live.

The five `instagram_*.html` fixtures are trimmed captures of real posts (just
their `og:` tags), chosen because they fail in different ways: two are complete,
one has no venue anywhere, one has no price, and one has nothing but a price
and "this sunday" with every detail in the flyer.
"""

from datetime import date, datetime
from pathlib import Path

import pytest

from isb_events.extract import Extraction, Listing, extract, extract_all, to_event
from isb_events.models import KARACHI
from isb_events.sources import instagram, whatsapp

FIXTURES = Path(__file__).parent / "fixtures"


def _page(name: str) -> str:
    return (FIXTURES / f"instagram_{name}.html").read_text()


def _listing(**kw) -> Listing:
    base = {
        "text": "Game Night this Sunday at Crema Lounge, 6-9pm",
        "source": "instagram",
        "url": "https://www.instagram.com/dostanacommunity/p/DcyySLaICT0/",
        "posted_at": date(2026, 9, 2),
    }
    return Listing(**{**base, **kw})


# -- the Instagram adapter ---------------------------------------------------


def test_share_button_urls_are_accepted():
    """The share button appends tracking parameters; that is the form curators send."""
    messy = "https://www.instagram.com/p/DcL_UC2inUx/?utm_source=ig_web_copy_link&igsi=MzRl"
    assert instagram.canonical_url(messy) == "https://www.instagram.com/p/DcL_UC2inUx/"
    assert instagram.canonical_url("https://instagram.com/reel/AbC-123/") is not None
    assert instagram.canonical_url("https://example.com/p/x/") is None
    assert instagram.canonical_url("what's on tomorrow?") is None


def test_the_caption_arrives_whole():
    """Venue, date, time and price are all in og:description — no vision needed."""
    listing = instagram.listing_from_page(_page("chess_club"), "x")
    assert "SIP, F8/3 Islamabad" in listing.text
    assert "Saturday, 22nd August" in listing.text
    assert "5:00 PM – 8:00 PM" in listing.text
    assert "PKR 1,500" in listing.text


def test_the_post_date_is_pulled_out_to_anchor_relative_dates():
    """ "this sunday" is only resolvable against the day it was posted."""
    assert instagram.listing_from_page(_page("game_night"), "x").posted_at == date(2026, 9, 2)
    assert instagram.listing_from_page(_page("chess_club"), "x").posted_at == date(2026, 8, 18)


def test_the_event_url_is_the_canonical_per_post_link():
    """Per *post*, not per organiser — Event.id hashes it.

    Using the organiser's page would give every listing by one organiser the
    same id, and the store upserts by id, so they would overwrite each other.
    """
    listing = instagram.listing_from_page(_page("tote_bag"), "https://www.instagram.com/p/x/")
    assert listing.url == "https://www.instagram.com/thesocialitiessociety_/p/DcQgX4ZImrt/"


def test_a_page_with_no_caption_yields_nothing():
    assert instagram.listing_from_page("<html><head></head></html>", "x") is None


def test_posted_at_is_none_when_unparseable():
    assert instagram.parse_posted_at(None) is None
    assert instagram.parse_posted_at("no date in here") is None


# -- Extraction -> Event -----------------------------------------------------


def test_a_complete_extraction_becomes_an_event():
    found = Extraction(
        is_event=True,
        title="Candle Making Workshop",
        date="2026-09-05",
        start_time="17:00",
        end_time="19:00",
        venue="Calmenara",
        price_text="Rs 2,000",
    )
    event = to_event(found, _listing())
    assert event.title == "Candle Making Workshop"
    assert event.starts_at == datetime(2026, 9, 5, 17, 0, tzinfo=KARACHI)
    assert event.ends_at == datetime(2026, 9, 5, 19, 0, tzinfo=KARACHI)
    assert event.sources == ["instagram"]
    assert event.url == _listing().url


@pytest.mark.parametrize(
    "found",
    [
        Extraction(is_event=False, decline_reason="not_an_event"),
        Extraction(is_event=False, decline_reason="unclear", title="Talent Show"),
        # is_event true is not enough: starts_at is required, and a digest
        # grouped by day cannot hold an undated listing.
        Extraction(is_event=True, title="Talent Show", start_time="18:00"),
        Extraction(is_event=True, title="Talent Show", date="2026-09-06"),
        Extraction(is_event=True, date="2026-09-06", start_time="18:00"),
    ],
)
def test_anything_short_of_a_dated_titled_event_is_dropped(found):
    assert to_event(found, _listing()) is None


def test_an_unparseable_date_is_dropped_not_raised():
    found = Extraction(is_event=True, title="X", date="next friday", start_time="18:00")
    assert to_event(found, _listing()) is None


def test_an_end_time_before_the_start_is_discarded():
    """A crossed-midnight or simply wrong end time is not worth keeping."""
    found = Extraction(
        is_event=True, title="Club Night", date="2026-09-05", start_time="23:00", end_time="02:00"
    )
    event = to_event(found, _listing())
    assert event.starts_at.hour == 23
    assert event.ends_at is None


def test_blank_optional_fields_become_none_not_empty_strings():
    found = Extraction(
        is_event=True, title="X", date="2026-09-05", start_time="18:00", venue="  ", price_text=""
    )
    event = to_event(found, _listing())
    assert event.venue is None and event.price_text is None


# -- extract(), with the model stubbed ---------------------------------------


class _FakeMessages:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return type("Response", (), {"parsed_output": self.result})()


class _FakeClient:
    def __init__(self, result):
        self.messages = _FakeMessages(result)


def test_the_post_date_and_text_both_reach_the_model():
    client = _FakeClient(Extraction(is_event=False, decline_reason="not_an_event"))
    extract(_listing(), client=client)
    sent = client.messages.calls[0]["messages"][0]["content"]
    text = next(b["text"] for b in sent if b["type"] == "text")
    assert "Post date: 2026-09-02" in text
    assert "Crema Lounge" in text


def test_an_image_is_sent_as_a_content_block():
    """The screenshot path: a flyer the caption does not describe."""
    client = _FakeClient(Extraction(is_event=False, decline_reason="unclear"))
    extract(_listing(image=b"\xff\xd8\xff-not-a-real-jpeg"), client=client)
    blocks = client.messages.calls[0]["messages"][0]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/jpeg"
    assert blocks[1]["type"] == "text"


def test_no_image_block_when_there_is_no_image():
    client = _FakeClient(Extraction(is_event=False, decline_reason="unclear"))
    extract(_listing(), client=client)
    assert all(b["type"] != "image" for b in client.messages.calls[0]["messages"][0]["content"])


def test_a_failed_model_call_costs_one_listing_not_the_run():
    """A batch of forwarded posts must survive one malformed message."""
    client = _FakeClient(RuntimeError("overloaded"))
    assert extract(_listing(), client=client) is None


def test_extract_all_keeps_what_succeeds():
    good = Extraction(is_event=True, title="X", date="2026-09-05", start_time="18:00")
    client = _FakeClient(good)
    assert len(extract_all([_listing(), _listing()], client=client)) == 2

    client = _FakeClient(Extraction(is_event=False, decline_reason="no_date"))
    assert extract_all([_listing()], client=client) == []


def test_the_schema_stays_narrow():
    """The schema *is* the privacy filter, so its shape is load-bearing.

    A real forwarded newsletter carried an IBAN, a bank account title, a third
    party's mobile number and the recipient's name. Only `registration_phone`
    can hold a number at all, and only a phone-shaped one. `decline_reason` is
    an enum precisely so a free-text explanation cannot quote the rest back.

    Widening this set is a deliberate act. Make it one.
    """
    assert set(Extraction.model_fields) == {
        "is_event",
        "decline_reason",
        "title",
        "date",
        "start_time",
        "end_time",
        "venue",
        "price_text",
        "category",
        "registration_phone",
        "event_url",
    }
    reason = Extraction.model_fields["decline_reason"].annotation
    assert "str" not in str(reason).replace("Literal", "")


# -- the registration number -------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("0303 5667670", "0303 5667670"),
        ("  0303   5667670 ", "0303 5667670"),
        ("0303-566-7670", "0303 5667670"),
        ("03035667670", "0303 5667670"),
        # The country code replaces the leading zero; normalise it back, since
        # the digest is read by a local audience.
        ("+92 303 5667670", "0303 5667670"),
        ("0092 303 5667670", "0303 5667670"),
    ],
)
def test_a_published_registration_number_is_kept(given, expected):
    """ "To register, WhatsApp: 0303 5667670" is the whole call to action."""
    found = Extraction(
        is_event=True,
        title="X",
        date="2026-09-05",
        start_time="14:00",
        registration_phone=given,
    )
    assert to_event(found, _listing()).contact_phone == expected


@pytest.mark.parametrize(
    "given",
    [
        "PK36SCBL0000001123456702",  # an IBAN
        "1234567890123456",  # a card or long account number
        "12345678901",  # 11 digits but not a mobile prefix
        "051 1234567",  # a landline: you cannot WhatsApp it
        "(0303) 5667670",  # brackets are not how these are written
        "0303 566767",  # a digit short
        "0303 56676701",  # a digit over
        "Account Title: Some Person",
        "Rs 5,000",
        "",
        "   ",
    ],
)
def test_anything_that_is_not_a_pakistani_mobile_is_dropped(given):
    """Too loose is the dangerous direction: a wrong number is worse than none.

    Eleven digits starting 03 is exact enough that an account number cannot
    fit, whatever the model was told to do.
    """
    found = Extraction(
        is_event=True,
        title="X",
        date="2026-09-05",
        start_time="14:00",
        registration_phone=given,
    )
    assert to_event(found, _listing()).contact_phone is None


def test_the_number_reaches_the_rendered_block():
    from isb_events.models import DigestWindow
    from isb_events.render import render

    found = Extraction(
        is_event=True,
        title="Starry Night Painting Workshop",
        date="2026-09-05",
        start_time="14:00",
        venue="TW Den, F-7 Markaz",
        price_text="Rs 5,000",
        registration_phone="0303 5667670",
    )
    event = to_event(found, whatsapp.listing_from_message(PAINTING, received_at=RECEIVED))
    text = "\n".join(render([event], DigestWindow.week_of(date(2026, 8, 31))))
    assert "📱 0303 5667670" in text


# -- the WhatsApp adapter ----------------------------------------------------
#
# `whatsapp_samples.txt` holds three real forwarded messages, separated by a
# row of #. They were chosen to break different things: one has two prices and
# no link, one carries a phone number, one lists three cities and gives both a
# doors time and a start time.

SAMPLES = (FIXTURES / "whatsapp_samples.txt").read_text().split("#" * 55)
CHESS, PAINTING, FILM = (s.strip() for s in SAMPLES)
RECEIVED = date(2026, 9, 1)


def test_a_message_needs_no_fetching_or_parsing():
    """The seam earns its keep here: the body already is the text."""
    listing = whatsapp.listing_from_message(CHESS, received_at=RECEIVED)
    assert "Chess N' Jams" in listing.text.replace("’", "'")
    assert listing.source == "whatsapp"
    assert listing.posted_at == RECEIVED


def test_a_message_with_no_link_still_gets_an_identity():
    """Two of three samples have no URL. `source_ref` carries identity instead."""
    listing = whatsapp.listing_from_message(CHESS, received_at=RECEIVED)
    assert listing.url is None
    assert listing.source_ref


def test_two_messages_from_one_organiser_get_different_ids():
    """The failure the organiser-page fallback would have caused.

    The store upserts by id, so hashing the organiser would leave them with one
    event between them, forever overwriting each other.
    """
    a = whatsapp.listing_from_message(CHESS, received_at=RECEIVED)
    b = whatsapp.listing_from_message(PAINTING, received_at=RECEIVED)
    assert a.source_ref != b.source_ref

    found = Extraction(is_event=True, title="X", date="2026-09-12", start_time="17:00")
    assert to_event(found, a).id != to_event(found, b).id


def test_the_same_message_forwarded_twice_is_one_event():
    a = whatsapp.listing_from_message(CHESS, received_at=RECEIVED)
    b = whatsapp.listing_from_message(f"  {CHESS}  ", received_at=date(2026, 9, 3))
    assert a.source_ref == b.source_ref


def test_a_link_in_the_body_becomes_the_event_url():
    """The film screening carries a registration form; the others carry nothing."""
    assert whatsapp.listing_from_message(FILM, received_at=RECEIVED).url == (
        "https://forms.gle/LVqpXMB9EZvhkfm88"
    )
    assert whatsapp.listing_from_message(PAINTING, received_at=RECEIVED).url is None


def test_the_organiser_line_is_read_when_present():
    assert whatsapp.organiser(CHESS) == "The Social Chess Club"
    assert whatsapp.organiser(FILM) == "Pakistan Film Society"
    assert whatsapp.organiser("no organiser line here") is None


def test_an_empty_message_yields_nothing():
    assert whatsapp.listing_from_message("", received_at=RECEIVED) is None
    assert whatsapp.listing_from_message("   \n ", received_at=RECEIVED) is None


def test_both_sources_produce_the_same_shape():
    """The whole point: one extractor, two adapters, one input type."""
    from_wa = whatsapp.listing_from_message(CHESS, received_at=RECEIVED)
    from_ig = instagram.listing_from_page(_page("chess_club"), "x")
    assert type(from_wa) is type(from_ig) is Listing
    for listing in (from_wa, from_ig):
        assert listing.text and listing.source and listing.posted_at


# -- an event with no link ---------------------------------------------------


def test_an_event_with_no_url_renders_without_one():
    """`render` used to print `event.url` unconditionally."""
    from isb_events.models import DigestWindow
    from isb_events.render import render

    listing = whatsapp.listing_from_message(CHESS, received_at=RECEIVED)
    found = Extraction(
        is_event=True,
        title="Chess N' Jams",
        date="2026-09-12",
        start_time="17:00",
        venue="Leafy Brew, Gulberg Arena Mall",
        price_text="Rs 1,500 online, Rs 2,000 on the door",
    )
    event = to_event(found, listing)
    assert event.url is None
    text = "\n".join(render([event], DigestWindow.week_of(date(2026, 9, 7))))
    assert "Chess N' Jams" in text
    assert "🎟 Rs 1,500 online, Rs 2,000 on the door" in text
    assert "None" not in text


def test_an_id_stays_stable_across_a_rebuild_from_the_store():
    """A forwarded event must not change id when read back out."""
    listing = whatsapp.listing_from_message(PAINTING, received_at=RECEIVED)
    found = Extraction(is_event=True, title="Starry Night", date="2026-09-05", start_time="14:00")
    event = to_event(found, listing)
    rebuilt = event.model_copy()
    assert event.id == rebuilt.id
    # and it does not collide with the same title from a different message
    other = to_event(found, whatsapp.listing_from_message(CHESS, received_at=RECEIVED))
    assert event.id != other.id


# -- the prompt file ---------------------------------------------------------


def test_the_prompt_loads_from_its_file():
    """It lives in `prompts/extract.md` so it can be tuned as plain text."""
    from isb_events.extract import PROMPT_PATH

    assert PROMPT_PATH.exists()
    assert PROMPT_PATH.read_text().lstrip().startswith("<!--")


def test_the_editing_note_does_not_reach_the_model():
    from isb_events.extract import SYSTEM

    assert "<!--" not in SYSTEM
    assert SYSTEM.startswith("You extract event listings")


def test_the_rules_the_live_run_proved_necessary_are_present():
    """Each of these was added because a real listing went wrong without it.

    Rule 12 is the one that matters most: without the time format stated,
    Haiku 4.5 returned "5:00 PM", the parser dropped it, and three good
    listings were lost as "incomplete".
    """
    from isb_events.extract import SYSTEM

    assert "24-hour" in SYSTEM  # rule 12 — cost three listings when missing
    assert "EARLIEST time an attendee is expected" in SYSTEM  # rule 4
    assert "Islamabad occurrence" in SYSTEM  # rule 9
    assert "three months" in SYSTEM  # rule 13


# -- the model picks the link ------------------------------------------------


def test_the_model_can_choose_between_several_links():
    """A real forward carried a Strava deep link and the actual event page.

    Taking the first URL found picked Strava, which is a tracking link, not the
    event.
    """
    text = (
        "*IRU Tuesday Community Easy Run*\n"
        "*Strava* https://strava.app.link/2AqN8xxTr6b\n"
        "*IRU Web* https://iru-expedition-pk.web.app/event/1789457241824\n"
    )
    found = Extraction(
        is_event=True,
        title="IRU Community Easy Run",
        date="2026-09-15",
        start_time="17:20",
        event_url="https://iru-expedition-pk.web.app/event/1789457241824",
    )
    listing = Listing(text=text, source="whatsapp", url="https://strava.app.link/2AqN8xxTr6b")
    assert to_event(found, listing).url == "https://iru-expedition-pk.web.app/event/1789457241824"


def test_a_url_not_present_in_the_text_is_rejected():
    """Copied exactly, or not at all.

    A repaired, shortened or invented link would put something in front of
    readers that the organiser never wrote.
    """
    text = "Come along! https://real.example/event/1"
    for invented in (
        "https://real.example/event/2",  # plausible, still not theirs
        "https://evil.example/phish",
        "real.example/event/1",  # no scheme
        "javascript:alert(1)",
    ):
        found = Extraction(
            is_event=True, title="X", date="2026-09-15", start_time="18:00", event_url=invented
        )
        listing = Listing(text=text, source="whatsapp", url=None)
        assert to_event(found, listing).url is None, invented


def test_the_adapter_url_is_the_fallback():
    """No choice from the model means keep whatever the adapter found."""
    found = Extraction(is_event=True, title="X", date="2026-09-15", start_time="18:00")
    listing = Listing(text="see https://a.example/1", source="whatsapp", url="https://a.example/1")
    assert to_event(found, listing).url == "https://a.example/1"


def test_date_conflict_is_a_refusal_the_schema_allows():
    """The defence against a stale forward being re-dated into the future."""
    found = Extraction(is_event=False, decline_reason="date_conflict")
    assert to_event(found, _listing()) is None

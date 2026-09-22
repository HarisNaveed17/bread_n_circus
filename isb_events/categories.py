"""The category vocabulary, and the one place that decides what is in it.

Every event carries one of these or nothing at all. The vocabulary is fixed and
small on purpose: the bot offers it as a list of taps, and a reader has to be
able to guess what is behind a label before tapping it. That rules out a bucket
like "arts & culture", which is a superset of music, comedy, theatre *and*
talks — a model cannot hold a line it cannot state, and the same title then
drifts between buckets on consecutive cron runs.

**`mixed` is a real answer, not a failure code.** It means the classifier read
the listing and it genuinely fits none of the other seven — a farmers' market, a
travel talk. It is offered in the picker like any other category, because a
bucket nobody can open is exactly the wrong place to put the events the
classifier found hardest. `None` is the separate thing: not classified yet. The
bot's Mixed Bag view serves both, so nothing is ever invisible to a reader
browsing by category, but the store keeps them apart — otherwise "3 events fit
nothing" and "30 events failed to classify" look identical in `?health=`.

**Format beats subject**, which is the less obvious direction. "Raag Yaman for
Students" is `workshops`, not `music`: someone tapping Music wants a gig, and
six sittings of a students' raag class is the worse failure. The rule is stated
for the model in `prompts/extract.md` and repeated here because it is the thing
most likely to be relitigated.

`bot/intent.py` carries its own copy of these names — it cannot import this
module, since `vercel.json` keeps `isb_events` out of the function bundle.
`tests/test_bot.py` pins the two together.
"""

from __future__ import annotations

# Bumped when the vocabulary or the precedence rule changes. It is part of the
# classification cache key, so bumping it re-classifies everything rather than
# leaving a corpus half-labelled under two vocabularies.
VOCAB_VERSION = "1"

# Every value is askable. See the module docstring on why `mixed` is among them.
CATEGORIES = (
    "music",
    "comedy",
    "theatre_and_film",
    "talks",
    "workshops",
    "sports",
    "social",
    "mixed",
)

_VALID = frozenset(CATEGORIES)

# The Black Hole's WordPress taxonomy, as it appears in the `event_listing_category-<slug>`
# CSS class. Half of these are real categories and half are the venue's own
# programme-series names — "Baat Se Baat" is a talk series, "Bazm e Taareekh o
# Adab" a history-and-literature evening — which is why the scraper cannot just
# Title-case the slug and call it a category.
#
# Deliberately not exhaustive: the live store holds slugs this map has never
# seen, and the site can add one any week. An unmapped slug yields None and the
# event falls through to the model, which is the correct outcome — better an
# unclassified event the classifier picks up than a wrong label nobody notices.
BLACKHOLE_SLUGS = {
    "theater": "theatre_and_film",
    "theatre": "theatre_and_film",
    "movie-screening": "theatre_and_film",
    "book-launch": "theatre_and_film",
    "baat-se-baat": "talks",
    "fikr-o-falsafah": "talks",
    "bazm-e-taareekh-o-adab": "talks",
    "healthcare": "talks",
    "environmental-issues": "talks",
    "laughing-matters": "workshops",
}


def clean_category(value: str | None) -> str | None:
    """Return a vocabulary value, or None for anything else.

    The same discipline as `extract._clean_phone`: the model is told what may go
    in this field, and this is the check that does not depend on it having
    listened. It also guards the paths no model touches — a Black Hole slug, a
    backfill reading a column written under an older vocabulary.

    An out-of-vocabulary string is dropped rather than stored. Storing it would
    put a value in the column that no filter can ever match and that `?health=`
    would count as classified, so the event would be invisible to the picker
    while looking fine on the dashboard.
    """
    if not value:
        return None
    candidate = value.strip().lower().replace(" ", "_").replace("-", "_")
    return candidate if candidate in _VALID else None

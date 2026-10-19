"""Invented feeds and articles for the seeded instance.

Everything here is made up: publications, people, places, prose. The repo is
public and the UX loop commits crops of this instance, so nothing real may
appear. Pure data and pure functions; `server.py` renders it, `seed.py` loads it.

Each feed exercises something a journey's failure_modes name: a teaser-only
feed (trust-the-full-article), a prolific one that would swamp a ranked view,
a quiet one, a Hebrew one, entities in titles, very long names, and feeds that
fail in each way triage buckets (diagnose-a-quiet-feed).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


@dataclass(frozen=True)
class Feed:
    slug: str
    title: str
    category: str
    count: int                    # entries in the feed
    gap: timedelta                # time between posts
    silent_for: timedelta = timedelta(0)   # newest post is this old
    teaser: bool = False          # feed carries one sentence, the page carries the article
    paywall: bool = False         # article page shows a teaser unless logged in
    lang: str = "en"
    authors: tuple[str, ...] = ("Staff",)
    tags: tuple[str, ...] = ()
    fail: str = ""                # how the feed URL fails after subscribing ("" = never)
    priority: int = 2             # 1 Must Read, 2 Normal, 3 Low
    full_content: bool = False
    paused: bool = False
    image: bool = False
    audio: bool = False


FEEDS: tuple[Feed, ...] = (
    Feed("tidewater", "The Tidewater Ledger", "News", 24, 5 * HOUR,
         authors=("Mara Okonjo", "Pell Arden"), tags=("harbour", "council", "transport"),
         priority=1, image=True),
    Feed("kettle", "Kettle & Code", "Tech", 14, 2 * DAY, teaser=True, full_content=True,
         authors=("Ivo Brennick",), tags=("python", "databases", "tooling")),
    Feed("marginalia", "Marginalia Weekly", "Blogs", 10, 3 * DAY,
         authors=("Juniper Vale",),
         tags=("essays", "books", "memory", "cities", "walking", "maps", "letters", "rain"),
         priority=1, full_content=True),
    Feed("harbourstats", "Harbour Statistics Office", "Numbers", 12, 4 * DAY,
         authors=("Release desk",), tags=("labour", "prices", "population")),
    Feed("orchard", "Orchard Lane Diary", "Blogs", 9, 6 * DAY, image=True,
         authors=("Tamsin Reed",), tags=("garden",), full_content=True),
    Feed("noa", "הפנקס של נועה", "Blogs", 8, 3 * DAY, lang="he",
         authors=("נועה ברקת",), tags=("ירושלים", "ספרים")),
    Feed("nighttrain", "Night Train Notes", "Blogs", 6, 7 * DAY, audio=True,
         authors=("Osric Hale",), tags=("podcast",)),
    Feed("fenwick", "Quarterly Fenwick Review", "Must Read", 8, 4 * DAY, paywall=True,
         teaser=True, full_content=True, authors=("Edda Marsh", "Corin Pike"),
         tags=("politics", "culture"), priority=1),
    Feed("signal", "Signal & Noise Digest", "Tech", 60, 2 * HOUR,
         authors=("Digest bot",), tags=("links",), priority=3),
    Feed("cartographer", "The Slow Cartographer", "Blogs", 10, 4 * DAY, silent_for=26 * DAY,
         authors=("Wren Calloway",), tags=("maps",)),
    Feed("verbose",
         "The Long-Title Society for the Preservation of Extraordinarily Verbose Headings, "
         "Subtitles and Associated Marginal Notes", "Blogs", 4, 5 * DAY,
         authors=("Bartholomew Fennimore-Quillfeather III",), tags=("typography",)),
    Feed("apostrophe", "Apostrophe's Revenge", "Blogs", 6, 2 * DAY,
         authors=("Dot O'Malley",), tags=("grammar", "it's", "<tags>")),
    # Feeds that break after subscribing, one per triage cause.
    Feed("pantry", "Broken Pantry", "Blogs", 5, 3 * DAY, fail="500"),
    Feed("gatekeeper", "Gatekeeper Gazette", "News", 5, DAY, fail="403", priority=1),
    Feed("vanished", "Vanished Village News", "News", 5, 2 * DAY, fail="404"),
    Feed("homepage", "Lantern Street Homepage", "Blogs", 5, 2 * DAY, fail="html"),
    Feed("refused", "Refused Radio", "Blogs", 5, 2 * DAY, fail="refused"),
    Feed("nowhere", "Nowhere Network", "Tech", 5, 2 * DAY, fail="dns"),
    Feed("dormant", "Dormant Almanac", "Numbers", 5, 9 * DAY, paused=True),
)

# The one article whose page changes after its first fetch (see-how-an-article-changed).
CHANGING = ("tidewater", 1)

_SUBJECTS = ("The ferry timetable", "A council budget", "The night market", "An old lighthouse",
             "The tram extension", "A library fund", "The harbour wall", "A school garden",
             "The river survey", "A bakery co-op", "The bus depot", "A rooftop orchard")
_TURNS = ("is changing again", "faces a second review", "gets an unexpected reprieve",
          "was quietly rewritten", "draws a crowd", "runs out of road", "finds a new champion",
          "outlives its critics", "is late, but not by much", "opens to mixed reviews")
_SENTENCES = (
    "Residents gathered on a wet Tuesday evening to hear the details read aloud.",
    "The figures, published without much ceremony, tell a more careful story than the headline.",
    "Nobody involved expected the matter to take this long, least of all the committee.",
    "A spokesperson said the plan would be revisited in the spring, weather permitting.",
    "Critics argue that the consultation was too short; supporters say it was long enough.",
    "The original proposal ran to forty pages, most of them about drainage.",
    "In the end the decision turned on a single paragraph near the back.",
    "Local traders were divided, though most agreed the signage needed work.",
    "It is the third time in a decade that the question has come up.",
    "A small exhibition of the early drawings is on show until the end of the month.",
    "The engineers' report, released on Friday, is cautious but not alarmed.",
    "Several readers wrote in to point out that the map was upside down.",
)
_HE_TITLES = ("טיול בוקר בשכונה", "על ספרים שלא סיימתי", "מכתב לחברה ותיקה",
              "הגשם הראשון של השנה", "שוק ביום שישי", "רשימות מהרכבת",
              "גינה על המרפסת", "מה למדתי השבוע")
_HE_SENTENCES = ("הבוקר יצאתי מוקדם, לפני שהעיר התעוררה.",
                 "בדרך עברתי ליד חנות הספרים הישנה, שעדיין פתוחה.",
                 "המחיר עלה ל־12 ש״ח, אבל הקפה עדיין טוב.",
                 "חשבתי על השאלה הזאת כל הדרך הביתה.",
                 "בסוף החלטתי לכתוב על זה כאן.")


def _rng(feed: Feed, i: int) -> random.Random:
    return random.Random(f"{feed.slug}:{i}")


def entry_title(feed: Feed, i: int) -> str:
    if feed.lang == "he":
        return _HE_TITLES[i % len(_HE_TITLES)]
    r = _rng(feed, i)
    base = f"{r.choice(_SUBJECTS)} {r.choice(_TURNS)}"
    if feed.slug == "apostrophe":
        return f"It's \"{base.lower()}\" & other <small> problems"
    if feed.slug == "verbose":
        return (f"{base}: a considered, unhurried and frankly exhaustive account, with "
                "footnotes, appendices and a short note on sources")
    if feed.slug == "harbourstats":
        return f"Monthly release {i + 1}: prices up {r.randint(1, 9)}.{r.randint(0, 9)}% on the year"
    return base


def paragraphs(feed: Feed, i: int, n: int = 5) -> list[str]:
    r = _rng(feed, i)
    pool = _HE_SENTENCES if feed.lang == "he" else _SENTENCES
    return [" ".join(r.sample(pool, 3)) for _ in range(n)]


def published(feed: Feed, i: int, now: datetime) -> datetime:
    """Entry 0 is the newest."""
    return now - feed.silent_for - feed.gap * i - timedelta(minutes=7 * (len(feed.slug) % 5))


def author(feed: Feed, i: int) -> str:
    return feed.authors[i % len(feed.authors)]


def entry_tags(feed: Feed, i: int) -> list[str]:
    if not feed.tags:
        return []
    if len(feed.tags) > 5:
        return list(feed.tags)          # the many-tags case, deliberately
    return [feed.tags[i % len(feed.tags)]]


def by_slug(slug: str) -> Feed:
    return next(f for f in FEEDS if f.slug == slug)

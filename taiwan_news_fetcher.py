"""
taiwan_news_fetcher.py

Fetches RSS headlines from Taiwanese news sources, translates them to English
where needed, categorizes them, and writes output to docs/taiwan_news.json.

Categories: Diplomacy, Military, Energy, Economy, Local Events
Max 20 stories per category, no story older than 7 days.
Replaces oldest entries when new stories are found.
No API keys required — uses deep-translator (Google Translate free tier).

Source notes:
  - taiwannews.com.tw: No confirmed public RSS endpoint exists; replaced with
    Focus Taiwan cross-strait feed (focustaiwan.tw/rss/cross-strait.xml), the
    English-language service of Taiwan's national wire agency (CNA).
  - All other requested sources confirmed active.
  - Liberty Times (news.ltn.com.tw) publishes in Traditional Chinese;
    stories are auto-translated to English via deep-translator.
"""

import json
import os
import re
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests
from deep_translator import GoogleTranslator

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

COUNTRY = "taiwan"
OUTPUT_DIR = Path("docs")
OUTPUT_FILE = OUTPUT_DIR / f"{COUNTRY}_news.json"

MAX_PER_CATEGORY = 20
MAX_AGE_DAYS = 7

CATEGORIES = ["Diplomacy", "Military", "Energy", "Economy", "Local Events"]

# ---------------------------------------------------------------------------
# RSS Sources
# ---------------------------------------------------------------------------

RSS_SOURCES = [
    {
        "name": "Taipei Times",
        "urls": [
            "https://www.taipeitimes.com/xml/index.rss",
        ],
        "lang": "en",
    },
    {
        # Replaces taiwannews.com.tw (no public RSS endpoint confirmed).
        # Focus Taiwan is the English service of CNA, Taiwan's national
        # wire agency — highly authoritative and actively maintained.
        "name": "Focus Taiwan (CNA)",
        "urls": [
            "https://focustaiwan.tw/rss",
            "https://focustaiwan.tw/rss/politics.xml",
            "https://focustaiwan.tw/rss/cross-strait.xml",
            "https://focustaiwan.tw/rss/business.xml",
            "https://focustaiwan.tw/rss/sci-tech.xml",
            "https://focustaiwan.tw/rss/society.xml",
        ],
        "lang": "en",
    },
    {
        "name": "Taiwan Today",
        "urls": [
            "https://api.taiwantoday.tw/en/rss.php",
        ],
        "lang": "en",
    },
    {
        # Liberty Times — one of Taiwan's highest-circulation newspapers.
        # Published in Traditional Chinese; translated automatically below.
        "name": "Liberty Times",
        "urls": [
            "https://news.ltn.com.tw/rss/all.xml",
            "https://news.ltn.com.tw/rss/politics.xml",
            "https://news.ltn.com.tw/rss/business.xml",
            "https://news.ltn.com.tw/rss/world.xml",
        ],
        "lang": "zh-TW",
    },
]

# ---------------------------------------------------------------------------
# Category keyword rules (applied after translation to English)
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS = {
    "Diplomacy": [
        "diplomacy", "diplomatic", "foreign minister", "ambassador", "embassy",
        "treaty", "bilateral", "multilateral", "united nations", "un ",
        "g7", "g20", "summit", "foreign affairs", "state department",
        "ministry of foreign affairs", "mofa", "cross-strait", "beijing",
        "prc", "china relations", "us-taiwan", "taiwan strait", "ally",
        "recognition", "de facto", "consulate", "visa", "trade pact",
        "international", "foreign policy", "state visit", "itf",
        "pacific", "indo-pacific",
    ],
    "Military": [
        "military", "armed forces", "army", "navy", "air force", "defense",
        "missile", "pla", "people's liberation army", "war games", "drills",
        "exercise", "troops", "weapons", "warship", "fighter jet",
        "defense budget", "conscription", "reserve", "national guard",
        "strait", "incursion", "adiz", "air defense", "invasion",
        "deterrence", "security", "coastguard", "submarine", "carrier",
        "combat", "munitions", "artillery", "soldier", "general",
    ],
    "Energy": [
        "energy", "nuclear", "power plant", "electricity", "renewable",
        "solar", "wind power", "lng", "liquefied natural gas", "coal",
        "fossil fuel", "carbon", "emissions", "climate", "tsmc power",
        "grid", "blackout", "power shortage", "taipower", "cpc",
        "oil", "gas", "fuel", "semiconductor energy", "green energy",
        "net zero", "hydrogen", "offshore wind", "photovoltaic",
    ],
    "Economy": [
        "economy", "economic", "gdp", "inflation", "interest rate",
        "central bank", "finance", "budget", "fiscal", "tax", "trade",
        "exports", "imports", "investment", "market", "stock", "taiex",
        "semiconductor", "tsmc", "chip", "supply chain", "manufacturing",
        "industry", "agribusiness", "tariff", "wto", "fta", "deficit",
        "surplus", "employment", "unemployment", "wages", "growth",
        "recession", "nt dollar", "currency", "foreign exchange",
        "ministry of finance", "ministry of economic affairs",
    ],
    "Local Events": [
        "city", "county", "mayor", "local", "taipei", "kaohsiung",
        "taichung", "tainan", "hsinchu", "taoyuan", "pingtung",
        "keelung", "yilan", "hualien", "taitung", "nantou", "chiayi",
        "flood", "earthquake", "typhoon", "landslide", "fire",
        "protest", "strike", "election", "education", "health",
        "hospital", "culture", "festival", "community", "infrastructure",
        "transportation", "high speed rail", "mrt", "road", "bridge",
        "aboriginal", "indigenous", "social welfare",
    ],
}


def classify(title: str, description: str) -> str:
    """Return the best matching category or 'Local Events' as fallback."""
    text = (title + " " + (description or "")).lower()
    scores = {cat: 0 for cat in CATEGORIES}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                scores[cat] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "Local Events"


# ---------------------------------------------------------------------------
# Translation helper
# ---------------------------------------------------------------------------

_translator = GoogleTranslator(source="auto", target="en")


def safe_translate(text: str, source_lang: str = "auto") -> str:
    """Translate text to English; return original on failure."""
    if not text or not text.strip():
        return text
    # Skip translation if clearly already English
    if source_lang == "en":
        return text
    latin_common = re.compile(r"\b(the|and|is|in|of|to|a|for|on|that|with)\b", re.I)
    if len(latin_common.findall(text)) >= 3:
        return text
    try:
        translator = GoogleTranslator(source=source_lang, target="en")
        result = translator.translate(text[:4900])
        return result if result else text
    except Exception as exc:
        log.warning("Translation failed for '%s…': %s", text[:60], exc)
        return text


# ---------------------------------------------------------------------------
# Feed fetching
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; StratagemdriveTaiwanNewsBot/1.0; "
        "+https://stratagemdrive.github.io)"
    )
}


def fetch_feed(url: str) -> list:
    """Fetch and parse a single RSS/Atom feed URL; return raw entries."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        log.info("  Fetched %d entries from %s", len(feed.entries), url)
        return feed.entries
    except Exception as exc:
        log.warning("  Could not fetch %s: %s", url, exc)
        return []


def parse_published(entry) -> datetime | None:
    """Extract a timezone-aware published datetime from a feed entry."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for attr in ("published", "updated"):
        s = getattr(entry, attr, None)
        if s:
            try:
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(s)
            except Exception:
                pass
    return None


def entry_to_story(entry, source_name: str, source_lang: str) -> dict | None:
    """Convert a feed entry to a story dict; return None if unusable."""
    title_raw = getattr(entry, "title", "") or ""
    desc_raw = getattr(entry, "summary", "") or ""
    desc_clean = re.sub(r"<[^>]+>", " ", desc_raw).strip()
    url = getattr(entry, "link", "") or ""

    published_dt = parse_published(entry)
    if not published_dt:
        published_dt = datetime.now(timezone.utc)

    age = datetime.now(timezone.utc) - published_dt
    if age > timedelta(days=MAX_AGE_DAYS):
        return None

    title_en = safe_translate(title_raw, source_lang)
    desc_en = safe_translate(desc_clean[:300], source_lang) if desc_clean else ""

    category = classify(title_en, desc_en)

    return {
        "title": title_en.strip(),
        "source": source_name,
        "url": url.strip(),
        "published_date": published_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "category": category,
    }


# ---------------------------------------------------------------------------
# JSON store management
# ---------------------------------------------------------------------------

def load_existing() -> dict:
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "stories" in data:
                return data
        except Exception as exc:
            log.warning("Could not load existing JSON: %s", exc)
    return {"stories": [], "last_updated": ""}


def save_output(data: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("Saved %d stories to %s", len(data["stories"]), OUTPUT_FILE)


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def merge_stories(existing_stories: list, new_stories: list) -> list:
    """
    Merge new stories into existing ones per category:
    - Drop stories older than MAX_AGE_DAYS
    - Deduplicate by URL
    - Keep up to MAX_PER_CATEGORY per category (newest first)
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)

    by_url: dict[str, dict] = {}
    for s in existing_stories:
        url = s.get("url", "")
        if url:
            by_url[url] = s
    for s in new_stories:
        url = s.get("url", "")
        if url:
            by_url[url] = s

    fresh = []
    for s in by_url.values():
        try:
            pub = datetime.strptime(
                s["published_date"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            if pub >= cutoff:
                fresh.append(s)
        except Exception:
            pass

    by_cat: dict[str, list] = {cat: [] for cat in CATEGORIES}
    for s in fresh:
        cat = s.get("category", "Local Events")
        if cat not in by_cat:
            cat = "Local Events"
        by_cat[cat].append(s)

    result = []
    for cat in CATEGORIES:
        entries = sorted(
            by_cat[cat],
            key=lambda x: x.get("published_date", ""),
            reverse=True,
        )
        result.extend(entries[:MAX_PER_CATEGORY])

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=== Taiwan News Fetcher starting ===")
    existing_data = load_existing()
    existing_stories = existing_data.get("stories", [])

    all_new: list[dict] = []

    for source in RSS_SOURCES:
        source_name = source["name"]
        source_lang = source.get("lang", "auto")
        log.info("Processing source: %s (lang: %s)", source_name, source_lang)
        for url in source["urls"]:
            entries = fetch_feed(url)
            for entry in entries:
                story = entry_to_story(entry, source_name, source_lang)
                if story:
                    all_new.append(story)
            time.sleep(1)

    log.info("Collected %d candidate new stories", len(all_new))

    merged = merge_stories(existing_stories, all_new)

    for cat in CATEGORIES:
        count = sum(1 for s in merged if s.get("category") == cat)
        log.info("  %-15s: %d stories", cat, count)

    output = {
        "country": COUNTRY,
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stories": merged,
    }

    save_output(output)
    log.info("=== Done ===")


if __name__ == "__main__":
    main()

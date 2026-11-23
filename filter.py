"""Stage 1: deterministic, zero-API pre-filter and transparent ranking."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import config
from fetcher import Item

log = logging.getLogger(__name__)

TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_")
TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
    "ref_src",
    "ref_url",
    "source",
    "spm",
    "yclid",
    "_hsenc",
    "_hsmi",
    "mkt_tok",
    "trk",
    "sc_campaign",
    "sc_channel",
    "sc_content",
    "sc_medium",
    "sc_outcome",
    "tab",
}

LISTICLE_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in config.LISTICLE_PATTERNS)


@dataclass
class FilterResult:
    items: list[Item] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #
def normalize_url(url: str) -> str:
    if not url:
        return ""
    raw = url.strip()
    if not raw.lower().startswith(("http://", "https://")):
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    host = (parts.netloc or "").lower()
    if not host:
        return ""
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or "/"

    if host.endswith("arxiv.org"):
        match = re.match(r"^/abs/([^/]+?)(?:v\d+)?$", path)
        if match:
            path = f"/abs/{match.group(1)}"
    if host == "github.com":
        path = path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if path == "":
            path = "/"

    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in TRACKING_KEYS or lowered.startswith(TRACKING_PREFIXES):
            continue
        query_pairs.append((key, value))
    query_pairs.sort()
    query = urlencode(query_pairs)

    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit(("https", host, path or "/", query, ""))


def content_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).netloc or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _host_in(host: str, domains: tuple[str, ...] | set[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


# --------------------------------------------------------------------------- #
# Classification helpers
# --------------------------------------------------------------------------- #
def is_listicle(title: str) -> bool:
    return any(pattern.search(title or "") for pattern in LISTICLE_RE)


def classify_ai(item: Item) -> bool:
    haystack = f"{item.title} {item.summary}".lower()
    if any(keyword in haystack for keyword in config.AI_STRONG_KEYWORDS):
        return True
    if item.meta.get("ai_feed") and any(
        keyword in haystack for keyword in config.AI_KEYWORDS
    ):
        return True
    return False


def _category(item: Item) -> str:
    return "AI Security" if item.ai_related else "Classic Security"


def _age_hours(item: Item, now: datetime) -> float:
    if item.published_at is None:
        return item.max_age_hours * 0.5
    published = item.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    age = (now - published).total_seconds() / 3600
    if age < 0:
        return 0.0
    return age


# --------------------------------------------------------------------------- #
# Scoring (every component is logged)
# --------------------------------------------------------------------------- #
def _engagement_score(item: Item) -> float:
    source = item.source_type
    if source == "hn":
        points = float(item.engagement.get("points") or 0)
        comments = float(item.engagement.get("comments") or 0)
        return math.log10(points + 1) * 2.0 + min(comments * 0.05, 0.8)
    if source == "github":
        stars = float(item.engagement.get("stars") or 0)
        age_days = float(item.meta.get("age_days") or 30)
        velocity = stars / max(age_days, 0.5)
        return math.log10(velocity + 1) * 1.2 + math.log10(stars + 1) * 0.6
    if source in ("bluesky", "mastodon"):
        likes = float(item.engagement.get("likes") or item.engagement.get("favourites") or 0)
        reposts = float(item.engagement.get("reposts") or item.engagement.get("reblogs") or 0)
        return math.log10(likes + 2 * reposts + 1) * 1.6
    if source == "cisa_kev":
        return 1.5
    if source == "reddit":
        ups = float(item.engagement.get("ups") or 0)
        return math.log10(ups + 1) * 1.2
    if source == "arxiv":
        return 1.0
    return 0.6


def score_item(item: Item, now: datetime) -> tuple[float, dict[str, float]]:
    parts: dict[str, float] = {}
    base = config.SOURCE_WEIGHTS.get(item.source_type, 2.0)
    if item.source_type == "rss":
        base = max(base, float(item.meta.get("weight") or base))
    parts["source"] = round(base, 3)

    haystack = f"{item.title}\n{item.summary}".lower()
    keyword_total = sum(
        weight
        for keyword, weight in config.KEYWORD_WEIGHTS.items()
        if keyword in haystack
    )
    parts["keywords"] = round(min(keyword_total, config.SCORE_KEYWORD_MAX), 3)
    parts["engagement"] = round(min(_engagement_score(item), config.SCORE_ENGAGEMENT_MAX), 3)

    horizon = max(item.max_age_hours, 1)
    age = _age_hours(item, now)
    recency_fraction = max(0.0, min(1.0, 1.0 - age / horizon))
    parts["recency"] = round(recency_fraction * config.SCORE_RECENCY_MAX, 3)

    if item.ai_related:
        parts["ai_bonus"] = config.SCORE_AI_BONUS
    if any(k in haystack for k in ("agentic", "mcp", "autonomous agent")):
        parts["agentic_bonus"] = config.SCORE_AGENTIC_BONUS
    host = _host(item.url)
    if _host_in(host, config.PREFERRED_DOMAINS):
        parts["preferred_domain"] = config.SCORE_PREFERRED_DOMAIN

    total = round(sum(parts.values()), 3)
    return total, parts


# --------------------------------------------------------------------------- #
# Stage 1 pipeline
# --------------------------------------------------------------------------- #
def prefilter(
    items: list[Item], sent_hashes: set[str], now: datetime
) -> FilterResult:
    counters: dict[str, int] = {
        "received": len(items),
        "duplicate": 0,
        "invalid_url": 0,
        "blocked_domain": 0,
        "stale": 0,
        "listicle": 0,
        "empty": 0,
        "kept": 0,
    }
    selected: list[Item] = []
    seen_hashes: set[str] = set()

    for item in items:
        url = normalize_url(item.url)
        if not url:
            counters["invalid_url"] += 1
            continue
        item.url = url
        item.hash = content_hash(url)

        if item.hash in sent_hashes or item.hash in seen_hashes:
            counters["duplicate"] += 1
            continue

        host = _host(url)
        if item.source_type in ("hn", "reddit", "bluesky", "mastodon") and _host_in(
            host, config.SOCIAL_DOMAIN_BLOCKLIST
        ):
            counters["blocked_domain"] += 1
            continue

        age = _age_hours(item, now)
        max_age_hours = item.max_age_hours
        if config.NEWS_TEST_MODE:
            max_age_hours = max(
                max_age_hours, config.NEWS_TEST_LOOKBACK_DAYS * 24
            )
        if age > max_age_hours:
            counters["stale"] += 1
            continue

        if is_listicle(item.title):
            counters["listicle"] += 1
            continue

        if len(item.title.strip()) < 8 or len((item.summary or "") + (item.content or "")) < 60:
            counters["empty"] += 1
            continue

        seen_hashes.add(item.hash)
        item.ai_related = classify_ai(item)
        item.category = _category(item)
        item.score, item.score_parts = score_item(item, now)
        selected.append(item)

    selected.sort(key=lambda entry: entry.score, reverse=True)
    kept: list[Item] = []
    per_source: dict[str, int] = {}
    for item in selected:
        if len(kept) >= config.MAX_CANDIDATES:
            break
        count = per_source.get(item.source_label, 0)
        if count >= config.MAX_PER_SOURCE_CANDIDATES:
            continue
        per_source[item.source_label] = count + 1
        kept.append(item)
    for index, item in enumerate(kept):
        item.id = index
    counters["kept"] = len(kept)

    log.info(
        "stage1 received=%d duplicate=%d invalid_url=%d blocked_domain=%d "
        "stale=%d listicle=%d empty=%d kept=%d",
        counters["received"],
        counters["duplicate"],
        counters["invalid_url"],
        counters["blocked_domain"],
        counters["stale"],
        counters["listicle"],
        counters["empty"],
        counters["kept"],
    )
    for item in kept:
        log.info(
            "candidate score=%.3f parts=%s source=%s title=%s",
            item.score,
            item.score_parts,
            item.source_label,
            item.title[:90],
        )
    return FilterResult(items=kept, counters=counters)


def truncate_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = window.rfind("\n\n")
    if cut < limit * 0.6:
        cut = window.rfind(". ")
    if cut < limit * 0.6:
        cut = limit
    return text[:cut].rstrip() + "\n…"


def _min_chars(item: Item) -> int:
    if item.source_type == "cisa_kev":
        return 0
    if item.is_repo:
        return config.MIN_REPO_CHARS
    if item.source_type == "arxiv":
        return config.MIN_ARXIV_CHARS
    return config.MIN_ARTICLE_CHARS


def finalize_candidates(items: list[Item]) -> list[Item]:
    out: list[Item] = []
    dropped = 0
    for item in items:
        item.content = truncate_text(
            item.content or item.summary, config.FULLTEXT_MAX_CHARS
        )
        minimum = _min_chars(item)
        if len(item.content) < minimum:
            dropped += 1
            log.info(
                "drop_short source=%s chars=%d min=%d title=%s",
                item.source_label,
                len(item.content),
                minimum,
                item.title[:80],
            )
            continue
        out.append(item)
    if dropped:
        log.info("stage1b dropped_short=%d remaining=%d", dropped, len(out))
    return out

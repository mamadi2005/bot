"""Data sources, isolated failures, and full-text extraction."""

from __future__ import annotations

import html as html_lib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

try:
    import trafilatura
except ImportError:  # pragma: no cover - dependency is pinned in requirements
    trafilatura = None

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
HN_API = "https://hn.algolia.com/api/v1/search_by_date"
BLUESKY_API = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
ARXIV_API = "https://export.arxiv.org/api/query"
CISA_KEV_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
)
REDDIT_BASE = "https://www.reddit.com"


@dataclass
class Item:
    title: str
    url: str
    source_type: str
    source_label: str
    summary: str = ""
    content: str = ""
    published_at: datetime | None = None
    engagement: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    is_repo: bool = False
    ai_related: bool = False
    max_age_hours: int = 24
    hash: str = ""
    score: float = 0.0
    score_parts: dict[str, float] = field(default_factory=dict)
    id: int = 0
    category: str = ""
    summary_html: str = ""


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #
_thread_local = threading.local()


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=config.HTTP_MAX_RETRIES,
        connect=config.HTTP_MAX_RETRIES,
        read=config.HTTP_MAX_RETRIES,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": config.USER_AGENT,
            "Accept-Language": "en-US,en;q=0.8",
        }
    )
    return session


def _session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = _build_session()
        _thread_local.session = session
    return session


def _get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int | None = None,
) -> Any:
    try:
        response = _session().get(
            url,
            params=params,
            headers=headers,
            timeout=timeout or config.REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.debug("HTTP error url=%s err=%s", _short_url(url), exc.__class__.__name__)
        return None
    if response.status_code != 200:
        log.debug(
            "HTTP status url=%s status=%s", _short_url(url), response.status_code
        )
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _get_bytes(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int | None = None,
) -> requests.Response | None:
    try:
        response = _session().get(
            url,
            params=params,
            headers=headers,
            timeout=timeout or config.REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.debug("HTTP error url=%s err=%s", _short_url(url), exc.__class__.__name__)
        return None
    if response.status_code != 200:
        log.debug(
            "HTTP status url=%s status=%s", _short_url(url), response.status_code
        )
        return None
    return response


def _github_headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if config.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    return headers


def _short_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        return parts.netloc or url[:60]
    except ValueError:
        return url[:60]


# --------------------------------------------------------------------------- #
# Small parsing helpers
# --------------------------------------------------------------------------- #
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _from_struct(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime(*value[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _from_ts(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _clean(text: str) -> str:
    if not text:
        return ""
    text = html_lib.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _html_to_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    try:
        soup = BeautifulSoup(raw_html, "html.parser")
        return _clean(soup.get_text("\n"))
    except Exception:
        return _clean(re.sub(r"<[^>]+>", " ", raw_html))


def _bs4_extract(raw_html: str) -> str:
    try:
        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup(
            ["script", "style", "nav", "footer", "aside", "header", "form", "noscript"]
        ):
            tag.decompose()
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = "\n\n".join(p for p in paragraphs if p)
        return _clean(text)
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Full-text extraction (runs after deterministic filtering)
# --------------------------------------------------------------------------- #
def _extract_article(url: str) -> str:
    response = _get_bytes(
        url,
        headers={"Accept": "text/html,application/xhtml+xml"},
        timeout=config.REQUEST_TIMEOUT,
    )
    if response is None:
        return ""
    content_type = (response.headers.get("content-type") or "").lower()
    if "pdf" in content_type or "octet-stream" in content_type:
        log.debug("skip non-html content type=%s host=%s", content_type, _short_url(url))
        return ""
    if content_type and "html" not in content_type and "xml" not in content_type:
        return ""
    page = response.text
    if len(page) < 400:
        return ""

    text = ""
    if trafilatura is not None:
        try:
            text = (
                trafilatura.extract(
                    page,
                    url=url,
                    include_comments=False,
                    include_tables=False,
                    favor_recall=True,
                )
                or ""
            )
        except TypeError:
            try:
                text = trafilatura.extract(page) or ""
            except Exception:
                text = ""
        except Exception as exc:
            log.debug("trafilatura failed host=%s err=%s", _short_url(url), exc)
            text = ""
    if len(text) < 200:
        text = _bs4_extract(page)
    return _clean(text)


def _fetch_repo_readme(full_name: str) -> str:
    response = _get_bytes(
        f"{GITHUB_API}/repos/{full_name}/readme",
        headers=_github_headers("application/vnd.github.raw"),
    )
    if response is not None and len(response.text.strip()) > 40:
        return response.text
    for name in ("README.md", "readme.md", "README.rst"):
        fallback = _get_bytes(
            f"https://raw.githubusercontent.com/{full_name}/HEAD/{name}",
            headers={"Accept": "text/plain"},
        )
        if fallback is not None and fallback.text.strip():
            return fallback.text
    return ""


def _load_content(item: Item) -> str:
    if item.source_type == "cisa_kev":
        return item.content or item.summary
    if item.is_repo:
        full_name = str(item.meta.get("full_name") or "")
        if not full_name:
            return ""
        readme = _fetch_repo_readme(full_name)
        if 0 < len(readme) < 400:
            item.meta["readme_only"] = True
        return readme
    text = _extract_article(item.url)
    if item.source_type == "arxiv" and len(text) < config.MIN_ARXIV_CHARS:
        text = _clean(f"{text}\n\n{item.summary}") if text else item.summary
    return text


def enrich_full_text(items: list[Item]) -> None:
    targets = items[: config.FULLTEXT_BUDGET]
    if not targets:
        return
    extracted = 0
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=config.FULLTEXT_WORKERS) as pool:
        futures = {pool.submit(_load_content, item): item for item in targets}
        for future in as_completed(futures):
            item = futures[future]
            try:
                text = future.result()
            except Exception as exc:
                log.debug("extract failed url=%s err=%s", _short_url(item.url), exc)
                text = ""
            if text and len(text) > 80:
                item.content = text
                extracted += 1
            elif not item.content:
                item.content = item.summary
    log.info(
        "full_text targets=%d extracted=%d elapsed=%.1fs",
        len(targets),
        extracted,
        time.monotonic() - started,
    )


# --------------------------------------------------------------------------- #
# Source A - GitHub search
# --------------------------------------------------------------------------- #
def _repo_relevant(full_name: str, description: str) -> bool:
    """Topics are excluded on purpose: spam repos stuff them with keywords."""
    raw = f"{full_name} {description}".lower()
    haystack = raw.replace("-", " ").replace("_", " ")
    if any(term in raw or term in haystack for term in config.REPO_DENY_TERMS):
        return False
    return any(
        keyword.lower().replace("-", " ").replace("_", " ") in haystack
        for keyword in config.GITHUB_KEYWORDS
    )


def fetch_github() -> list[Item]:
    out: list[Item] = []
    seen: set[str] = set()
    skipped = 0
    since = (_utcnow() - timedelta(days=config.GITHUB_WINDOW_DAYS)).strftime("%Y-%m-%d")
    for keyword in config.GITHUB_KEYWORDS:
        params = {
            "q": (
                f"{keyword} in:name,description "
                f"created:>={since} stars:>={config.GITHUB_MIN_STARS}"
            ),
            "sort": "stars",
            "order": "desc",
            "per_page": config.GITHUB_PER_KEYWORD,
        }
        data = _get_json(
            f"{GITHUB_API}/search/repositories",
            params=params,
            headers=_github_headers(),
        )
        if not data or not isinstance(data, dict):
            continue
        for repo in data.get("items", []):
            full_name = repo.get("full_name")
            if not full_name or full_name in seen:
                continue
            description = _clean(repo.get("description") or "")
            topics = repo.get("topics") or []
            if not _repo_relevant(full_name, description):
                skipped += 1
                continue
            seen.add(full_name)
            created = _parse_iso(repo.get("created_at"))
            age_days = 30.0
            if created:
                age_days = max((_utcnow() - created).total_seconds() / 86400, 0.1)
            title = f"{full_name}: {description}" if description else full_name
            out.append(
                Item(
                    title=title,
                    url=repo.get("html_url") or "",
                    source_type="github",
                    source_label="GitHub",
                    summary=description,
                    published_at=created,
                    is_repo=True,
                    max_age_hours=config.REPO_MAX_AGE_HOURS,
                    engagement={"stars": int(repo.get("stargazers_count") or 0)},
                    meta={
                        "full_name": full_name,
                        "stars": int(repo.get("stargazers_count") or 0),
                        "forks": int(repo.get("forks_count") or 0),
                        "language": repo.get("language"),
                        "created_at": repo.get("created_at"),
                        "age_days": round(age_days, 2),
                        "owner": (repo.get("owner") or {}).get("login"),
                        "topics": repo.get("topics") or [],
                    },
                )
            )
    if skipped:
        log.info("github keyword_miss=%d kept=%d", skipped, len(out))
    _enrich_github_meta(out)
    return out


def _enrich_github_meta(items: list[Item]) -> None:
    budget = 20
    for item in sorted(items, key=lambda i: i.meta.get("stars", 0), reverse=True):
        if budget <= 0:
            break
        if item.meta.get("stars", 0) < 25:
            continue
        owner = item.meta.get("owner")
        if owner and budget > 0:
            budget -= 1
            user = _get_json(
                f"{GITHUB_API}/users/{owner}", headers=_github_headers(), timeout=15
            )
            if isinstance(user, dict):
                owner_created = _parse_iso(user.get("created_at"))
                if owner_created:
                    item.meta["owner_age_days"] = int(
                        (_utcnow() - owner_created).total_seconds() / 86400
                    )
                item.meta["owner_type"] = user.get("type")
        full_name = item.meta.get("full_name")
        if full_name and budget > 0:
            budget -= 1
            releases = _get_json(
                f"{GITHUB_API}/repos/{full_name}/releases",
                params={"per_page": 1},
                headers=_github_headers(),
                timeout=15,
            )
            item.meta["has_releases"] = bool(releases)
        item.meta["suspicious"] = _github_suspicious(item)
        if item.meta["suspicious"]:
            log.debug("suspicious repo flagged full_name=%s", item.meta.get("full_name"))


def _github_suspicious(item: Item) -> bool:
    stars = int(item.meta.get("stars") or 0)
    age_days = item.meta.get("age_days")
    owner_age = item.meta.get("owner_age_days")
    if age_days is not None and age_days <= 7 and stars >= 50:
        return True
    if owner_age is not None and owner_age < 30:
        return True
    return False


# --------------------------------------------------------------------------- #
# Source B - Hacker News (Algolia)
# --------------------------------------------------------------------------- #
def fetch_hn() -> list[Item]:
    out: list[Item] = []
    seen: set[str] = set()
    since_ts = int(
        (_utcnow() - timedelta(hours=config.SOCIAL_MAX_AGE_HOURS)).timestamp()
    )
    for query in config.HN_QUERIES:
        params = {
            "query": query,
            "tags": "story",
            "numericFilters": (
                f"points>{config.HN_MIN_POINTS},created_at_i>{since_ts}"
            ),
            "hitsPerPage": config.HN_PER_QUERY,
        }
        data = _get_json(HN_API, params=params)
        if not isinstance(data, dict):
            continue
        for hit in data.get("hits", []):
            object_id = hit.get("objectID")
            if not object_id or object_id in seen:
                continue
            seen.add(object_id)
            title = _clean(html_lib.unescape(hit.get("title") or ""))
            if not title:
                continue
            story_text = _html_to_text(hit.get("story_text") or "")
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
            if not hit.get("url") and len(story_text) < 300:
                continue
            out.append(
                Item(
                    title=title,
                    url=url,
                    source_type="hn",
                    source_label="Hacker News",
                    summary=story_text[:600],
                    published_at=_from_ts(hit.get("created_at_i")),
                    max_age_hours=config.SOCIAL_MAX_AGE_HOURS,
                    engagement={
                        "points": int(hit.get("points") or 0),
                        "comments": int(hit.get("num_comments") or 0),
                    },
                    meta={
                        "hn_id": object_id,
                        "hn_url": f"https://news.ycombinator.com/item?id={object_id}",
                    },
                )
            )
    return out


# --------------------------------------------------------------------------- #
# Source C - Bluesky public search
# --------------------------------------------------------------------------- #
def fetch_bluesky() -> list[Item]:
    out: list[Item] = []
    seen: set[str] = set()
    for query in config.BLUESKY_QUERIES:
        data = _get_json(
            BLUESKY_API,
            params={"q": query, "sort": "top", "limit": config.BLUESKY_PER_QUERY},
        )
        if not isinstance(data, dict):
            continue
        for post in data.get("posts", []):
            uri = post.get("uri") or ""
            if not uri or uri in seen:
                continue
            record = post.get("record") or {}
            text = _clean(record.get("text") or "")
            if len(text) < 60:
                continue
            handle = (post.get("author") or {}).get("handle") or ""
            rkey = uri.rsplit("/", 1)[-1]
            social_url = (
                f"https://bsky.app/profile/{handle}/post/{rkey}"
                if handle and rkey
                else ""
            )
            external = ((post.get("embed") or {}).get("external") or {}).get("uri")
            url = external if isinstance(external, str) and external.startswith("http") else social_url
            if not url:
                continue
            seen.add(uri)
            out.append(
                Item(
                    title=text.split("\n", 1)[0][:160],
                    url=url,
                    source_type="bluesky",
                    source_label="Bluesky",
                    summary=text[:600],
                    published_at=_parse_iso(record.get("createdAt")),
                    max_age_hours=config.SOCIAL_MAX_AGE_HOURS,
                    engagement={
                        "likes": int(post.get("likeCount") or 0),
                        "reposts": int(post.get("repostCount") or 0),
                    },
                    meta={"social_url": social_url, "handle": handle},
                )
            )
    return out


# --------------------------------------------------------------------------- #
# Source D - Mastodon (infosec.exchange hashtags)
# --------------------------------------------------------------------------- #
def fetch_mastodon() -> list[Item]:
    out: list[Item] = []
    seen: set[str] = set()
    hosts = list(config.MASTODON_HOSTS)
    for tag in config.MASTODON_TAGS:
        data = None
        host = ""
        for candidate in hosts:
            data = _get_json(
                f"{candidate}/api/v1/timelines/tag/{tag}",
                params={"limit": config.MASTODON_PER_TAG},
            )
            if isinstance(data, list):
                host = candidate
                hosts.remove(candidate)
                hosts.insert(0, candidate)
                break
        if not isinstance(data, list):
            log.warning("mastodon tag=%s unavailable on all instances", tag)
            continue
        label = f"Mastodon ({host.replace('https://', '')})"
        for entry in data:
            status = entry.get("reblog") or entry
            status_id = status.get("id")
            if not status_id or str(status_id) in seen:
                continue
            seen.add(str(status_id))
            body = _html_to_text(status.get("content") or "")
            if len(body) < 60:
                continue
            external = ""
            try:
                soup = BeautifulSoup(status.get("content") or "", "html.parser")
                for anchor in soup.find_all("a", href=True):
                    href = anchor["href"]
                    if href.startswith("http") and "infosec.exchange" not in href:
                        external = href
                        break
            except Exception:
                external = ""
            url = external or status.get("url") or status.get("uri") or ""
            if not url:
                continue
            first_line = body.split("\n", 1)[0][:160]
            out.append(
                Item(
                    title=f"{first_line} (#{tag})",
                    url=url,
                    source_type="mastodon",
                    source_label=label,
                    summary=body[:600],
                    published_at=_parse_iso(status.get("created_at")),
                    max_age_hours=config.SOCIAL_MAX_AGE_HOURS,
                    engagement={
                        "favourites": int(status.get("favourites_count") or 0),
                        "reblogs": int(status.get("reblogs_count") or 0),
                    },
                    meta={"social_url": status.get("url") or "", "tag": tag},
                )
            )
    return out


# --------------------------------------------------------------------------- #
# Sources E + F - RSS
# --------------------------------------------------------------------------- #
def _fetch_feed(feed: config.Feed) -> list[Item]:
    out: list[Item] = []
    response = _get_bytes(
        feed.url,
        headers={
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"
        },
        timeout=config.REQUEST_TIMEOUT,
    )
    if response is None:
        return out
    parsed = feedparser.parse(response.content)
    if getattr(parsed, "bozo", 0):
        log.debug("rss bozo feed=%s err=%s", feed.name, getattr(parsed, "bozo_exception", ""))
    for entry in parsed.entries[: config.RSS_PER_FEED]:
        title = _clean(entry.get("title") or "")
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        summary = _html_to_text(
            entry.get("summary") or entry.get("description") or ""
        )
        published = (
            _from_struct(entry.get("published_parsed"))
            or _from_struct(entry.get("updated_parsed"))
        )
        out.append(
            Item(
                title=title,
                url=link,
                source_type="rss",
                source_label=feed.name,
                summary=summary[:800],
                published_at=published,
                max_age_hours=config.RSS_MAX_AGE_HOURS,
                meta={"feed": feed.name, "weight": feed.weight, "ai_feed": feed.ai},
            )
        )
    return out


def fetch_rss() -> list[Item]:
    out: list[Item] = []
    for feed in config.RSS_SECURITY_FEEDS + config.RSS_AI_FEEDS:
        try:
            out.extend(_fetch_feed(feed))
        except Exception as exc:
            log.warning("rss feed failed feed=%s err=%s", feed.name, exc)
    return out


# --------------------------------------------------------------------------- #
# Source G - arXiv cs.CR
# --------------------------------------------------------------------------- #
def fetch_arxiv() -> list[Item]:
    params = {
        "search_query": "cat:cs.CR",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": config.ARXIV_MAX_RESULTS,
    }
    response = _get_bytes(ARXIV_API, params=params, timeout=config.REQUEST_TIMEOUT)
    if response is None:
        return []
    parsed = feedparser.parse(response.content)
    out: list[Item] = []
    for entry in parsed.entries:
        title = _clean(entry.get("title") or "")
        link = (entry.get("link") or entry.get("id") or "").strip()
        if not title or not link:
            continue
        link = link.replace("http://", "https://", 1)
        out.append(
            Item(
                title=title,
                url=link,
                source_type="arxiv",
                source_label="arXiv cs.CR",
                summary=_clean(entry.get("summary") or "")[:800],
                published_at=(
                    _from_struct(entry.get("published_parsed"))
                    or _from_struct(entry.get("updated_parsed"))
                ),
                max_age_hours=config.ARXIV_MAX_AGE_HOURS,
                engagement={"abstract_chars": len(entry.get("summary") or "")},
                meta={"authors": [a.get("name") for a in entry.get("authors", [])][:6]},
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Source H - CISA KEV
# --------------------------------------------------------------------------- #
def fetch_cisa_kev() -> list[Item]:
    data = _get_json(CISA_KEV_URL, timeout=40)
    if not isinstance(data, dict):
        return []
    cutoff = (_utcnow() - timedelta(days=7)).date()
    out: list[Item] = []
    for vuln in data.get("vulnerabilities", []):
        added = vuln.get("dateAdded")
        try:
            added_date = date.fromisoformat(str(added))
        except (TypeError, ValueError):
            continue
        if added_date < cutoff:
            continue
        cve = str(vuln.get("cveID") or "").strip()
        if not cve:
            continue
        title = " ".join(
            part
            for part in (
                str(vuln.get("vendorProject") or "").strip(),
                str(vuln.get("product") or "").strip(),
                str(vuln.get("vulnerabilityName") or "").strip(),
            )
            if part
        )
        summary = _clean(
            f"{vuln.get('shortDescription') or ''}\n\n"
            f"Required action: {vuln.get('requiredAction') or 'n/a'}\n"
            f"Due date: {vuln.get('dueDate') or 'n/a'}"
        )
        out.append(
            Item(
                title=f"{cve}: {title}",
                url=f"https://nvd.nist.gov/vuln/detail/{cve}",
                source_type="cisa_kev",
                source_label="CISA KEV",
                summary=summary,
                content=summary,
                published_at=datetime.combine(
                    added_date, dt_time.min, tzinfo=timezone.utc
                ),
                max_age_hours=config.KEV_MAX_AGE_HOURS,
                engagement={"kev": 1},
                meta={
                    "cve": cve,
                    "vendor": vuln.get("vendorProject"),
                    "product": vuln.get("product"),
                    "ransomware": vuln.get("knownRansomwareCampaignUse"),
                },
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Source I - Reddit (best effort only)
# --------------------------------------------------------------------------- #
def fetch_reddit() -> list[Item]:
    out: list[Item] = []
    seen: set[str] = set()
    for subreddit in ("netsec", "bugbounty"):
        try:
            out.extend(_reddit_json(subreddit, seen))
        except Exception as exc:
            log.debug("reddit json failed sub=%s err=%s", subreddit, exc)
        if not out:
            try:
                out.extend(_reddit_rss(subreddit, seen))
            except Exception as exc:
                log.debug("reddit rss failed sub=%s err=%s", subreddit, exc)
    return out


def _reddit_json(subreddit: str, seen: set[str]) -> list[Item]:
    data = _get_json(
        f"{REDDIT_BASE}/r/{subreddit}/new.json",
        params={"limit": config.REDDIT_PER_SUB, "raw_json": 1},
        timeout=8,
    )
    if not isinstance(data, dict):
        return []
    out: list[Item] = []
    for child in (data.get("data") or {}).get("children", []):
        post = child.get("data") or {}
        permalink = post.get("permalink") or ""
        if not permalink or permalink in seen or post.get("over_18"):
            continue
        seen.add(permalink)
        is_self = bool(post.get("is_self"))
        url = (
            f"{REDDIT_BASE}{permalink}"
            if is_self
            else (post.get("url_overridden_by_dest") or post.get("url") or "")
        )
        if not url:
            continue
        host = (urlsplit(url).netloc or "").lower()
        if host.endswith(("i.redd.it", "imgur.com", "v.redd.it")):
            continue
        title = _clean(post.get("title") or "")
        if not title:
            continue
        body = _clean(post.get("selftext") or "")
        out.append(
            Item(
                title=title,
                url=url,
                source_type="reddit",
                source_label=f"r/{subreddit}",
                summary=body[:800] or title,
                published_at=_from_ts(post.get("created_utc")),
                max_age_hours=config.REDDIT_MAX_AGE_HOURS,
                engagement={"ups": int(post.get("ups") or 0)},
                meta={"subreddit": subreddit, "is_self": is_self},
            )
        )
    return out


def _reddit_rss(subreddit: str, seen: set[str]) -> list[Item]:
    response = _get_bytes(
        f"{REDDIT_BASE}/r/{subreddit}/.rss",
        headers={"Accept": "application/rss+xml, application/xml;q=0.9"},
        timeout=8,
    )
    if response is None:
        return []
    parsed = feedparser.parse(response.content)
    out: list[Item] = []
    for entry in parsed.entries[: config.REDDIT_PER_SUB]:
        link = (entry.get("link") or "").strip()
        title = _clean(entry.get("title") or "")
        if not link or not title or link in seen:
            continue
        seen.add(link)
        out.append(
            Item(
                title=title,
                url=link,
                source_type="reddit",
                source_label=f"r/{subreddit}",
                summary=_html_to_text(entry.get("summary") or "")[:800],
                published_at=(
                    _from_struct(entry.get("published_parsed"))
                    or _from_struct(entry.get("updated_parsed"))
                ),
                max_age_hours=config.REDDIT_MAX_AGE_HOURS,
                meta={"subreddit": subreddit},
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _sources() -> list[tuple[str, Any]]:
    sources: list[tuple[str, Any]] = []
    if config.ENABLE_GITHUB:
        sources.append(("github", fetch_github))
    if config.ENABLE_HN:
        sources.append(("hn", fetch_hn))
    if config.ENABLE_BLUESKY:
        sources.append(("bluesky", fetch_bluesky))
    if config.ENABLE_MASTODON:
        sources.append(("mastodon", fetch_mastodon))
    if config.ENABLE_RSS:
        sources.append(("rss", fetch_rss))
    if config.ENABLE_ARXIV:
        sources.append(("arxiv", fetch_arxiv))
    if config.ENABLE_CISA_KEV:
        sources.append(("cisa_kev", fetch_cisa_kev))
    if config.ENABLE_REDDIT:
        sources.append(("reddit", fetch_reddit))
    return sources


def _run_source(name: str, func: Any) -> tuple[str, list[Item], float, str]:
    started = time.monotonic()
    try:
        items = func() or []
        error = ""
    except Exception as exc:  # one broken source must never kill the run
        items = []
        error = f"{exc.__class__.__name__}: {exc}"
        log.exception("source crashed source=%s", name)
    return name, items, time.monotonic() - started, error


def fetch_all() -> list[Item]:
    sources = _sources()
    if not sources:
        log.warning("no sources enabled")
        return []
    collected: list[Item] = []
    with ThreadPoolExecutor(max_workers=min(6, len(sources))) as pool:
        futures = {pool.submit(_run_source, name, func): name for name, func in sources}
        for future in as_completed(futures):
            name, items, elapsed, error = future.result()
            collected.extend(items)
            if error:
                log.warning(
                    "source=%s items=%d elapsed=%.1fs error=%s",
                    name,
                    len(items),
                    elapsed,
                    error,
                )
            else:
                log.info(
                    "source=%s items=%d elapsed=%.1fs", name, len(items), elapsed
                )
    log.info("fetch_total items=%d", len(collected))
    return collected

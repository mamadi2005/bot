"""Central configuration: credentials, sources, scoring weights, budgets, flags."""

from __future__ import annotations

import html
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

try:  # optional, only for local development
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"
USERS_FILE = DATA_DIR / "users.json"
CHANNELS_FILE = DATA_DIR / "channels.json"
TEHRAN_TZ = ZoneInfo("Asia/Tehran")

USER_AGENT = (
    "SecIntelDigestBot/1.0 (security news digest; +https://github.com/) "
    "python-requests"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def env_int(name: str, default: int) -> int:
    raw = env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def env_chat_id(name: str) -> int | str:
    """Return a numeric chat id when possible, otherwise '@channelname'."""
    raw = env_str(name).strip().strip("'\"")
    if not raw:
        return ""
    try:
        return int(raw)
    except ValueError:
        return raw


# --------------------------------------------------------------------------- #
# Secrets (never logged; validated lazily in main.py)
# --------------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN = env_str("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = env_chat_id("ADMIN_CHAT_ID")
GITHUB_TOKEN = env_str("GITHUB_TOKEN")

# --------------------------------------------------------------------------- #
# Webhook / scheduled-trigger deployment (Render + GitHub Actions)
# --------------------------------------------------------------------------- #
DATABASE_URL = env_str("DATABASE_URL")
DB_PATH = env_str("DB_PATH", "data/bot.db")
PUBLIC_BASE_URL = env_str("PUBLIC_BASE_URL").rstrip("/")
# Shared secret in the webhook path (/webhook/<secret>) and Telegram's
# X-Telegram-Bot-Api-Secret-Token header.
WEBHOOK_SECRET = env_str("WEBHOOK_SECRET")
# Bearer token required by POST /trigger_news; GitHub Actions sends it.
TRIGGER_TOKEN = env_str("TRIGGER_TOKEN")
# Render injects PORT; kept here so the CLI can report it consistently.
PORT = env_int("PORT", 8080)
# How long a news message lives before the cleanup pass deletes it.
NEWS_TTL_HOURS = max(1, env_int("NEWS_TTL_HOURS", 24))
# External triggers decide when the slot-aware job is run.
NEWS_TEST_MODE = env_bool("NEWS_TEST_MODE", False)
NEWS_TEST_LOOKBACK_DAYS = max(1, env_int("NEWS_TEST_LOOKBACK_DAYS", 15))
NEWS_TEST_INTERVAL_SECONDS = max(60, env_int("NEWS_TEST_INTERVAL_SECONDS", 300))


def _gemini_keys() -> list[str]:
    """GEMINI_API_KEY_1..3 plus the legacy single GEMINI_API_KEY, deduplicated."""
    keys: list[str] = []
    for index in range(1, 4):
        value = env_str(f"GEMINI_API_KEY_{index}")
        if value:
            keys.append(value)
    legacy = env_str("GEMINI_API_KEY")
    if legacy:
        keys.insert(0, legacy)
    unique: list[str] = []
    for key in keys:
        if key not in unique:
            unique.append(key)
    return unique


GEMINI_API_KEYS = _gemini_keys()
GEMINI_API_KEY = GEMINI_API_KEYS[0] if GEMINI_API_KEYS else ""


def _parse_chat_ids(raw: str) -> list[int | str]:
    """Comma-separated numeric ids and/or @channel usernames."""
    out: list[int | str] = []
    for part in raw.replace(";", ",").split(","):
        value = part.strip().strip("'\"")
        if not value:
            continue
        if value.startswith("@"):
            out.append(value)
            continue
        try:
            out.append(int(value))
        except ValueError:
            continue
    return list(dict.fromkeys(out))


TELEGRAM_CHAT_IDS = _parse_chat_ids(env_str("TELEGRAM_CHAT_IDS"))


def recipient_chat_ids() -> list[int | str]:
    """Everyone who should receive every digest item on each run."""
    if TELEGRAM_CHAT_IDS:
        return list(TELEGRAM_CHAT_IDS)
    return [ADMIN_CHAT_ID] if ADMIN_CHAT_ID else []


# --------------------------------------------------------------------------- #
# Feature flags
# --------------------------------------------------------------------------- #
DRY_RUN = env_bool("DRY_RUN", False)
DEBUG = env_bool("DEBUG", False)

ENABLE_GITHUB = env_bool("ENABLE_GITHUB", True)
ENABLE_HN = env_bool("ENABLE_HN", True)
ENABLE_BLUESKY = env_bool("ENABLE_BLUESKY", True)
ENABLE_MASTODON = env_bool("ENABLE_MASTODON", True)
ENABLE_RSS = env_bool("ENABLE_RSS", True)
ENABLE_ARXIV = env_bool("ENABLE_ARXIV", True)
ENABLE_CISA_KEV = env_bool("ENABLE_CISA_KEV", True)
ENABLE_REDDIT = env_bool("ENABLE_REDDIT", True)

ENABLE_PDF = env_bool("ENABLE_PDF", False)


# --------------------------------------------------------------------------- #
# AI
# --------------------------------------------------------------------------- #
GEMINI_MODEL = env_str("GEMINI_MODEL", "gemini-2.5-flash")
TRIAGE_TEMPERATURE = 0.0
SUMMARY_TEMPERATURE = 0.2

TRIAGE_THRESHOLD = env_int("TRIAGE_THRESHOLD", 7)
MAX_ITEMS_PER_RUN = env_int("MAX_ITEMS_PER_RUN", 10)
MAX_AI_CALLS_PER_RUN = env_int("MAX_AI_CALLS_PER_RUN", 12)
DAILY_AI_BUDGET = env_int("DAILY_AI_BUDGET", 200)
AI_MAX_RETRIES = env_int("AI_MAX_RETRIES", 4)
AI_BACKOFF_BASE = env_float("AI_BACKOFF_BASE", 2.0)
AI_MAX_OUTPUT_TOKENS = env_int("AI_MAX_OUTPUT_TOKENS", 1024)


# --------------------------------------------------------------------------- #
# Pipeline budgets
# --------------------------------------------------------------------------- #
MAX_CANDIDATES = env_int("MAX_CANDIDATES", 25)
MAX_PER_SOURCE_CANDIDATES = env_int("MAX_PER_SOURCE_CANDIDATES", 6)
FULLTEXT_MAX_CHARS = env_int("FULLTEXT_MAX_CHARS", 6000)
FULLTEXT_BUDGET = env_int("FULLTEXT_BUDGET", 30)
FULLTEXT_WORKERS = env_int("FULLTEXT_WORKERS", 8)
REQUEST_TIMEOUT = env_int("REQUEST_TIMEOUT", 20)
HTTP_MAX_RETRIES = env_int("HTTP_MAX_RETRIES", 2)

MIN_ARTICLE_CHARS = env_int("MIN_ARTICLE_CHARS", 500)
MIN_REPO_CHARS = env_int("MIN_REPO_CHARS", 250)
MIN_ARXIV_CHARS = env_int("MIN_ARXIV_CHARS", 300)

# Keep dedup state at least one day longer than the historical test window.
_default_sent_retention = (
    max(14, NEWS_TEST_LOOKBACK_DAYS + 1) if NEWS_TEST_MODE else 14
)
SENT_LOG_RETENTION_DAYS = env_int(
    "SENT_LOG_RETENTION_DAYS", _default_sent_retention
)

SOCIAL_MAX_AGE_HOURS = env_int("SOCIAL_MAX_AGE_HOURS", 36)
RSS_MAX_AGE_HOURS = env_int("RSS_MAX_AGE_HOURS", 72)
REPO_MAX_AGE_HOURS = env_int("REPO_MAX_AGE_HOURS", 7 * 24)
ARXIV_MAX_AGE_HOURS = env_int("ARXIV_MAX_AGE_HOURS", 7 * 24)
KEV_MAX_AGE_HOURS = env_int("KEV_MAX_AGE_HOURS", 7 * 24)
REDDIT_MAX_AGE_HOURS = env_int("REDDIT_MAX_AGE_HOURS", 24)


# --------------------------------------------------------------------------- #
# Source thresholds
# --------------------------------------------------------------------------- #
GITHUB_MIN_STARS = env_int("GITHUB_MIN_STARS", 10)
GITHUB_WINDOW_DAYS = env_int("GITHUB_WINDOW_DAYS", 7)
GITHUB_PER_KEYWORD = env_int("GITHUB_PER_KEYWORD", 10)

# Keyword-stuffed commercial spam (game cheats, MEV bots, crack bundles) uses
# security words to ride GitHub search. Match against name + description.
REPO_DENY_TERMS = [
    "aimbot",
    "triggerbot",
    "wallhack",
    "spoofer",
    "keyauth",
    "external cheat",
    "cheat menu",
    "mod menu",
    "anticheat",
    "cracked",
    "keygen",
    "license key",
    "activation key",
    "free download",
    "downloader",
    "arbitrage",
    "sandwich attack",
    "mev bot",
    "trading bot",
    "sniper bot",
    "airdrop",
    "presale",
    "usdt",
    "virtual card",
    "uncensored",
    "roblox",
    "fivem",
    "minecraft",
    "fortnite",
    "valorant",
    "unban",
    "hwid",
]
HN_MIN_POINTS = env_int("HN_MIN_POINTS", 50)
HN_PER_QUERY = env_int("HN_PER_QUERY", 20)
BLUESKY_PER_QUERY = env_int("BLUESKY_PER_QUERY", 25)
MASTODON_PER_TAG = env_int("MASTODON_PER_TAG", 20)
# infosec.exchange now requires auth for its API; these instances still serve
# public hashtag timelines. Tried in order, first working one wins per run.
MASTODON_HOSTS = [
    host.strip().rstrip("/")
    for host in env_str(
        "MASTODON_HOSTS", "https://infosec.town,https://mastodon.social"
    ).split(",")
    if host.strip()
]
RSS_PER_FEED = env_int("RSS_PER_FEED", 15)
ARXIV_MAX_RESULTS = env_int("ARXIV_MAX_RESULTS", 30)
REDDIT_PER_SUB = env_int("REDDIT_PER_SUB", 25)


# --------------------------------------------------------------------------- #
# Delivery behaviour
# --------------------------------------------------------------------------- #
MESSAGE_DELAY_SAME_CHAT = env_float("MESSAGE_DELAY_SAME_CHAT", 1.0)
# Delay between two different recipients so large broadcasts stay under
# Telegram's ~30 messages/second global limit.
BROADCAST_DELAY = env_float("BROADCAST_DELAY", 0.05)
MAX_MESSAGE_CHARS = 4000
NEWS_LIFETIME_SECONDS = NEWS_TTL_HOURS * 60 * 60
LOCAL_POLL_SECONDS = 10
LOCAL_CLEANUP_SECONDS = 60


def local_fetch_interval_seconds() -> int:
    """Validate the local news interval independently of ordinary one-shot runs."""
    raw = env_str("LOCAL_FETCH_INTERVAL_SECONDS", "14400")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            "LOCAL_FETCH_INTERVAL_SECONDS must be an integer from 60 to 604800"
        ) from exc
    if not 60 <= value <= 604800:
        raise ValueError("LOCAL_FETCH_INTERVAL_SECONDS must be an integer from 60 to 604800")
    return value


def news_expiry_warning(lang: str = "en") -> str:
    return (
        "This news message will be deleted after about 24 hours. "
        "Save it to Saved Messages to keep it."
    )


PDF_SEND_HOUR = env_int("PDF_SEND_HOUR", 18)
PDF_BUFFER_MAX_CHARS = env_int("PDF_BUFFER_MAX_CHARS", 30000)

GITHUB_KEYWORDS = [
    "pentest",
    "bugbounty",
    "exploit",
    "ai-security",
    "llm-security",
    "prompt-injection",
    "mcp-security",
    "red-team",
]

HN_QUERIES = [
    "vulnerability",
    "exploit",
    "bug bounty",
    "zero-day",
    "prompt injection",
    "LLM security",
    "MCP security",
    "AI red team",
]

BLUESKY_QUERIES = [
    "bug bounty",
    "prompt injection",
    "CVE exploit",
    "LLM security",
    "MCP security",
]

MASTODON_TAGS = [
    "cve",
    "bugbounty",
    "appsec",
    "llm",
    "promptinjection",
    "aisecurity",
]


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    weight: float = 3.0
    ai: bool = False


RSS_SECURITY_FEEDS = [
    Feed("PortSwigger Research", "https://portswigger.net/research/rss", 4.0),
    Feed("ProjectDiscovery", "https://blog.projectdiscovery.io/rss/", 3.2),
    Feed("Assetnote", "https://www.assetnote.io/resources/rss.xml", 3.4),
    Feed("Detectify", "https://blog.detectify.com/feed/", 2.6),
    Feed("Intigriti", "https://blog.intigriti.com/feed/", 2.6),
    Feed("Trail of Bits", "https://blog.trailofbits.com/feed/", 3.8),
    Feed("GitHub Security Lab", "https://github.blog/tag/github-security-lab/feed/", 3.6),
    Feed("BleepingComputer", "https://www.bleepingcomputer.com/feed/", 2.2),
    Feed("The Hacker News", "https://feeds.feedburner.com/TheHackersNews", 2.2),
    Feed("SANS ISC", "https://isc.sans.edu/rssfeed.xml", 2.8),
]

RSS_AI_FEEDS = [
    Feed("Embrace The Red", "https://embracethered.com/blog/index.xml", 4.0, ai=True),
    Feed("Simon Willison", "https://simonwillison.net/atom/everything/", 2.8, ai=True),
    Feed("HiddenLayer", "https://hiddenlayer.com/innovation-hub/feed/", 3.2, ai=True),
    Feed("Protect AI", "https://protectai.com/blog/rss.xml", 3.2, ai=True),
    Feed(
        "NVIDIA AI Red Team",
        "https://developer.nvidia.com/blog/tag/ai-red-team/feed/",
        3.2,
        ai=True,
    ),
    Feed("OWASP GenAI", "https://genai.owasp.org/feed/", 3.2, ai=True),
]


# --------------------------------------------------------------------------- #
# Scoring weights (transparent; every component is logged)
# --------------------------------------------------------------------------- #
SOURCE_WEIGHTS = {
    "github": 2.0,
    "hn": 2.6,
    "bluesky": 1.4,
    "mastodon": 1.6,
    "rss": 3.0,
    "arxiv": 3.4,
    "cisa_kev": 3.2,
    "reddit": 2.2,
}

KEYWORD_WEIGHTS = {
    "prompt injection": 3.0,
    "indirect prompt injection": 3.2,
    "jailbreak": 2.5,
    "guardrail": 2.2,
    "rag poisoning": 3.0,
    "model poisoning": 2.6,
    "model extraction": 2.4,
    "mcp": 2.0,
    "agentic": 2.0,
    "huggingface": 1.8,
    "ai security": 2.5,
    "llm": 1.5,
    "zero-day": 3.0,
    "0day": 2.8,
    "cve-": 2.5,
    "rce": 2.5,
    "ssrf": 2.5,
    "xss": 2.0,
    "sqli": 2.0,
    "idor": 2.2,
    "ssti": 2.2,
    "xxe": 2.0,
    "csrf": 1.8,
    "deserialization": 2.2,
    "waf bypass": 2.8,
    "filter bypass": 2.4,
    "request smuggling": 2.8,
    "cache poisoning": 2.4,
    "race condition": 2.0,
    "subdomain takeover": 2.2,
    "container escape": 2.6,
    "kubernetes": 1.8,
    "privilege escalation": 2.2,
    "authentication bypass": 2.4,
    "bug bounty": 2.0,
    "red team": 1.8,
    "poc": 1.4,
    "exploit": 2.2,
    "supply chain": 1.6,
}

AI_KEYWORDS = (
    "prompt injection",
    "jailbreak",
    "llm",
    "ai security",
    "ai red team",
    "agentic",
    "mcp",
    "rag poisoning",
    "guardrail",
    "model poisoning",
    "model extraction",
    "adversarial ml",
    "huggingface",
    "garak",
    "pyrit",
    "promptfoo",
    "genai",
    "generative ai",
    "language model",
    "anthropic",
    "openai",
)

AI_STRONG_KEYWORDS = (
    "prompt injection",
    "jailbreak",
    "mcp",
    "rag poisoning",
    "model poisoning",
    "model extraction",
    "ai security",
    "llm security",
    "guardrail",
    "agentic ai",
    "adversarial ml",
)

SCORE_ENGAGEMENT_MAX = 4.0
SCORE_KEYWORD_MAX = 4.0
SCORE_RECENCY_MAX = 2.0
SCORE_AI_BONUS = 0.8
SCORE_PREFERRED_DOMAIN = 0.5
SCORE_AGENTIC_BONUS = 0.3

PREFERRED_DOMAINS = {
    "github.com",
    "portswigger.net",
    "blog.trailofbits.com",
    "projectdiscovery.io",
    "assetnote.io",
    "arxiv.org",
    "nvd.nist.gov",
    "cisa.gov",
    "googleprojectzero.blogspot.com",
    "embracethered.com",
    "simonwillison.net",
    "hiddenlayer.com",
    "protectai.com",
    "genai.owasp.org",
    "developer.nvidia.com",
    "microsoft.com",
    "cloud.google.com",
    "aws.amazon.com",
}

# Social aggregators link out; drop known noise and circular/self-referential hosts.
SOCIAL_DOMAIN_BLOCKLIST = (
    "x.com",
    "twitter.com",
    "t.co",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "linkedin.com",
    "threads.net",
    "discord.gg",
    "t.me",
    "telegram.me",
    "pinterest.com",
    "amazon.com",
    "bit.ly",
    "ow.ly",
)

LISTICLE_PATTERNS = (
    r"^\s*top\s*\d+\b",
    r"^\s*\d+\s+(best|top|greatest|essential|must[- ]read)\b",
    r"\bbest\b.{0,40}\b(20\d{2}|tools|resources|blogs|courses)\b",
    r"\bultimate\s+guide\b",
    r"\bbeginner'?s?\s+guide\b",
    r"\bcheat\s*sheet\b",
    r"\bawesome\s+(list|collection)\b",
    r"\bhow\s+to\s+become\b",
    r"\bcareer\s+(advice|guide|path)\b",
    r"\bsalary\b",
    r"\bwebinar\b",
    r"\bsponsored\b",
    r"\bgiveaway\b",
)


# --------------------------------------------------------------------------- #
# Language and rate limiting
# --------------------------------------------------------------------------- #
SUPPORTED_LANGS = ("en",)
DEFAULT_LANG = "en"

USER_RATE_WINDOW_SECONDS = env_int("USER_RATE_WINDOW_SECONDS", 60)
USER_RATE_MAX_MESSAGES = env_int("USER_RATE_MAX_MESSAGES", 3)
USER_MUTE_SECONDS = env_int("USER_MUTE_SECONDS", 5 * 60)
STATS_ACTIVE_DAYS = env_int("STATS_ACTIVE_DAYS", 30)


# --------------------------------------------------------------------------- #
# User-facing copy (English only)
# --------------------------------------------------------------------------- #
def welcome_text(lang: str, chat_id: int | str) -> str:
    return (
        "Hello! 👋 Welcome to the news bot.\n"
        "You have been registered successfully. ✅\n"
        "News subscription: active\n"
        "The bot sends news in English. Use /unsubscribe to stop."
    )


def help_text(lang: str = "en") -> str:
    return (
        "<b>Bot help</b>\n\n"
        "/start — show settings and enable news\n"
        "/subscribe — turn news on\n"
        "/unsubscribe — turn news off\n"
        "/help — this help\n\n"
        "News is collected throughout the day and delivered in batches. "
        "News messages are deleted after about 24 hours; settings messages stay."
    )


def settings_text(lang: str, subscribed: bool) -> str:
    state = "active ✅" if subscribed else "inactive ⛔"
    return f"<b>Your settings</b>\n\nSubscription: {state}\nNews language: English"


def subscription_changed_text(lang: str, subscribed: bool) -> str:
    return "News subscription is now active. ✅" if subscribed else "News subscription is now off. ⛔"


def toggle_subscription_text(lang: str, subscribed: bool) -> str:
    return "Turn news off" if subscribed else "Turn news on"


def help_button_text(lang: str) -> str:
    return "Help"


def main_keyboard(lang: str = "en") -> dict:
    """Persistent reply keyboard so /start is always one tap away."""
    return {
        "keyboard": [
            [{"text": "/start"}, {"text": "/help"}],
            [{"text": "/subscribe"}, {"text": "/unsubscribe"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def hint_text(lang: str) -> str:
    return "Use /start to see your settings."


def register_first_text(lang: str) -> str:
    return "Please register first with /start."


def join_required_hint_text(lang: str) -> str:
    return "Please join the channel first, then tap the confirm button."


def joined_and_registered_text(lang: str) -> str:
    return "Registered ✅"


def join_required_text(lang: str, channels: list[dict] | None = None) -> str:
    lines: list[str] = []
    for entry in channels or []:
        name = html.escape(str(entry.get("name") or entry.get("username") or ""), quote=False)
        link = html.escape(str(entry.get("link") or ""), quote=True)
        if name and link:
            lines.append(f'📢 <a href="{link}">{name}</a>')
        elif name:
            lines.append(f"📢 {name}")
    body = "\n".join(lines)
    return (
        "⚠️ To use zerobot, you must first join the channel below:\n\n"
        f"{body}\n\nAfter joining, click the button below."
    )


def confirm_join_text(lang: str) -> str:
    return "✅ I've Joined"


def join_confirmed_text(lang: str) -> str:
    return "Membership confirmed. ✅"


def join_again_text(lang: str) -> str:
    return "Join the channel first, then press the button."


def stats_text(
    lang: str,
    total: int,
    subscribed: int,
    active: int,
    days: int = STATS_ACTIVE_DAYS,
) -> str:
    return (
        "<b>Bot statistics</b>\n\n"
        f"Total users: <code>{total}</code>\n"
        f"Subscribed: <code>{subscribed}</code>\n"
        f"Active in the last {days} days: <code>{active}</code>"
    )


def channel_added_text(lang: str, channel: str) -> str:
    return f"✅ Channel <code>{channel}</code> was added to the required list."


def channel_removed_text(lang: str, channel: str) -> str:
    return f"🗑 Channel <code>{channel}</code> was removed from the list."


def channel_missing_text(lang: str, channel: str) -> str:
    return f"❗️ Channel <code>{channel}</code> is not in the list."


def channel_exists_text(lang: str, channel: str) -> str:
    return f"ℹ️ Channel <code>{channel}</code> is already configured."


def channel_usage_text(lang: str = "en") -> str:
    return (
        "Usage: <code>/addchannel @channelusername</code> or "
        "<code>/removechannel @channelusername</code>"
    )


def repo_warning(lang: str) -> str:
    return REPO_WARNING


def ai_disclaimer(lang: str) -> str:
    return AI_DISCLAIMER


def ai_disclaimer_no_ai(lang: str) -> str:
    return AI_DISCLAIMER_NO_AI


def source_link_text(lang: str) -> str:
    return "Read the full source"


REPO_WARNING = (
    "⚠️ <b>Unreviewed:</b> freshly created repository. Read the code before running it."
)
AI_DISCLAIMER = "AI-generated summary — verify the original source."
AI_DISCLAIMER_NO_AI = "Automatic summary (no AI) — verify the original source."
QUOTA_NOTICE = (
    "⚠️ Gemini's daily quota is exhausted; plain summaries (title + link) were sent."
)

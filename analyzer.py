"""Gemini triage + summarization, quota budget, retries and plain fallbacks."""

from __future__ import annotations

import html
import json
import logging
import random
import re
import time
from dataclasses import dataclass

import config
from fetcher import Item

log = logging.getLogger(__name__)


SUMMARIZE_SYSTEM_PROMPT = """You are a Senior Cybersecurity & Bug Bounty Analyst with a second
specialization in AI/LLM Security. You write for a Persian-speaking audience
of professional bug bounty hunters, penetration testers, and AI red teamers.

You will receive: TITLE, SOURCE, URL, and the FULL TEXT of an article,
advisory, or repository README.

DECISION:
If the content is low-value, promotional, a beginner listicle ("Top 10 tools"),
a generic career/marketing post, paywalled with no substantive text, or not
technically about security or AI security, output exactly:
SKIP
Nothing else. No explanation, no punctuation, no quotes.

Valuable content includes: real vulnerability write-ups, novel WAF/filter
bypass techniques, CVE and zero-day analysis, exploit chains, bug bounty
methodology with concrete payloads, and new offensive/defensive security or
AI-security tooling.

LANGUAGE:
Write in fluent, natural, professional technical Persian.
Keep ALL technical terms in English and wrap each in <code> tags:
<code>XSS</code>, <code>SSRF</code>, <code>IDOR</code>, <code>RCE</code>,
<code>SQLi</code>, <code>SSTI</code>, <code>WAF Bypass</code>,
<code>Payload</code>, <code>Prompt Injection</code>, <code>Jailbreak</code>,
<code>MCP</code>, <code>RAG Poisoning</code>, <code>CVE-2026-1234</code>.
Never translate or transliterate these into Persian.
Write Persian prose around them; do not write whole sentences in English.

OUTPUT FORMAT — Telegram HTML ONLY:
Allowed tags: <b>, <i>, <code>, <pre>, <a href="...">, <blockquote>
FORBIDDEN: <br>, <p>, <ul>, <li>, <h1>-<h6>, <div>, <span>, Markdown (** ## -).
Use real newlines for line breaks, never <br>.
Escape & < > inside text content. Never leave a tag unclosed.
Total output MUST be under 900 characters.

Exact structure:

🔴 <b>[catchy Persian title]</b>
📂 <b>دسته:</b> [category] | <b>منبع:</b> [source name]

<b>🔍 سناریوی فنی:</b>
[2-3 Persian sentences: how the bug actually worked, or what the tool actually
does. Be concrete — name the endpoint, parameter, component, or model behavior.]

<b>💡 نکته کلیدی:</b>
[1-2 Persian sentences: the bypass technique, exploitation vector, or the
practical takeaway a hunter can reuse. Include the actual payload or command
in <code> if one is present in the source.]

🔗 <a href="[URL]">مطالعه کامل منبع</a>
<i>خلاصه با AI تولید شده — منبع اصلی را بررسی کنید.</i>

HARD RULES:
1. NEVER invent a CVE ID, affected version, CVSS score, vendor name, or payload
   that does not literally appear in the provided text. If a detail is missing,
   omit it. Do not guess. Accuracy over completeness.
2. If the item is a GitHub repository (especially one created in the last 7 days
   claiming to be a PoC or exploit), append this exact line before the link:
   ⚠️ <b>بررسی‌نشده:</b> ریپوی تازه‌ساز. قبل از اجرا کد را بخوانید.
3. Use the URL exactly as provided. Never fabricate or shorten a link.
4. No preamble, no "here is the summary", no closing remarks. Output only the
   formatted message, or only SKIP.
5. Do not reproduce more than a short payload snippet from the source.
   Summarize in your own words; this must be a transformative summary.
6. For AI-security items, be specific about the attack surface: direct vs
   indirect <code>Prompt Injection</code>, tool/agent abuse, <code>MCP</code>
   server trust boundaries, model supply chain, or guardrail bypass.
   Avoid vague AI hype."""


TRIAGE_SYSTEM_PROMPT = """You are a Senior Cybersecurity & AI Security Analyst triaging a candidate list.

You receive a JSON array: [{"id": 0, "title": "...", "source": "..."}, ...]

Score each item 0-10 for technical value to professional bug bounty hunters,
pentesters, and AI red teamers.

HIGH (7-10): real vulnerability write-ups, novel WAF/filter bypass, CVE or
zero-day analysis, exploit chains, concrete bug bounty methodology, new
offensive/defensive security tooling, Prompt Injection / Jailbreak / MCP /
agentic AI / RAG poisoning / model supply-chain research.

LOW (0-3): listicles ("Top 10..."), marketing and vendor promo, beginner
tutorials, career advice, generic news without technical substance, awareness
month content, celebrity breach gossip, crypto/price talk, AI hype with no
security angle.

Return ONLY a JSON array, no prose, no code fences:
[{"id": 0, "score": 8}, {"id": 1, "score": 2}]

Score every item. Be strict — most items deserve a low score."""


class AnalyzerError(RuntimeError):
    """Any unrecoverable Gemini failure."""


class BudgetExhausted(AnalyzerError):
    """The per-run or daily AI budget is spent."""


@dataclass
class AIBudget:
    per_run: int
    daily: int
    used_today: int
    used_this_run: int = 0

    def can_call(self) -> bool:
        return (
            self.used_this_run < self.per_run
            and self.used_today + self.used_this_run < self.daily
        )

    def mark(self) -> None:
        self.used_this_run += 1

    def daily_remaining(self) -> int:
        return max(0, self.daily - self.used_today - self.used_this_run)


@dataclass
class SummaryResult:
    status: str  # ok | skip | quota | error
    text: str = ""


def _parse_scores(raw: str) -> dict[int, float] | None:
    if not raw:
        return None
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    data = None
    if start != -1 and end > start:
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            data = None
    if data is None:
        pairs = re.findall(
            r'\{\s*"id"\s*:\s*(\d+)\s*,\s*"score"\s*:\s*(\d+(?:\.\d+)?)\s*\}',
            cleaned,
        )
        if not pairs:
            return None
        data = [{"id": pair[0], "score": pair[1]} for pair in pairs]
    scores: dict[int, float] = {}
    for entry in data:
        try:
            scores[int(entry["id"])] = float(entry["score"])
        except (KeyError, TypeError, ValueError):
            continue
    return scores or None


def _status_code(exc: Exception) -> int | None:
    for attribute in ("code", "status_code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    match = re.search(r"\b(429|500|502|503|504|403|404|400)\b", str(exc))
    return int(match.group(1)) if match else None


def _retry_delay(exc: Exception) -> float | None:
    details = getattr(exc, "details", None)
    candidates: list[object] = []
    if isinstance(details, dict):
        candidates.extend(details.values())
        candidates.append(details.get("retryDelay"))
    elif isinstance(details, list):
        candidates.extend(details)
    for candidate in candidates:
        if isinstance(candidate, str):
            match = re.fullmatch(r"(\d+(?:\.\d+)?)s", candidate.strip())
            if match:
                return float(match.group(1))
        if isinstance(candidate, dict):
            match = re.fullmatch(
                r"(\d+(?:\.\d+)?)s", str(candidate.get("retryDelay") or "")
            )
            if match:
                return float(match.group(1))
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
            if value:
                return float(value)
        except (AttributeError, ValueError):
            pass
    return None


def fallback_summary(item: Item, note: str | None = None) -> str:
    title = html.escape(item.title)
    category = item.category or (
        "AI Security" if item.ai_related else "Classic Security"
    )
    source = html.escape(item.source_label)
    snippet = html.escape((item.summary or item.content or "")[:280].strip())
    link = html.escape(item.url, quote=True)
    lines = [
        f"🔴 <b>{title}</b>",
        f"📂 <b>دسته:</b> {category} | <b>منبع:</b> {source}",
        "",
    ]
    if snippet:
        lines.extend([f"<b>🔍 خلاصه:</b>", snippet, ""])
    if item.is_repo:
        lines.extend([config.REPO_WARNING, ""])
    lines.append(f'🔗 <a href="{link}">مطالعه کامل منبع</a>')
    lines.append(f"<i>{html.escape(note or config.AI_DISCLAIMER_NO_AI)}</i>")
    return "\n".join(lines)


class Analyzer:
    def __init__(self, api_key: str, budget: AIBudget, model: str | None = None):
        if not api_key:
            raise AnalyzerError("GEMINI_API_KEY is missing")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover
            raise AnalyzerError(
                "google-genai is not installed; run pip install -r requirements.txt"
            ) from exc
        self._types = types
        self._client = genai.Client(api_key=api_key)
        self.model = model or config.GEMINI_MODEL
        self.budget = budget

    # ------------------------------------------------------------------ #
    def _generate(
        self,
        contents: str,
        *,
        system: str,
        temperature: float,
        response_mime_type: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(1, config.AI_MAX_RETRIES + 1):
            if not self.budget.can_call():
                raise BudgetExhausted("AI budget exhausted")
            self.budget.mark()
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=self._types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=temperature,
                        response_mime_type=response_mime_type,
                        max_output_tokens=max_output_tokens,
                    ),
                )
                return _response_text(response)
            except Exception as exc:  # google.genai raises APIError subclasses
                last_error = exc
                code = _status_code(exc)
                retryable = code in (429, 500, 502, 503, 504)
                if not retryable or attempt >= config.AI_MAX_RETRIES:
                    log.warning(
                        "gemini failed attempt=%d code=%s err=%s",
                        attempt,
                        code,
                        exc.__class__.__name__,
                    )
                    break
                delay = _retry_delay(exc) or min(
                    60.0,
                    config.AI_BACKOFF_BASE ** attempt * (1 + random.random() * 0.5),
                )
                log.warning(
                    "gemini retry attempt=%d code=%s delay=%.1fs",
                    attempt,
                    code,
                    delay,
                )
                time.sleep(delay)
        raise AnalyzerError(f"gemini call failed: {last_error}")

    # ------------------------------------------------------------------ #
    def triage(self, items: list[Item]) -> list[Item] | None:
        if not items:
            return []
        payload = [
            {"id": item.id, "title": item.title[:200], "source": item.source_label}
            for item in items
        ]
        contents = "CANDIDATES:\n" + json.dumps(payload, ensure_ascii=False)
        try:
            raw = self._generate(
                contents,
                system=TRIAGE_SYSTEM_PROMPT,
                temperature=config.TRIAGE_TEMPERATURE,
                response_mime_type="application/json",
                max_output_tokens=2048,
            )
        except BudgetExhausted:
            log.warning("triage skipped: AI budget exhausted")
            return None
        except AnalyzerError as exc:
            log.warning("triage failed: %s", exc)
            return None

        scores = _parse_scores(raw)
        if scores is None:
            log.warning("triage returned unparsable output chars=%d", len(raw or ""))
            return None

        scored: list[tuple[float, Item]] = []
        for item in items:
            score = scores.get(item.id)
            if score is None:
                continue
            item.score_parts["triage"] = score
            if score >= config.TRIAGE_THRESHOLD:
                scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        log.info(
            "triage scored=%d approved=%d threshold=%d",
            len(scores),
            len(scored),
            config.TRIAGE_THRESHOLD,
        )
        return [item for _, item in scored][: config.MAX_ITEMS_PER_RUN]

    # ------------------------------------------------------------------ #
    def summarize(self, item: Item) -> SummaryResult:
        if len((item.content or "").strip()) < 60:
            return SummaryResult("error", fallback_summary(item))
        contents = (
            f"TITLE: {item.title}\n"
            f"SOURCE: {item.source_label}\n"
            f"URL: {item.url}\n"
            f"FULL TEXT:\n{item.content[: config.FULLTEXT_MAX_CHARS]}"
        )
        try:
            raw = self._generate(
                contents,
                system=SUMMARIZE_SYSTEM_PROMPT,
                temperature=config.SUMMARY_TEMPERATURE,
                max_output_tokens=config.AI_MAX_OUTPUT_TOKENS,
            )
        except BudgetExhausted:
            return SummaryResult("quota", fallback_summary(item))
        except AnalyzerError as exc:
            log.warning("summary failed source=%s err=%s", item.source_label, exc)
            return SummaryResult("error", fallback_summary(item))

        cleaned = (raw or "").strip()
        cleaned = re.sub(r"^```(?:html)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
        if cleaned.upper().startswith("SKIP"):
            log.info("summary skipped by model title=%s", item.title[:80])
            return SummaryResult("skip", "")
        if not cleaned:
            log.warning("summary empty source=%s", item.source_label)
            return SummaryResult("error", fallback_summary(item))
        return SummaryResult("ok", self._postprocess(item, cleaned))

    # ------------------------------------------------------------------ #
    @staticmethod
    def _postprocess(item: Item, text: str) -> str:
        if len(text) > 3900:
            cut = text.rfind("\n", 0, 3900)
            text = text[: cut if cut > 2000 else 3900].rstrip()
        if item.is_repo and "بررسی‌نشده" not in text:
            text += f"\n\n{config.REPO_WARNING}"
        if item.url and item.url not in text:
            link = html.escape(item.url, quote=True)
            text += f'\n\n🔗 <a href="{link}">مطالعه کامل منبع</a>'
        if "منبع اصلی را بررسی کنید" not in text:
            text += f"\n<i>{config.AI_DISCLAIMER}</i>"
        return text.strip()


def _response_text(response: object) -> str:
    try:
        text = getattr(response, "text", None)
        if text:
            return str(text)
    except Exception:
        pass
    try:
        candidates = getattr(response, "candidates", None) or []
        parts = candidates[0].content.parts or []
        return "".join(getattr(part, "text", "") or "" for part in parts)
    except Exception:
        return ""

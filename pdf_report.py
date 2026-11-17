"""Optional daily Persian PDF digest (WeasyPrint + Vazirmatn)."""

from __future__ import annotations

import html
import logging
from datetime import date
from pathlib import Path

import config

log = logging.getLogger(__name__)

TEMPLATE = """<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
{font_faces}
@page {{
  size: A4;
  margin: 18mm 15mm 20mm 15mm;
  @bottom-center {{
    content: counter(page) " / " counter(pages);
    font-family: "Vazirmatn", sans-serif;
    font-size: 9pt;
    color: #777;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: "Vazirmatn", "DejaVu Sans", sans-serif;
  direction: rtl;
  text-align: right;
  color: #1b1b1b;
  font-size: 11pt;
  line-height: 1.85;
}}
header {{
  border-bottom: 3px solid #0b5cad;
  padding-bottom: 10px;
  margin-bottom: 18px;
}}
header h1 {{ font-size: 20pt; margin: 0 0 4px 0; color: #0b5cad; }}
header .sub {{ font-size: 10pt; color: #555; }}
.item {{
  page-break-inside: avoid;
  border-bottom: 1px solid #e3e3e3;
  padding: 10px 0 14px 0;
}}
.item h2 {{ font-size: 13pt; margin: 4px 0 6px 0; }}
.meta {{ font-size: 9pt; color: #666; }}
.body {{ font-size: 10.5pt; }}
.body code, .body pre, .link {{
  direction: ltr;
  unicode-bidi: embed;
  text-align: left;
  font-family: "DejaVu Sans Mono", monospace;
  font-size: 9pt;
}}
.body pre {{
  white-space: pre-wrap;
  background: #f5f7fa;
  border: 1px solid #e1e5ea;
  border-radius: 4px;
  padding: 8px;
}}
.body blockquote {{
  margin: 8px 0;
  padding: 6px 10px;
  border-right: 3px solid #0b5cad;
  background: #f7f9fc;
}}
a {{ color: #0b5cad; text-decoration: none; word-break: break-all; }}
footer {{ margin-top: 24px; font-size: 9pt; color: #666; }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="sub">{subtitle}</div>
</header>
{items}
<footer>{footer}</footer>
</body>
</html>
"""


def _font_faces(font_dir: Path) -> str:
    regular = font_dir / "Vazirmatn-Regular.ttf"
    bold = font_dir / "Vazirmatn-Bold.ttf"
    faces = [
        "@font-face {\n"
        '  font-family: "Vazirmatn";\n'
        f'  src: url("{regular.as_uri()}");\n'
        "  font-weight: 400;\n"
        "}"
    ]
    if bold.exists():
        faces.append(
            "@font-face {\n"
            '  font-family: "Vazirmatn";\n'
            f'  src: url("{bold.as_uri()}");\n'
            "  font-weight: 700;\n"
            "}"
        )
    return "\n".join(faces)


def _render_item(item: dict) -> str:
    body = str(item.get("html") or "")
    body = body.replace("<tg-spoiler>", "<span>").replace("</tg-spoiler>", "</span>")
    title = html.escape(str(item.get("title") or ""))
    category = html.escape(str(item.get("category") or ""))
    source = html.escape(str(item.get("source") or ""))
    url = html.escape(str(item.get("url") or ""), quote=True)
    return (
        '<section class="item">\n'
        f'  <div class="meta">{category} · {source}</div>\n'
        f"  <h2>{title}</h2>\n"
        f'  <div class="body">{body}</div>\n'
        f'  <div class="link"><a href="{url}">{url}</a></div>\n'
        "</section>"
    )


def build_daily_pdf(
    items: list[dict], output_path: Path, day: str | None = None
) -> Path | None:
    if not items:
        return None
    try:
        from weasyprint import HTML
    except Exception as exc:  # pragma: no cover - platform dependent
        log.warning("weasyprint unavailable: %s", exc.__class__.__name__)
        return None

    font_dir = config.ROOT_DIR / "assets" / "fonts"
    if not (font_dir / "Vazirmatn-Regular.ttf").exists():
        log.warning(
            "Vazirmatn fonts missing in %s; PDF skipped (see README)", font_dir
        )
        return None

    document = TEMPLATE.format(
        title="رادار امنیت",
        subtitle=(
            "خلاصه روزانه امنیت سایبری و هوش مصنوعی — "
            f"{html.escape(str(day or date.today()))} — {len(items)} تحلیل"
        ),
        footer=(
            "این گزارش با AI تولید شده است. پیش از استناد، منبع اصلی هر مورد را "
            "بررسی کنید."
        ),
        font_faces=_font_faces(font_dir),
        items="\n".join(_render_item(item) for item in items),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        HTML(string=document, base_url=str(config.ROOT_DIR)).write_pdf(
            str(output_path)
        )
    except Exception as exc:
        log.warning("pdf render failed: %s: %s", exc.__class__.__name__, exc)
        return None
    log.info("pdf built path=%s items=%d", output_path.name, len(items))
    return output_path


def pdf_caption(day: str, count: int) -> str:
    return (
        "📄 <b>گزارش روزانه امنیت سایبری و هوش مصنوعی</b>\n"
        f"🗓 <code>{html.escape(day)}</code> | {count} تحلیل\n"
        "<i>این گزارش با AI تولید شده — منابع اصلی را بررسی کنید.</i>"
    )

# رادار امنیت — ربات هوش تهدید سایبری و امنیت هوش مصنوعی

ربات کاملاً رایگان و سرورلس که هر ۴ ساعت جدیدترین تحلیل‌های **امنیت سایبری** و **AI Security** را از منابع معتبر جمع‌آوری می‌کند، با `Gemini` به فارسی خلاصه می‌کند و در تلگرام به شما می‌فرستد.

- اجرا روی **GitHub Actions** (بدون سرور، بدون هزینه)
- **بدون دیتابیس و بدون Google Sheets** — فقط یک فایل `data/state.json` که خود Workflow کامیت می‌کند
- هوش مصنوعی: **Google Gemini** (`gemini-2.5-flash`، قابل تغییر)
- تحویل: **Telegram Bot API** با HTML امن‌شده و پیام‌های حداکثر ۴۰۹۶ کاراکتری

---

## ۱) این ربات چه کاری انجام می‌دهد؟

```
GitHub Actions (هر ۴ ساعت)
        │
        ▼
  fetcher.py ── ۹ منبع، هر کدام ایزوله
        │        GitHub / Hacker News / Bluesky / Mastodon
        │        RSS امنیتی / RSS هوش مصنوعی / arXiv / CISA KEV / Reddit
        ▼
  filter.py ── فیلتر قطعی و رایگان (بدون API)
        │        حذف تکراری با SHA-256 لینک، حذف لیستیکل، امتیازدهی شفاف
        ▼
  analyzer.py ── یک فراخوانی دسته‌ای برای Triage + یک فراخوانی برای هر مورد
        │        بودجه سهمیه، backoff روی 429/503، fallback بدون AI
        ▼
  telegram_sender.py ── پاک‌سازی HTML، شکستن پیام، retry، fallback متنی
        │
        └── ارسال همه خلاصه‌ها به همه گیرنده‌های TELEGRAM_CHAT_IDS
```

هر منبع در `try/except` مستقل اجرا می‌شود؛ خرابی یک منبع هرگز کل اجرا را متوقف نمی‌کند.

---

## ۲) ساختار پروژه

```
.
├── config.py                    # منابع، کلیدواژه‌ها، وزن‌ها، بودجه‌ها، متن‌های فارسی
├── fetcher.py                   # ۹ منبع + استخراج متن کامل + README ریپوها
├── filter.py                    # Stage 1: dedup، فیلتر، امتیازدهی
├── analyzer.py                  # پرامپت‌های Gemini، triage، summarize، backoff
├── store.py                     # state.json: dedup، شمارنده AI، offset آپدیت‌ها
├── telegram_sender.py           # getUpdates، sanitizer، splitter، retry
├── pdf_report.py                # PDF فارسی روزانه (اختیاری)
├── main.py                      # هماهنگی، تایم‌زون تهران، مسیریابی تحویل
├── data/state.json              # وضعیت پایدار (خودکار کامیت می‌شود)
├── requirements.txt
├── .github/workflows/bot_runner.yml
├── assets/fonts/                # فونت وزیرمتن برای PDF
├── .env.example
└── README.md
```

---

## ۳) پیش‌نیازها

| مورد | لازم برای | رایگان؟ |
|---|---|---|
| حساب تلگرام + یک ربات از BotFather | ارسال پیام | بله |
| کلید Google AI Studio | خلاصه‌سازی | بله (سهمیه محدود) |
| حساب GitHub | اجرای Actions | بله |

Google Sheets، پروژه Google Cloud و Service Account **لازم نیست**.

---

## ۴) مرحله ۱ — ساخت ربات تلگرام

1. به [@BotFather](https://t.me/BotFather) پیام دهید.
2. دستور `/newbot` را بفرستید.
3. یک نام نمایشی و یک یوزرنیم انتخاب کنید (یوزرنیم باید به `bot` ختم شود).
4. BotFather یک توکن می‌دهد شبیه `123456789:AA...`. این مقدار را در جای امن نگه دارید → بعداً در Secret به نام `TELEGRAM_BOT_TOKEN` قرار می‌گیرد.

> این توکن مثل رمز عبور است. هیچ‌وقت آن را داخل کد، README یا چت عمومی نگذارید.

### آیدی عددی خودتان

ربات با دستور `/start` آیدی عددی شما را نشان می‌دهد. همین عدد را در `TELEGRAM_CHAT_IDS` می‌گذارید تا خلاصه‌ها به‌صورت خودکار برایتان بیاید.

### کانال (اختیاری)

اگر می‌خواهید خلاصه‌ها در یک کانال هم منتشر شوند:

1. ربات را با دسترسی **Post Messages** ادمین کانال کنید.
2. آیدی کانال را به `TELEGRAM_CHAT_IDS` اضافه کنید:
   - کانال عمومی: `@my_channel`
   - کانال خصوصی: عددی مثل `-1001234567890`

---

## ۵) مرحله ۲ — کلید Gemini

1. وارد [Google AI Studio → API Keys](https://aistudio.google.com/apikey) شوید.
2. `Create API key` را بزنید و پروژه را انتخاب کنید.
3. کلید را کپی کنید → Secret به نام `GEMINI_API_KEY`.

**نکات سهمیه رایگان:**

- سهمیه رایگان Gemini سقف **درخواست در روز** دارد و ممکن است تغییر کند. مقدار فعلی را در AI Studio ببینید.
- طبق شرایط سرویس، در پلن رایگان ممکن است داده‌های ارسالی برای بهبود محصول استفاده شود. تمام محتوای این ربات **عمومی** است، اما اگر این موضوع برایتان مهم است، پلن پرداختی را بررسی کنید.
- این پروژه طوری طراحی شده که با تمام‌شدن سهمیه متوقف نشود: خلاصه ساده «عنوان + لینک» ارسال می‌شود.

اگر مدل پیش‌فرض در دسترس نبود، یک متغیر Repository به نام `GEMINI_MODEL` بسازید و نام مدل موجود در AI Studio را بگذارید.

---

## ۶) مرحله ۳ — GitHub و Secrets

1. یک ریپازیتوری جدید بسازید: [github.com/new](https://github.com/new).
2. همه فایل‌های این پروژه را آپلود کنید. **فایل `.env` را هرگز آپلود نکنید.**
3. برو به `Settings → Secrets and variables → Actions` و این Secretها را بسازید:

| نام Secret | مقدار | استفاده |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | توکن BotFather | ارسال پیام |
| `GEMINI_API_KEY` | کلید AI Studio | خلاصه‌سازی |
| `TELEGRAM_CHAT_IDS` | آیدی‌های عددی با کاما، و/یا `@channel` | همه گیرنده‌های خلاصه |
| `ADMIN_CHAT_ID` | آیدی عددی خودتان (اختیاری) | گزارش خطا، اگر در `TELEGRAM_CHAT_IDS` نبود |

> `GITHUB_TOKEN` را نسازید؛ GitHub آن را خودکار در اختیار Workflow می‌گذارد.

4. اگر PDF می‌خواهید، در تب `Variables` یک متغیر به نام `ENABLE_PDF` با مقدار `true` بسازید. (بخش ۹)

### دسترسی نوشتن برای کامیت وضعیت

Workflow باید `data/state.json` را بعد از هر اجرا کامیت کند تا در اجرای بعدی تکراری‌ها ارسال نشوند:

1. `Settings → Actions → General`
2. در بخش **Workflow permissions** گزینه **Read and write permissions** را انتخاب و ذخیره کنید.

فایل Workflow هم `permissions: contents: write` دارد. اگر ریپو محافظت‌شده است، اجازه push برای `github-actions[bot]` را بدهید.

> `data/state.json` فقط هش لینک‌های ارسال‌شده و آمار مصرف AI را دارد؛ هیچ اطلاعات شخصی در آن نیست و برای ریپوی عمومی بی‌خطر است.

---

## ۷) مرحله ۴ — اولین اجرا

1. برو به تب **Actions** ریپازیتوری.
2. اگر پیام فعال‌سازی دیدید، `I understand my workflows, go ahead and enable them` را بزنید.
3. از ستون چپ `Security Intelligence Bot` را انتخاب کنید.
4. `Run workflow` → تیک `dry_run` را **بزنید** → `Run workflow`.
5. داخل لاگ اجرا ببینید:
   - `source=... items=...` برای هر منبع
   - `stage1 ... kept=...`
   - `candidate score=...`
   - خلاصه‌ها در بخش `DRY RUN PREVIEW`

در حالت dry-run هیچ پیامی ارسال نمی‌شود و `state.json` تغییر نمی‌کند. اگر خروجی درست بود، `/start` را به ربات بفرستید، آیدی را در `TELEGRAM_CHAT_IDS` بگذارید و بعد Workflow را بدون dry-run اجرا کنید.

---

## ۸) مرحله ۵ — زمان‌بندی هر ۴ ساعت

Workflow با cron زیر هر ۴ ساعت اجرا می‌شود:

```yaml
- cron: "0 */4 * * *"
```

ساعت‌های تهران: **۰۳:۳۰، ۰۷:۳۰، ۱۱:۳۰، ۱۵:۳۰، ۱۹:۳۰، ۲۳:۳۰**

در هر اجرا همه موارد تأییدشده (حداکثر `MAX_ITEMS_PER_RUN`) به همه گیرنده‌ها ارسال می‌شوند.

نکات مهم:

- cron گیت‌هاب **UTC** است و اجرا «best-effort» است؛ ممکن است ۱۵ تا ۶۰ دقیقه تأخیر داشته باشد یا به‌ندرت یک اجرا حذف شود.
- گیت‌هاب زمان‌بندی Workflowها را پس از **۶۰ روز بی‌فعالیتی** در ریپوهای عمومی غیرفعال می‌کند. هر چند وقت یک‌بار یک اجرای دستی بزنید یا یک commit کوچک بدهید.
- `concurrency` طوری تنظیم شده که دو اجرا هم‌زمان نشوند (`cancel-in-progress: false`).
- تاریخ‌ها با `Asia/Tehran` محاسبه می‌شوند، نه UTC.

---

## ۹) PDF روزانه (اختیاری)

فونت **Vazirmatn** (مجوز OFL) از قبل در `assets/fonts/` قرار دارد؛ اگر حذف شده بود، از [github.com/rastikerdar/vazirmatn/releases](https://github.com/rastikerdar/vazirmatn/releases) این دو فایل را دوباره بگیرید و commit کنید:

- `Vazirmatn-Regular.ttf`
- `Vazirmatn-Bold.ttf`

سپس متغیر Repository بسازید: `ENABLE_PDF = true`. در اولین اجرای بعد از ساعت ۱۸ تهران (اجرای ۱۹:۳۰) PDF ساخته و برای همه گیرنده‌ها ارسال می‌شود.

اگر فونت یا کتابخانه سیستمی موجود نباشد، PDF با یک هشدار رد می‌شود و بقیه ربات بدون مشکل کار می‌کند. فایل PDF تولیدشده در Artifactهای همان اجرا هم آپلود می‌شود.

---

## ۱۰) دستورهای ربات

| دستور | کار |
|---|---|
| `/start` | نمایش `Chat ID` شما برای دریافت خودکار |
| `/help` | راهنما |

ارسال خلاصه‌ها خودکار و هر ۴ ساعت انجام می‌شود؛ دیتابیس کاربری وجود ندارد.

---

## ۱۱) کجا چه‌چیزی را تغییر بدهم؟

| می‌خواهم… | فایل / محل |
|---|---|
| منبع جدید اضافه کنم | `config.py` → `RSS_SECURITY_FEEDS` / `RSS_AI_FEEDS` / `GITHUB_KEYWORDS` / `HN_QUERIES` |
| وزن منابع یا کلیدواژه‌ها را عوض کنم | `config.py` → `SOURCE_WEIGHTS` / `KEYWORD_WEIGHTS` |
| پرامپت خلاصه یا Triage را تغییر دهم | `analyzer.py` → `SUMMARIZE_SYSTEM_PROMPT` / `TRIAGE_SYSTEM_PROMPT` |
| متن خوش‌آمد یا راهنما را عوض کنم | `config.py` → `start_text()` / `help_text()` |
| گیرنده‌ها را عوض کنم | Secret `TELEGRAM_CHAT_IDS` |
| تعداد خلاصه در هر اجرا | `MAX_ITEMS_PER_RUN` (پیش‌فرض ۱۰) |
| سقف فراخوانی AI | `MAX_AI_CALLS_PER_RUN` / `DAILY_AI_BUDGET` |
| فاصله اجرا | cron در `.github/workflows/bot_runner.yml` |
| ساعت ارسال PDF | `PDF_SEND_HOUR` (پیش‌فرض ۱۸ به وقت تهران) |
| تعداد روز نگهداری dedup | `SENT_LOG_RETENTION_DAYS` (پیش‌فرض ۱۴) |
| آستانه پذیرش Triage | `TRIAGE_THRESHOLD` (پیش‌فرض ۷ از ۱۰) |
| مدل Gemini | متغیر `GEMINI_MODEL` |
| خاموش کردن یک منبع | متغیرهای `ENABLE_*` (مثلاً `ENABLE_REDDIT=false`) |
| سرورهای Mastodon | متغیر `MASTODON_HOSTS` (لیست جدا با کاما؛ پیش‌فرض `infosec.town,mastodon.social`) |
| سقف هر منبع در لیست کاندیدها | `MAX_PER_SOURCE_CANDIDATES` (پیش‌فرض ۶) |

---

## ۱۲) اجرای محلی

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env      # مقادیر را پر کنید
```

```bash
# بدون هیچ ارسال/نوشتن، بدون AI — برای تست منابع
python main.py --dry-run --no-ai

# تست کامل با AI، بدون ارسال
python main.py --dry-run --limit 5

# اجرای واقعی
python main.py
```

در ویندوز اگر می‌خواهید PDF بسازید، WeasyPrint به GTK نیاز دارد؛ می‌توانید `ENABLE_PDF=false` بگذارید و PDF را فقط روی GitHub Actions تولید کنید.

---

## ۱۳) عیب‌یابی

| خطا / علامت | علت | راه‌حل |
|---|---|---|
| `telegram rejected the bot token (401)` | توکن اشتباه | توکن BotFather را دوباره در Secret بگذارید |
| `telegram 409: a webhook is set` | وبهوک روی ربات ست شده | یک‌بار `https://api.telegram.org/bot<TOKEN>/deleteWebhook` را در مرورگر باز کنید |
| `403 chat not found` | آیدی اشتباه یا کاربر ربات را استارت نکرده | کاربر `/start` بفرستد و آیدی عددی را در `TELEGRAM_CHAT_IDS` بگذارید |
| `can't parse entities` | HTML نامعتبر | sanitizer باید جلوی آن را بگیرد؛ لاگ `parse failed ... retrying as plain` را ببینید |
| `404 model not found` | نام مدل Gemini منقضی شده | متغیر `GEMINI_MODEL` را با مدل موجود در AI Studio عوض کنید |
| `429` از Gemini | سهمیه/نرخ | فاصله اجرا را بیشتر یا `MAX_ITEMS_PER_RUN` را کمتر کنید؛ ربات خودش fallback می‌کند |
| هیچ پیامی نمی‌رسد | گیرنده تنظیم نشده | `/start` بفرستید و آیدی را در `TELEGRAM_CHAT_IDS` بگذارید؛ لاگ `no recipients` را ببینید |
| پیام‌ها تکراری می‌آیند | `state.json` کامیت نمی‌شود | بخش ۶ (Workflow permissions) را انجام دهید و لاگ `state saved` را ببینید |
| Workflow موقع کامیت خطا می‌دهد | دسترسی read-only یا branch protection | `Read and write permissions` را فعال کنید |
| Reddit خطا می‌دهد | 403/429 برای IP دیتاسنتر | طبیعی است؛ منبع ایزوله است و بقیه ادامه می‌دهند |
| Bluesky یا Mastodon خالی است | API عمومی تغییر کرده یا IP محدود شده | Mastodon را با متغیر `MASTODON_HOSTS` روی سرور دیگری بگذارید؛ Bluesky از `api.bsky.app` استفاده می‌کند |
| PDF ساخته نمی‌شود | فونت یا کتابخانه سیستمی نیست | فونت‌ها را در `assets/fonts` بگذارید و `ENABLE_PDF=true` کنید |
| Workflow اجرا نمی‌شود | زمان‌بندی غیرفعال شده | یک `Run workflow` دستی بزنید (بعد از ۶۰ روز بی‌فعالیتی) |

---

## ۱۴) امنیت و اعتماد

- همه Secretها فقط از محیط خوانده می‌شوند؛ لاگ‌ها توکن/کلید را redact می‌کنند.
- خروجی مدل همیشه از sanitizer عبور می‌کند: فقط تگ‌های مجاز تلگرام باقی می‌مانند و متن escape می‌شود.
- هر ریپوی تازه‌ساز GitHub با هشدار «بررسی‌نشده» ارسال می‌شود؛ ریپوهای مشکوک (README-only، اکانت جوان، ستاره نامتناسب) در متادیتا علامت می‌خورند.
- مدل اجازه ندارد CVE، نسخه، CVSS یا payload از خودش بسازد؛ لینک منبع همیشه همراه پیام است.
- خلاصه‌ها transformative هستند و متن کامل بازنشر نمی‌شود. به ToS منابع و paywall احترام بگذارید.
- هیچ اسکرپینگ X/Twitter انجام نمی‌شود؛ محتوای امنیتی از منبع اصلی (GitHub، بلاگ، arXiv، advisory) گرفته می‌شود.

---

## ۱۵) محدودیت‌های رایگان

- **Gemini**: سهمیه روزانه محدود و متغیر؛ ممکن است بعضی اجراها fallback شوند.
- **GitHub Actions**: برای ریپوهای عمومی رایگان است؛ زمان‌بندی ممکن است تأخیر داشته باشد.
- **Telegram**: حدود ۳۰ پیام در ثانیه؛ ربات بین پیام‌های یک چت فاصله می‌گذارد.
- **منابع**: APIهای عمومی ممکن است بدون اطلاع تغییر کنند یا محدود شوند؛ هر منبع ایزوله است و با خطا کل اجرا متوقف نمی‌شود.

---

## ۱۶) مجوز و مسئولیت

این پروژه برای پایش دفاعی و آموزشی ساخته شده است. استفاده از payloadها و ابزارهای ذکرشده فقط در محیط‌های مجاز و با اجازه کتبی قانونی است. مسئولیت استفاده بر عهده کاربر است.

# رادار امنیت — ربات هوش تهدید سایبری و امنیت هوش مصنوعی

ربات تحلیل‌های **امنیت سایبری** و **AI Security** را با هوش مصنوعی خلاصه می‌کند و در تلگرام می‌فرستد. معماری **ترکیبی** است: سرور وبهوک و **اسکالر داخلی** روی **Render**، و GitHub Actions فقط به‌عنوان بکاپ.

- **وبهوک روی Render**: پاسخ به `/start`، `/subscribe`، `/unsubscribe`، `/help` و دکمه‌های تنظیمات
- **دیتابیس SQLite** (یا Postgres) برای کاربران، کانال‌ها، خبرهای ارسال‌شده و صف حذف
- **زمان‌بندی داخلی**: ساعت ۰۸:۰۰، ۱۲:۰۰، ۱۶:۰۰، ۲۰:۰۰ و ۰۰:۰۰ به وقت تهران؛ اسلات ازدست‌رفته یک بار جبران می‌شود
- **خلاصهٔ متناسب با زبان کاربر**: خلاصهٔ هوش مصنوعی جداگانه به فارسی و انگلیسی، بررسی زبان خروجی و انتخاب زبان به‌روز کاربر هنگام ارسال
- **حذف خودکار ۲۴ ساعته**: فقط پیام‌های خبری حذف می‌شوند؛ پیام تنظیمات کاربر باقی می‌ماند
- هوش مصنوعی: **Google Gemini** با **تا ۳ کلید** و چرخش خودکار روی سهمیه
- **عضویت اجباری در کانال** `@aiceur_zero` با لینک و دکمهٔ تایید؛ **کیبورد ثابت** با `/start` و `/lang`
- **مقیاس‌پذیری**: تأخیر بین گیرندگان برای رعایت محدودیت نرخ تلگرام، و غیرفعال‌سازی خودکار کاربرانی که ربات را بلاک کرده‌اند
- **ضد اسپم**: حداکثر ۳ پیام در دقیقه؛ بیشتر از آن، ۵ دقیقه نادیده گرفته می‌شود

> **استقرار:** فایل `render.yaml` را به Render وصل کنید، در Render مقادیر `TELEGRAM_BOT_TOKEN`، کلیدهای Gemini و `ADMIN_CHAT_ID` را بگذارید، سپس در GitHub دو Secret با نام `RENDER_BASE_URL` و `TRIGGER_TOKEN` بسازید. راهنمای کامل در بخش «استقرار» پایین همین فایل است.

> اجرای محلی فقط برای آزمون است: تلگرام به وبهوک HTTPS نیاز دارد. برای اجرای محلی `python server.py` را با یک تونل (مثلاً ngrok) اجرا کنید. اگر ربات روی Render فعال است، هم‌زمان اجرای محلی نکنید.


---

## ۱) این ربات چه کاری انجام می‌دهد؟

```
رایانهٔ روشن (حالت محلی) یا GitHub Actions
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
├─ server.py             # Flask: /webhook، /trigger_news، /health، /set_webhook
├─ main.py               # یک اجرای خبر: fetch → filter → summarize → deliver → expire
├─ storage.py            # SQLite/Postgres: users، channels، news_messages، sent_items، settings
├─ telegram_sender.py    # Bot API، HTML، retry، وبهوک و دستورهای دوزبانه (fa/en)
├─ analyzer.py           # Gemini (fa/en)، triage، summarize، سهمیه و retry
├─ fetcher.py            # منابع RSS/GitHub/HN + استخراج متن کامل
├─ filter.py             # Stage 1: dedup، تازگی، امتیازدهی اولیه
├─ channels_store.py     # نرمال‌سازی نام کانال + وضعیت عضویت
├─ channels_config.py    # کانال‌های اجباری (username + لینک) — بدون نیاز به AI
├─ config.py             # تنظیمات از متغیرهای محیطی
├─ render.yaml           # Blueprint استقرار روی Render (دیسک، env، health)
├─ requirements-web.txt  # وابستگی‌های Render (شامل Flask و gunicorn)
├─ requirements.txt      # وابستگی‌های اجرای محلی/تست
├─ .github/workflows/news_cron.yml  # زمان‌بندی تهران → POST /trigger_news
└─ data/bot.db           # دیتابیس SQLite (روی Render روی دیسک /var/data)
```

> فایل‌های `store.py` و `users_store.py` فقط برای سازگاری اجرای قدیمی مانده‌اند؛ اجرای جدید از `storage.py` استفاده می‌کند.

---

## ۳) پیش‌نیازها

- یک ربات تلگرام از @BotFather (توکن)
- حداقل یک کلید Google Gemini (`GEMINI_API_KEY` یا `_1.._3`)
- حساب Render (پلن Free کافی است)
- مخزن GitHub برای اجرای زمان‌بندی

---

## ۴) استقرار روی Render

۱. مخزن را به Render وصل کنید: **New → Blueprint** و فایل `render.yaml` را انتخاب کنید.

۲. در تب **Environment** این مقادیر را بگذارید:

| متغیر | مقدار |
|---|---|
| `TELEGRAM_BOT_TOKEN` | توکن ربات |
| `GEMINI_API_KEY` | کلید Gemini (یا `_1`..`_3` برای چند کلید) |
| `ADMIN_CHAT_ID` | شناسهٔ عددی خودتان برای آمار و هشدارها |
| `TELEGRAM_CHAT_IDS` | (اختیاری) گیرندگان ثابت، مثلاً یک کانال |
| `WEBHOOK_SECRET` | خودکار ساخته می‌شود؛ دست نزنید |
| `TRIGGER_TOKEN` | خودکار ساخته می‌شود؛ همان را در GitHub Secret بگذارید |
| `DB_PATH` | `/tmp/bot.db` (پیش‌فرض پلن Free؛ اگر مسیر غیرقابل‌نوشتن باشد خودکار به temp می‌رود) |

۳. بعد از اولین دیپلوی، آدرس سرویس را بردارید (مثل `https://my-bot.onrender.com`) و یک بار وبهوک را تنظیم کنید:

```bash
curl -X POST https://my-bot.onrender.com/set_webhook \
  -H "Authorization: Bearer <TRIGGER_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"base_url": "https://my-bot.onrender.com"}'
```

۴. سلامت سرویس: `https://my-bot.onrender.com/health` باید `{"ok": true}` بدهد.

> **مهم:** در پلن Free دیسک ماندگار پشتیبانی نمی‌شود (بلوک `disk` از `render.yaml` حذف شده است). برای نگه‌داشتن دائمی کاربران، یک Postgres رایگان (مثلاً Neon) بسازید و `DATABASE_URL` را تنظیم کنید؛ بدون آن داده‌ها با هر ری‌استارت پاک می‌شوند (هرچند هر عضو با اولین دکمه دوباره ثبت‌نام می‌شود).

---

## ۵) زمان‌بندی ارسال و بکاپ GitHub Actions

منبع اصلی زمان‌بندی، **اسکالر داخل خود سرویس Render** است (`scheduler.py`). تا وقتی سرویس بیدار است، در ساعت‌های ۰۰:۰۰، ۰۸:۰۰، ۱۲:۰۰، ۱۶:۰۰ و ۲۰:۰۰ تهران خودش خبر را می‌فرستد و اگر Render خواب باشد و بیدار شود، **اسلات ازدست‌رفته را یک بار جبران می‌کند** (هر اسلات حداکثر یک بار؛ اسلات خراب حداکثر ۳ تلاش).

GitHub Actions فقط **بکاپ** و رفع نیاز به بیدارباش دستی است. اگر Workflow شکست بخورد، ارسال متوقف نمی‌شود.

| Secret | مقدار |
|---|---|
| `RENDER_BASE_URL` | آدرس سرویس Render |
| `TRIGGER_TOKEN` | همان مقدار `TRIGGER_TOKEN` در Render |

| تهران | UTC (cron) |
|---|---|
| 08:00 | `35 4 * * *` |
| 12:00 | `35 8 * * *` |
| 16:00 | `35 12 * * *` |
| 20:00 | `35 16 * * *` |
| 00:00 | `35 20 * * *` |

وضعیت اسکالر در `GET /health` زیر کلید `scheduler` دیده می‌شود (`next_run`). برای خاموش‌کردن آن `SCHEDULER_ENABLED=false` بگذارید.

اجرای دستی برای آزمون: **Actions → News Cron → Run workflow** و گزینهٔ `dry_run` را روشن کنید.

---

## ۶) دستورهای ربات

| دستور | چه کسی | کار |
|---|---|---|
| `/start` | همه | ثبت‌نام؛ بار اول پیام خوش‌آمد به زبان کاربر، بارهای بعد صفحهٔ تنظیمات |
| `/lang` | همه | انتخاب زبان با دکمه‌های «فارسی 🇮🇷» و «English 🇬🇧» |
| `/subscribe` | همه | فعال‌کردن اشتراک خبر |
| `/unsubscribe` | همه | غیرفعال‌کردن اشتراک خبر |
| `/help` | همه | راهنما |
| `/stats` | فقط ادمین | تعداد کل کاربران، مشترک‌ها و فعال‌ها در ۳۰ روز اخیر |
| `/addchannel @channel` | فقط ادمین | افزودن کانال اجباری (روش پیشنهادی: `channels_config.py`) |
| `/removechannel @channel` | فقط ادمین | حذف کانال اجباری |

- **ادمین** یعنی `chat_id` شما برابر Secret `ADMIN_CHAT_ID` باشد. دستورهای مدیریتی فقط برای همین شناسه کار می‌کنند.
- **جعبهٔ اشتراک** با دکمه‌های `toggle_subscription`، `lang_menu` و `help` هم کار می‌کند؛ نیازی به تایپ دستور نیست.
- **عضویت اجباری**: کانال اجباری `@aiceur_zero` در `channels_config.py` تعریف شده است (کانال‌های `/addchannel` هم اضافه می‌شوند). کاربر غیرعضو پیام عضویت با لینک کانال و دکمهٔ «✅ تایید عضویت» (`check_membership_aiceur_zero`) می‌گیرد و تا عضو نشدن ثبت‌نام نمی‌شود؛ با زدن دکمه عضویتش دوباره بررسی می‌شود. **ربات باید در کانال ادمین باشد**؛ اگر نباشد بررسی آن کانال رد می‌شود تا کاربر قفل نشود.
- **زبان**: هنگام `/start` زبان از `language_code` تلگرام تشخیص داده می‌شود (هر کد `fa*` فارسی، بقیه انگلیسی، بدون کد فارسی). با `/lang` قابل تغییر است و در ستون `language` دیتابیس ذخیره می‌شود. هر کاربر خلاصهٔ خبر را به زبان خودش می‌گیرد و پرامپت AI هم همان زبان را الزام می‌کند.
- **ضد اسپم**: بیش از ۳ پیام در دقیقه = ۵ دقیقه سکوت. ادمین معاف است.
- **ارسال**: هر کسی `/start` بزند و مشترک بماند گیرنده است؛ خبرهای تازه در اسلات‌های تعیین‌شده می‌رسند و فقط هر خبر ۲۴ ساعت بعد حذف می‌شود.

---

## ۷) کجا چه‌چیزی را تغییر بدهم؟

| می‌خواهم… | فایل / محل |
|---|---|
| منبع جدید اضافه کنم | `config.py` → `RSS_SECURITY_FEEDS` / `RSS_AI_FEEDS` / `GITHUB_KEYWORDS` / `HN_QUERIES` |
| وزن منابع یا کلیدواژه‌ها را عوض کنم | `config.py` → `SOURCE_WEIGHTS` / `KEYWORD_WEIGHTS` |
| پرامپت خلاصه یا Triage را تغییر دهم | `analyzer.py` → `SUMMARIZE_SYSTEM_PROMPT(_EN)` / `TRIAGE_SYSTEM_PROMPT` |
| متن خوش‌آمد، راهنما، تنظیمات یا دکمه‌ها را عوض کنم | `config.py` → `welcome_text()` / `help_text()` / `settings_text()` / `join_required_text()` |
| کانال اجباری اضافه/حذف کنم | `channels_config.py` → `REQUIRED_CHANNELS` (username + link) |
| تشخیص زبان یا زبان پیش‌فرض را عوض کنم | `config.py` → `detect_language()` / `SUPPORTED_LANGS` / `DEFAULT_LANG` |
| گیرنده‌ها را عوض کنم | Secret `TELEGRAM_CHAT_IDS` (کاربران `/start` خودکار اضافه می‌شوند) |
| تعداد خلاصه در هر اجرا | `MAX_ITEMS_PER_RUN` (پیش‌فرض ۱۰) |
| سقف فراخوانی AI | `MAX_AI_CALLS_PER_RUN` / `DAILY_AI_BUDGET` |
| ساعت‌های ارسال | `scheduler.py` → `RUN_HOURS` |
| ساعت ارسال PDF | `PDF_SEND_HOUR` (پیش‌فرض ۱۸ به وقت تهران) |
| تعداد روز نگهداری dedup | `SENT_LOG_RETENTION_DAYS` (پیش‌فرض ۱۴) |
| آستانه پذیرش Triage | `TRIAGE_THRESHOLD` (پیش‌فرض ۷ از ۱۰) |
| مدل Gemini | متغیر `GEMINI_MODEL` |
| تعداد کلیدهای Gemini | `config.py` → `_gemini_keys()` (پیش‌فرض ۳ عدد: `GEMINI_API_KEY_1..3`) |
| سقف پیام و مدت سکوت ضد اسپم | `USER_RATE_MAX_MESSAGES` / `USER_RATE_WINDOW_SECONDS` / `USER_MUTE_SECONDS` |
| بازه «کاربر فعال» در آمار | `STATS_ACTIVE_DAYS` (پیش‌فرض ۳۰) |
| خاموش کردن یک منبع | متغیرهای `ENABLE_*` (مثلاً `ENABLE_REDDIT=false`) |
| سرورهای Mastodon | متغیر `MASTODON_HOSTS` (لیست جدا با کاما؛ پیش‌فرض `infosec.town,mastodon.social`) |
| سقف هر منبع در لیست کاندیدها | `MAX_PER_SOURCE_CANDIDATES` (پیش‌فرض ۶) |

---

## ۸) اجرای محلی و تست

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt flask
cp .env.example .env      # سپس مقادیر را پر کنید
```

### تست‌ها (بدون شبکه)

```bash
python -m unittest discover -s tests -p "test_*.py"
```

### یک اجرای خبر از خط فرمان

```bash
# دریافت و خلاصه بدون ارسال (بدون AI)
python main.py --dry-run --no-ai

# دریافت، خلاصه با AI، بدون ارسال
python main.py --dry-run --limit 5

# فقط پاک‌سازی پیام‌های قدیمی‌تر از ۲۴ ساعت
python main.py --cleanup-only

# یک اجرای واقعی (ارسال به کاربران)
python main.py
```

### اجرای سرور وبهوک

telegram به HTTPS نیاز دارد، پس برای تست محلی یک تونل لازم است:

```bash
python server.py                 # روی پورت $PORT یا 8080
ngrok http 8080                  # آدرس https را بردارید

curl -X POST http://127.0.0.1:8080/set_webhook \
  -H "Authorization: Bearer $TRIGGER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"base_url": "https://<ngrok-host>"}'
```

> اگر ربات روی Render فعال است، هم‌زمان اجرای محلی وبهوک را تنظیم نکنید؛ فقط یکی از آن‌ها می‌تواند وبهوک تلگرام را در دست داشته باشد.

---

## ۹) عیب‌یابی

| خطا / علامت | علت | راه‌حل |
|---|---|---|
| `telegram rejected the bot token (401)` | توکن اشتباه | توکن BotFather را دوباره در Secret بگذارید |
| `telegram 409: a webhook is set` | وبهوک روی ربات ست شده | یک‌بار `https://api.telegram.org/bot<TOKEN>/deleteWebhook` را در مرورگر باز کنید |
| `403 chat not found` | آیدی اشتباه یا کاربر ربات را استارت نکرده | کاربر `/start` بفرستد و آیدی عددی را در `TELEGRAM_CHAT_IDS` بگذارید |
| `can't parse entities` | HTML نامعتبر | sanitizer باید جلوی آن را بگیرد؛ لاگ `parse failed ... retrying as plain` را ببینید |
| `404 model not found` | نام مدل Gemini منقضی شده | متغیر `GEMINI_MODEL` را با مدل موجود در AI Studio عوض کنید |
| `429` از Gemini | سهمیه/نرخ | فاصله اجرا را بیشتر یا `MAX_ITEMS_PER_RUN` را کمتر کنید؛ ربات خودش fallback می‌کند |
| هیچ پیامی نمی‌رسد | گیرنده تنظیم نشده | `/start` بفرستید و آیدی را در `TELEGRAM_CHAT_IDS` بگذارید؛ لاگ `no recipients` را ببینید |
| پیام‌ها تکراری می‌آیند | قفل اجرای هم‌زمان یا خطای شبکه | در `/news_status` دنبال `running` بگردید و لاگ Render را ببینید |
| Workflow موقع کامیت خطا می‌دهد | دسترسی read-only یا branch protection | `Read and write permissions` را فعال کنید |
| Reddit خطا می‌دهد | 403/429 برای IP دیتاسنتر | طبیعی است؛ منبع ایزوله است و بقیه ادامه می‌دهند |
| Bluesky یا Mastodon خالی است | API عمومی تغییر کرده یا IP محدود شده | Mastodon را با متغیر `MASTODON_HOSTS` روی سرور دیگری بگذارید؛ Bluesky از `api.bsky.app` استفاده می‌کند |
| PDF ساخته نمی‌شود | فونت یا کتابخانه سیستمی نیست | فونت‌ها را در `assets/fonts` بگذارید و `ENABLE_PDF=true` کنید |
| Workflow اجرا نمی‌شود | زمان‌بندی غیرفعال شده | یک `Run workflow` دستی بزنید (بعد از ۶۰ روز بی‌فعالیتی) |
| کاربر جدید در آمار نیست | کاربر `/start` نزده یا `DATABASE_URL` بین ری‌استارت‌ها عوض شده | `/health` و لاگ Render را ببینید؛ برای ماندگاری از Postgres یا دیسک استفاده کنید |
| `/stats` جواب نمی‌دهد | `ADMIN_CHAT_ID` تنظیم نشده یا با آیدی شما فرق دارد | Secret را با آیدی عددی خودتان پر کنید |
| `/addchannel` کار نمی‌کند | ربات در کانال ادمین نیست | ربات را با دسترسی **Post Messages** ادمین کانال کنید؛ لاگ `join check skipped` را ببینید |
| کاربر واقعاً عضو کانال است ولی مسدود می‌شود | آیدی کانال اشتباه است | آیدی را با `@` و یوزرنیم دقیق بدهید (`/addchannel @mychannel`) |
| کاربر بعد از چند پیام جواب نمی‌گیرد | ضد اسپم فعال شده | طبیعی است؛ بعد از ۵ دقیقه خودکار آزاد می‌شود |

---

## ۱۰) امنیت و اعتماد

- همه Secretها فقط از محیط خوانده می‌شوند؛ لاگ‌ها توکن/کلید را redact می‌کنند.
- خروجی مدل همیشه از sanitizer عبور می‌کند: فقط تگ‌های مجاز تلگرام باقی می‌مانند و متن escape می‌شود.
- هر ریپوی تازه‌ساز GitHub با هشدار «بررسی‌نشده» ارسال می‌شود؛ ریپوهای مشکوک (README-only، اکانت جوان، ستاره نامتناسب) در متادیتا علامت می‌خورند.
- مدل اجازه ندارد CVE، نسخه، CVSS یا payload از خودش بسازد؛ لینک منبع همیشه همراه پیام است.
- خلاصه‌ها transformative هستند و متن کامل بازنشر نمی‌شود. به ToS منابع و paywall احترام بگذارید.
- هیچ اسکرپینگ X/Twitter انجام نمی‌شود؛ محتوای امنیتی از منبع اصلی (GitHub، بلاگ، arXiv، advisory) گرفته می‌شود.

---

## ۱۱) محدودیت‌های پلن رایگان

- **Gemini**: سهمیه روزانه محدود و متغیر؛ ممکن است بعضی اجراها fallback شوند.
- **GitHub Actions**: برای ریپوهای عمومی رایگان است؛ زمان‌بندی ممکن است تأخیر داشته باشد.
- **Telegram**: حدود ۳۰ پیام در ثانیه؛ ربات بین پیام‌های یک چت فاصله می‌گذارد.
- **منابع**: APIهای عمومی ممکن است بدون اطلاع تغییر کنند یا محدود شوند؛ هر منبع ایزوله است و با خطا کل اجرا متوقف نمی‌شود.

---

## ۱۲) مجوز و مسئولیت

این پروژه برای پایش دفاعی و آموزشی ساخته شده است. استفاده از payloadها و ابزارهای ذکرشده فقط در محیط‌های مجاز و با اجازه کتبی قانونی است. مسئولیت استفاده بر عهده کاربر است.

---

## ۱۳) به‌روزرسانی کد و استقرار (Git)

هر تغییری در کد یا README فقط بعد از push به GitHub روی ربات اعمال می‌شود؛ GitHub Actions نسخه‌ی داخل ریپازیتوری را اجرا می‌کند.

```bash
git status
git add .
git commit -m "feat: add language selection, multi-api keys, user stats, admin channel management, rate limit"
git push
```

- در ویندوز همین دستورها را در PowerShell یا Git Bash داخل پوشه پروژه بزنید.
- اگر اولین push به شاخه `main` است، ممکن است GitHub نیاز به تنظیم upstream داشته باشد: `git push -u origin main`.
- قبل از push مطمئن شوید `.env` کامیت نمی‌شود (در `.gitignore` هست).
- دیتابیس `data/bot.db` **نیازی به ساخت دستی ندارد**؛ `storage.py` جدول‌ها را در اولین اجرا می‌سازد. فایل دیتابیس را کامیت نکنید (در `.gitignore` هست).
- بعد از push، از تب **Actions** می‌توانید `Run workflow` را بزنید تا بدون انتظار برای cron اجرا شود.

---

## ۱۴) عکس و بیوی ربات در BotFather

عکس پروفایل و متن معرفی از داخل کد قابل تنظیم نیستند و فقط در BotFather تغییر می‌کنند:

1. در تلگرام به [@BotFather](https://t.me/BotFather) پیام دهید.
2. `/mybots` → ربات خودتان را انتخاب کنید.
3. **Edit Bot** → **Edit Botpic**: یک عکس مربعی (حداقل ۳۲۰×۳۲۰) بفرستید.
4. **Edit Bot** → **Edit About**: یک متن کوتاه (حدود ۱۲۰ کاراکتر) برای صفحه‌ی ربات بگذارید.
5. **Edit Bot** → **Edit Description**: توضیح بلندتری که قبل از `/start` دیده می‌شود.
6. اختیاری: **Edit Commands** → این لیست را ثبت کنید تا کاربر با `/` ببیند:

```text
start - شروع و انتخاب زبان
lang - تغییر زبان
help - راهنما
```

دستورهای `/stats`، `/addchannel` و `/removechannel` را در لیست عمومی نگذارید؛ مخفی و مخصوص ادمین هستند.

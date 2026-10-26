# langfeed
بات Telethon روی GitHub Actions: هر ۶ ساعت فیدهای feeds.json را می‌خواند و فقط آیتم‌های جدید را در تاپیکِ همان سایت می‌فرستد. صوت‌ها به‌صورت Audio (پلیر موزیک تلگرام، قابل پخش قبل از دانلود کامل) ارسال می‌شوند.

## راه‌اندازی
1. ریپوی جدید و خصوصی بساز و این فایل‌ها را بریز. (ریپو و بات جدا از پروژه‌ی اصلی، به‌خاطر AuthKeyDuplicated)
2. گروه سوپرگروه + Topics روشن؛ بات ادمین با دسترسی Manage Topics.
3. روی یک ماشین: `pip install telethon && python scripts/make_session.py` → مقدار TG_SESSION.
4. Secrets: TG_API_ID, TG_API_HASH, TG_SESSION, TG_CHAT_ID (مثل -100xxxxxxxxxx).
5. Actions → check_feeds → Run: آدرس فیدها را تأیید کن (VOA و BNE حدسی‌اند) و در feeds.json اصلاح کن.
6. Actions → run → Run workflow. اجرای اول فقط ۳ آیتم آخر هر سایت را می‌فرستد و بقیه را «دیده‌شده» علامت می‌زند.

## کاتالوگ سایت‌ها (اسکنر)
`sites.json` فهرست ۲۴ سایت یادگیری است. workflow به نام **scan** (هفتگی یا دستی) هر سایت را (sitemap + دنبال کردن لینک‌ها؛ BBC با date-probe) می‌خزد و صفحه‌ها و لینک فایل‌ها (pdf, mp3, ...) را در D1 می‌نویسد؛ چیزی دانلود نمی‌شود.
- بار اول: Actions → **init_db** را دوباره اجرا کن (جدول‌های `cat_*` اضافه می‌شوند) و بعد **deploy-worker**.
- Actions → **scan** → Run: `sites` = `bbc,peg` برای چند سایت، `url` برای سایت دلخواه، `ia` برای جست‌وجوی Internet Archive.
- سایتی که در یک اجرا (حدود ۵ ساعت) تمام نشود، اجرای بعدی از همان‌جا ادامه می‌دهد (در `/catalog` با ⏳).
- دستورهای تلگرام: `/catalog`، `/sections bbc`، `/search present perfect`، `/search bbc gold`، `/files bbc pdf 10`.
- محدودیت رایگان D1: ۱۰۰ هزار نوشتن در روز؛ اگر کل سایت‌ها زیاد شد، با `sites` در چند روز پخش کن.

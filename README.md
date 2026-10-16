# FX Telegram Bot — Standalone v2

نسخه مستقل و رایگان ربات تلگرام فارکس، جدا از Pokee.

## امکانات v2
- Gemini API + Google Search grounding برای داده‌های زنده
- تحلیل چندلایه بازار با `/analyze EURUSD H1`
- دستیار News Trading با `/news-trade EURUSD`
- Market Scanner با `/scan`
- Risk Calculator با `/risk`
- Daily Brief با `/brief`
- هشدار قیمت با `/alert` و مدیریت هشدار از Mini App مالک
- Mini App مالک با طراحی جدید و کنترل قابلیت‌ها
- سهمیه روزانه اختصاصی برای هر کاربر؛ `0` یعنی نامحدود
- حالت Maintenance و مدیریت کاربران/درخواست‌ها
- گزارش روزانه خودکار
- Broadcast به کاربران مجاز
- هیچ Telegram/ngrok secret قدیمی داخل repository قرار نمی‌گیرد

## Environment variables
Required:
- `TELEGRAM_BOT_TOKEN`
- `GEMINI_API_KEY`
- `OWNER_ID` (numeric Telegram user ID of the owner)

Recommended for owner-panel security:
- `PANEL_ACCESS_TOKEN`: independent, long random secret for opening the panel outside Telegram. Never reuse the bot token. If set, open the panel with `/#token=YOUR_SECRET`; the token is read from the URL fragment (not sent in the HTTP request URL) and immediately removed from the address bar.
- `PANEL_ALLOWED_ORIGIN`: optional exact origin to allow for cross-origin browser access. Leave blank for same-origin hosting.

Optional:
- `GEMINI_MODEL` (default: `gemini-2.5-flash`)
- `GEMINI_SEARCH` (default: `true`)
- `PORT` (Render supplies this)

## Local
```bash
pip install -r requirements.txt
python bot.py
```

For deployment, use the `render.yaml` in the folder you actually deploy. The repository-root package starts with `python bot.py`; the nested `fx-telegram-bot-v3.zip` package starts with `python telegram-bot/bot.py`. Do not mix the two directory layouts.


## V3 feature pack
- Free / Pro / VIP plans with daily quotas
- XP, levels, activity streaks and trading statistics
- Personal Watchlist
- Trading Journal with result tracking
- User-owned smart price alerts
- Market analysis, news trading, scanner, risk calculator and daily brief
- Owner Command Center controls plans, limits, features, users and broadcasts

### Telegram commands
`/plan`, `/profile`, `/watchlist`, `/journal`, `/trade`, `/trade-result`, `/analyze`, `/news-trade`, `/scan`, `/risk`, `/brief`, `/alert`

No Pokee runtime is required. Configure `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, and `OWNER_ID`.

## Security and correctness updates
- Telegram Mini App `initData` is verified with a 24-hour freshness window and duplicate-key rejection.
- Panel API credentials are no longer derived from the public Telegram bot token; owner authentication uses Telegram `initData` or a separate `PANEL_ACCESS_TOKEN`.
- JSON writes are atomic and file permissions are restricted where supported.
- Repeated trade-result submissions no longer double-count win/loss statistics or award XP repeatedly.
- Trade prices and buy/sell stop/target ordering are validated before journal insertion.
- Price alerts notify their creator (legacy alerts without an owner ID go to `OWNER_ID`).
- Panel cross-origin access is not wildcard-enabled.
- The owner panel uses Lenis and GSAP for motion, and a local React SpotlightCard adaptation based on the React Bits copy-paste component approach. External CDN access is required for those visual enhancements; the base panel remains usable if they fail to load.

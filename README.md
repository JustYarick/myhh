# AutoHH - Auto-apply to HH.ru with AI

Telegram-managed auto-apply bot for HH.ru with Gemini AI cover letters and vacancy analysis.

## Features

- Auto-apply to HH.ru vacancies
- Gemini AI generates cover letters (2-3 sentences, human-like)
- AI analyzes and filters vacancies by relevance
- Full control via Telegram bot
- Anti-fraud: random delays, rate limits, UA rotation
- Docker deployment
- SQLite for history and stats

## Setup

### 1. Clone and configure

```bash
cp .env.example .env
# Edit .env with your tokens:
# - TG_BOT_TOKEN: from @BotFather
# - TG_ALLOWED_USERS: your Telegram user ID
# - GEMINI_API_KEY: from Google AI Studio
```

### 2. Start with Docker

```bash
docker-compose up -d
```

### 3. Login to HH.ru

```bash
docker exec -it autohh python -m autohh --login
```

Follow the browser prompts to log in. Session is saved automatically.

### 4. Control via Telegram

Send `/start` to your bot. Use the menu:

- **Start** - begin auto-applying
- **Stop** - stop the process
- **Status** - current state
- **Settings** - configure everything
- **Stats** - today's statistics
- **History** - recent applications
- **Login** - re-login to HH.ru

## Configuration

All settings configurable via Telegram bot or `.env`:

| Setting | Default | Description |
|---------|---------|-------------|
| SEARCH_TEXT | Python Developer | Search query |
| AREA_CODE | 113 | Region (113 = Russia) |
| MAX_PAGES | 3 | Pages to process |
| MAX_APPS_PER_DAY | 50 | Daily limit |
| MAX_APPS_PER_HOUR | 10 | Hourly limit |
| DELAY_MIN | 5 | Min delay between applies (sec) |
| DELAY_MAX | 15 | Max delay between applies (sec) |
| PROXY_URL | - | SOCKS5/HTTP proxy |

## Anti-Fraud

- Random delays between applies (5-15s by default)
- Configurable daily/hourly limits
- User-Agent rotation (8 realistic UAs)
- Viewport size randomization
- Random scroll behavior
- Auto-pause on captcha detection
- Pause after 3 consecutive errors

## Project Structure

```
autohh/
├── config.py          # Settings from .env + DB overrides
├── database.py        # SQLite: applied vacancies, settings, stats
├── models.py          # Pydantic models
├── anti_fraud.py      # Delays, limits, UA rotation
├── scheduler.py       # Main automation loop
├── bot/
│   ├── app.py         # Telegram bot setup
│   ├── handlers.py    # Command handlers
│   └── keyboards.py   # Inline keyboards
├── services/
│   ├── browser.py     # Playwright browser manager
│   ├── hh_search.py   # Vacancy search
│   ├── hh_apply.py    # Apply to vacancies
│   ├── hh_auth.py     # HH.ru login
│   └── gemini.py      # Gemini AI integration
├── __main__.py        # Entry point
├── Dockerfile
└── docker-compose.yml
```

# 📸 Instagram Monitor Bot

A production-ready Discord bot that monitors Instagram account availability and sends real-time DM alerts when accounts go down or are restored.

---

## ✨ Features

| Feature | Details |
|---|---|
| Slash commands | `/track`, `/untrack`, `/list`, `/status`, `/history` |
| Admin commands | `/broadcast`, `/analytics` |
| Smart scheduler | Queue-based, jittered intervals (2–5 min normal, 30–60 sec on alert) |
| Rate limit protection | Token bucket + per-account exponential backoff |
| Persistent storage | SQLite with WAL mode (Railway volume ready) |
| Fully async | `asyncio` + `httpx` + `aiosqlite` |
| Multi-user | Each user tracks up to 20 accounts independently |
| Notifications | Discord DM on status change (down / restored) |

---

## 📁 Project Structure

```
insta-monitor-bot/
├── bot.py          # Discord bot + slash commands
├── monitor.py      # Async scheduler + HTTP checker
├── db.py           # SQLite persistence layer
├── config.py       # All config loaded from ENV
├── utils.py        # Helpers: embeds, rate limiter, time utils
├── requirements.txt
├── Procfile        # Railway / Heroku process definition
├── railway.json    # Railway deployment config
├── .env.example    # Template for environment variables
└── .gitignore
```

---

## 🚀 Quick Start (Local)

### 1. Clone & install dependencies

```bash
git clone https://github.com/yourname/insta-monitor-bot.git
cd insta-monitor-bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create your Discord bot

1. Go to https://discord.com/developers/applications
2. Click **New Application** → give it a name
3. Go to **Bot** tab → click **Add Bot**
4. Under **Token** → click **Reset Token** and copy it
5. Under **Privileged Gateway Intents** — you don't need any for this bot
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages` (for DMs)
7. Copy the generated URL and open it to invite the bot to your server

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
DISCORD_TOKEN=your_token_here
ADMIN_USER_IDS=your_discord_user_id
DATABASE_PATH=data/monitor.db
```

> **How to find your Discord User ID**: Enable Developer Mode in Discord Settings → User Settings → Advanced → Developer Mode. Then right-click your username → Copy ID.

### 4. Run the bot

```bash
python bot.py
```

You should see:
```
2024-01-01 00:00:00 | INFO     | __main__ | Logged in as YourBot#1234 (ID: ...)
2024-01-01 00:00:00 | INFO     | monitor  | Monitor scheduler started.
```

---

## ☁️ Railway Deployment (Production)

### Step 1 — Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/yourname/insta-monitor-bot.git
git push -u origin main
```

### Step 2 — Create Railway project

1. Go to https://railway.app and log in
2. Click **New Project** → **Deploy from GitHub repo**
3. Select your repository
4. Railway will auto-detect the Python project

### Step 3 — Add a Persistent Volume (critical for SQLite)

Railway's filesystem is ephemeral — without a volume, your database resets on every deploy.

1. In your Railway project, click your service
2. Go to **Volumes** tab
3. Click **Add Volume**
4. Mount path: `/data`
5. Click **Create**

Then set your env var: `DATABASE_PATH=/data/monitor.db`

### Step 4 — Set Environment Variables

In Railway dashboard → your service → **Variables** tab, add:

```
DISCORD_TOKEN          = your_discord_bot_token
ADMIN_USER_IDS         = your_discord_user_id
DATABASE_PATH          = /data/monitor.db
MAX_REQUESTS_PER_MINUTE = 30
LOG_LEVEL              = INFO
```

Add any other variables from `.env.example` that you want to customise.

### Step 5 — Deploy

Railway deploys automatically on every push to `main`. 

To deploy manually: go to **Deployments** tab → **Deploy Now**.

### Step 6 — Verify

Check the **Logs** tab in Railway. You should see:
```
INFO | Logged in as YourBot#1234
INFO | Monitor scheduler started.
```

---

## 🎮 Bot Commands

### User Commands

| Command | Description |
|---|---|
| `/track <username>` | Start monitoring an Instagram account |
| `/untrack <username>` | Stop monitoring an account |
| `/list` | Show all your tracked accounts with their current status |
| `/status <username>` | Check the current known status of an account |
| `/history <username>` | View recent status change events for an account |

### Admin Commands (ADMIN_USER_IDS only)

| Command | Description |
|---|---|
| `/broadcast <message>` | Send a DM to all registered users |
| `/analytics` | View global monitoring statistics |

---

## ⚙️ Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | — | **Required.** Your Discord bot token |
| `ADMIN_USER_IDS` | — | Comma-separated Discord user IDs for admins |
| `DATABASE_PATH` | `data/monitor.db` | Path to SQLite database file |
| `MAX_REQUESTS_PER_MINUTE` | `30` | Global HTTP request rate cap |
| `SEMAPHORE_LIMIT` | `5` | Max concurrent HTTP checks |
| `NORMAL_CHECK_INTERVAL_MIN` | `120` | Min seconds between normal checks |
| `NORMAL_CHECK_INTERVAL_MAX` | `300` | Max seconds between normal checks |
| `ALERT_CHECK_INTERVAL_MIN` | `30` | Min seconds between checks after status change |
| `ALERT_CHECK_INTERVAL_MAX` | `60` | Max seconds between checks after status change |
| `ALERT_MODE_DURATION` | `300` | Seconds to stay in high-frequency mode after a change |
| `REQUEST_TIMEOUT` | `15.0` | HTTP request timeout in seconds |
| `MAX_RETRIES` | `3` | Retries per check before giving up |
| `BACKOFF_BASE` | `2.0` | Exponential backoff base (seconds) |
| `MAX_BACKOFF` | `120.0` | Maximum backoff duration (seconds) |
| `MAX_ACCOUNTS_PER_USER` | `20` | Max accounts a single user can track |
| `QUEUE_SLEEP_INTERVAL` | `3.0` | Seconds to sleep when no accounts are due |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## 🏗️ Architecture

```
Discord User
     │
     ▼
 bot.py (slash commands)
     │
     ├── /track → db.add_tracking()
     ├── /untrack → db.remove_tracking()
     ├── /list → db.list_tracked()
     └── /status → db.get_account_status()

monitor.py (background scheduler)
     │
     ├── get_accounts_due() ← db.py
     ├── _check_instagram() via httpx
     │       ├── 200 OK + no "not available" text → "available"
     │       ├── 404 → "unavailable"
     │       └── 429/403 → backoff + retry
     │
     ├── status changed? → log_event() + notify_callback()
     │                              │
     │                              ▼
     │                     bot._notify_users()
     │                       └── user.send(embed)
     │
     └── schedule next_check (normal or alert mode)

db.py (SQLite via aiosqlite)
     ├── users
     ├── tracked_accounts (username, user_id, last_status, next_check, ...)
     └── events (status change log)
```

### Rate Limiting Strategy

- **TokenBucket**: Enforces `MAX_REQUESTS_PER_MINUTE` globally across all concurrent checks
- **Semaphore**: Caps concurrent HTTP connections at `SEMAPHORE_LIMIT`
- **Per-account backoff**: On 429/403, the account's `backoff_until` is set; it's skipped until that time passes
- **Jitter**: All intervals have random jitter to avoid thundering herd patterns

### Scaling

The bot is designed to efficiently handle **500–1000+ accounts**:

- Only accounts with `next_check <= now` are processed in each loop pass
- Checks are distributed over time rather than batched
- With `MAX_REQUESTS_PER_MINUTE=30` and avg interval of 3.5 min, the bot can sustainably track ~105 accounts. Raise the rate limit for more.
- For very large scale (1000+ accounts), consider increasing `SEMAPHORE_LIMIT` and `MAX_REQUESTS_PER_MINUTE` gradually while monitoring Instagram's response.

---

## 🛡️ Notes on Instagram Checks

The bot checks account availability by making an HTTP GET request to `https://www.instagram.com/{username}/` and inspecting the response:

- **HTTP 200** with normal profile content → `available`  
- **HTTP 200** with "Sorry, this page isn't available" text → `unavailable`  
- **HTTP 404** → `unavailable`  
- **HTTP 429 / 403** → rate limited, back off and retry

> **Note**: Instagram's frontend is subject to change. If detection stops working, update the response parsing in `monitor.py`'s `_check_instagram()` function.

---

## 📄 License

MIT

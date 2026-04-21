import os
from dotenv import load_dotenv

load_dotenv()

# Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
ADMIN_USER_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_USER_IDS", "").split(",")
    if x.strip().isdigit()
]

# Database
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/monitor.db")

# Rate limiting
MAX_REQUESTS_PER_MINUTE = int(os.getenv("MAX_REQUESTS_PER_MINUTE", "30"))
SEMAPHORE_LIMIT = int(os.getenv("SEMAPHORE_LIMIT", "5"))

# Monitoring intervals (seconds)
NORMAL_CHECK_INTERVAL_MIN = int(os.getenv("NORMAL_CHECK_INTERVAL_MIN", "120"))   # 2 min
NORMAL_CHECK_INTERVAL_MAX = int(os.getenv("NORMAL_CHECK_INTERVAL_MAX", "300"))   # 5 min
ALERT_CHECK_INTERVAL_MIN = int(os.getenv("ALERT_CHECK_INTERVAL_MIN", "30"))      # 30 sec
ALERT_CHECK_INTERVAL_MAX = int(os.getenv("ALERT_CHECK_INTERVAL_MAX", "60"))      # 60 sec
ALERT_MODE_DURATION = int(os.getenv("ALERT_MODE_DURATION", "300"))               # 5 min

# HTTP
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "15.0"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BACKOFF_BASE = float(os.getenv("BACKOFF_BASE", "2.0"))
MAX_BACKOFF = float(os.getenv("MAX_BACKOFF", "120.0"))

# Bot behaviour
MAX_ACCOUNTS_PER_USER = int(os.getenv("MAX_ACCOUNTS_PER_USER", "20"))
QUEUE_SLEEP_INTERVAL = float(os.getenv("QUEUE_SLEEP_INTERVAL", "3.0"))   # seconds between dequeues

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

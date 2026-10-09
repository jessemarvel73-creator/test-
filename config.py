import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip()

WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")

SHOP_NAME = os.getenv("SHOP_NAME", "YOUR SHOP").strip()


ADMIN_KEY = os.getenv("ADMIN_KEY", "").strip()

# Admin security controls. Keep the defaults conservative; these can be
# overridden in Render Environment Variables if operationally necessary.
ADMIN_SESSION_MAX_AGE_SECONDS = max(900, int(os.getenv("ADMIN_SESSION_MAX_AGE_SECONDS", "43200")))
ADMIN_LOGIN_MAX_FAILURES = max(3, min(20, int(os.getenv("ADMIN_LOGIN_MAX_FAILURES", "5"))))
ADMIN_LOGIN_LOCK_SECONDS = max(60, int(os.getenv("ADMIN_LOGIN_LOCK_SECONDS", "900")))

# Telegram signs webhook deliveries with this secret header. Keep it in the
# Render environment. When omitted, bot.py derives a stable secret from BOT_TOKEN.
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

SUPPORT_USERNAME = os.getenv(
    "SUPPORT_USERNAME",
    ADMIN_USERNAME
).strip()

CURRENCY = "XTR"

MAX_REVIEW_WORDS = 100

LOW_STOCK_THRESHOLD = 3

# Background maintenance controls. Values can be overridden in Render/Environment.
PENDING_ORDER_TTL_MINUTES = max(5, int(os.getenv("PENDING_ORDER_TTL_MINUTES", "30")))
MAINTENANCE_INTERVAL_SECONDS = max(30, int(os.getenv("MAINTENANCE_INTERVAL_SECONDS", "60")))
MAINTENANCE_BATCH_SIZE = max(10, min(200, int(os.getenv("MAINTENANCE_BATCH_SIZE", "100"))))
STOCK_ALERT_SEND_DELAY_SECONDS = max(0, float(os.getenv("STOCK_ALERT_SEND_DELAY_SECONDS", "0.05")))

REFERRAL_DISCOUNT_PER_USER = 1
REFERRAL_MAX_DISCOUNT = 70
REFERRAL_DISCOUNT_DAYS = 7

FREE_REVIEW = True

DEBUG = os.getenv("DEBUG", "0") == "1"


def validate_config():
    errors = []

    if not BOT_TOKEN:
        errors.append("BOT_TOKEN is missing.")

    if not ADMIN_ID:
        errors.append("ADMIN_ID is missing.")

    if not WEBAPP_URL:
        errors.append("WEBAPP_URL is missing.")

    if not ADMIN_KEY:
        errors.append("ADMIN_KEY is missing.")

    if errors:
        raise RuntimeError(
            "Configuration error:\n" + "\n".join(errors)
        )


PORT = int(os.getenv("PORT", "10000"))

# Telegram Gaming Shop — Commercial Edition

A standalone Telegram Bot + Mini App gaming-store template with product management, inventory, cart, orders, Telegram Stars payments, promo codes, referrals, loyalty, reviews, feedback, support tickets, notifications, giveaways, broadcasts, media uploads, operational analytics, safe inventory CSV export, and an admin panel.

## Important isolation rule

This package is designed to run as a completely separate deployment from any existing store.

Each customer/deployment must use its own:

- Telegram bot token
- PostgreSQL database
- Admin Telegram ID
- Admin key
- Web App URL
- Telegram webhook secret

Never point a new deployment at an existing production database unless you intentionally want the same data.

## Required environment variables

```text
BOT_TOKEN=
ADMIN_ID=
ADMIN_KEY=
WEBAPP_URL=https://your-service.onrender.com
DATABASE_URL=
SHOP_NAME=YOUR SHOP
ADMIN_USERNAME=
SUPPORT_USERNAME=
TELEGRAM_WEBHOOK_SECRET=
```

Optional settings already supported by the application are documented in `.env.example`.

## Fast Render deployment

1. Create a new GitHub repository from this template.
2. Deploy the repository as a Python Web Service on Render.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Set the environment variables above in Render.
6. Set `WEBAPP_URL` to the public HTTPS URL of the new service.
7. Create/use a PostgreSQL database and place its connection string in `DATABASE_URL`.
8. Open `/admin` and sign in using `ADMIN_KEY`.

The service exposes `/health` for health checks and external monitoring.

## Operations and migrations

- The admin panel includes an **Analytics** section with period-based paid-order, revenue, daily summary, and top-product reporting.
- Inventory metadata can be exported from Admin → Analytics. Item payloads are intentionally never included in the CSV export.
- Auditable SQL release migrations live under `migrations/`. Back up PostgreSQL before applying a migration manually.
- The service now fails startup if database initialization/migration fails instead of serving a partially initialized shop.

For a manual upgrade, run the current migration against the deployment database:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_commercial_hardening.sql
```

## Branding

The source uses the placeholder brand `YOUR SHOP` so the package contains no YOUR SHOP production branding or URL.

To rebrand all text assets before deployment:

```bash
python tools/rebrand.py --name "MY NEW SHOP"
```

After rebranding, set the same name in `SHOP_NAME`.

## Keep Alive

The included GitHub Actions workflow is intentionally generic. It reads the target URL from the repository variable `SHOP_HEALTH_URL`.

Create a GitHub Actions repository variable:

```text
SHOP_HEALTH_URL=https://your-service.onrender.com/health
```

Then enable the workflow. It runs every 5 minutes and retries failed health requests.

Keep Alive is best-effort. Render Free services can still be restarted or suspended by Render, and free instances spin down after 15 minutes without inbound traffic.

## Data storage

Use PostgreSQL for persistent production data. Do not use a local SQLite database for a Render deployment because the Free web-service filesystem is ephemeral.

## Security

Never commit `.env`, database credentials, Telegram bot tokens, or other secrets. Use Render environment variables and GitHub repository secrets/variables as appropriate.

# Commercial Edition — Release Notes

## Hardening and fixes

- Unified the manual schema with the runtime promo redemption model by adding `promo_redemptions.quantity`.
- Added an auditable `migrations/002_commercial_hardening.sql` migration with operational indexes and delivery retry fields.
- Added schema version markers in runtime initialization.
- Admin state-changing API requests now reject cross-site browser contexts using Origin/Referer and Sec-Fetch-Site checks.
- Added Content-Security-Policy-Report-Only, conditional HSTS and existing security headers remain enabled.
- Database initialization is now fail-fast: the web service does not continue as a partially initialized shop after a migration failure.
- Removed release-time cache files and known one-byte placeholder artifacts.

## New features

- Admin Analytics section with selectable 7/30/90/365-day periods.
- Revenue, paid-order, daily summary and top-product reporting.
- Inventory CSV export for available, sold or all inventory metadata.
- CSV export intentionally excludes delivery item payloads.

## Validation performed

- Python compile check: passed.
- JavaScript syntax check for all webapp bundles: passed.
- Application import and route registration smoke test: passed.
- Final package is rebuilt from a clean release directory.

# Database migrations

The application applies compatibility migrations during startup. The SQL files in this directory are the auditable release history for manual or staged deployments.

## Current release

Run `002_commercial_hardening.sql` after the initial schema when upgrading an existing PostgreSQL database. It is idempotent and does not expose inventory item payloads.

For production, take a database backup before applying migrations and verify `/health` after deployment.

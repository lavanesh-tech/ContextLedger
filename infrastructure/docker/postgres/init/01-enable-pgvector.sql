-- Runs once, when the Postgres data volume is first initialised.
-- Alembic migrations (Phase 2+) also issue CREATE EXTENSION IF NOT EXISTS,
-- so managed databases such as AWS RDS do not depend on this script.
CREATE EXTENSION IF NOT EXISTS vector;

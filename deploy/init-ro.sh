#!/bin/sh
# Read-only database role for Grafana (and any other reader): swcollect_ro. Runs once, when the data volume is
# created (docker-entrypoint-initdb.d, after 90-swcollect.sql). For an existing volume, run it by hand:
#   docker compose -f deploy/docker-compose.yml exec db sh /docker-entrypoint-initdb.d/91-swcollect-ro.sh
# It may be run again: it only (re)sets the password and the grants.
#
# swcollect_ro can SELECT every table and view of sw_meta, sw_raw, sw_rel, sw_core and sw_ts, now and later
# (default privileges for the tables swcollect creates). It cannot write. Note: sw_raw.ncm_configarchive holds full
# configuration text; revoke SELECT on it from swcollect_ro if readers must not see it.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v ro_pw="${SWCOLLECT_RO_PASSWORD:-swcollect_ro}" -v db="$POSTGRES_DB" -v owner="$POSTGRES_USER" <<'EOSQL'
SELECT 'CREATE ROLE swcollect_ro LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'swcollect_ro')
\gexec
ALTER ROLE swcollect_ro WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'ro_pw';
ALTER ROLE swcollect_ro SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE :"db" TO swcollect_ro;
CREATE SCHEMA IF NOT EXISTS sw_meta;
CREATE SCHEMA IF NOT EXISTS sw_raw;
CREATE SCHEMA IF NOT EXISTS sw_rel;
CREATE SCHEMA IF NOT EXISTS sw_core;
CREATE SCHEMA IF NOT EXISTS sw_ts;
GRANT USAGE ON SCHEMA sw_meta, sw_raw, sw_rel, sw_core, sw_ts TO swcollect_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA sw_meta, sw_raw, sw_rel, sw_core, sw_ts TO swcollect_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" GRANT SELECT ON TABLES TO swcollect_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" GRANT USAGE ON SCHEMAS TO swcollect_ro;
EOSQL

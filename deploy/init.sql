-- swcollect database bootstrap. Runs once, when the data volume is created.
-- The tables are created by swcollect itself (generated from Metadata.Property), not here.
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE SCHEMA IF NOT EXISTS sw_meta;   -- instances, catalog, runs, watermarks, coverage, change log, relationships, flows
CREATE SCHEMA IF NOT EXISTS sw_raw;    -- one table per SWIS entity, 1:1 with the source, raw row kept as jsonb
CREATE SCHEMA IF NOT EXISTS sw_rel;    -- one view per proven SWIS relationship
CREATE SCHEMA IF NOT EXISTS sw_core;   -- canonical entities (contracts.yaml) with real foreign keys + dead-letter queue
CREATE SCHEMA IF NOT EXISTS sw_ts;     -- TimescaleDB hypertables for metrics

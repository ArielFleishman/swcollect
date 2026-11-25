"""DDL generated from what SWIS says about itself (Metadata.Property), plus the static sw_meta schema.

  sw_meta   instances, the entity catalog, runs, watermarks, coverage, change log, relationships, flows, push
  sw_raw    one table per collected inventory entity (and config archive): SWIS property names verbatim
  sw_ts     one hypertable per metrics entity

Raw tables never reject a row for content: no foreign keys here (they live in sw_core, phase 2), and every
column but the identity is nullable. Times are `timestamp` without zone, exactly as received.
"""
from __future__ import annotations

import hashlib
import re

PG_MAX_IDENT = 63

# SWIS type -> PostgreSQL type. Anything else is stored as text and flagged "needs proof".
TYPE_MAP = {
    "System.Byte": "smallint",
    "System.SByte": "smallint",
    "System.Int16": "smallint",
    "System.UInt16": "integer",
    "System.Int32": "integer",
    "System.UInt32": "bigint",
    "System.Int64": "bigint",
    "System.UInt64": "numeric",
    "System.Single": "real",
    "System.Double": "double precision",
    "System.Decimal": "numeric",
    "System.Boolean": "boolean",
    "System.Guid": "uuid",
    "System.String": "text",
    "System.Char": "text",
    "System.Uri": "text",
    "System.Type": "text",
    "System.DateTime": "timestamp",          # as received; no zone conversion (user decision 2026-11-04)
    "System.DateTimeOffset": "timestamptz",  # carries its own offset
    "System.TimeSpan": "text",
    "System.String[]": "jsonb",
    "System.Int32[]": "jsonb",
    "System.Guid[]": "jsonb",
    "SolarWinds.InformationService.PropertyBag": "jsonb",
}
NEEDS_PROOF = "טעון הוכחה"


def pg_type(swis_type: str | None) -> tuple[str, bool]:
    """(PostgreSQL type, known). Unknown SWIS types become text and are marked by the caller."""
    t = (swis_type or "").strip()
    if t in TYPE_MAP:
        return TYPE_MAP[t], True
    return "text", False


def _short(name: str) -> str:
    """Lower-case identifier, at most 63 characters; a longer one keeps a hash suffix so it stays unique."""
    if len(name) <= PG_MAX_IDENT:
        return name
    h = hashlib.sha1(name.encode()).hexdigest()[:8]
    return f"{name[:PG_MAX_IDENT - 9]}_{h}"


def table_name(entity: str) -> str:
    """Orion.NPM.Interfaces -> orion_npm_interfaces (max 63 chars, hash suffix when shortened)."""
    return _short(re.sub(r"[^a-z0-9]+", "_", entity.lower()).strip("_"))


def column_name(prop: str) -> str:
    """SWIS property names are kept verbatim (quoted). Only a name over 63 characters is shortened."""
    return prop if len(prop) <= PG_MAX_IDENT else _short(prop)


def q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# ---------------------------------------------------------------------------------------------------------------
# sw_meta: static. Each statement is idempotent.
# ---------------------------------------------------------------------------------------------------------------
META_DDL = [
    "CREATE SCHEMA IF NOT EXISTS sw_meta",
    "CREATE SCHEMA IF NOT EXISTS sw_raw",
    "CREATE SCHEMA IF NOT EXISTS sw_rel",
    "CREATE SCHEMA IF NOT EXISTS sw_core",
    "CREATE SCHEMA IF NOT EXISTS sw_ts",
    """CREATE TABLE IF NOT EXISTS sw_meta.instance (
        instance_id   serial PRIMARY KEY,
        instance_key  text NOT NULL UNIQUE,          -- <network_id>/<host:port> (SPEC-B §6)
        network_id    text NOT NULL,
        host          text NOT NULL,
        port          integer NOT NULL,
        swis_url      text NOT NULL,
        utc_offset_minutes integer,                  -- measured: GETDATE() - GETUTCDATE()
        modules       jsonb,                         -- Orion.InstalledModule as received
        first_seen    timestamptz NOT NULL DEFAULT now(),
        last_seen     timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS sw_meta.sync_run (
        run_id      bigserial PRIMARY KEY,
        instance_id integer NOT NULL REFERENCES sw_meta.instance,
        job         text NOT NULL,                   -- discover | inventory | metrics | events | curate | verify
        started_at  timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz,
        status      text NOT NULL DEFAULT 'running', -- running | ok | partial | failed
        entities    integer, rows bigint, pages integer, requests integer, ms bigint,
        error       text,
        detail      jsonb)""",
    """CREATE TABLE IF NOT EXISTS sw_meta.entity_catalog (
        instance_id   integer NOT NULL REFERENCES sw_meta.instance,
        entity        text NOT NULL,
        class         text NOT NULL,                 -- inventory | metrics | events | excluded
        collect       boolean NOT NULL,
        reason        text,
        key_columns   text[],
        key_status    text,                          -- metadata | sample_unique | row_hash | override
        time_field    text,
        watermark     jsonb,
        sink_table    text,                          -- sw_raw.<t> / sw_ts.<t>; NULL when not collected
        columns       jsonb NOT NULL,                -- [{name, column, swis_type, pg_type, known}]
        excluded_fields jsonb NOT NULL DEFAULT '{}',
        navigation    jsonb NOT NULL DEFAULT '{}',
        doc           text,
        discovered_run bigint REFERENCES sw_meta.sync_run,
        discovered_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (instance_id, entity))""",
    """CREATE TABLE IF NOT EXISTS sw_meta.relationship (
        relationship_id bigserial PRIMARY KEY,
        instance_id     integer NOT NULL REFERENCES sw_meta.instance,
        source_entity   text NOT NULL,
        name            text NOT NULL,               -- navigation property or override name
        target_entity   text NOT NULL,
        origin          text NOT NULL,               -- navigation | metadata | override | name_match
        source_columns  text[],
        target_columns  text[],
        columns_origin  text,                        -- inferred | override | none
        cardinality     text,
        method          text,                        -- navigation | values
        status          text NOT NULL,               -- proven | partial | mismatch | not_proven | error
        pairs_checked   integer, mismatches integer, sources_checked integer,
        detail          text,
        proof_swql      jsonb,
        basis           text,
        proven_at       timestamptz NOT NULL DEFAULT now(),
        UNIQUE (instance_id, source_entity, name, target_entity))""",
    """CREATE TABLE IF NOT EXISTS sw_meta.sync_state (
        instance_id    integer NOT NULL REFERENCES sw_meta.instance,
        entity         text NOT NULL,
        watermark_kind text NOT NULL,                -- id | time
        watermark_field text NOT NULL,
        value          text,                         -- the @LastID / last time, as received
        updated_at     timestamptz NOT NULL DEFAULT now(),
        run_id         bigint REFERENCES sw_meta.sync_run,
        PRIMARY KEY (instance_id, entity))""",
    """CREATE TABLE IF NOT EXISTS sw_meta.coverage (
        run_id        bigint NOT NULL REFERENCES sw_meta.sync_run,
        instance_id   integer NOT NULL REFERENCES sw_meta.instance,
        entity        text NOT NULL,
        swis_count    bigint,
        received      bigint,
        stored        bigint,
        status        text NOT NULL,                 -- ok | mismatch | empty | error | excluded
        note          text,
        checked_at    timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (run_id, entity))""",
    """CREATE TABLE IF NOT EXISTS sw_meta.change_log (
        change_id   bigserial PRIMARY KEY,
        run_id      bigint REFERENCES sw_meta.sync_run,
        instance_id integer NOT NULL REFERENCES sw_meta.instance,
        entity      text NOT NULL,
        row_key     jsonb NOT NULL,
        change      text NOT NULL,                   -- insert | update | soft_delete | undelete
        fields      jsonb,                           -- {field: [old, new]}
        changed_at  timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS change_log_entity ON sw_meta.change_log (instance_id, entity, changed_at)",
    """CREATE TABLE IF NOT EXISTS sw_meta.flow (
        flow_id     bigserial PRIMARY KEY,
        run_id      bigint REFERENCES sw_meta.sync_run,
        position    integer NOT NULL,
        source_tables text[] NOT NULL,
        target_table text NOT NULL,
        relationships bigint[],
        rule        text,
        rows_in bigint, rows_out bigint, rows_dlq bigint,
        ran_at      timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS sw_meta.push_message (
        message_id  bigserial PRIMARY KEY,
        instance_id integer REFERENCES sw_meta.instance,
        network     text NOT NULL,
        received_at timestamptz NOT NULL DEFAULT now(),
        body_sha256 text NOT NULL,
        duplicate_of bigint REFERENCES sw_meta.push_message,
        headers     jsonb,
        body        text NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sw_meta.alert_reconcile (
        instance_id   integer NOT NULL REFERENCES sw_meta.instance,
        alert_key     text NOT NULL,
        push_message_id bigint REFERENCES sw_meta.push_message,
        alert_history_id bigint,
        status        text NOT NULL,                 -- matched | poll_only | push_only
        push_at timestamptz, poll_at timestamptz,
        PRIMARY KEY (instance_id, alert_key))""",
    # Phase 2 ------------------------------------------------------------------------------------------------
    # Rows sw_raw cannot hold by its key (key column null, or the same key twice in one pull). Kept with the raw
    # row and the rule, never dropped: the raw layer does not reject content, it parks what its key cannot place.
    """CREATE TABLE IF NOT EXISTS sw_meta.raw_quarantine (
        quarantine_id bigserial PRIMARY KEY,
        run_id      bigint NOT NULL REFERENCES sw_meta.sync_run,
        instance_id integer NOT NULL REFERENCES sw_meta.instance,
        entity      text NOT NULL,
        rule        text NOT NULL,                   -- key_null | key_duplicate
        row_key     jsonb,
        raw         jsonb NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now())""",
    "ALTER TABLE sw_meta.coverage ADD COLUMN IF NOT EXISTS quarantined bigint",
    "ALTER TABLE sw_meta.coverage ADD COLUMN IF NOT EXISTS changes jsonb",
    "ALTER TABLE sw_meta.relationship ADD COLUMN IF NOT EXISTS view_name text",
    "ALTER TABLE sw_meta.relationship ADD COLUMN IF NOT EXISTS stored_status text",   # stored rows vs SWIS
    "ALTER TABLE sw_meta.relationship ADD COLUMN IF NOT EXISTS stored_detail text",
    "ALTER TABLE sw_meta.relationship ADD COLUMN IF NOT EXISTS stored_orphans bigint",
    "ALTER TABLE sw_meta.flow ADD COLUMN IF NOT EXISTS detail jsonb",
    # Dead-letter queue of the curated layer (SPEC-B §9). One row per (record, rule); a failure seen again only
    # moves last_run; a failure that stops happening gets resolved_at. Nothing is deleted.
    """CREATE TABLE IF NOT EXISTS sw_core.dead_letter (
        dlq_id      bigserial PRIMARY KEY,
        core_table  text NOT NULL,
        instance_id integer NOT NULL REFERENCES sw_meta.instance,
        raw_table   text NOT NULL,
        raw_key     jsonb NOT NULL,
        rule        text NOT NULL,                   -- key_not_null | ref
        field       text NOT NULL,
        value       text,
        detail      text,
        raw         jsonb NOT NULL,
        first_run   bigint REFERENCES sw_meta.sync_run,
        last_run    bigint REFERENCES sw_meta.sync_run,
        first_seen  timestamptz NOT NULL DEFAULT now(),
        last_seen   timestamptz NOT NULL DEFAULT now(),
        resolved_at timestamptz,
        UNIQUE (core_table, instance_id, raw_key, rule, field))""",
    # Phase 4 ------------------------------------------------------------------------------------------------
    "ALTER TABLE sw_meta.sync_state ADD COLUMN IF NOT EXISTS detail jsonb",     # e.g. the time of the last ID
    "ALTER TABLE sw_meta.instance ADD COLUMN IF NOT EXISTS metrics_cadence jsonb",
    "ALTER TABLE sw_meta.push_message ADD COLUMN IF NOT EXISTS loki_pushed boolean NOT NULL DEFAULT false",
    "ALTER TABLE sw_meta.push_message ADD COLUMN IF NOT EXISTS key_value text",
    "CREATE INDEX IF NOT EXISTS push_message_sha ON sw_meta.push_message (network, body_sha256)",
    "ALTER TABLE sw_meta.alert_reconcile ADD COLUMN IF NOT EXISTS key_value text",
    "ALTER TABLE sw_meta.alert_reconcile ADD COLUMN IF NOT EXISTS latency_sec double precision",
    "ALTER TABLE sw_meta.alert_reconcile ADD COLUMN IF NOT EXISTS detail text",
    "ALTER TABLE sw_meta.alert_reconcile ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()",
    # An entry Loki refused, kept with the reason (the dead-letter principle for the log sink).
    """CREATE TABLE IF NOT EXISTS sw_meta.loki_rejected (
        rejected_id bigserial PRIMARY KEY,
        run_id      bigint REFERENCES sw_meta.sync_run,
        instance_id integer REFERENCES sw_meta.instance,
        labels      jsonb NOT NULL,
        ts_ns       bigint NOT NULL,
        line        text NOT NULL,
        metadata    jsonb,
        reason      text NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now())""",
    "ALTER TABLE sw_meta.loki_rejected ADD COLUMN IF NOT EXISTS resolved_at timestamptz",
    "ALTER TABLE sw_meta.loki_rejected ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 1",
]


def interface_name_function() -> str:
    """sw_core.normalize_interface_name(text): SPEC-B §6, the same prefix table as swcheck/pipeline.py."""
    from .core_rules import INTERFACE_PREFIXES
    cases = "\n".join(f"        WHEN {literal(k)} THEN {literal(v)}" for k, v in INTERFACE_PREFIXES.items())
    return f"""CREATE OR REPLACE FUNCTION sw_core.normalize_interface_name(name text) RETURNS text
LANGUAGE sql IMMUTABLE AS $f$
  SELECT CASE WHEN m IS NULL THEN btrim(name) ELSE
    COALESCE((CASE lower(m[1])
{cases}
    END) || m[2], btrim(name)) END
  FROM (SELECT regexp_match(btrim(name), '^([A-Za-z][A-Za-z-]*?)\\s*(\\d.*)$') AS m) x
$f$"""


# ---------------------------------------------------------------------------------------------------------------
# Generated tables
# ---------------------------------------------------------------------------------------------------------------
def _column_defs(entry: dict) -> list[str]:
    defs = []
    for c in entry["columns"]:
        comment = c["swis_type"] if c["known"] else f"{c['swis_type']} – {NEEDS_PROOF}"
        defs.append((c["column"], c["pg_type"], comment))
    return defs


def raw_table_ddl(entry: dict) -> list[str]:
    """CREATE / evolve sw_raw.<table> for one entity entry of the discovered catalog."""
    t = f"sw_raw.{q(entry['table'])}"
    by_row_hash = entry["key_status"] == "row_hash"
    key_cols = ["_row_hash"] if by_row_hash else [column_name(k) for k in entry["key"]]
    lines = ["instance_id integer NOT NULL REFERENCES sw_meta.instance"]
    for col, typ, _ in _column_defs(entry):
        lines.append(f"{q(col)} {typ}{' NOT NULL' if col in key_cols else ''}")
    lines += [
        "_raw jsonb NOT NULL",                         # the row exactly as SWIS returned it
        "_row_hash text NOT NULL",
        "_first_seen timestamptz NOT NULL DEFAULT now()",
        "_last_seen timestamptz NOT NULL DEFAULT now()",
        "_deleted_at timestamptz",
        "_run_id bigint REFERENCES sw_meta.sync_run",
        f"PRIMARY KEY (instance_id, {', '.join(q(k) for k in key_cols)})",
    ]
    out = [f"CREATE TABLE IF NOT EXISTS {t} (\n    " + ",\n    ".join(lines) + ")"]
    out += _evolve_and_comment(t, entry)
    return out


def ts_table_ddl(entry: dict) -> list[str]:
    """CREATE sw_ts.<table> as a hypertable on the time field (as received), unique on (instance, key, time)."""
    t = f"sw_ts.{q(entry['table'])}"
    time_col = column_name(entry["time_field"])
    key = [column_name(k) for k in entry["key"] if k != entry["time_field"]] if entry["key_status"] != "row_hash" else []
    lines = ["instance_id integer NOT NULL REFERENCES sw_meta.instance"]
    for col, typ, _ in _column_defs(entry):
        lines.append(f"{q(col)} {typ}{' NOT NULL' if col == time_col else ''}")
    lines += ["_raw jsonb NOT NULL", "_row_hash text NOT NULL",
              "_first_seen timestamptz NOT NULL DEFAULT now()", "_run_id bigint REFERENCES sw_meta.sync_run"]
    ident = [q(k) for k in key] if key else [q("_row_hash")]
    unique = ", ".join(["instance_id"] + ident + [q(time_col)])
    out = [
        f"CREATE TABLE IF NOT EXISTS {t} (\n    " + ",\n    ".join(lines) + f",\n    UNIQUE ({unique}))",
        f"SELECT create_hypertable({literal(t)}, by_range({literal(time_col)}), if_not_exists => TRUE)",
    ]
    if any(c["name"] == "NodeID" for c in entry["columns"]):
        out.append(f"CREATE INDEX IF NOT EXISTS {q(_short(entry['table'] + '_node_time'))} "
                   f"ON {t} (instance_id, {q('NodeID')}, {q(time_col)} DESC)")
    out += _evolve_and_comment(t, entry)
    return out


def _evolve_and_comment(t: str, entry: dict) -> list[str]:
    out = []
    # A field that appears later is added; a field that disappears is kept (never dropped).
    for col, typ, comment in _column_defs(entry):
        out.append(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {q(col)} {typ}")
        out.append(f"COMMENT ON COLUMN {t}.{q(col)} IS {literal(comment)}")
    note = f"SWIS {entry['entity']} ({entry['class']}); key: {entry['key_status']}; {entry.get('doc', '')}"
    out.append(f"COMMENT ON TABLE {t} IS {literal(note)}")
    return out


def entity_ddl(entry: dict) -> list[str]:
    if not entry.get("collect"):
        return []
    sinks = entry.get("sinks") or []
    if "ts" in sinks:
        return ts_table_ddl(entry)
    if "raw" in sinks:
        return raw_table_ddl(entry)
    return []          # events that go only to Loki get no table

"""PostgreSQL access for swcollect: sw_meta bookkeeping and applying the generated DDL."""
from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from .config import Network, Settings
from .ddl import META_DDL, entity_ddl


def connect(settings: Settings) -> psycopg.Connection:
    # autocommit: each atomic unit is an explicit `with conn.transaction()` block.
    return psycopg.connect(settings.pg_dsn, connect_timeout=15, autocommit=True)


def apply_meta(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        for stmt in META_DDL:
            conn.execute(stmt)


def upsert_instance(conn: psycopg.Connection, net: Network, info: dict) -> int:
    row = conn.execute(
        """INSERT INTO sw_meta.instance (instance_key, network_id, host, port, swis_url, utc_offset_minutes, modules)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (instance_key) DO UPDATE SET last_seen = now(), swis_url = EXCLUDED.swis_url,
               utc_offset_minutes = COALESCE(EXCLUDED.utc_offset_minutes, sw_meta.instance.utc_offset_minutes),
               modules = COALESCE(EXCLUDED.modules, sw_meta.instance.modules)
           RETURNING instance_id""",
        (net.instance_key, net.stable_network_id, net.host, net.port, net.url, info.get("utc_offset_minutes"),
         Jsonb(info["modules"]) if "modules" in info else None)).fetchone()
    return row[0]


def start_run(conn: psycopg.Connection, instance_id: int, job: str) -> int:
    return conn.execute("INSERT INTO sw_meta.sync_run (instance_id, job) VALUES (%s, %s) RETURNING run_id",
                        (instance_id, job)).fetchone()[0]


def finish_run(conn: psycopg.Connection, run_id: int, status: str, *, entities=None, rows=None, requests=None,
               ms=None, error=None, detail=None) -> None:
    conn.execute(
        """UPDATE sw_meta.sync_run SET finished_at = now(), status = %s, entities = %s, rows = %s, requests = %s,
               ms = %s, error = %s, detail = %s WHERE run_id = %s""",
        (status, entities, rows, requests, ms, error, Jsonb(detail) if detail is not None else None, run_id))


def save_catalog(conn: psycopg.Connection, instance_id: int, run_id: int, entries: dict[str, dict]) -> None:
    with conn.cursor() as cur:
        for e in entries.values():
            sink = None
            if e.get("collect") and e.get("table"):
                sink = ("sw_ts." if "ts" in e["sinks"] else "sw_raw." if "raw" in e["sinks"] else "") + e["table"]
                sink = sink if "." in sink else None
            cur.execute(
                """INSERT INTO sw_meta.entity_catalog (instance_id, entity, class, collect, reason, key_columns,
                       key_status, time_field, watermark, sink_table, columns, excluded_fields, navigation, doc,
                       discovered_run, discovered_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (instance_id, entity) DO UPDATE SET class = EXCLUDED.class,
                       collect = EXCLUDED.collect, reason = EXCLUDED.reason, key_columns = EXCLUDED.key_columns,
                       key_status = EXCLUDED.key_status, time_field = EXCLUDED.time_field,
                       watermark = EXCLUDED.watermark, sink_table = EXCLUDED.sink_table, columns = EXCLUDED.columns,
                       excluded_fields = EXCLUDED.excluded_fields, navigation = EXCLUDED.navigation,
                       doc = EXCLUDED.doc, discovered_run = EXCLUDED.discovered_run, discovered_at = now()""",
                (instance_id, e["entity"], e["class"], e["collect"], e["reason"], e.get("key") or [],
                 e.get("key_status"), e.get("time_field"), Jsonb(e.get("watermark")), sink,
                 Jsonb(e["columns"]), Jsonb(e["excluded_fields"]), Jsonb(e["navigation"]), e.get("doc"), run_id))


def save_relationships(conn: psycopg.Connection, instance_id: int, rels: list) -> None:
    with conn.cursor() as cur:
        for r in rels:
            cur.execute(
                """INSERT INTO sw_meta.relationship (instance_id, source_entity, name, target_entity, origin,
                       source_columns, target_columns, columns_origin, cardinality, method, status, pairs_checked,
                       mismatches, sources_checked, detail, proof_swql, basis, proven_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (instance_id, source_entity, name, target_entity) DO UPDATE SET
                       origin = EXCLUDED.origin, source_columns = EXCLUDED.source_columns,
                       target_columns = EXCLUDED.target_columns, columns_origin = EXCLUDED.columns_origin,
                       cardinality = EXCLUDED.cardinality, method = EXCLUDED.method, status = EXCLUDED.status,
                       pairs_checked = EXCLUDED.pairs_checked, mismatches = EXCLUDED.mismatches,
                       sources_checked = EXCLUDED.sources_checked, detail = EXCLUDED.detail,
                       proof_swql = EXCLUDED.proof_swql, basis = EXCLUDED.basis, proven_at = now()""",
                (instance_id, r.source, r.name, r.target, r.origin, r.source_columns, r.target_columns,
                 r.columns_origin, r.cardinality, r.method, r.status, r.pairs_checked, r.mismatches,
                 r.sources_checked, r.detail, Jsonb(r.proof_swql), r.basis))


def apply_entity_ddl(conn: psycopg.Connection, entries: dict[str, dict]) -> dict[str, str]:
    """Creates / evolves sw_raw and sw_ts tables. One savepoint per entity: a failure is reported, not fatal."""
    errors: dict[str, str] = {}
    for name, e in entries.items():
        stmts = entity_ddl(e)
        if not stmts:
            continue
        try:
            with conn.transaction():
                for s in stmts:
                    conn.execute(s)
        except psycopg.Error as ex:
            errors[name] = str(ex).strip().splitlines()[0]
    return errors

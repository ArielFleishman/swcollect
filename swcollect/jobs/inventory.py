"""Inventory: full pull of every inventory entity into sw_raw, UPSERT by key (plan §2, sw_raw).

Per entity:
  1. COUNT(*) on SWIS (what SolarWinds says it has, for coverage)
  2. SELECT every collected column, paged with WITH ROWS in key order (never SELECT * without a filter)
  3. rows whose key is null, or whose key appears twice in this pull, go to sw_meta.raw_quarantine with the raw row
  4. COPY into a stage table, then UPSERT: changed rows are diffed field by field into sw_meta.change_log
  5. soft delete (_deleted_at) only after a complete pull: no error, received = COUNT, no duplicate keys, and a
     non-empty result (an empty result may be permission filtering, so it never deletes)
  6. one sw_meta.coverage row: SWIS COUNT, received, stored, quarantined, status
The raw row is stored exactly as received (_raw). Typed columns hold the same values; a value that does not fit
its column type is stored as NULL there, kept in _raw, and counted in the coverage note.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field

import psycopg

from ..config import Network
from ..ddl import q
from ..swis.client import SwisClient, SwisError
from .retry import retry

NO_LIMIT = 50_000_000
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")


# ---------------------------------------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------------------------------------
def to_text(v, pg: str) -> tuple[str | None, bool]:
    """(text for COPY, fits). None stays None. A value that does not fit the column type gives (None, False)."""
    if v is None:
        return None, True
    try:
        if pg in ("smallint", "integer", "bigint"):
            if isinstance(v, bool):
                return str(int(v)), True
            if isinstance(v, float) and not v.is_integer():
                return None, False
            return str(int(v)), True
        if pg in ("real", "double precision", "numeric"):
            if isinstance(v, bool):
                return None, False
            return repr(float(v)) if not isinstance(v, int) else str(v), True
        if pg == "boolean":
            if isinstance(v, bool):
                return ("true" if v else "false"), True
            if str(v).lower() in ("true", "false", "0", "1"):
                return {"0": "false", "1": "true"}.get(str(v), str(v).lower()), True
            return None, False
        if pg == "uuid":
            return str(uuid.UUID(str(v))), True
        if pg in ("timestamp", "timestamptz"):
            s = str(v)
            return (s, True) if _ISO.match(s) else (None, False)
        if pg == "jsonb":
            return json.dumps(v, ensure_ascii=False), True
        return (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)), True
    except (TypeError, ValueError, OverflowError):
        return None, False


def row_hash(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------------------
@dataclass
class EntityResult:
    entity: str
    swis_count: int | None = None
    received: int = 0
    stored: int | None = None
    quarantined: int = 0
    status: str = "ok"
    notes: list[str] = field(default_factory=list)
    changes: dict = field(default_factory=dict)
    requests: int = 0
    ms: int = 0


def load_entries(conn: psycopg.Connection, instance_id: int, classes=("inventory",)) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT entity, class, collect, reason, key_columns, key_status, time_field, sink_table, columns, watermark
           FROM sw_meta.entity_catalog WHERE instance_id = %s ORDER BY entity""", (instance_id,)).fetchall()
    out = {}
    for ent, cls, collect, reason, key, ks, tf, sink, cols, wm in rows:
        out[ent] = {"entity": ent, "class": cls, "collect": collect, "reason": reason, "key": key or [],
                    "key_status": ks, "time_field": tf, "sink_table": sink, "columns": cols, "watermark": wm}
    return out


def instance_id_of(conn: psycopg.Connection, net: Network) -> int:
    row = conn.execute("SELECT instance_id FROM sw_meta.instance WHERE instance_key = %s", (net.instance_key,)).fetchone()
    if not row:
        raise SystemExit(f"No instance {net.instance_key} in sw_meta.instance: run `swcollect discover --network "
                         f"{net.name}` first")
    return row[0]


def _select(client: SwisClient, entry: dict, cols: list[str], count: int, page_size: int):
    order = [k for k in entry["key"]] if entry["key_status"] != "row_hash" else (cols if count > page_size else [])
    swql = f"SELECT " + ", ".join(f"e.{c}" for c in cols) + f" FROM {entry['entity']} e"
    if order:
        swql += " ORDER BY " + ", ".join(f"e.{c}" for c in order)
    return client.query_all(swql, page_size=page_size, max_rows=NO_LIMIT), swql


def _bad_columns(client: SwisClient, entity: str, cols: list[str]) -> list[str]:
    """When the full SELECT fails, find the columns SWIS rejects (one TOP 1 query each, at most 200)."""
    bad = []
    for c in cols[:200]:
        try:
            client.query(f"SELECT TOP 1 e.{c} FROM {entity} e")
        except SwisError:
            bad.append(c)
    return bad


def pull_entity(conn: psycopg.Connection, client: SwisClient, instance_id: int, run_id: int, entry: dict,
                page_size: int) -> EntityResult:
    res = EntityResult(entry["entity"])
    t0, n0 = time.perf_counter(), len(client.stats)
    cols = [c["name"] for c in entry["columns"]]
    types = {c["name"]: c["pg_type"] for c in entry["columns"]}
    try:
        res.swis_count = int(retry(lambda: client.query(f"SELECT COUNT(*) AS N FROM {entry['entity']}")).rows[0]["N"])
    except (SwisError, IndexError, KeyError, TypeError) as e:
        res.notes.append(f"COUNT failed: {e}")
    try:
        try:
            qr, swql = retry(lambda: _select(client, entry, cols, res.swis_count or 0, page_size))
        except SwisError as e:
            if e.status is None or e.status >= 500:
                raise
            bad = _bad_columns(client, entry["entity"], cols)
            if not bad:
                raise
            res.notes.append(f"SWIS rejected {len(bad)} column(s), pulled without them: {bad} ({e})")
            cols = [c for c in cols if c not in bad]
            qr, swql = retry(lambda: _select(client, entry, cols, res.swis_count or 0, page_size))
    except SwisError as e:
        res.status, res.notes = "error", res.notes + [f"SELECT failed: {e}"]
        res.requests, res.ms = len(client.stats) - n0, round((time.perf_counter() - t0) * 1000)
        return res
    rows = qr.rows
    res.received = len(rows)
    res.requests = len(client.stats) - n0
    _store(conn, instance_id, run_id, entry, cols, types, rows, res)
    res.ms = round((time.perf_counter() - t0) * 1000)
    return res


def _store(conn, instance_id, run_id, entry, cols, types, rows, res: EntityResult, full: bool = True,
           window: tuple[str, str] | None = None):
    """full=False: an incremental time window (events job, NCM.ConfigArchive): no soft delete, and `stored` is
    counted inside the window (column, lower bound as received)."""
    table = entry["sink_table"].split(".", 1)[1]
    t = f"sw_raw.{q(table)}"
    by_hash = entry["key_status"] == "row_hash"
    key = ["_row_hash"] if by_hash else list(entry["key"])
    # 1. place every row by its key; park what the key cannot place
    placed, quarantine, seen = [], [], {}
    misfit: dict[str, int] = {}
    for r in rows:
        h = row_hash(r)
        kv = (h,) if by_hash else tuple(r.get(k) for k in key)
        if not by_hash and any(v is None for v in kv):
            quarantine.append(("key_null", dict(zip(key, kv)), r))
            continue
        if kv in seen:
            quarantine.append(("key_duplicate", dict(zip(key, kv)), r))
            if seen[kv] is not None:                       # park the first one too: neither is chosen
                quarantine.append(("key_duplicate", dict(zip(key, kv)), placed[seen[kv]][1]))
                placed[seen[kv]] = None
                seen[kv] = None
            continue
        values = []
        for c in cols:
            txt, fits = to_text(r.get(c), types[c])
            if not fits:
                misfit[c] = misfit.get(c, 0) + 1
            values.append(txt)
        seen[kv] = len(placed)
        placed.append((values, r, h, kv))
    placed = [p for p in placed if p is not None]
    duplicates = any(rule == "key_duplicate" for rule, _, _ in quarantine)
    res.quarantined = len(quarantine)
    if misfit:
        res.notes.append(f"values that do not fit their column type (kept in _raw, NULL in the column): {misfit}")

    stage_cols = [f"c{i}" for i in range(len(cols))]
    key_idx = {k: cols.index(k) for k in key if k in cols}
    casts = [f"s.c{i}::{types[c]}" for i, c in enumerate(cols)]
    key_join = (f"r._row_hash = s._row_hash" if by_hash else
                " AND ".join(f"r.{q(k)} = s.c{key_idx[k]}::{types[k]}" for k in key))
    key_json_s = ("jsonb_build_object('_row_hash', s._row_hash)" if by_hash else
                  "jsonb_build_object(" + ", ".join(f"{_lit(k)}, s._raw->{_lit(k)}" for k in key) + ")")
    key_json_r = ("jsonb_build_object('_row_hash', r._row_hash)" if by_hash else
                  "jsonb_build_object(" + ", ".join(f"{_lit(k)}, r._raw->{_lit(k)}" for k in key) + ")")
    ent = entry["entity"]
    with conn.transaction():
        for rule, kv, raw in quarantine:
            conn.execute("INSERT INTO sw_meta.raw_quarantine (run_id, instance_id, entity, rule, row_key, raw) "
                         "VALUES (%s,%s,%s,%s,%s,%s)",
                         (run_id, instance_id, ent, rule, json.dumps(kv, default=str), json.dumps(raw, default=str)))
        conn.execute(f"CREATE TEMP TABLE _stage ({', '.join(c + ' text' for c in stage_cols)}"
                     f"{', ' if stage_cols else ''}_raw jsonb, _row_hash text) ON COMMIT DROP")
        with conn.cursor().copy(f"COPY _stage ({', '.join(stage_cols + ['_raw', '_row_hash'])}) FROM STDIN") as cp:
            for values, raw, h, _ in placed:
                cp.write_row(values + [json.dumps(raw, ensure_ascii=False), h])
        had_rows = conn.execute(f"SELECT EXISTS (SELECT 1 FROM {t} WHERE instance_id = %s)", (instance_id,)).fetchone()[0]
        p = {"run": run_id, "inst": instance_id, "ent": ent}
        # 2. change log: updated (field diff of the raw rows), undeleted, inserted (not on the first load)
        conn.execute(f"""
            INSERT INTO sw_meta.change_log (run_id, instance_id, entity, row_key, change, fields)
            SELECT %(run)s, %(inst)s, %(ent)s, {key_json_s},
                   CASE WHEN r._deleted_at IS NOT NULL THEN 'undelete' ELSE 'update' END,
                   (SELECT jsonb_object_agg(k, jsonb_build_array(r._raw -> k, s._raw -> k))
                      FROM jsonb_object_keys(r._raw || s._raw) k WHERE (r._raw -> k) IS DISTINCT FROM (s._raw -> k))
            FROM _stage s JOIN {t} r ON r.instance_id = %(inst)s AND {key_join}
            WHERE r._row_hash <> s._row_hash OR r._deleted_at IS NOT NULL""", p)
        if had_rows:
            conn.execute(f"""
                INSERT INTO sw_meta.change_log (run_id, instance_id, entity, row_key, change)
                SELECT %(run)s, %(inst)s, %(ent)s, {key_json_s}, 'insert' FROM _stage s
                WHERE NOT EXISTS (SELECT 1 FROM {t} r WHERE r.instance_id = %(inst)s AND {key_join})""", p)
        # 3. upsert
        names = [q(c) for c in cols]
        conflict = q("_row_hash") if by_hash else ", ".join(q(k) for k in key)
        updates = ", ".join([f"{n} = EXCLUDED.{n}" for n in names] +
                            ["_raw = EXCLUDED._raw", "_row_hash = EXCLUDED._row_hash", "_run_id = EXCLUDED._run_id",
                             "_last_seen = now()", "_deleted_at = NULL"])
        conn.execute(f"""
            INSERT INTO {t} (instance_id{''.join(', ' + n for n in names)}, _raw, _row_hash, _run_id)
            SELECT %(inst)s{''.join(', ' + c for c in casts)}, s._raw, s._row_hash, %(run)s FROM _stage s
            ON CONFLICT (instance_id, {conflict}) DO UPDATE SET {updates}""", p)
        # 4. soft delete only after a complete, trustworthy pull
        complete = (full and res.status == "ok" and res.swis_count is not None and res.received == res.swis_count
                    and not duplicates and res.received > 0)
        if not full:
            res.notes.append("incremental window: rows outside it are not compared, nothing is soft-deleted")
        elif complete:
            conn.execute(f"""
                WITH d AS (UPDATE {t} r SET _deleted_at = now()
                           WHERE r.instance_id = %(inst)s AND r._deleted_at IS NULL AND r._run_id IS DISTINCT FROM %(run)s
                           RETURNING {key_json_r} AS k)
                INSERT INTO sw_meta.change_log (run_id, instance_id, entity, row_key, change)
                SELECT %(run)s, %(inst)s, %(ent)s, k, 'soft_delete' FROM d""", p)
        if full and not complete:
            why = ("an empty result may be permission filtering" if res.received == 0 else
                   "duplicate keys in this pull" if duplicates else
                   f"received {res.received} ≠ SWIS COUNT {res.swis_count}")
            res.notes.append(f"soft delete skipped: {why}")
        if window:
            res.stored = conn.execute(f"SELECT count(*) FROM {t} WHERE instance_id = %s AND _deleted_at IS NULL "
                                      f"AND {q(window[0])} > %s::timestamp", (instance_id, window[1])).fetchone()[0]
        else:
            res.stored = conn.execute(f"SELECT count(*) FROM {t} WHERE instance_id = %s AND _deleted_at IS NULL",
                                      (instance_id,)).fetchone()[0]
        res.changes = dict(conn.execute("SELECT change, count(*) FROM sw_meta.change_log WHERE run_id = %s AND "
                                        "entity = %s GROUP BY change", (run_id, ent)).fetchall())
    if res.swis_count is None:
        res.status = "partial"
    elif res.received == 0:
        res.status = "empty"
    elif res.received != res.swis_count or res.stored != res.received - res.quarantined:
        res.status = "mismatch"
    if duplicates:
        res.notes.append(f"key {key} ({entry['key_status']}) is not unique on this pull: re-run discover; "
                         "the duplicates are in sw_meta.raw_quarantine")


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def save_coverage(conn, run_id: int, instance_id: int, res: EntityResult):
    conn.execute(
        """INSERT INTO sw_meta.coverage (run_id, instance_id, entity, swis_count, received, stored, quarantined,
               status, note, changes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (run_id, entity) DO UPDATE SET swis_count = EXCLUDED.swis_count, received = EXCLUDED.received,
               stored = EXCLUDED.stored, quarantined = EXCLUDED.quarantined, status = EXCLUDED.status,
               note = EXCLUDED.note, changes = EXCLUDED.changes""",
        (run_id, instance_id, res.entity, res.swis_count, res.received, res.stored, res.quarantined, res.status,
         "; ".join(res.notes) or None, json.dumps(res.changes) if res.changes else None))


def run(conn: psycopg.Connection, client: SwisClient, net: Network, page_size: int,
        only: list[str] | None = None, log=print, job: str = "inventory") -> dict:
    from ..db import finish_run, start_run
    instance_id = instance_id_of(conn, net)
    entries = load_entries(conn, instance_id)
    run_id = start_run(conn, instance_id, job)
    t0 = time.perf_counter()
    targets = [e for e in entries.values() if e["class"] == "inventory" and e["collect"]
               and (e["sink_table"] or "").startswith("sw_raw.") and (not only or e["entity"] in only)]
    totals = {"entities": 0, "rows": 0, "requests": 0, "status": {}}
    for e in targets:
        res = pull_entity(conn, client, instance_id, run_id, e, page_size)
        save_coverage(conn, run_id, instance_id, res)
        totals["entities"] += 1
        totals["rows"] += res.received
        totals["requests"] += res.requests
        totals["status"][res.status] = totals["status"].get(res.status, 0) + 1
        if res.status not in ("ok", "empty") or res.quarantined:
            log(f"  {res.entity}: {res.status}, SWIS {res.swis_count}, received {res.received}, stored {res.stored}, "
                f"quarantined {res.quarantined}. {'; '.join(res.notes)}")
    if not only:      # nothing is skipped silently: every entity not pulled here is in coverage with its reason
        for e in entries.values():
            if e not in targets:
                why = e["reason"] if not e["collect"] else f"{e['class']}: collected by the {e['class']} job"
                save_coverage(conn, run_id, instance_id,
                              EntityResult(e["entity"], status="excluded" if not e["collect"] else "other_job",
                                           notes=[why], received=0))
    ms = round((time.perf_counter() - t0) * 1000)
    bad = sum(v for k, v in totals["status"].items() if k not in ("ok", "empty"))
    finish_run(conn, run_id, "ok" if not bad else "partial", entities=totals["entities"], rows=totals["rows"],
               requests=totals["requests"], ms=ms, detail=totals)
    totals.update(run_id=run_id, ms=ms, instance_id=instance_id)
    return totals

"""Metrics: incremental pull of every metrics entity into its TimescaleDB hypertable (plan §3).

Cadence follows Orion's own sampling: the smallest StatCollection on Orion.Nodes / Orion.NPM.Interfaces gives
every max(min_minutes, StatCollection), clamped (collector.yaml metrics_policy). The scheduler (phase 5) reads it
from sw_meta.instance.metrics_cadence.

Per entity:
  first run   backfill: everything Orion still holds, from MIN(time) to now, in time slices
  next runs   WHERE time > watermark − overlap (re-reads samples Orion writes late or recomputes), paged in
              (time, key) order. Never an unfiltered SELECT.
  store       COPY → stage → INSERT … ON CONFLICT (instance, key, time) DO UPDATE only when the row changed;
              a changed sample (re-aggregation) is diffed into sw_meta.change_log. Null key/time or the same
              (key, time) twice in one pull → sw_meta.raw_quarantine.
  watermark   sw_meta.sync_state = the newest time received, as received; it moves only after the commit.
  coverage    SWIS COUNT in the window vs received vs stored in the window; samples whose node/interface is not
              in sw_core are counted (no hard FK on hypertables: a sample is never lost to inventory lag).
Times are compared and stored exactly as received (no time-zone conversion).
"""
from __future__ import annotations

import json
import time
from datetime import timedelta

import psycopg

from ..config import Network
from ..ddl import literal, q
from ..swis.client import SwisClient, SwisError
from ..swis.timeutil import parse_dt
from .inventory import EntityResult, instance_id_of, load_entries, row_hash, save_coverage, to_text
from .retry import retry

NO_LIMIT = 50_000_000
TIME_FMT = "%Y-%m-%dT%H:%M:%S"


# ---------------------------------------------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------------------------------------------
def cadence(stat_minutes: list[int], policy: dict) -> dict:
    """Polling interval and overlap from the StatCollection values Orion reports (minutes)."""
    vals = [int(v) for v in stat_minutes if v]
    lo, hi = (policy.get("clamp_minutes") or [1, 15])
    smallest = min(vals) if vals else None
    every = max(int(policy.get("min_minutes", 5)), smallest or 0)
    every = min(max(every, lo), hi)
    overlap = max(int(policy.get("overlap_factor", 2)) * (max(vals) if vals else every),
                  int(policy.get("overlap_min_minutes", 10)))
    return {"every_minutes": every, "overlap_minutes": overlap, "stat_collection_minutes": sorted(set(vals)),
            "basis": "StatCollection on Orion.Nodes / Orion.NPM.Interfaces" if vals else
                     "no StatCollection readable: policy minimum used (needs proof)"}


def read_cadence(client: SwisClient, policy: dict) -> dict:
    vals = []
    for ent in ("Orion.Nodes", "Orion.NPM.Interfaces"):
        try:
            r = retry(lambda: client.query(f"SELECT MIN(e.StatCollection) AS A, MAX(e.StatCollection) AS B "
                                           f"FROM {ent} e")).rows
            if r:
                vals += [r[0].get("A"), r[0].get("B")]
        except SwisError:
            pass
    return cadence([v for v in vals if v is not None], policy)


def _fmt(d) -> str:
    return d.strftime(TIME_FMT)


def _slices(start, end, hours: int):
    t = start
    while t <= end:
        yield t, t + timedelta(hours=hours)
        t += timedelta(hours=hours)


# ---------------------------------------------------------------------------------------------------------------
# One entity
# ---------------------------------------------------------------------------------------------------------------
def _watermark(conn, instance_id: int, entity: str):
    r = conn.execute("SELECT value FROM sw_meta.sync_state WHERE instance_id = %s AND entity = %s",
                     (instance_id, entity)).fetchone()
    return r[0] if r else None


def _pull(client: SwisClient, entry: dict, cols: list[str], where: str, page_size: int) -> list[dict]:
    tf = entry["time_field"]
    order = [tf] + [k for k in entry["key"] if k != tf]
    swql = (f"SELECT {', '.join('e.' + c for c in cols)} FROM {entry['entity']} e WHERE {where} "
            f"ORDER BY {', '.join('e.' + c for c in order)}")
    return retry(lambda: client.query_all(swql, page_size=page_size, max_rows=NO_LIMIT)).rows


def pull_entity(conn, client: SwisClient, instance_id: int, run_id: int, entry: dict, cad: dict, policy: dict,
                page_size: int) -> EntityResult:
    res = EntityResult(entry["entity"])
    t0, n0 = time.perf_counter(), len(client.stats)
    tf, ent = entry["time_field"], entry["entity"]
    cols = [c["name"] for c in entry["columns"]]
    wm = _watermark(conn, instance_id, ent)
    try:
        if wm is None:                                     # backfill
            r = retry(lambda: client.query(f"SELECT MIN(e.{tf}) AS A, MAX(e.{tf}) AS B, COUNT(*) AS N "
                                           f"FROM {ent} e")).rows[0]
            res.swis_count = int(r.get("N") or 0)
            start, end = parse_dt(r.get("A")), parse_dt(r.get("B"))
            rows, window = [], None
            if start and end:
                hours = int(policy.get("backfill_slice_hours", 24))
                upper = f"e.{tf} <= {literal(str(r.get('B')))}"     # samples newer than MAX come next run
                for a, b in _slices(start, end, hours):
                    rows += _pull(client, entry, cols, f"e.{tf} >= {literal(_fmt(a))} AND e.{tf} < {literal(_fmt(b))} "
                                                       f"AND {upper}", page_size)
            res.notes.append(f"backfill from {r.get('A')} to {r.get('B')} in {policy.get('backfill_slice_hours', 24)} h "
                             "slices")
        else:
            since = parse_dt(wm) - timedelta(minutes=cad["overlap_minutes"])
            window = _fmt(since)
            cond = f"e.{tf} > {literal(window)}"
            r = retry(lambda: client.query(f"SELECT MAX(e.{tf}) AS B, COUNT(*) AS N FROM {ent} e WHERE {cond}")).rows[0]
            res.swis_count = int(r.get("N") or 0)
            # The same upper bound for COUNT and the pull: samples written meanwhile come in the next run.
            rows = _pull(client, entry, cols, f"{cond} AND e.{tf} <= {literal(str(r['B']))}", page_size)                 if r.get("B") is not None else []
            res.notes.append(f"window {tf} > {window} (watermark {wm} − overlap {cad['overlap_minutes']} min)")
    except SwisError as e:
        res.status, res.notes = "error", res.notes + [f"SWIS: {e}"]
        res.requests, res.ms = len(client.stats) - n0, round((time.perf_counter() - t0) * 1000)
        return res
    res.received = len(rows)
    res.requests = len(client.stats) - n0
    try:
        newest = _store(conn, instance_id, run_id, entry, cols, rows, window, res)
    except psycopg.Error as e:
        # The sink did not confirm: nothing of this entity was committed and the watermark stays where it was,
        # so the next run pulls the same window again. No data is lost.
        res.status = "error"
        res.notes.append(f"PostgreSQL rejected the batch, watermark not moved: {str(e).strip().splitlines()[0]}")
        newest = None
    if newest and res.status != "error":
        # The sink confirmed (committed) before the watermark moves.
        conn.execute("""INSERT INTO sw_meta.sync_state (instance_id, entity, watermark_kind, watermark_field, value, run_id)
                        VALUES (%s, %s, 'time', %s, %s, %s)
                        ON CONFLICT (instance_id, entity) DO UPDATE SET value = EXCLUDED.value, updated_at = now(),
                            run_id = EXCLUDED.run_id
                        WHERE sw_meta.sync_state.value IS NULL OR sw_meta.sync_state.value < EXCLUDED.value""",
                     (instance_id, ent, tf, newest, run_id))
    res.ms = round((time.perf_counter() - t0) * 1000)
    return res


def _store(conn, instance_id, run_id, entry, cols, rows, window, res: EntityResult) -> str | None:
    t = f"sw_ts.{q(entry['sink_table'].split('.', 1)[1])}"
    tf = entry["time_field"]
    by_hash = entry["key_status"] == "row_hash"
    ident = ["_row_hash"] if by_hash else [k for k in entry["key"] if k != tf]
    types = {c["name"]: c["pg_type"] for c in entry["columns"]}
    placed, quarantine, seen, misfit = [], [], {}, {}
    newest = None
    for r in rows:
        h = row_hash(r)
        tv = r.get(tf)
        kv = (h, tv) if by_hash else tuple(r.get(k) for k in ident) + (tv,)
        if tv is None or any(v is None for v in kv):
            quarantine.append(("key_null", dict(zip(ident + [tf], kv)), r))
            continue
        if kv in seen:
            quarantine.append(("key_duplicate", dict(zip(ident + [tf], kv)), r))
            if seen[kv] is not None:
                quarantine.append(("key_duplicate", dict(zip(ident + [tf], kv)), placed[seen[kv]][1]))
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
        placed.append((values, r, h))
        newest = tv if newest is None or str(tv) > str(newest) else newest
    placed = [p for p in placed if p is not None]
    res.quarantined = len(quarantine)
    if misfit:
        res.notes.append(f"values that do not fit their column type (kept in _raw, NULL in the column): {misfit}")
    stage = [f"c{i}" for i in range(len(cols))]
    idx = {c: i for i, c in enumerate(cols)}
    casts = [f"s.c{i}::{types[c]}" for i, c in enumerate(cols)]
    match = " AND ".join([f"m.{q(k)} = s.c{idx[k]}::{types[k]}" for k in ident if not by_hash] +
                         (["m._row_hash = s._row_hash"] if by_hash else []) + [f"m.{q(tf)} = s.c{idx[tf]}::{types[tf]}"])
    key_json = "jsonb_build_object(" + ", ".join(f"{literal(k)}, s._raw -> {literal(k)}" for k in ident + [tf]
                                                 if k != "_row_hash") + (", '_row_hash', s._row_hash" if by_hash else "") + ")"
    names = [q(c) for c in cols]
    conflict = ", ".join(["instance_id"] + [q(k) for k in ident] + [q(tf)])
    upd = ", ".join([f"{n} = EXCLUDED.{n}" for n in names] +
                    ["_raw = EXCLUDED._raw", "_row_hash = EXCLUDED._row_hash", "_run_id = EXCLUDED._run_id"])
    p = {"run": run_id, "inst": instance_id, "ent": entry["entity"]}
    with conn.transaction():
        for rule, kv, raw in quarantine:
            conn.execute("INSERT INTO sw_meta.raw_quarantine (run_id, instance_id, entity, rule, row_key, raw) "
                         "VALUES (%s,%s,%s,%s,%s,%s)", (run_id, instance_id, entry["entity"], rule,
                                                         json.dumps(kv, default=str), json.dumps(raw, default=str)))
        conn.execute(f"CREATE TEMP TABLE _mstage ({', '.join(c + ' text' for c in stage)}, _raw jsonb, _row_hash text) "
                     "ON COMMIT DROP")
        with conn.cursor().copy(f"COPY _mstage ({', '.join(stage + ['_raw', '_row_hash'])}) FROM STDIN") as cp:
            for values, raw, h in placed:
                cp.write_row(values + [json.dumps(raw, ensure_ascii=False), h])
        # a sample Orion recomputed: field diff of the raw rows (rare: only inside the overlap window)
        conn.execute(f"""
            INSERT INTO sw_meta.change_log (run_id, instance_id, entity, row_key, change, fields)
            SELECT %(run)s, %(inst)s, %(ent)s, {key_json}, 'update',
                   (SELECT jsonb_object_agg(k, jsonb_build_array(m._raw -> k, s._raw -> k))
                      FROM jsonb_object_keys(m._raw || s._raw) k WHERE (m._raw -> k) IS DISTINCT FROM (s._raw -> k))
            FROM _mstage s JOIN {t} m ON m.instance_id = %(inst)s AND {match} WHERE m._row_hash <> s._row_hash""", p)
        stats = conn.execute(f"""
            INSERT INTO {t} (instance_id, {', '.join(names)}, _raw, _row_hash, _run_id)
            SELECT %(inst)s, {', '.join(casts)}, s._raw, s._row_hash, %(run)s FROM _mstage s
            ON CONFLICT ({conflict}) DO UPDATE SET {upd} WHERE {t}._row_hash <> EXCLUDED._row_hash
            RETURNING (xmax = 0)""", p).fetchall()
        res.changes = {"insert": sum(1 for (ins,) in stats if ins), "update": sum(1 for (ins,) in stats if not ins)}
        if window is None:
            res.stored = conn.execute(f"SELECT count(*) FROM {t} WHERE instance_id = %s", (instance_id,)).fetchone()[0]
        else:
            res.stored = conn.execute(f"SELECT count(*) FROM {t} WHERE instance_id = %s AND {q(tf)} > %s::timestamp",
                                      (instance_id, window)).fetchone()[0]
    # Fidelity of the pull: everything SWIS counted was received and stored. Samples we hold in the window that SWIS
    # no longer returns (Orion deleted or re-aggregated them) are kept, never deleted: that is a note, not a failure.
    if res.received == 0:
        res.status = "empty" if res.swis_count == 0 else "mismatch"
    elif res.swis_count != res.received or res.stored < res.received - res.quarantined:
        res.status = "mismatch"
    elif res.stored > res.received - res.quarantined:
        res.notes.append(f"{res.stored - res.received + res.quarantined} samples stored in this window that SWIS no "
                         "longer returns (deleted or re-aggregated at the source): kept")
    return newest


# ---------------------------------------------------------------------------------------------------------------
# Policies and views
# ---------------------------------------------------------------------------------------------------------------
def apply_policies(conn, entry: dict, policy: dict) -> list[str]:
    tab = entry["sink_table"].split(".", 1)[1]
    t = f"sw_ts.{q(tab)}"
    notes = []
    enabled = conn.execute("SELECT compression_enabled FROM timescaledb_information.hypertables "
                           "WHERE hypertable_schema = 'sw_ts' AND hypertable_name = %s", (tab,)).fetchone()
    if policy.get("compress_after") and enabled is not None and not enabled[0]:
        seg = ", ".join(["instance_id"] + [q(k) for k in entry["key"] if k != entry["time_field"]]
                        if entry["key_status"] != "row_hash" else ["instance_id"])
        with conn.transaction():
            conn.execute(f"ALTER TABLE {t} SET (timescaledb.compress, timescaledb.compress_segmentby = "
                         f"{literal(seg)}, timescaledb.compress_orderby = {literal(q(entry['time_field']) + ' DESC')})")
            conn.execute(f"SELECT add_compression_policy({literal(t)}, INTERVAL {literal(policy['compress_after'])}, "
                         f"if_not_exists => TRUE)")
        notes.append(f"compression after {policy['compress_after']} (proposal)")
    if policy.get("retention"):
        conn.execute(f"SELECT add_retention_policy({literal(t)}, INTERVAL {literal(policy['retention'])}, "
                     f"if_not_exists => TRUE)")
        notes.append(f"retention {policy['retention']} (proposal)")
    return notes


def build_view(conn, instance_id: int, entry: dict) -> dict:
    """sw_ts.v_<table>: the samples with their device / interface from sw_core, joined only along a relationship
    proven between this metrics entity and Orion.Nodes / Orion.NPM.Interfaces (forward or reverse)."""
    from ..core import find_proven
    tab = entry["sink_table"].split(".", 1)[1]
    cols = {c["name"] for c in entry["columns"]}
    proven = conn.execute("""SELECT relationship_id, source_entity, target_entity, source_columns, target_columns
                             FROM sw_meta.relationship WHERE instance_id = %s AND status = 'proven'""",
                          (instance_id,)).fetchall()
    have = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'sw_core'").fetchall()}
    sel, joins, used, orphan_conds = ["m.*"], [], {}, []
    for col, parent_entity, core_table, alias, extra in (
            ("NodeID", "Orion.Nodes", "device", "d", ['"Caption" AS device_caption']),
            ("InterfaceID", "Orion.NPM.Interfaces", "interface", "i", ['"Name" AS interface_name'])):
        if col not in cols or core_table not in have:
            continue
        rid = find_proven(proven, entry["entity"], parent_entity, [col], [col])
        if not rid:
            used[core_table] = f"not joined: no proven relationship {entry['entity']}.{col} -> {parent_entity}"
            continue
        joins.append(f"LEFT JOIN sw_core.{core_table} {alias} ON {alias}.instance_id = m.instance_id "
                     f"AND {alias}.{q(col)} = m.{q(col)} AND {alias}.deleted_at IS NULL")
        sel += [f"{alias}.id AS {core_table}_id", f"{alias}.stable_id AS {core_table}_stable_id"] + \
               [f"{alias}.{e}" for e in extra]
        used[core_table] = f"joined by relationship {rid}"
        orphan_conds.append((col, core_table, alias))
    v = f"sw_ts.{q('v_' + tab)}"
    with conn.transaction():
        conn.execute(f"DROP VIEW IF EXISTS {v}")
        conn.execute(f"CREATE VIEW {v} AS SELECT {', '.join(sel)} FROM sw_ts.{q(tab)} m {' '.join(joins)}")
    return {"view": f"sw_ts.v_{tab}", "joins": used, "orphan_conds": orphan_conds}


def orphans(conn, instance_id: int, entry: dict, view: dict, window: str | None) -> dict:
    out = {}
    tab = entry["sink_table"].split(".", 1)[1]
    for col, core_table, _alias in view["orphan_conds"]:
        cond = f" AND m.{q(entry['time_field'])} > %s::timestamp" if window else ""
        args = (instance_id, window) if window else (instance_id,)
        out[core_table] = conn.execute(
            f"""SELECT count(*) FROM sw_ts.{q(tab)} m WHERE m.instance_id = %s{cond} AND m.{q(col)} IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM sw_core.{core_table} c WHERE c.instance_id = m.instance_id
                                AND c.{q(col)} = m.{q(col)} AND c.deleted_at IS NULL)""", args).fetchone()[0]
    return out


# ---------------------------------------------------------------------------------------------------------------
def run(conn: psycopg.Connection, client: SwisClient, net: Network, page_size: int, policy: dict,
        only: list[str] | None = None, log=print) -> dict:
    from ..db import finish_run, start_run
    instance_id = instance_id_of(conn, net)
    entries = load_entries(conn, instance_id)
    run_id = start_run(conn, instance_id, "metrics")
    t0 = time.perf_counter()
    cad = read_cadence(client, policy)
    conn.execute("ALTER TABLE sw_meta.instance ADD COLUMN IF NOT EXISTS metrics_cadence jsonb")
    conn.execute("UPDATE sw_meta.instance SET metrics_cadence = %s WHERE instance_id = %s",
                 (json.dumps(cad), instance_id))
    targets = [e for e in entries.values() if e["class"] == "metrics" and e["collect"]
               and (e["sink_table"] or "").startswith("sw_ts.") and e.get("time_field")
               and (not only or e["entity"] in only)]
    totals = {"entities": 0, "rows": 0, "requests": 0, "status": {}, "cadence": cad, "inserted": 0, "updated": 0}
    for e in targets:
        wm_before = _watermark(conn, instance_id, e["entity"])
        res = pull_entity(conn, client, instance_id, run_id, e, cad, policy, page_size)
        if res.status != "error":
            res.notes += apply_policies(conn, e, policy)
            view = build_view(conn, instance_id, e)
            window = None if wm_before is None else _fmt(parse_dt(wm_before) - timedelta(minutes=cad["overlap_minutes"]))
            orph = orphans(conn, instance_id, e, view, window)
            res.notes.append(f"view {view['view']}: {view['joins']}")
            if any(orph.values()):
                res.notes.append(f"samples whose parent is not in sw_core (kept): {orph}")
        save_coverage(conn, run_id, instance_id, res)
        totals["entities"] += 1
        totals["rows"] += res.received
        totals["requests"] += res.requests
        totals["inserted"] += res.changes.get("insert", 0)
        totals["updated"] += res.changes.get("update", 0)
        totals["status"][res.status] = totals["status"].get(res.status, 0) + 1
        if res.status not in ("ok", "empty") or res.quarantined:
            log(f"  {res.entity}: {res.status}, SWIS {res.swis_count}, received {res.received}, stored {res.stored}, "
                f"quarantined {res.quarantined}. {'; '.join(res.notes)}")
    ms = round((time.perf_counter() - t0) * 1000)
    bad = sum(v for k, v in totals["status"].items() if k not in ("ok", "empty"))
    finish_run(conn, run_id, "ok" if not bad else "partial", entities=totals["entities"], rows=totals["rows"],
               requests=totals["requests"], ms=ms, detail=totals)
    totals.update(run_id=run_id, ms=ms, instance_id=instance_id)
    return totals

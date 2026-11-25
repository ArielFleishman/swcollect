"""Events: alert history, events, syslog/traps, audit and configuration changes into Loki (plan §4, §5).

Per event source (catalog/collector.yaml `events`):
  id watermark    WHERE id > @LastID AND time >= last seen time − lookback, bounded by MAX(id) read first
                  (never an unfiltered read; an event written meanwhile comes in the next run)
  time watermark  WHERE time > watermark − overlap, bounded by MAX(time) (NCM.ConfigArchive: ConfigID is a Guid)
  first run       backfill: the last backfill_days before the newest event
  Loki            one entry per row: labels {job, network, instance, source, kind}, the line = the row exactly as
                  received (JSON; `loki_omit` fields such as the config text are left out and named in metadata),
                  structured metadata = id, node, the time as received and the rule that placed it on Loki's axis
  also_raw        NCM.ConfigArchive is also upserted into sw_raw (the full config text, linked to the device)
  watermark       moves only after Loki confirmed (204) and PostgreSQL committed. An entry Loki refuses is kept in
                  sw_meta.loki_rejected with the reason, so nothing is dropped.
Then the alert reconciliation pairs pushed messages with Orion.AlertHistory rows (reconcile.py).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

import psycopg

from ..config import Network
from ..ddl import literal
from ..sinks.loki import Loki, LokiError, line_json
from ..swis.client import SwisClient, SwisError
from ..swis.timeutil import parse_dt
from .inventory import EntityResult, _store, instance_id_of, load_entries, save_coverage
from .retry import retry

NO_LIMIT = 50_000_000
TIME_FMT = "%Y-%m-%dT%H:%M:%S"
_AWARE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")
NODE_FIELDS = ("NodeID", "NetworkNode", "RelatedNodeId", "CoreNodeID")


def to_epoch_ns(value, rule: str, offset_minutes: int | None) -> tuple[int | None, str]:
    """(ns, how) for Loki. A value with Z / an offset is read as such; otherwise per the declared rule."""
    if value is None or value == "":
        return None, "no time"
    s = str(value).strip()
    if _AWARE.search(s):
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(d.timestamp() * 1e9), "explicit offset in the value"
    d = parse_dt(s)
    if d is None:
        return None, "unparsed time"
    if rule == "utc":
        return int(d.replace(tzinfo=timezone.utc).timestamp() * 1e9), "utc (declared)"
    if offset_minutes is None:
        return None, "server_local declared but the server offset is unknown"
    u = d - timedelta(minutes=offset_minutes)
    return int(u.replace(tzinfo=timezone.utc).timestamp() * 1e9), f"server_local - {offset_minutes} min (declared)"


def measure_offset(client: SwisClient) -> tuple[int | None, datetime | None]:
    try:
        r = retry(lambda: client.query("SELECT TOP 1 GETUTCDATE() AS U, GETDATE() AS L FROM Orion.Engines")).rows[0]
    except (SwisError, IndexError):
        return None, None
    u, l = parse_dt(r["U"]), parse_dt(r["L"])
    return round((l - u).total_seconds() / 60), u


def _state(conn, inst, entity):
    r = conn.execute("SELECT watermark_kind, value, detail FROM sw_meta.sync_state WHERE instance_id = %s AND "
                     "entity = %s", (inst, entity)).fetchone()
    return r if r else (None, None, None)


def _kinds(client: SwisClient, ev: dict) -> dict:
    kf = ev.get("kind_from")
    if not kf:
        return {}
    try:
        rows = retry(lambda: client.query(f"SELECT e.{kf['key']} AS K, e.{kf['name']} AS N FROM {kf['entity']} e")).rows
        return {r["K"]: str(r["N"]).lower() for r in rows}
    except SwisError:
        return {}


def pull_source(conn, client: SwisClient, loki: Loki, net: Network, inst: int, run_id: int, entry: dict, ev: dict,
                policy: dict, offset: int | None, server_utc: datetime | None, page_size: int) -> EntityResult:
    res = EntityResult(entry["entity"])
    t0, n0 = time.perf_counter(), len(client.stats)
    ent, tf = entry["entity"], entry.get("time_field")
    cols = [c["name"] for c in entry["columns"]]
    wm = entry.get("watermark") or {}
    kind, value, detail = _state(conn, inst, ent)
    window = None
    try:
        if wm.get("kind") == "id":
            idf = wm["field"]
            if value is None:                                  # backfill
                top = retry(lambda: client.query(f"SELECT MAX(e.{tf}) AS T FROM {ent} e")).rows[0].get("T")
                floor = _fmt(parse_dt(top) - timedelta(days=int(policy.get("backfill_days", 30)))) if top else None
                cond = f"e.{tf} >= {literal(floor)}" if floor else "1 = 1"
                res.notes.append(f"backfill: {tf} >= {floor} ({policy.get('backfill_days', 30)} days before the newest)")
            else:
                last_t = (detail or {}).get("time")
                floor = _fmt(parse_dt(last_t) - timedelta(hours=int(policy.get("lookback_hours", 24)))) if last_t else None
                cond = f"e.{idf} > {int(value)}" + (f" AND e.{tf} >= {literal(floor)}" if floor else "")
                res.notes.append(f"window {idf} > {value}" + (f", {tf} >= {floor}" if floor else ""))
            b = retry(lambda: client.query(f"SELECT MAX(e.{idf}) AS B, COUNT(*) AS N FROM {ent} e WHERE {cond}")).rows[0]
            res.swis_count = int(b.get("N") or 0)
            rows = [] if b.get("B") is None else _pull(client, ent, cols, f"{cond} AND e.{idf} <= {int(b['B'])}",
                                                      [idf], page_size)
        elif wm.get("kind") == "time":
            if value is None:
                top = retry(lambda: client.query(f"SELECT MAX(e.{tf}) AS T FROM {ent} e")).rows[0].get("T")
                window = _fmt(parse_dt(top) - timedelta(days=int(policy.get("backfill_days", 30)))) if top else None
            else:
                window = _fmt(parse_dt(value) - timedelta(minutes=int(policy.get("overlap_minutes", 60))))
            cond = f"e.{tf} > {literal(window)}" if window else "1 = 1"
            b = retry(lambda: client.query(f"SELECT MAX(e.{tf}) AS B, COUNT(*) AS N FROM {ent} e WHERE {cond}")).rows[0]
            res.swis_count = int(b.get("N") or 0)
            order = [tf] + [k for k in entry["key"] if k != tf]
            rows = [] if b.get("B") is None else _pull(client, ent, cols, f"{cond} AND e.{tf} <= {literal(str(b['B']))}",
                                                      order, page_size)
            res.notes.append(f"window {tf} > {window}")
        else:
            res.status, res.notes = "error", ["no watermark field on this server: not pulled (needs proof)"]
            return res
    except SwisError as e:
        res.status, res.notes = "error", res.notes + [f"SWIS: {e}"]
        res.requests, res.ms = len(client.stats) - n0, round((time.perf_counter() - t0) * 1000)
        return res
    res.received = len(rows)
    res.requests = len(client.stats) - n0

    # 1. also_raw: the full rows (config text) into sw_raw first
    if ev.get("also_raw") and rows:
        types = {c["name"]: c["pg_type"] for c in entry["columns"]}
        raw_res = EntityResult(ent, swis_count=res.swis_count, received=res.received)
        try:
            _store(conn, inst, run_id, entry, cols, types, rows, raw_res, full=False,
                   window=(tf, window) if wm.get("kind") == "time" and window else None)
        except psycopg.Error as e:
            res.status = "error"
            res.notes.append(f"sw_raw rejected the batch, watermark not moved: {str(e).strip().splitlines()[0]}")
            return res
        res.notes.append(f"sw_raw.{entry['sink_table'].split('.', 1)[1]}: {raw_res.changes or 'no changes'}")
        res.quarantined += raw_res.quarantined

    # 2. Loki
    kinds = _kinds(client, ev)
    rule = ev.get("time_zone", "server_local")
    omit = ev.get("loki_omit") or []
    entries, no_time, newest_ns = [], 0, None
    labels_base = {"job": "swcollect", "network": net.stable_network_id, "instance": net.instance_key, "source": ent}
    for r in rows:
        ns, how = to_epoch_ns(r.get(tf), rule, offset)
        if ns is None:
            no_time += 1
            ns = time.time_ns()
            how = f"{how}: placed at ingestion time"
        k = ev.get("kind", "event")
        if ev.get("kind_from"):
            k = kinds.get(r.get(ev["kind_from"]["field"]), "log")
        meta = {"swis_time": r.get(tf), "time_rule": how, "run_id": run_id}
        if wm.get("kind") == "id":
            meta[wm["field"]] = r.get(wm["field"])
        for key in entry["key"]:
            meta.setdefault(key, r.get(key))
        node = next((r.get(f) for f in NODE_FIELDS if r.get(f) is not None), None)
        if node is not None:
            meta["node"] = node
        if omit:
            meta["omitted"] = ",".join(f for f in omit if f in r) + f" (in {entry.get('sink_table') or 'SWIS'})"
        entries.append((dict(labels_base, kind=k), ns, line_json(r, omit), meta))
        newest_ns = ns if newest_ns is None or ns > newest_ns else newest_ns
    if no_time:
        res.notes.append(f"{no_time} rows without a usable time were placed at ingestion time")
    if newest_ns and server_utc:
        ahead = (newest_ns / 1e9 - server_utc.replace(tzinfo=timezone.utc).timestamp()) / 60
        if ahead > 5:
            res.notes.append(f"newest event is {ahead:.0f} min in the future of the server clock under rule '{rule}': "
                             "the time_zone rule for this source looks wrong")
    try:
        pr = loki.push(entries)
    except LokiError as e:
        res.status = "error"
        res.notes.append(f"Loki did not confirm, watermark not moved: {e}")
        return res
    if pr.rejected:
        with conn.transaction():
            for labels, ts, line, meta, reason in pr.rejected:
                conn.execute("INSERT INTO sw_meta.loki_rejected (run_id, instance_id, labels, ts_ns, line, metadata, "
                             "reason) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                             (run_id, inst, json.dumps(labels), ts, line, json.dumps(meta, default=str), reason))
        res.notes.append(f"{len(pr.rejected)} entries refused by Loki, kept in sw_meta.loki_rejected")
    res.stored = pr.accepted
    res.changes = {"loki_accepted": pr.accepted, "loki_rejected": len(pr.rejected), "loki_requests": pr.requests}

    # 3. watermark: only now (Loki confirmed, PostgreSQL committed)
    if rows:
        if wm["kind"] == "id":
            top = max(rows, key=lambda r: r[wm["field"]])
            new_detail = {"time": max(str(r.get(tf)) for r in rows if r.get(tf) is not None)} if tf else {}
            conn.execute("""INSERT INTO sw_meta.sync_state (instance_id, entity, watermark_kind, watermark_field, value,
                                run_id, detail) VALUES (%s, %s, 'id', %s, %s, %s, %s)
                            ON CONFLICT (instance_id, entity) DO UPDATE SET value = EXCLUDED.value, detail =
                                EXCLUDED.detail, run_id = EXCLUDED.run_id, updated_at = now()
                            WHERE sw_meta.sync_state.value IS NULL OR
                                  sw_meta.sync_state.value::numeric < EXCLUDED.value::numeric""",
                         (inst, ent, wm["field"], str(top[wm["field"]]), run_id, json.dumps(new_detail)))
        else:
            newest = max(str(r[tf]) for r in rows if r.get(tf) is not None)
            conn.execute("""INSERT INTO sw_meta.sync_state (instance_id, entity, watermark_kind, watermark_field, value,
                                run_id) VALUES (%s, %s, 'time', %s, %s, %s)
                            ON CONFLICT (instance_id, entity) DO UPDATE SET value = EXCLUDED.value,
                                run_id = EXCLUDED.run_id, updated_at = now()
                            WHERE sw_meta.sync_state.value IS NULL OR sw_meta.sync_state.value < EXCLUDED.value""",
                         (inst, ent, tf, newest, run_id))
    if res.swis_count != res.received:
        res.status = "mismatch"
    elif res.received == 0:
        res.status = "empty"
    res.ms = round((time.perf_counter() - t0) * 1000)
    return res


def resend_rejected(conn, loki: Loki, inst: int, limit: int = 20000) -> int:
    """Entries Loki refused before are offered again: a refusal can be temporary (an entry "too new" because the
    Orion clock runs ahead, a limit hit). An accepted one gets resolved_at; the others stay with their reason."""
    rows = conn.execute("""SELECT rejected_id, labels, ts_ns, line, metadata FROM sw_meta.loki_rejected
                           WHERE instance_id = %s AND resolved_at IS NULL ORDER BY ts_ns LIMIT %s""",
                        (inst, limit)).fetchall()
    if not rows:
        return 0
    try:
        pr = loki.push([(lbl, ts, line, meta or {}) for _id, lbl, ts, line, meta in rows])
    except LokiError:
        return 0
    still = {(json.dumps(lbl, sort_keys=True), ts, line) for lbl, ts, line, _m, _r in pr.rejected}
    done = [rid for rid, lbl, ts, line, _m in rows if (json.dumps(lbl, sort_keys=True), ts, line) not in still]
    with conn.transaction():
        conn.execute("UPDATE sw_meta.loki_rejected SET resolved_at = now() WHERE rejected_id = ANY(%s)", (done,))
        conn.execute("UPDATE sw_meta.loki_rejected SET attempts = attempts + 1 WHERE rejected_id = ANY(%s)",
                     ([r[0] for r in rows if r[0] not in set(done)],))
    return len(done)


def _fmt(d) -> str:
    return d.strftime(TIME_FMT)


def _pull(client: SwisClient, ent: str, cols: list[str], where: str, order: list[str], page_size: int) -> list[dict]:
    swql = (f"SELECT {', '.join('e.' + c for c in cols)} FROM {ent} e WHERE {where} "
            f"ORDER BY {', '.join('e.' + c for c in order)}")
    return retry(lambda: client.query_all(swql, page_size=page_size, max_rows=NO_LIMIT)).rows


def run(conn: psycopg.Connection, client: SwisClient, loki: Loki, net: Network, page_size: int, rules: dict,
        only: list[str] | None = None, log=print) -> dict:
    from ..db import finish_run, start_run
    from ..reconcile import reconcile, resend_pushes
    policy = rules.get("events_policy") or {}
    inst = instance_id_of(conn, net)
    entries = load_entries(conn, inst)
    run_id = start_run(conn, inst, "events")
    t0 = time.perf_counter()
    offset, server_utc = measure_offset(client)
    if offset is not None:
        conn.execute("UPDATE sw_meta.instance SET utc_offset_minutes = %s WHERE instance_id = %s", (offset, inst))
    else:
        offset = conn.execute("SELECT utc_offset_minutes FROM sw_meta.instance WHERE instance_id = %s",
                              (inst,)).fetchone()[0]
    totals = {"entities": 0, "rows": 0, "requests": 0, "status": {}, "loki_accepted": 0, "loki_rejected": 0,
              "utc_offset_minutes": offset}
    for name, ev in (rules.get("events") or {}).items():
        e = entries.get(name)
        if not e or not e["collect"] or e["class"] != "events" or (only and name not in only):
            continue
        res = pull_source(conn, client, loki, net, inst, run_id, e, ev, policy, offset, server_utc, page_size)
        save_coverage(conn, run_id, inst, res)
        totals["entities"] += 1
        totals["rows"] += res.received
        totals["requests"] += res.requests
        totals["loki_accepted"] += res.changes.get("loki_accepted", 0)
        totals["loki_rejected"] += res.changes.get("loki_rejected", 0)
        totals["status"][res.status] = totals["status"].get(res.status, 0) + 1
        if res.status not in ("ok", "empty") or any("future" in n or "refused" in n for n in res.notes):
            log(f"  {res.entity}: {res.status}, SWIS {res.swis_count}, received {res.received}, Loki {res.stored}. "
                f"{'; '.join(res.notes)}")
    totals["push_resent"] = resend_pushes(conn, loki, net, inst)
    totals["rejected_resent"] = resend_rejected(conn, loki, inst)
    hist_rule = ((rules.get("events") or {}).get("Orion.AlertHistory") or {}).get("time_zone", "server_local")
    totals["reconcile"] = reconcile(conn, client, net, inst, rules.get("push") or {}, offset, run_id, hist_rule)
    ms = round((time.perf_counter() - t0) * 1000)
    bad = sum(v for k, v in totals["status"].items() if k not in ("ok", "empty"))
    finish_run(conn, run_id, "ok" if not bad else "partial", entities=totals["entities"], rows=totals["rows"],
               requests=totals["requests"], ms=ms, detail=totals)
    totals.update(run_id=run_id, ms=ms, instance_id=inst)
    return totals

"""verify: is what SolarWinds has what we hold? (plan Verification; SPEC-B §11, §13.3, §15)

Each check is ok / warn / fail with its evidence. The report is stored as a sync_run (job verify) and written to
reports/verify-<network_id>-<time>.json. It reads the latest run of each job and, for event sources, compares the
IDs SWIS has with the IDs Loki holds (last `hours`). It writes nothing to SWIS.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from .config import ROOT, Network
from .ddl import literal, q
from .jobs.inventory import instance_id_of
from .sinks.loki import Loki, LokiError
from .swis.client import SwisClient, SwisError
from .swis.timeutil import parse_dt

REPORTS = ROOT / "reports"
INVENTORY_JOBS = ("inventory", "inventory_core", "routing")


def _check(name: str, status: str, detail: str, items=None) -> dict:
    return {"check": name, "status": status, "detail": detail, "items": items or []}


def _latest_coverage(conn, inst: int, jobs: tuple) -> list[tuple]:
    return conn.execute(
        """SELECT DISTINCT ON (c.entity) c.entity, c.status, c.swis_count, c.received, c.stored, c.quarantined, c.note,
                  r.job, r.run_id, r.started_at
           FROM sw_meta.coverage c JOIN sw_meta.sync_run r USING (run_id)
           WHERE r.instance_id = %s AND r.job = ANY(%s) AND c.status NOT IN ('excluded', 'other_job')
           ORDER BY c.entity, r.run_id DESC""", (inst, list(jobs))).fetchall()


def check_coverage(conn, inst: int, jobs: tuple, label: str) -> dict:
    rows = _latest_coverage(conn, inst, jobs)
    if not rows:
        return _check(label, "fail", f"no {label} run yet")
    bad = [{"entity": e, "status": s, "swis": n, "received": rc, "stored": st, "note": note}
           for e, s, n, rc, st, qn, note, *_ in rows if s not in ("ok", "empty")]
    empty = [e for e, s, *_ in rows if s == "empty"]
    rows_total = sum(r[3] or 0 for r in rows)
    status = "fail" if bad else "ok"
    return _check(label, status, f"{len(rows)} entities, {rows_total} rows in their latest runs; "
                                 f"{len(bad)} not matching SWIS; {len(empty)} empty (may be permission filtering)",
                  bad + [{"entity": e, "status": "empty"} for e in empty])


def check_catalog(conn, inst: int) -> dict:
    by = dict(conn.execute("SELECT class, count(*) FROM sw_meta.entity_catalog WHERE instance_id = %s GROUP BY 1",
                           (inst,)).fetchall())
    no_reason = conn.execute("SELECT count(*) FROM sw_meta.entity_catalog WHERE instance_id = %s AND NOT collect "
                             "AND coalesce(reason, '') = ''", (inst,)).fetchone()[0]
    return _check("catalog", "fail" if no_reason else "ok",
                  f"Metadata.Entity: {sum(by.values())} entities, {by}; every excluded entity has a reason"
                  if not no_reason else f"{no_reason} excluded entities without a reason")


def check_metrics_duplicates(conn, inst: int, hours: int) -> dict:
    rows = conn.execute("""SELECT entity, key_columns, key_status, time_field, sink_table FROM sw_meta.entity_catalog
                           WHERE instance_id = %s AND class = 'metrics' AND collect AND sink_table LIKE 'sw_ts.%%'""",
                        (inst,)).fetchall()
    dups = []
    for ent, key, ks, tf, sink in rows:
        tab = sink.split(".", 1)[1]
        ident = ["_row_hash"] if ks == "row_hash" else [k for k in key if k != tf]
        cols = ", ".join(q(c) for c in ident + [tf])
        n = conn.execute(f"""SELECT count(*) - count(DISTINCT ({cols})) FROM sw_ts.{q(tab)} WHERE instance_id = %s
                             AND {q(tf)} > (SELECT max({q(tf)}) FROM sw_ts.{q(tab)} WHERE instance_id = %s)
                                           - make_interval(hours => %s)""", (inst, inst, hours)).fetchone()[0]
        if n:
            dups.append({"entity": ent, "duplicates": n})
    return _check("metrics: duplicates", "fail" if dups else "ok",
                  f"{len(rows)} hypertables, last {hours} h: " + ("duplicates found" if dups else "no duplicate samples"),
                  dups)


def check_loki(conn, client: SwisClient, loki: Loki, net: Network, inst: int, rules: dict, hours: int) -> dict:
    out, bad = [], []
    for ent, ev in (rules.get("events") or {}).items():
        st = conn.execute("""SELECT value, detail, watermark_field FROM sw_meta.sync_state WHERE instance_id = %s AND
                             entity = %s AND watermark_kind = 'id'""", (inst, ent)).fetchone()
        if not st:
            continue
        wm, detail, idf = int(st[0]), st[1] or {}, st[2]
        tf = ev.get("time")
        last = parse_dt((detail or {}).get("time"))
        if not last:
            continue
        floor = (last.replace(microsecond=0) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            swis = {r["I"] for r in client.query_all(
                f"SELECT e.{idf} AS I FROM {ent} e WHERE e.{tf} >= {literal(floor)} AND e.{idf} <= {wm} "
                f"ORDER BY e.{idf}", max_rows=500_000).rows}
        except SwisError as e:
            bad.append({"source": ent, "error": f"SWIS: {e}"})
            continue
        now = time.time_ns()
        sel = f'{{job="swcollect",instance="{net.instance_key}",source="{ent}"}}'
        try:
            got = set()
            for _ts, line, _lbl in loki.query_range(sel, now - (hours + 72) * 3600 * 10**9, now + 86400 * 10**9):
                try:
                    got.add(json.loads(line).get(idf))
                except ValueError:
                    pass
        except LokiError as e:
            bad.append({"source": ent, "error": f"Loki: {e}"})
            continue
        missing = sorted(swis - got)
        item = {"source": ent, "swis": len(swis), "in_loki": len(swis & got), "missing": missing[:20],
                "missing_count": len(missing)}
        out.append(item)
        if missing:
            bad.append(item)
    if not out and not bad:
        return _check("events: Loki completeness", "warn", "no ID-based event source has a watermark yet")
    return _check("events: Loki completeness", "fail" if bad else "ok",
                  f"IDs SWIS has (last {hours} h, up to each watermark) vs IDs in Loki: " +
                  ", ".join(f"{i['source']} {i['in_loki']}/{i['swis']}" for i in out), bad or out)


def check_relationships(conn, inst: int) -> dict:
    by = dict(conn.execute("SELECT status, count(*) FROM sw_meta.relationship WHERE instance_id = %s GROUP BY 1",
                           (inst,)).fetchall())
    differ = conn.execute("""SELECT source_entity || '.' || name, stored_status, stored_detail FROM sw_meta.relationship
                             WHERE instance_id = %s AND stored_status IN ('differs', 'error')""", (inst,)).fetchall()
    return _check("relationships", "warn" if differ else "ok",
                  f"{by}; proven links whose stored rows differ from SWIS: {len(differ)}",
                  [{"relationship": a, "stored": b, "detail": c} for a, b, c in differ])


def check_flows(conn, inst: int) -> dict:
    run = conn.execute("SELECT max(run_id) FROM sw_meta.sync_run WHERE instance_id = %s AND job = 'curate'",
                       (inst,)).fetchone()[0]
    if not run:
        return _check("flows", "fail", "no curate run yet")
    flows = conn.execute("SELECT target_table, rows_in, rows_out, rows_dlq FROM sw_meta.flow WHERE run_id = %s "
                         "ORDER BY position", (run,)).fetchall()
    unbalanced = [{"table": t, "in": i, "out": o, "dlq": d} for t, i, o, d in flows if i != o + d]
    dlq = conn.execute("""SELECT core_table, rule, count(*) FROM sw_core.dead_letter WHERE instance_id = %s AND
                          resolved_at IS NULL GROUP BY 1, 2 ORDER BY 1""", (inst,)).fetchall()
    st = "fail" if unbalanced else ("warn" if dlq else "ok")
    return _check("flows: sw_raw → sw_core", st,
                  f"curate run {run}: {len(flows)} flows, rows in = out + dead-letter for "
                  f"{len(flows) - len(unbalanced)}; open dead-letter rows: {sum(n for *_, n in dlq)}",
                  unbalanced + [{"dead_letter": t, "rule": r, "rows": n} for t, r, n in dlq])


def check_parked(conn, inst: int, hours: int) -> dict:
    qn = conn.execute("""SELECT entity, rule, count(*) FROM sw_meta.raw_quarantine WHERE instance_id = %s AND
                         created_at > now() - make_interval(hours => %s) GROUP BY 1, 2""", (inst, hours)).fetchall()
    lr = conn.execute("""SELECT count(*) FROM sw_meta.loki_rejected WHERE instance_id = %s AND resolved_at IS NULL""",
                      (inst,)).fetchone()[0]
    items = [{"quarantine": e, "rule": r, "rows": n} for e, r, n in qn] + ([{"loki_rejected": lr}] if lr else [])
    return _check("parked rows (kept, not lost)", "warn" if items else "ok",
                  f"last {hours} h: quarantined {sum(n for *_, n in qn)}; refused by Loki and not yet accepted on "
                  f"re-send: {lr}", items)


def check_push(conn, inst: int, hours: int) -> dict:
    rows = dict(conn.execute("""SELECT status, count(*) FROM sw_meta.alert_reconcile WHERE instance_id = %s AND
                                alert_key LIKE 'h:%%' AND coalesce(poll_at, push_at) > now() - make_interval(hours => %s)
                                GROUP BY 1""", (inst, hours)).fetchall())
    po = conn.execute("""SELECT count(*) FROM sw_meta.alert_reconcile WHERE instance_id = %s AND alert_key LIKE 'p:%%'
                         AND status = 'push_only' AND push_at > now() - make_interval(hours => %s)""",
                      (inst, hours)).fetchone()[0]
    lat = conn.execute("""SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_sec), max(latency_sec)
                          FROM sw_meta.alert_reconcile WHERE instance_id = %s AND status = 'matched' AND
                          alert_key LIKE 'h:%%' AND poll_at > now() - make_interval(hours => %s)""",
                       (inst, hours)).fetchone()
    return _check("alerts: push vs poll", "warn" if rows.get("poll_only") or po else "ok",
                  f"last {hours} h history: matched {rows.get('matched', 0)}, poll_only (no push: filled by the poll) "
                  f"{rows.get('poll_only', 0)}; push_only {po}; latency median {lat[0]} s, max {lat[1]} s")


def check_freshness(conn, inst: int, sched: dict, cadence: dict | None) -> dict:
    from .scheduler import interval_minutes
    stale = []
    last = dict((j, t) for j, t in conn.execute(
        """SELECT job, max(finished_at) FROM sw_meta.sync_run WHERE instance_id = %s AND status IN ('ok', 'partial')
           GROUP BY job""", (inst,)).fetchall())
    now = datetime.now(timezone.utc)
    for job in ("discover", "inventory", "inventory_core", "routing", "metrics", "events"):
        mins = interval_minutes(job, sched, cadence)
        if mins is None:
            continue
        t = last.get(job)
        age = (now - t).total_seconds() / 60 if t else None
        if age is None or age > 2 * mins + 5:
            stale.append({"job": job, "last_ok": t.isoformat() if t else None,
                          "age_minutes": round(age) if age else None, "expected_every_minutes": mins})
    return _check("freshness", "warn" if stale else "ok",
                  "every job ran within twice its interval" if not stale else f"{len(stale)} jobs late or never run",
                  stale)


def run(conn: psycopg.Connection, client: SwisClient, loki: Loki, net: Network, rules: dict, hours: int = 24,
        write_file: bool = True) -> dict:
    from .db import finish_run, start_run
    inst = instance_id_of(conn, net)
    run_id = start_run(conn, inst, "verify")
    t0 = time.perf_counter()
    cad = conn.execute("SELECT metrics_cadence FROM sw_meta.instance WHERE instance_id = %s", (inst,)).fetchone()[0]
    checks = [
        check_catalog(conn, inst),
        check_coverage(conn, inst, INVENTORY_JOBS, "inventory: SWIS vs sw_raw"),
        check_coverage(conn, inst, ("metrics",), "metrics: SWIS vs sw_ts"),
        check_metrics_duplicates(conn, inst, hours),
        check_coverage(conn, inst, ("events",), "events: SWIS vs Loki (per run)"),
        check_loki(conn, client, loki, net, inst, rules, hours),
        check_relationships(conn, inst),
        check_flows(conn, inst),
        check_parked(conn, inst, hours),
        check_push(conn, inst, hours),
        check_freshness(conn, inst, rules.get("schedule") or {}, cad),
    ]
    overall = "fail" if any(c["status"] == "fail" for c in checks) else \
        "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    report = {"network": net.name, "instance": net.instance_key, "at": datetime.now(timezone.utc).isoformat(),
              "hours": hours, "overall": overall, "checks": checks}
    if write_file:
        REPORTS.mkdir(exist_ok=True)
        path = REPORTS / f"verify-{net.stable_network_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        report["file"] = str(path)
    finish_run(conn, run_id, {"ok": "ok", "warn": "partial", "fail": "failed"}[overall], entities=len(checks),
               requests=len(client.stats), ms=round((time.perf_counter() - t0) * 1000),
               detail={"overall": overall, "checks": [{k: c[k] for k in ("check", "status", "detail")} for c in checks]})
    report["run_id"] = run_id
    return report

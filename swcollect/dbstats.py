"""`swcollect dbstats`: what reached PostgreSQL for one network.

Per schema and table: the live rows of this instance and the newest time (as received). Then every collected entity
whose sw_raw / sw_ts table holds no row, with its latest coverage status and note. An empty table is explained only
when coverage says `empty` (SWIS returned no rows: permission filtering, a module that is not installed, or no such
devices), the entity is excluded, or another job fills it (`other_job`); anything else is listed as unexplained and makes the command exit 1.
"""
from __future__ import annotations

import json

from . import db
from .ddl import q


def collect(conn, instance_id: int) -> dict:
    out = {"schemas": {}, "empty": [], "unexplained": []}
    cat = conn.execute("""SELECT entity, class, collect, sink_table, time_field FROM sw_meta.entity_catalog
                          WHERE instance_id = %s ORDER BY entity""", (instance_id,)).fetchall()
    cov = {r[0]: (r[1], r[2]) for r in conn.execute(
        """SELECT DISTINCT ON (entity) entity, status, note FROM sw_meta.coverage WHERE instance_id = %s
           ORDER BY entity, checked_at DESC""", (instance_id,)).fetchall()}
    for entity, cls, collect_, sink, tf in cat:
        if not sink:
            continue
        schema, table = sink.split(".", 1)
        live = " AND _deleted_at IS NULL" if schema == "sw_raw" else ""
        newest = f", max({q(tf)})::text" if tf and schema == "sw_ts" else ", NULL::text"
        try:
            n, top = conn.execute(f"SELECT count(*){newest} FROM {schema}.{q(table)} WHERE instance_id = %s{live}",
                                  (instance_id,)).fetchone()
        except Exception as e:                        # a table discover listed but could not create
            conn.rollback()
            n, top = None, f"error: {str(e).splitlines()[0]}"
        out["schemas"].setdefault(schema, []).append({"table": table, "entity": entity, "rows": n, "newest": top})
        if not n:
            status, note = cov.get(entity, (None, None))
            item = {"entity": entity, "table": sink, "coverage": status, "note": note}
            # other_job: filled by another job (NCM.ConfigArchive by events) that has not run for it yet
            (out["empty"] if status in ("empty", "excluded", "other_job") else out["unexplained"]).append(item)
    for schema, where in (("sw_core", "deleted_at IS NULL"), ("sw_rel", None)):
        kind = "BASE TABLE" if schema == "sw_core" else "VIEW"
        for (t,) in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND "
                                 "table_type = %s ORDER BY 1", (schema, kind)).fetchall():
            cond = "instance_id = %s" + (f" AND {where}" if where and t != "dead_letter" else "")
            if t == "dead_letter":
                cond += " AND resolved_at IS NULL"
            n = conn.execute(f"SELECT count(*) FROM {schema}.{q(t)} WHERE {cond}", (instance_id,)).fetchone()[0]
            out["schemas"].setdefault(schema, []).append({"table": t, "rows": n})
    meta = {}
    for t in ("sync_run", "coverage", "change_log", "relationship", "flow", "raw_quarantine", "loki_rejected",
              "alert_reconcile", "push_message"):
        if t == "flow":
            n = conn.execute("SELECT count(*) FROM sw_meta.flow f JOIN sw_meta.sync_run r ON r.run_id = f.run_id "
                             "WHERE r.instance_id = %s", (instance_id,)).fetchone()[0]
        else:
            n = conn.execute(f"SELECT count(*) FROM sw_meta.{t} WHERE instance_id = %s", (instance_id,)).fetchone()[0]
        meta[t] = n
    out["schemas"]["sw_meta"] = [{"table": t, "rows": n} for t, n in meta.items()]
    return out


def main(settings, net, as_json: bool = False) -> int:
    with db.connect(settings) as conn:
        r = conn.execute("SELECT instance_id FROM sw_meta.instance WHERE instance_key = %s",
                         (net.instance_key,)).fetchone()
        if not r:
            print(f"{net.name}: no instance {net.instance_key} in sw_meta.instance – run discover first")
            return 1
        rep = collect(conn, r[0])
    if as_json:
        print(json.dumps(rep, indent=2, default=str, ensure_ascii=False))
    else:
        print(f"{net.name} ({net.instance_key}): rows in PostgreSQL")
        for schema in ("sw_raw", "sw_core", "sw_rel", "sw_ts", "sw_meta"):
            items = rep["schemas"].get(schema, [])
            total = sum(i["rows"] or 0 for i in items)
            filled = sum(1 for i in items if i["rows"])
            print(f"  {schema:8} {len(items):4} tables, {filled:4} with rows, {total:>10,} rows")
            if schema in ("sw_core", "sw_ts", "sw_meta"):
                for i in items:
                    extra = f"  newest {i['newest']}" if i.get("newest") else ""
                    print(f"           {i['table']:40} {i['rows'] if i['rows'] is not None else '-':>10}{extra}")
        print(f"  empty collected tables explained by coverage ({len(rep['empty'])}): "
              + ", ".join(i["entity"] for i in rep["empty"]))
        if rep["unexplained"]:
            print(f"  UNEXPLAINED empty tables ({len(rep['unexplained'])}):")
            for i in rep["unexplained"]:
                print(f"    {i['entity']} ({i['table']}): coverage {i['coverage']} {i['note'] or ''}")
    return 1 if rep["unexplained"] else 0

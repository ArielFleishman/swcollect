"""The steps the CLI (and later the scheduler) run, as functions over a connection and a SWIS client."""
from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

import psycopg

from . import core, db, rel_views
from .config import Network
from .discover import discover, load_rules, write_catalog
from .jobs.inventory import instance_id_of, load_entries
from .relationships import discover_relationships
from .swis.client import SwisClient


def discover_step(client: SwisClient, net: Network, out: Path | None, conn: psycopg.Connection | None,
                  prove: bool = True) -> dict:
    rules = load_rules()
    t0 = time.perf_counter()
    catalog = discover(client, net, rules)
    rels = discover_relationships(client, catalog, rules, prove_them=prove)
    catalog["summary"]["relationships"] = dict(Counter(r.status for r in rels))
    catalog["relationships"] = [r.as_dict() for r in rels]
    ms = round((time.perf_counter() - t0) * 1000)
    catalog["generated"]["swis_requests"] = len(client.stats)
    catalog["generated"]["ms"] = ms
    result = {"catalog": catalog, "rels": rels, "ms": ms, "path": None, "errors": {}, "instance_id": None,
              "run_id": None}
    if out:
        result["path"] = write_catalog(catalog, out)
    if conn is None:
        return result
    db.apply_meta(conn)
    instance_id = db.upsert_instance(conn, net, catalog["server"])
    run_id = db.start_run(conn, instance_id, "discover")
    with conn.transaction():
        db.save_catalog(conn, instance_id, run_id, catalog["entities"])
        db.save_relationships(conn, instance_id, rels)
    errors = db.apply_entity_ddl(conn, catalog["entities"])
    db.finish_run(conn, run_id, "partial" if errors else "ok", entities=catalog["summary"]["entities"],
                  requests=len(client.stats), ms=ms,
                  detail={"summary": catalog["summary"], "ddl_errors": errors, "catalog": str(result["path"])})
    result.update(instance_id=instance_id, run_id=run_id, errors=errors)
    return result


def curate_step(conn: psycopg.Connection, client: SwisClient | None, net: Network) -> dict:
    db.apply_meta(conn)
    inst = instance_id_of(conn, net)
    entries = load_entries(conn, inst)
    run_id = db.start_run(conn, inst, "curate")
    t0 = time.perf_counter()
    views = rel_views.build(conn, client, inst, entries)
    flows = core.run(conn, inst, run_id, entries)
    bad = [f for f in flows if "skipped" not in f and f["in"] != f["out"] + f["dlq"]]
    db.finish_run(conn, run_id, "ok" if not bad else "partial", entities=len(flows),
                  rows=sum(f.get("out", 0) for f in flows), requests=len(client.stats) if client else 0,
                  ms=round((time.perf_counter() - t0) * 1000), detail={"views": views, "flows": flows})
    return {"run_id": run_id, "instance_id": inst, "views": views, "flows": flows, "unbalanced": bad}

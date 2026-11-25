"""CLI: python -m swcollect mock | check | discover | ddl | inventory | curate | metrics | events | hook | erd |
        run | serve | schedule | verify | e2e"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter

from . import config as cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="swcollect")
    ap.add_argument("--config", help="config.yaml path (default: config.yaml, else config.example.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mock", help="start the mock SWIS server (from swcheck)")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=17774)
    m.add_argument("--latency-ms", type=int, default=0)
    m.add_argument("--scale", type=int, default=1)
    m.add_argument("--schema-all", action="store_true",
                   help="also serve every entity page of catalog/swis-schema-2026.2.json with synthetic rows")
    m.add_argument("--rich", action="store_true",
                   help="realistic rows for every schema entity and metric history (mock/rich.py); "
                        "implies --schema-all")
    m.add_argument("--history-days", type=int, default=7, help="rich: days of 10-minute samples (default 7)")
    m.add_argument("--hourly-days", type=int, default=7,
                   help="rich: days of hourly samples before the 10-minute ones (default 7)")

    c = sub.add_parser("check", help="readiness: SWIS (read-only), PostgreSQL + TimescaleDB, Loki")
    c.add_argument("--network", help="SWIS profile to check (omit to check only the sinks)")
    c.add_argument("--wait", type=int, default=0, help="retry for up to N seconds until everything is ready")

    d = sub.add_parser("discover", help="read the server's Metadata, classify every entity, prove relationships, "
                                        "write catalog/discovered/<network_id>.yaml and create the tables")
    d.add_argument("--network", required=True)
    d.add_argument("--no-db", action="store_true", help="only write the YAML file; do not touch PostgreSQL")
    d.add_argument("--no-proof", action="store_true", help="list relationship candidates without proving them")
    d.add_argument("--out", help="output file (default: catalog/discovered/<network_id>.yaml)")

    g = sub.add_parser("ddl", help="print the DDL generated from a discovered catalog")
    g.add_argument("--network", required=True)
    g.add_argument("--entity", help="only this entity")

    i = sub.add_parser("inventory", help="full pull of every inventory entity into sw_raw (UPSERT, change log, "
                                         "soft delete, coverage)")
    i.add_argument("--network", required=True)
    i.add_argument("--entity", action="append", help="only this entity (repeatable)")

    u = sub.add_parser("curate", help="sw_rel views for proven relationships, then the flows into sw_core")
    u.add_argument("--network", required=True)

    mt = sub.add_parser("metrics", help="incremental pull of every metrics entity into its TimescaleDB hypertable "
                                         "(backfill on the first run)")
    mt.add_argument("--network", required=True)
    mt.add_argument("--entity", action="append", help="only this entity (repeatable)")

    ev = sub.add_parser("events", help="incremental pull of every event source into Loki (+ NCM configs into sw_raw), "
                                       "then push/poll reconciliation of alerts")
    ev.add_argument("--network", required=True)
    ev.add_argument("--entity", action="append", help="only this event source (repeatable)")

    hk = sub.add_parser("hook", help="push receiver for the Orion alert action: POST /hook/<network>")
    hk.add_argument("--host")
    hk.add_argument("--port", type=int)

    rn = sub.add_parser("run", help="run jobs now, in order (default: all)")
    rn.add_argument("--network", required=True)
    rn.add_argument("--once", nargs="*", default=["all"],
                    help="jobs: discover inventory inventory_core routing metrics events verify, or all")

    sv = sub.add_parser("serve", help="the collector service: every job on its schedule + the push receiver")
    sv.add_argument("--network", action="append", help="profile(s) to collect (default: every read-only profile)")
    sv.add_argument("--no-hook", action="store_true", help="do not start the push receiver")
    sv.add_argument("--tick", type=int, default=15, help="seconds between schedule checks")

    sc = sub.add_parser("schedule", help="show which jobs are due for a network and why")
    sc.add_argument("--network", required=True)

    vf = sub.add_parser("verify", help="SWIS vs what is stored: coverage, Loki completeness, flows, freshness")
    vf.add_argument("--network", required=True)
    vf.add_argument("--hours", type=int, default=24)

    ee = sub.add_parser("e2e", help="end-to-end check against the running mock, PostgreSQL, Loki and push receiver")
    ee.add_argument("--network", required=True)
    ee.add_argument("--hook-url", default="http://127.0.0.1:18090/hook",
                    help="the push receiver as the MOCK reaches it (compose: http://collector:18090/hook)")

    db_ = sub.add_parser("dbstats", help="rows per schema and table in PostgreSQL, and every empty collected table "
                                        "with its coverage status (proof that the data reached the database)")
    db_.add_argument("--network", required=True)
    db_.add_argument("--json", action="store_true", help="print JSON instead of text")

    gd = sub.add_parser("dashboards", help="write the Grafana dashboards to deploy/grafana/dashboards/*.json")
    gd.add_argument("--out", help="output directory (default: deploy/grafana/dashboards)")

    r = sub.add_parser("erd", help="write docs/data-model.md and docs/data-model.html from the database")
    r.add_argument("--network", required=True)

    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")      # Hebrew reasons on a cp1252 console
    if args.cmd == "mock":
        from werkzeug.serving import WSGIRequestHandler

        from .mock.mock_swis import create_app
        WSGIRequestHandler.protocol_version = "HTTP/1.1"     # keep-alive, like SWIS (no socket exhaustion)
        print(f"Mock SWIS on http://{args.host}:{args.port}  (user readonly / password mock, scale {args.scale}"
              f"{', schema-all' if args.schema_all else ''}{', rich' if args.rich else ''})")
        app = create_app(args.latency_ms, args.scale, args.schema_all, args.rich, args.history_days, args.hourly_days)
        app.run(host=args.host, port=args.port, threaded=True)
        return 0

    settings = cfg.load(args.config)
    if args.cmd == "check":
        from .check import run
        deadline = time.monotonic() + args.wait
        while True:
            results = run(settings, args.network)
            ok = all(r["ok"] for r in results)
            if ok or time.monotonic() >= deadline:
                break
            time.sleep(3)
        for r in results:
            print(f"[{'OK ' if r['ok'] else 'ERR'}] {r['check']}: {r['detail']} ({r['ms']} ms)")
        return 0 if ok else 1
    if args.cmd == "discover":
        return cmd_discover(settings, args)
    if args.cmd == "ddl":
        return cmd_ddl(settings, args)
    if args.cmd == "inventory":
        return cmd_inventory(settings, args)
    if args.cmd == "curate":
        return cmd_curate(settings, args)
    if args.cmd == "metrics":
        return cmd_metrics(settings, args)
    if args.cmd == "events":
        return cmd_events(settings, args)
    if args.cmd == "hook":
        from werkzeug.serving import WSGIRequestHandler

        from .webhook import create_app as hook_app
        WSGIRequestHandler.protocol_version = "HTTP/1.1"
        redact_token_in_access_log()
        host, port = args.host or settings.hook_host, args.port or settings.hook_port
        print(f"swcollect push receiver on http://{host}:{port}/hook/<network>  "
              f"(token {'required' if settings.hook_token else 'NOT set: set SWCOLLECT_HOOK_TOKEN'})")
        hook_app(settings).run(host=host, port=port, threaded=True, load_dotenv=False)
        return 0
    if args.cmd == "erd":
        return cmd_erd(settings, args)
    if args.cmd == "dashboards":
        from pathlib import Path

        from . import dashboards
        for path in dashboards.write(Path(args.out) if args.out else dashboards.OUT_DIR):
            print(f"  wrote {path}")
        return 0
    if args.cmd == "dbstats":
        from . import dbstats
        return dbstats.main(settings, settings.network(args.network), args.json)
    if args.cmd == "run":
        return cmd_run(settings, args)
    if args.cmd == "serve":
        return cmd_serve(settings, args)
    if args.cmd == "schedule":
        return cmd_schedule(settings, args)
    if args.cmd == "verify":
        return cmd_verify(settings, args)
    if args.cmd == "e2e":
        from .e2e import E2E
        return 0 if E2E(settings, settings.network(args.network), args.hook_url, settings.hook_token).run() else 1
    return 2


def redact_token_in_access_log():
    """A ?token= in the URL never reaches the access log."""
    import logging
    import re

    class _NoToken(logging.Filter):
        def filter(self, record):
            record.msg, record.args = re.sub(r"token=[^&\s\"]+", "token=<REDACTED>", record.getMessage()), ()
            return True
    logging.getLogger("werkzeug").addFilter(_NoToken())


def _catalog_path(net) -> "cfg.Path":
    return cfg.CATALOG_DIR / "discovered" / f"{net.stable_network_id}.yaml"


def cmd_discover(settings, args) -> int:
    from pathlib import Path

    from .steps import discover_step
    from .swis.client import SwisClient

    net = settings.network(args.network)
    client = SwisClient(net, net.resolve_password(), capture=False)
    out = Path(args.out) if args.out else _catalog_path(net)
    if args.no_db:
        r = discover_step(client, net, out, None, prove=not args.no_proof)
    else:
        from . import db
        with db.connect(settings) as conn:
            r = discover_step(client, net, out, conn, prove=not args.no_proof)
    catalog, rels, s = r["catalog"], r["rels"], r["catalog"]["summary"]
    print(f"{net.name}: {s['entities']} entities in Metadata.Entity → collected {s['collected']} "
          f"(inventory {s.get('inventory', 0)}, metrics {s.get('metrics', 0)}, events {s.get('events', 0)}), "
          f"excluded {s.get('excluded', 0)} with a reason")
    keys = Counter(e.get("key_status") for e in catalog["entities"].values() if e["collect"])
    print(f"  identity: {dict(keys)}")
    print(f"  relationships: {len(rels)} candidates → {s['relationships']}")
    for note in catalog["generated"]["notes"]:
        print(f"  note: {note}")
    print(f"  {len(client.stats)} SWIS requests in {r['ms']} ms; catalog written to {r['path']}")
    if args.no_db:
        return 0
    errors = r["errors"]
    tables = sum(1 for e in catalog["entities"].values() if e["collect"] and {"raw", "ts"} & set(e["sinks"]))
    print(f"  PostgreSQL: instance {r['instance_id']}, run {r['run_id']}; {tables - len(errors)} of {tables} tables "
          f"created or evolved" + (f"; {len(errors)} DDL errors (sw_meta.sync_run.detail)" if errors else ""))
    for name, err in list(errors.items())[:10]:
        print(f"    DDL error {name}: {err}")
    return 0 if not errors else 1


def cmd_inventory(settings, args) -> int:
    from . import db
    from .jobs import inventory
    from .swis.client import SwisClient
    net = settings.network(args.network)
    client = SwisClient(net, net.resolve_password(), capture=False)
    with db.connect(settings) as conn:
        db.apply_meta(conn)
        t = inventory.run(conn, client, net, settings.page_size, only=args.entity)
    print(f"{net.name}: inventory run {t['run_id']}: {t['entities']} entities, {t['rows']} rows, "
          f"{t['requests']} SWIS requests, {t['ms']} ms; status {t['status']}")
    return 0 if set(t["status"]) <= {"ok", "empty"} else 1


def cmd_curate(settings, args) -> int:
    from . import db
    from .steps import curate_step
    from .swis.client import SwisClient
    net = settings.network(args.network)
    client = SwisClient(net, net.resolve_password(), capture=False)
    with db.connect(settings) as conn:
        r = curate_step(conn, client, net)
    views, flows = r["views"], r["flows"]
    print(f"{net.name}: sw_rel: {views['views']} views; stored rows vs SWIS: {views['stored']}")
    for v in views["skipped"]:
        print(f"  view skipped: {v}")
    for f in flows:
        if "skipped" in f:
            print(f"  sw_core.{f['table']}: skipped ({f['skipped']})")
        else:
            extra = f"; not enforced: {f['unproven']}" if f["unproven"] else ""
            print(f"  sw_core.{f['table']}: in {f['in']} = out {f['out']} + dead-letter {f['dlq']} "
                  f"{f['rules'] or ''}{extra}")
    print(f"  curate run {r['run_id']} done")
    return 0 if not r["unbalanced"] else 1


def cmd_metrics(settings, args) -> int:
    from . import db
    from .discover import load_rules
    from .jobs import metrics
    from .swis.client import SwisClient
    net = settings.network(args.network)
    client = SwisClient(net, net.resolve_password(), capture=False)
    policy = load_rules().get("metrics_policy") or {}
    with db.connect(settings) as conn:
        db.apply_meta(conn)
        t = metrics.run(conn, client, net, settings.page_size, policy, only=args.entity)
    c = t["cadence"]
    print(f"{net.name}: metrics run {t['run_id']}: cadence every {c['every_minutes']} min, overlap "
          f"{c['overlap_minutes']} min (StatCollection {c['stat_collection_minutes']}; {policy.get('status', '')})")
    print(f"  {t['entities']} entities, {t['rows']} samples received, {t['inserted']} inserted, {t['updated']} updated "
          f"(re-aggregated), {t['requests']} SWIS requests, {t['ms']} ms; status {t['status']}")
    return 0 if set(t["status"]) <= {"ok", "empty"} else 1


def cmd_events(settings, args) -> int:
    from . import db
    from .discover import load_rules
    from .jobs import events
    from .sinks.loki import Loki
    from .swis.client import SwisClient
    net = settings.network(args.network)
    client = SwisClient(net, net.resolve_password(), capture=False)
    rules = load_rules()
    loki = Loki(settings.loki_url, batch_entries=int((rules.get("events_policy") or {}).get("batch_entries", 1000)))
    with db.connect(settings) as conn:
        db.apply_meta(conn)
        t = events.run(conn, client, loki, net, settings.page_size, rules, only=args.entity)
    print(f"{net.name}: events run {t['run_id']}: {t['entities']} sources, {t['rows']} rows → Loki accepted "
          f"{t['loki_accepted']}, refused {t['loki_rejected']} (kept); server offset {t['utc_offset_minutes']} min; "
          f"{t['requests']} SWIS requests, {t['ms']} ms; status {t['status']}")
    r = t["reconcile"]
    if "error" in r:
        print(f"  reconcile: {r['error']}")
    else:
        print(f"  alerts push vs poll: history {r['history']} (poll_only = no push yet: filled by the poll), "
              f"push_only {r['push_only']}, matched now {r['matched_now']}, latency {r['latency_sec']}; "
              f"pushes re-sent to Loki {t['push_resent']}")
    return 0 if set(t["status"]) <= {"ok", "empty"} else 1


def cmd_run(settings, args) -> int:
    from .scheduler import ORDER, run_once
    jobs = list(ORDER) if args.once in ([], ["all"]) else args.once
    net = settings.network(args.network)
    ok = True
    for r in run_once(settings, net, jobs):
        print(f"{net.name}: {r}")
        ok = ok and "error" not in r
    return 0 if ok else 1


def cmd_schedule(settings, args) -> int:
    from . import db
    from .discover import load_rules
    from .scheduler import due_jobs
    net = settings.network(args.network)
    with db.connect(settings) as conn:
        db.apply_meta(conn)
        due = due_jobs(conn, net, load_rules().get("schedule") or {})
    print(f"{net.name}: due now: " + (", ".join(f"{j} ({w})" for j, w in due) if due else "nothing"))
    return 0


def cmd_verify(settings, args) -> int:
    from . import db
    from .discover import load_rules
    from .sinks.loki import Loki
    from .swis.client import SwisClient
    from .verify import run
    net = settings.network(args.network)
    with db.connect(settings) as conn:
        db.apply_meta(conn)
        rep = run(conn, SwisClient(net, net.resolve_password(), capture=False), Loki(settings.loki_url), net,
                  load_rules(), hours=args.hours)
    for c in rep["checks"]:
        print(f"  [{c['status'].upper():4}] {c['check']}: {c['detail']}")
        for it in c["items"][:5]:
            print(f"         {it}")
    print(f"{net.name}: verify {rep['overall'].upper()} (run {rep['run_id']}); report {rep.get('file')}")
    return 0 if rep["overall"] != "fail" else 1


def cmd_serve(settings, args) -> int:
    from .scheduler import Service
    names = args.network or [n for n, p in settings.networks.items() if not p.actions_enabled]
    nets = [settings.network(n) for n in names]
    svc = Service(settings, nets, tick_seconds=args.tick)
    svc.start()
    print(f"swcollect service: {', '.join(n.name for n in nets)}; schedule from catalog/collector.yaml")
    try:
        if args.no_hook:
            while True:
                time.sleep(3600)
        from werkzeug.serving import WSGIRequestHandler

        from .webhook import create_app as hook_app
        WSGIRequestHandler.protocol_version = "HTTP/1.1"
        redact_token_in_access_log()
        print(f"push receiver on http://{settings.hook_host}:{settings.hook_port}/hook/<network> "
              f"(token {'required' if settings.hook_token else 'NOT set: set SWCOLLECT_HOOK_TOKEN'})")
        hook_app(settings).run(host=settings.hook_host, port=settings.hook_port, threaded=True, load_dotenv=False)
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop.set()
    return 0


def cmd_erd(settings, args) -> int:
    from . import db, erd
    from .jobs.inventory import instance_id_of
    net = settings.network(args.network)
    with db.connect(settings) as conn:
        paths = erd.write(conn, instance_id_of(conn, net))
    for p in paths:
        print(f"written {p}")
    return 0


def cmd_ddl(settings, args) -> int:
    import yaml

    from .ddl import META_DDL, entity_ddl
    net = settings.network(args.network)
    path = _catalog_path(net)
    if not path.exists():
        print(f"No catalog at {path}: run `swcollect discover --network {args.network}` first", file=sys.stderr)
        return 1
    entries = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("entities") or {}
    if not args.entity:
        for s in META_DDL:
            print(s.strip() + ";\n")
    for name, e in entries.items():
        if args.entity and name != args.entity:
            continue
        for s in entity_ddl(e):
            print(s.strip() + ";\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

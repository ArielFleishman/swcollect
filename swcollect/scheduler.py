"""The collector service: runs every job on its schedule (catalog/collector.yaml `schedule`), plus the push receiver.

  due       a job is due when its last run (sw_meta.sync_run) is older than its interval, or, for daily_at, when
            today's time has passed and it has not run since. A failed run is retried after
            retry_after_failure_minutes. Times come from the database clock; daily_at is the collector host's time.
  order     discover (when the network has no catalog yet, or daily), inventory, inventory_core, routing, metrics,
            events, verify. `then` jobs (curate) run right after their job.
  load      one thread per network, its jobs one after another: at most one SWIS query stream per network.
  locks     each (instance, job) runs under pg_try_advisory_lock, so two collectors never run the same job for the
            same network at once; the second one skips it.
  failure   an exception is recorded on the run (status failed, error) and never stops the service.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import psycopg

from . import db
from .config import CATALOG_DIR, Network, Settings
from .discover import load_rules

ORDER = ("discover", "inventory", "inventory_core", "routing", "metrics", "events", "verify")


def interval_minutes(job: str, sched: dict, cadence: dict | None) -> int | None:
    j = sched.get(job) or {}
    if "daily_at" in j:
        return 24 * 60
    every = j.get("every_minutes")
    if every == "cadence":
        return int((cadence or {}).get("every_minutes") or 5)
    return int(every) if every else None


def _last_runs(conn, inst: int | None) -> dict:
    if inst is None:
        return {}
    out = {}
    for job, last_any, last_status, last_ok in conn.execute(
            """SELECT job, max(started_at),
                      (array_agg(status ORDER BY started_at DESC))[1],
                      max(started_at) FILTER (WHERE status IN ('ok', 'partial'))
               FROM sw_meta.sync_run WHERE instance_id = %s GROUP BY job""", (inst,)).fetchall():
        out[job] = {"any": last_any, "status": last_status, "ok": last_ok}
    return out


def due_jobs(conn, net: Network, sched: dict, now: datetime | None = None) -> list[tuple[str, str]]:
    """[(job, why)] in ORDER. `now` is timezone-aware (the database clock)."""
    now = now or conn.execute("SELECT now()").fetchone()[0]
    row = conn.execute("SELECT instance_id, metrics_cadence FROM sw_meta.instance WHERE instance_key = %s",
                       (net.instance_key,)).fetchone()
    inst, cad = (row[0], row[1]) if row else (None, None)
    has_catalog = inst is not None and conn.execute(
        "SELECT EXISTS (SELECT 1 FROM sw_meta.entity_catalog WHERE instance_id = %s)", (inst,)).fetchone()[0]
    if not has_catalog:
        return [("discover", "no catalog for this network yet")]
    last = _last_runs(conn, inst)
    retry = timedelta(minutes=int(sched.get("retry_after_failure_minutes", 5)))
    out = []
    for job in ORDER:
        j = sched.get(job)
        if not j:
            continue
        lr = last.get(job) or {}
        if lr.get("status") in ("failed", "running") and lr.get("any") and now - lr["any"] < retry:
            continue                                   # failed or still marked running: wait before retrying
        if "daily_at" in j:
            hh, mm = (int(x) for x in str(j["daily_at"]).split(":"))
            local = now.astimezone()
            today = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            slot = today if local >= today else today - timedelta(days=1)
            if not lr.get("ok") or lr["ok"] < slot:
                out.append((job, f"daily at {j['daily_at']}: last ok {lr.get('ok')}"))
        else:
            mins = interval_minutes(job, sched, cad)
            if not lr.get("ok") or now - lr["ok"] >= timedelta(minutes=mins):
                out.append((job, f"every {mins} min: last ok {lr.get('ok')}"))
    return out


class JobRunner:
    """Runs one job for one network: its own connection, an advisory lock, failure recorded on the run."""

    def __init__(self, settings: Settings, net: Network, log=print):
        from .sinks.loki import Loki
        from .swis.client import SwisClient
        self.settings, self.net, self.log = settings, net, log
        self.client = SwisClient(net, net.resolve_password(), capture=False)
        self.loki = Loki(settings.loki_url)

    def run(self, job: str) -> dict:
        rules = load_rules()
        sched = rules.get("schedule") or {}
        with db.connect(self.settings) as conn:
            db.apply_meta(conn)
            key = f"swcollect:{self.net.instance_key}:{job}"
            got = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (key,)).fetchone()[0]
            if not got:
                return {"job": job, "skipped": "another collector holds the lock"}
            started = conn.execute("SELECT now()").fetchone()[0]
            try:
                self.client.stats.clear()
                out = self._run(conn, job, rules, sched)
                for then in (sched.get(job) or {}).get("then") or []:
                    out[then] = self._run(conn, then, rules, sched)
                return out
            except Exception as e:  # noqa: BLE001 – recorded, never stops the service
                msg = f"{type(e).__name__}: {e}"
                conn.execute("""UPDATE sw_meta.sync_run r SET status = 'failed', finished_at = now(), error = %s
                                FROM sw_meta.instance i WHERE r.instance_id = i.instance_id AND i.instance_key = %s
                                AND r.status = 'running' AND r.started_at >= %s""",
                             (msg + "\n" + traceback.format_exc()[-2000:], self.net.instance_key, started))
                inst = conn.execute("SELECT instance_id FROM sw_meta.instance WHERE instance_key = %s",
                                    (self.net.instance_key,)).fetchone()
                if inst and not conn.execute("SELECT EXISTS (SELECT 1 FROM sw_meta.sync_run WHERE instance_id = %s "
                                             "AND job = %s AND started_at >= %s)", (inst[0], job, started)).fetchone()[0]:
                    rid = db.start_run(conn, inst[0], job)
                    db.finish_run(conn, rid, "failed", error=msg)
                return {"job": job, "error": msg}
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))

    def _run(self, conn, job: str, rules: dict, sched: dict) -> dict:
        from . import steps, verify
        from .jobs import events, inventory, metrics
        ps = self.settings.page_size
        if job == "discover":
            r = steps.discover_step(self.client, self.net, CATALOG_DIR / "discovered" /
                                    f"{self.net.stable_network_id}.yaml", conn)
            return {"job": job, "run_id": r["run_id"], "summary": r["catalog"]["summary"], "ddl_errors": len(r["errors"])}
        if job in ("inventory", "inventory_core", "routing"):
            only = (sched.get(job) or {}).get("entities") if job != "inventory" else None
            r = inventory.run(conn, self.client, self.net, ps, only=only, log=self.log, job=job)
            return {"job": job, "run_id": r["run_id"], "status": r["status"], "rows": r["rows"]}
        if job == "curate":
            r = steps.curate_step(conn, self.client, self.net)
            return {"job": job, "run_id": r["run_id"], "views": r["views"]["views"], "unbalanced": len(r["unbalanced"])}
        if job == "metrics":
            r = metrics.run(conn, self.client, self.net, ps, rules.get("metrics_policy") or {}, log=self.log)
            return {"job": job, "run_id": r["run_id"], "status": r["status"], "inserted": r["inserted"],
                    "updated": r["updated"], "cadence": r["cadence"]["every_minutes"]}
        if job == "events":
            r = events.run(conn, self.client, self.loki, self.net, ps, rules, log=self.log)
            return {"job": job, "run_id": r["run_id"], "status": r["status"], "loki": r["loki_accepted"],
                    "reconcile": {k: r["reconcile"].get(k) for k in ("history", "push_only")}}
        if job == "verify":
            r = verify.run(conn, self.client, self.loki, self.net, rules)
            return {"job": job, "run_id": r["run_id"], "overall": r["overall"],
                    "checks": {c["check"]: c["status"] for c in r["checks"]}}
        raise ValueError(f"unknown job {job}")


def run_once(settings: Settings, net: Network, jobs: list[str], log=print) -> list[dict]:
    """Runs the given jobs now, in ORDER (curate after inventory jobs via `then`)."""
    runner = JobRunner(settings, net, log)
    order = [j for j in ORDER if j in jobs] + [j for j in jobs if j not in ORDER]
    return [runner.run(j) for j in order]


class Service:
    def __init__(self, settings: Settings, networks: list[Network], tick_seconds: int = 15, log=print):
        self.settings, self.networks, self.tick, self.log = settings, networks, tick_seconds, log
        self.stop = threading.Event()

    def _loop(self, net: Network):
        runner = JobRunner(self.settings, net, self.log)
        while not self.stop.is_set():
            try:
                sched = load_rules().get("schedule") or {}
                with db.connect(self.settings) as conn:
                    db.apply_meta(conn)
                    due = due_jobs(conn, net, sched)
                for job, why in due:
                    if self.stop.is_set():
                        break
                    t0 = time.perf_counter()
                    self.log(f"[{datetime.now():%H:%M:%S}] {net.name}: {job} ({why})")
                    r = runner.run(job)
                    self.log(f"[{datetime.now():%H:%M:%S}] {net.name}: {job} done in "
                             f"{time.perf_counter() - t0:.1f} s: {r}")
            except psycopg.Error as e:
                self.log(f"[{datetime.now():%H:%M:%S}] {net.name}: PostgreSQL unavailable ({e}); retrying")
            except Exception as e:  # noqa: BLE001
                self.log(f"[{datetime.now():%H:%M:%S}] {net.name}: scheduler error {type(e).__name__}: {e}")
            self.stop.wait(self.tick)

    def start(self) -> list[threading.Thread]:
        threads = [threading.Thread(target=self._loop, args=(n,), name=f"swcollect-{n.name}", daemon=True)
                   for n in self.networks]
        for t in threads:
            t.start()
        return threads

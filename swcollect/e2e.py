"""End-to-end check against running services (plan, Verification): the mock SWIS, PostgreSQL + TimescaleDB, Loki and
the push receiver, all over HTTP. It runs inside the closed compose network (scripts/e2e.ps1) or on a host with the
dev ports. It changes the MOCK through its test-only /mock/admin endpoints; it never sends a write to a real SWIS:
it refuses to run unless the profile answers /mock/admin.

Steps (each PASS / FAIL with its evidence):
  1  run every job once: discover, inventory + curate, metrics, events, verify
  2  coverage: SWIS COUNT = stored for every inventory entity; every entity has a status; the planted orphan link is
     in the dead-letter queue; core relationships proven; every flow balanced
  3  source changes: add / change / remove a node → insert, update with the field diff, soft delete, the removed
     node's interfaces in the dead-letter queue; a hidden entity is reported empty and nothing is deleted
  4  metrics: 30 minutes later + 2 recomputed samples → appended, updated with old and new value, no duplicates
  5  alerts: triggers / resets pushed to the receiver with every 4th push lost → the poll fills exactly the lost ones
  6  Loki completeness for every ID-based source (verify)
  7  outage: Loki down for one events run → error, watermark kept; next run fills the gap
"""
from __future__ import annotations

import time

import requests

from .config import Network, Settings


class E2E:
    def __init__(self, settings: Settings, net: Network, hook_url: str, token: str | None, log=print):
        self.s, self.net, self.hook_url, self.token, self.log = settings, net, hook_url, token, log
        self.admin_base = f"{net.scheme}://{net.host}:{net.port}/mock/admin"
        self.auth = (net.username, net.resolve_password() or "")
        self.results: list[dict] = []

    def admin(self, what: str, body: dict) -> dict:
        r = requests.post(f"{self.admin_base}/{what}", json=body, auth=self.auth, timeout=60)
        r.raise_for_status()
        return r.json()

    def step(self, name: str, ok: bool, evidence: str):
        self.results.append({"step": name, "ok": bool(ok), "evidence": evidence})
        self.log(f"  [{'PASS' if ok else 'FAIL'}] {name}: {evidence}")

    def verify_until_loki_ok(self, runner, seconds: int = 150) -> dict:
        """Loki makes accepted entries queryable after a short delay: verify again until the completeness check
        passes or the time is up (the last verify is returned either way)."""
        t = time.monotonic()
        while True:
            v = runner.run("verify")
            if v.get("checks", {}).get("events: Loki completeness") == "ok" or time.monotonic() - t > seconds:
                return v
            time.sleep(10)

    def wait_for_new_event(self, conn, inst: int, after: int, seconds: int = 120):
        from .swis.client import SwisClient
        c = SwisClient(self.net, self.net.resolve_password(), capture=False)
        t = time.monotonic()
        while time.monotonic() - t < seconds:
            if (c.query("SELECT MAX(e.EventID) AS M FROM Orion.Events e").rows[0]["M"] or 0) > after:
                return
            time.sleep(3)

    def q(self, conn, sql, *a):
        return conn.execute(sql, a).fetchall()

    def run(self) -> bool:
        from . import db
        from .scheduler import JobRunner
        try:
            self.admin("inventory", {})
        except requests.RequestException as e:
            raise SystemExit(f"{self.admin_base} does not answer ({e}): e2e runs only against the mock") from e
        runner = JobRunner(self.s, self.net, log=lambda *a: None)
        conn = db.connect(self.s)
        db.apply_meta(conn)
        # The e2e moves the mock's clock 9 minutes. A mock already moved by an earlier run writes events further
        # ahead than Loki accepts (10 minutes), so it must be a freshly started one.
        from .swis.timeutil import parse_dt
        u = runner.client.query("SELECT TOP 1 GETUTCDATE() AS U FROM Orion.Engines").rows[0]["U"]
        skew = (parse_dt(u) - conn.execute("SELECT now() AT TIME ZONE 'UTC'").fetchone()[0]).total_seconds()
        if abs(skew) > 120:
            raise SystemExit(f"the mock's clock is {skew / 60:+.0f} min off: restart the mock before the e2e")
        ik = self.net.instance_key

        # 1 -----------------------------------------------------------------------------------------------------
        self.log("1. every job once")
        out = {j: runner.run(j) for j in ("discover", "inventory", "metrics", "events", "verify")}
        errors = {j: r.get("error") for j, r in out.items() if r.get("error")}
        self.step("every job ran", not errors, str(errors or {j: r.get("run_id") for j, r in out.items()}))
        inst = self.q(conn, "SELECT instance_id FROM sw_meta.instance WHERE instance_key = %s", ik)[0][0]

        # 2 -----------------------------------------------------------------------------------------------------
        self.log("2. coverage, relationships, flows")
        run = self.q(conn, "SELECT max(run_id) FROM sw_meta.sync_run WHERE instance_id = %s AND job = 'inventory'", inst)[0][0]
        bad = self.q(conn, """SELECT entity, status, swis_count, stored FROM sw_meta.coverage WHERE run_id = %s
                              AND status NOT IN ('ok', 'empty', 'excluded', 'other_job')""", run)
        self.step("SWIS COUNT = stored (inventory)", not bad, f"run {run}; not matching: {bad}")
        missing = self.q(conn, """SELECT count(*) FROM sw_meta.entity_catalog e WHERE instance_id = %s AND NOT EXISTS
                                  (SELECT 1 FROM sw_meta.coverage c WHERE c.run_id = %s AND c.entity = e.entity)""",
                         inst, run)[0][0]
        total = self.q(conn, "SELECT count(*) FROM sw_meta.entity_catalog WHERE instance_id = %s", inst)[0][0]
        self.step("every entity in Metadata.Entity has a status", missing == 0, f"{total} entities, {missing} without")
        dl = self.q(conn, """SELECT count(*) FROM sw_core.dead_letter WHERE instance_id = %s AND core_table = 'link'
                             AND value = '99999' AND resolved_at IS NULL""", inst)[0][0]
        self.step("planted orphan link in the dead-letter queue", dl == 1, f"{dl} row(s)")
        core = self.q(conn, """SELECT source_entity || '.' || name, status FROM sw_meta.relationship WHERE instance_id = %s
                               AND (source_entity, name) IN (('Orion.Nodes', 'Interfaces'), ('Orion.NPM.Interfaces', 'Node'),
                               ('Orion.TopologyConnections', 'SrcNode'), ('NCM.Nodes', 'OrionNode'))""", inst)
        self.step("core relationships proven", len(core) == 4 and all(s == "proven" for _, s in core), str(core))
        cur = self.q(conn, "SELECT max(run_id) FROM sw_meta.sync_run WHERE instance_id = %s AND job = 'curate'", inst)[0][0]
        unb = self.q(conn, "SELECT target_table FROM sw_meta.flow WHERE run_id = %s AND rows_in <> rows_out + rows_dlq", cur)
        nflows = self.q(conn, "SELECT count(*) FROM sw_meta.flow WHERE run_id = %s", cur)[0][0]
        self.step("every flow balanced (in = out + dead-letter)", not unb and nflows > 10, f"{nflows} flows, unbalanced {unb}")

        # 3 -----------------------------------------------------------------------------------------------------
        self.log("3. source changes")
        ch = self.admin("inventory", {"add_node": True, "change_node": 3, "remove_node": 5, "hide": ["Orion.NodeVlans"]})
        r = runner.run("inventory")
        run = r["run_id"]
        log = {(c, k.get("NodeID")): f for c, k, f in self.q(
            conn, "SELECT change, row_key, fields FROM sw_meta.change_log WHERE run_id = %s AND entity = 'Orion.Nodes'", run)}
        added = ("insert", ch["added_node"]) in log or ("undelete", ch["added_node"]) in log
        self.step("insert (or undelete) / update with field diff / soft delete",
                  added and ("soft_delete", 5) in log and ("update", 3) in log
                  and "Caption" in (log.get(("update", 3)) or {}), str(sorted(k for k in log)))
        vl = self.q(conn, "SELECT status, stored FROM sw_meta.coverage WHERE run_id = %s AND entity = 'Orion.NodeVlans'", run)
        self.step("an empty result deletes nothing", bool(vl) and vl[0][0] == "empty" and vl[0][1] > 0, str(vl))
        orphans = self.q(conn, """SELECT count(*) FROM sw_core.dead_letter WHERE instance_id = %s AND core_table = 'interface'
                                  AND rule = 'ref' AND resolved_at IS NULL""", inst)[0][0]
        self.step("children of the removed node in the dead-letter queue", orphans > 0, f"{orphans} interfaces")
        self.admin("inventory", {"unhide": ["Orion.NodeVlans"], "restore_node": 5})
        runner.run("inventory")

        # 4 -----------------------------------------------------------------------------------------------------
        self.log("4. metrics")
        re_ = self.admin("metrics", {"reaggregate": {"entity": "Orion.CPULoad", "field": "AvgLoad", "rows": 2}})
        # 9 minutes: new samples, and events stay within Loki's 10-minute tolerance for future timestamps
        added = self.admin("metrics", {"advance_minutes": 9})["added"]
        r = runner.run("metrics")
        self.step("new samples appended, recomputed ones updated",
                  r.get("inserted", 0) >= sum(added.values()) and r.get("updated") == 2,
                  f"inserted {r.get('inserted')} (expected {sum(added.values())}), updated {r.get('updated')} "
                  f"(expected 2: {re_['reaggregated']})")
        d = self.q(conn, 'SELECT count(*) - count(DISTINCT ("NodeID", "DateTime")) FROM sw_ts.orion_cpuload '
                         'WHERE instance_id = %s', inst)[0][0]
        self.step("no duplicate samples", d == 0, f"{d} duplicates")

        # 5 -----------------------------------------------------------------------------------------------------
        self.log("5. alerts: push + safety net")
        url = self.hook_url.rstrip("/") + f"/{self.net.name}"
        al = self.admin("alerts", {"trigger": 6, "reset": 3, "drop_every": 4, "push_url": url, "token": self.token})
        sent = [p for p in al["pushes"] if not p["dropped"]]
        self.step("pushes accepted by the receiver", all(p["status"] == 200 for p in sent),
                  f"{len(sent)} sent: {[p['status'] for p in sent]}")
        runner.run("events")
        st = dict(self.q(conn, """SELECT alert_history_id, status FROM sw_meta.alert_reconcile WHERE instance_id = %s
                                  AND alert_key LIKE 'h:%%'""", inst))
        want = {h: ("poll_only" if p["dropped"] else "matched") for h, p in zip(al["history"], al["pushes"])}
        got = {h: st.get(h) for h in want}
        self.step("the poll fills exactly the lost pushes", got == want,
                  f"lost {[h for h, s in want.items() if s == 'poll_only']}; got {got}")

        # 6 -----------------------------------------------------------------------------------------------------
        self.log("6. verify (Loki completeness and the rest)")
        v = self.verify_until_loki_ok(runner)
        lk = v.get("checks", {}).get("events: Loki completeness")
        self.step("every event ID SWIS has is in Loki", lk == "ok", f"verify: {v.get('checks')}")

        # 7 -----------------------------------------------------------------------------------------------------
        self.log("7. Loki outage")
        from .sinks.loki import Loki
        before = self.q(conn, "SELECT value FROM sw_meta.sync_state WHERE instance_id = %s AND entity = 'Orion.Events'",
                        inst)[0][0]
        self.wait_for_new_event(conn, inst, int(before))              # the mock writes events as time passes
        real = runner.loki
        runner.loki = Loki("http://127.0.0.1:1", retries=1, sleep=lambda s: None)
        r = runner.run("events")
        runner.loki = real
        kept = self.q(conn, "SELECT value FROM sw_meta.sync_state WHERE instance_id = %s AND entity = 'Orion.Events'",
                      inst)[0][0]
        self.step("Loki down: error, watermark kept", "error" in (r.get("status") or {}) and kept == before,
                  f"status {r.get('status')}, watermark {before} → {kept}")
        runner.run("events")
        v = self.verify_until_loki_ok(runner)
        after = self.q(conn, "SELECT value FROM sw_meta.sync_state WHERE instance_id = %s AND entity = 'Orion.Events'",
                       inst)[0][0]
        self.step("next run fills the gap", int(after) > int(before) and
                  v.get("checks", {}).get("events: Loki completeness") == "ok", f"watermark {before} → {after}")
        conn.close()
        ok = all(r["ok"] for r in self.results)
        self.log(f"e2e: {sum(r['ok'] for r in self.results)}/{len(self.results)} passed → {'PASS' if ok else 'FAIL'}")
        return ok

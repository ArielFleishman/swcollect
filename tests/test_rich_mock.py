"""Rich mock (`swcollect mock --rich`): realistic rows for every schema entity, consistent references, metric history
with honest gaps. Unit tests run anywhere; the PostgreSQL load test needs SWCOLLECT_IT=1.

The load test uses its own database (swcollect_it_rich, recreated each run), because sw_raw tables are shared by
every network of a database and get their key from the first network that creates them; the plain mock's keys
differ from the rich mock's.
"""
import os
import re
import time
import unittest
import uuid
from datetime import datetime, timedelta

from swcollect.config import Network
from swcollect.discover import load_rules
from swcollect.mock import fixtures, rich
from swcollect.mock.mock_swis import MockSwis, create_app

IT = os.environ.get("SWCOLLECT_IT") == "1"
DSN = os.environ.get("SWCOLLECT_PG_DSN", "postgresql://swcollect:swcollect@127.0.0.1:15432/swcollect")


class RichMockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = MockSwis(rich=True, history_days=1, hourly_days=1)
        cls.D = cls.m.data
        cls.info = cls.m.rich_info
        cls.generated = set(cls.D) - cls.info["curated"]

    def test_every_schema_entity_has_rows_or_a_reason(self):
        for name, page in rich.pages().items():
            if name.startswith(("System.", "Metadata.")) or "System.Indication" in (page.get("bases") or []):
                continue
            rows = self.D.get(name)
            if rows:
                continue
            self.assertIn(name, self.info["empty"] | {e: "" for e in fixtures.EXTRA_ENTITIES} |
                          {e: "" for e in fixtures.EMPTY_SCHEMA}, f"{name} has no rows and no reason")

    def test_no_placeholder_values(self):
        copies = {m for m, src in rich.MIRRORS.items() if src in self.info["curated"]}   # curated data, copied
        for name in self.generated - copies:
            for r in self.D[name][:50]:
                for f, v in r.items():
                    # the plain mock's placeholders are the field's own name and a number: "Caption-0", "Name-3-a"
                    if isinstance(v, str) and f != "Uri":
                        self.assertNotRegex(v, rf"^{re.escape(f)}-\d+(-[ab])?$", f"{name}.{f} = {v!r}")

    def test_values_fit_their_documented_types(self):
        for name in self.generated:
            fields = rich.doc_fields(name)
            for r in self.D[name][:500]:
                for f, t in fields.items():
                    self.assertTrue(rich.fits(r.get(f), t), f"{name}.{f} = {r.get(f)!r} does not fit {t}")

    def test_references_point_at_real_rows(self):
        nodes = {n["NodeID"] for n in self.D["Orion.Nodes"]}
        ifaces = {i["InterfaceID"] for i in self.D["Orion.NPM.Interfaces"]}
        guids = {n["NodeID"] for n in self.D["Cirrus.Nodes"]}
        ncm_ifs = {i["InterfaceID"] for i in self.D["NCM.Interfaces"]}
        configs = {c["ConfigID"] for c in self.D["NCM.ConfigArchive"]}
        assigns = {a["CustomPollerAssignmentID"] for a in self.D["Orion.NPM.CustomPollerAssignment"]}
        for name in self.generated:
            for r in self.D[name]:
                for f, v in r.items():
                    if v is None:
                        continue
                    t = rich.doc_type(name, f)
                    if f in ("NodeID", "CoreNodeID") and isinstance(v, int):
                        self.assertIn(v, nodes, f"{name}.{f}")
                    elif f == "NodeID" and t == "System.Guid":
                        self.assertIn(v, guids, f"{name}.{f}")
                    elif f == "InterfaceID" and isinstance(v, int) and name != "NCM.ConfigInterfaceIpAddress":
                        self.assertIn(v, ifaces, f"{name}.{f}")
                    elif f == "InterfaceID" and t == "System.Guid" and name not in ("NCM.Interfaces", "Cirrus.Interfaces"):
                        self.assertIn(v, ncm_ifs, f"{name}.{f}")
                    elif f.lower() == "configid" and name.startswith("NCM.") and name != "NCM.ConfigArchive":
                        self.assertIn(v, configs, f"{name}.{f}")
                    elif f == "CustomPollerAssignmentID":
                        self.assertIn(v, assigns, f"{name}.{f}")

    def test_documented_types_and_keys_in_metadata(self):
        props = {(p["EntityName"], p["Name"]): p for p in self.D["Metadata.Property"]}
        self.assertEqual(props[("NCM.Interfaces", "InterfaceID")]["Type"], "System.Guid")
        self.assertTrue(props[("NCM.Interfaces", "InterfaceID")]["IsKey"])
        self.assertEqual(props[("NCM.CiscoFlash", "FlashSize")]["Type"], "System.Int64")
        ents = {e["FullName"]: e for e in self.D["Metadata.Entity"]}
        self.assertEqual(ents["Orion.HardwareHealth.HardwareInfo"]["BaseType"], "Orion.HardwareHealth.HardwareInfoBase")

    def test_history_has_honest_gaps(self):
        byname = {n["Caption"]: n["NodeID"] for n in self.D["Orion.Nodes"]}
        model = self.info["model"]
        cpu = self.D["Orion.CPULoad"]
        down, unmanaged = byname["AGG-06"], byname["ACC-14"]
        down_at = rich._fmt(model.down_at[down])
        self.assertFalse([r for r in cpu if r["NodeID"] == down and r["DateTime"] >= down_at])
        self.assertTrue([r for r in cpu if r["NodeID"] == down and r["DateTime"] < down_at])
        um_at = rich._fmt(model.unmanaged_at[unmanaged])
        self.assertFalse([r for r in cpu if r["NodeID"] == unmanaged and r["DateTime"] >= um_at])
        # Orion keeps pinging a down node: response rows with 100 % loss and a null time (never 0)
        rt = [r for r in self.D["Orion.ResponseTime"] if r["NodeID"] == down and r["DateTime"] >= down_at]
        self.assertTrue(rt)
        self.assertTrue(all(r["PercentLoss"] == 100 and r["AvgResponseTime"] is None for r in rt))
        # one day of 10-minute samples plus one day of hourly ones per Up SNMP node
        per = {}
        for r in cpu:
            per[r["NodeID"]] = per.get(r["NodeID"], 0) + 1
        self.assertEqual(per[byname["P-CORE-01"]], 144 + 24)

    def test_series_are_sorted_and_keys_unique(self):
        for e, (keys, _step) in rich.RICH_SERIES.items():
            rows = self.D[e]
            self.assertEqual([r["DateTime"] for r in rows], sorted(r["DateTime"] for r in rows), e)
            ids = [tuple(r[k] for k in keys) + (r["DateTime"],) for r in rows]
            self.assertEqual(len(ids), len(set(ids)), f"{e}: duplicate samples")

    def test_live_sampling_continues_the_curve(self):
        m = MockSwis(rich=True, history_days=1, hourly_days=1)
        n0 = len(m.data["Orion.NPM.InterfaceTraffic"])
        out = m.change_metrics({"advance_minutes": 30})
        self.assertGreater(out["added"]["Orion.NPM.InterfaceTraffic"], 0)
        self.assertEqual(len(m.data["Orion.NPM.InterfaceTraffic"]), n0 + out["added"]["Orion.NPM.InterfaceTraffic"])
        new = m.data["Orion.NPM.InterfaceTraffic"][-1]
        key = (new["InterfaceID"],)
        t = datetime.strptime(new["DateTime"], "%Y-%m-%dT%H:%M:%S")
        self.assertEqual(new["InAveragebps"], m.rich_info["model"].sample("Orion.NPM.InterfaceTraffic", key, t)[
            "InAveragebps"])

    def test_time_index_gives_the_same_answer(self):
        rows = self.D["Orion.NPM.InterfaceTraffic"]
        a, b = rows[len(rows) // 3]["DateTime"], rows[len(rows) // 2]["DateTime"]
        q = (f"SELECT e.InterfaceID, e.DateTime FROM Orion.NPM.InterfaceTraffic e WHERE e.DateTime >= '{a}' AND "
             f"e.DateTime < '{b}' ORDER BY e.DateTime, e.InterfaceID")
        self.m._tick_metrics("Orion.NPM.InterfaceTraffic")
        t0 = time.perf_counter()
        fast = self.m.query(q, {})
        took = time.perf_counter() - t0
        expect = sorted(((r["InterfaceID"], r["DateTime"]) for r in self.D["Orion.NPM.InterfaceTraffic"]
                         if a <= r["DateTime"] < b), key=lambda x: (x[1], x[0]))
        self.assertEqual([(r["InterfaceID"], r["DateTime"]) for r in fast], expect)
        self.assertLess(took, 5)

    def test_deterministic(self):
        now = datetime(2026, 11, 22, 10, 0, 0)
        d1, d2 = fixtures.build(now_utc=now), fixtures.build(now_utc=now)
        rich.extend(d1, now, now + fixtures.TZ_OFFSET, 1, 1)
        rich.extend(d2, now, now + fixtures.TZ_OFFSET, 1, 1)
        for e in ("NCM.CiscoChassis", "NCM.Interfaces", "Orion.Events", "Orion.NPM.InterfaceTraffic"):
            self.assertEqual(d1[e], d2[e], e)

    def test_default_mock_is_unchanged(self):
        plain = MockSwis(schema_all=True)
        self.assertEqual(plain.series, {k: v for k, v in plain.series.items()})
        self.assertNotIn("Orion.NPM.CustomPollerStatistics", plain.series)
        self.assertEqual(plain.data["Orion.NPM.CustomPollers"][0]["CustomPollerID"], "a1")
        self.assertEqual(len(plain.data["Orion.OLM.LogEntry"]), 250)


@unittest.skipUnless(IT, "integration: set SWCOLLECT_IT=1 with PostgreSQL + TimescaleDB running")
class RichLoadTest(unittest.TestCase):
    """The rich mock goes through the real jobs into PostgreSQL: sw_raw, sw_core, sw_ts."""
    DB = "swcollect_it_rich"

    @classmethod
    def setUpClass(cls):
        import psycopg

        from swcollect.jobs import inventory, metrics
        from swcollect.steps import curate_step, discover_step
        from swcollect.swis.client import SwisClient

        from .flask_session import FlaskSession
        admin = psycopg.connect(DSN, autocommit=True)
        admin.execute(f"DROP DATABASE IF EXISTS {cls.DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {cls.DB}")
        admin.close()
        cls.dsn = DSN.rsplit("/", 1)[0] + "/" + cls.DB
        cls.conn = psycopg.connect(cls.dsn, autocommit=True)
        from pathlib import Path
        cls.conn.execute((Path(__file__).resolve().parents[1] / "deploy" / "init.sql").read_text(encoding="utf-8"))
        cls.app = create_app(rich=True, history_days=1, hourly_days=1)
        cls.net = Network(name="itrich", host=f"it-{uuid.uuid4().hex[:8]}", port=17774, scheme="http",
                          username="readonly", password="mock", network_id="itrich")
        cls.client = SwisClient(cls.net, "mock", session=FlaskSession(cls.app), capture=False)
        cls.inst = discover_step(cls.client, cls.net, None, cls.conn)["instance_id"]
        cls.inv = inventory.run(cls.conn, cls.client, cls.net, 5000, log=lambda *a: None)
        cls.cur = curate_step(cls.conn, cls.client, cls.net)
        cls.met = metrics.run(cls.conn, cls.client, cls.net, 5000, load_rules()["metrics_policy"],
                              log=lambda *a: None)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_sw_raw_matches_the_mock(self):
        swis = self.app.config["MOCK_SWIS"]
        for entity, sink in self.conn.execute(
                "SELECT entity, sink_table FROM sw_meta.entity_catalog WHERE instance_id = %s AND class = 'inventory' "
                "AND collect", (self.inst,)).fetchall():
            n = self.conn.execute(f"SELECT count(*) FROM {sink} WHERE instance_id = %s AND _deleted_at IS NULL",
                                  (self.inst,)).fetchone()[0]
            self.assertEqual(n, len(swis.data.get(entity, [])), entity)

    def test_core_tables_filled_and_flows_balanced(self):
        from swcollect import dbstats
        rep = dbstats.collect(self.conn, self.inst)
        core = {i["table"]: i["rows"] for i in rep["schemas"]["sw_core"]}
        for t in ("device", "interface", "link", "ncm_node", "hardware_component", "routing_neighbor", "subnet"):
            self.assertGreater(core[t], 0, t)
        self.assertEqual(rep["unexplained"], [])
        bad = self.conn.execute("SELECT target_table FROM sw_meta.flow f JOIN sw_meta.sync_run r USING (run_id) "
                                "WHERE r.instance_id = %s AND rows_in <> rows_out + rows_dlq", (self.inst,)).fetchall()
        self.assertEqual(bad, [])

    def test_metrics_coverage_exact(self):
        self.assertEqual(set(self.met["status"]), {"ok"})
        swis = self.app.config["MOCK_SWIS"]
        for e in rich.RICH_SERIES:
            sink = self.conn.execute("SELECT sink_table FROM sw_meta.entity_catalog WHERE instance_id = %s AND "
                                     "entity = %s", (self.inst, e)).fetchone()[0]
            n = self.conn.execute(f"SELECT count(*) FROM {sink} WHERE instance_id = %s", (self.inst,)).fetchone()[0]
            self.assertGreaterEqual(n, len(swis.data[e]) - 200, e)    # live sampling may add rows meanwhile
            self.assertGreater(n, 0, e)


if __name__ == "__main__":
    unittest.main()

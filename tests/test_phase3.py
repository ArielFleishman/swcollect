"""Phase 3: metrics into TimescaleDB. Unit tests run anywhere; the integration test needs SWCOLLECT_IT=1."""
import os
import unittest
import uuid
from datetime import datetime
from unittest import mock

from swcollect.config import Network
from swcollect.discover import load_rules
from swcollect.jobs.metrics import _slices, cadence

IT = os.environ.get("SWCOLLECT_IT") == "1"
DSN = os.environ.get("SWCOLLECT_PG_DSN", "postgresql://swcollect:swcollect@127.0.0.1:15432/swcollect")
POLICY = load_rules()["metrics_policy"]


class CadenceTest(unittest.TestCase):
    def test_follows_stat_collection(self):
        c = cadence([10, 10, 9], POLICY)
        self.assertEqual(c["every_minutes"], 9)                 # smallest StatCollection, above the 5-minute floor
        self.assertEqual(c["overlap_minutes"], 20)              # 2 × the largest
        self.assertEqual(cadence([2], POLICY)["every_minutes"], 5)     # never faster than min_minutes
        self.assertEqual(cadence([60], POLICY)["every_minutes"], 15)   # clamped
        self.assertEqual(cadence([60], POLICY)["overlap_minutes"], 120)

    def test_unknown_stat_collection_is_marked(self):
        c = cadence([], POLICY)
        self.assertEqual(c["every_minutes"], 5)
        self.assertIn("needs proof", c["basis"])

    def test_slices_cover_the_range(self):
        a, b = datetime(2026, 11, 1), datetime(2026, 11, 3, 5)
        s = list(_slices(a, b, 24))
        self.assertEqual(s[0][0], a)
        self.assertGreater(s[-1][1], b)
        for (x1, y1), (x2, _y2) in zip(s, s[1:]):
            self.assertEqual(y1, x2)                            # contiguous, no gap, no overlap


@unittest.skipUnless(IT, "integration: set SWCOLLECT_IT=1 with PostgreSQL + TimescaleDB running")
class MetricsIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        from swcollect.jobs import inventory
        from swcollect.mock.mock_swis import create_app
        from swcollect.steps import curate_step, discover_step
        from swcollect.swis.client import SwisClient

        from .flask_session import FlaskSession
        cls.app = create_app(schema_all=True)
        cls.net = Network(name="it", host=f"it-{uuid.uuid4().hex[:8]}", port=17774, scheme="http",
                          username="readonly", password="mock", network_id="it")
        cls.client = SwisClient(cls.net, "mock", session=FlaskSession(cls.app), capture=False)
        cls.conn = psycopg.connect(DSN, autocommit=True)
        cls.inst = discover_step(cls.client, cls.net, None, cls.conn)["instance_id"]
        inventory.run(cls.conn, cls.client, cls.net, 5000, log=lambda *a: None)
        curate_step(cls.conn, cls.client, cls.net)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def metrics(self, **kw):
        from swcollect.jobs import metrics
        return metrics.run(self.conn, self.client, self.net, 5000, POLICY, log=lambda *a: None, **kw)

    def admin(self, body):
        return self.app.test_client().post("/mock/admin/metrics", json=body, auth=("readonly", "mock")).get_json()

    def test_backfill_then_incremental(self):
        # 1. backfill: everything Orion holds, coverage exact, no duplicates
        t1 = self.metrics()
        self.assertEqual(set(t1["status"]), {"ok"})
        self.assertEqual(t1["inserted"], t1["rows"])
        swis = self.client.query("SELECT COUNT(*) AS N FROM Orion.CPULoad").rows[0]["N"]
        stored = self.q("SELECT count(*) FROM sw_ts.orion_cpuload WHERE instance_id = %s", self.inst)[0][0]
        self.assertEqual(stored, swis)
        self.assertEqual(self.q('SELECT count(*) - count(DISTINCT ("NodeID", "DateTime")) FROM sw_ts.orion_cpuload '
                                'WHERE instance_id = %s', self.inst)[0][0], 0)
        wm1 = dict(self.q("SELECT entity, value FROM sw_meta.sync_state WHERE instance_id = %s", self.inst))
        self.assertIn("Orion.CPULoad", wm1)
        cad = self.q("SELECT metrics_cadence FROM sw_meta.instance WHERE instance_id = %s", self.inst)[0][0]
        self.assertEqual((cad["every_minutes"], cad["overlap_minutes"]), (10, 20))

        # 2. Orion recomputes 2 stored samples, then 30 minutes pass
        re = self.admin({"reaggregate": {"entity": "Orion.CPULoad", "field": "AvgLoad", "rows": 2}})["reaggregated"]
        added = self.admin({"advance_minutes": 30})["added"]
        t2 = self.metrics()
        self.assertEqual(set(t2["status"]), {"ok"})
        self.assertEqual(t2["inserted"], sum(added.values()))
        self.assertEqual(t2["updated"], 2)
        ch = self.q("SELECT row_key, fields FROM sw_meta.change_log c JOIN sw_meta.sync_run r USING (run_id) "
                    "WHERE r.instance_id = %s AND c.entity = 'Orion.CPULoad' AND change = 'update'", self.inst)
        self.assertEqual(sorted(k["NodeID"] for k, _ in ch), sorted(r["NodeID"] for r in re))
        for _k, f in ch:
            self.assertEqual(f["AvgLoad"][1] - f["AvgLoad"][0], 1)        # old and new value both kept
        wm2 = dict(self.q("SELECT entity, value FROM sw_meta.sync_state WHERE instance_id = %s", self.inst))
        self.assertGreater(wm2["Orion.CPULoad"], wm1["Orion.CPULoad"])

        # 3. nothing new at the source: the overlap is re-read, nothing is written
        t3 = self.metrics()
        self.assertEqual((t3["inserted"], t3["updated"]), (0, 0))
        self.assertGreater(t3["rows"], 0)

        # 4. the sink rejects a batch: status error, watermark unchanged, the next run fills the gap
        self.admin({"advance_minutes": 20})
        import psycopg
        from swcollect.jobs import metrics as m
        with mock.patch.object(m, "_store", side_effect=psycopg.OperationalError("simulated outage")):
            t4 = self.metrics(only=["Orion.CPULoad"])
        self.assertEqual(t4["status"], {"error": 1})
        wm4 = dict(self.q("SELECT entity, value FROM sw_meta.sync_state WHERE instance_id = %s", self.inst))
        self.assertEqual(wm4["Orion.CPULoad"], wm2["Orion.CPULoad"])
        t5 = self.metrics(only=["Orion.CPULoad"])
        self.assertEqual(t5["status"], {"ok": 1})
        self.assertGreater(t5["inserted"], 0)
        swis = self.client.query("SELECT COUNT(*) AS N FROM Orion.CPULoad").rows[0]["N"]
        stored = self.q("SELECT count(*) FROM sw_ts.orion_cpuload WHERE instance_id = %s", self.inst)[0][0]
        self.assertEqual(stored, swis)

        # 5. policies and the join to sw_core
        comp = self.q("SELECT compression_enabled FROM timescaledb_information.hypertables WHERE "
                      "hypertable_schema = 'sw_ts' AND hypertable_name = 'orion_cpuload'")[0][0]
        self.assertTrue(comp)
        v = self.q("SELECT count(*), count(device_id) FROM sw_ts.v_orion_cpuload WHERE instance_id = %s", self.inst)[0]
        self.assertEqual(v[0], v[1])                                       # every sample has its device


if __name__ == "__main__":
    unittest.main()

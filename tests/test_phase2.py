"""Phase 2: inventory into sw_raw, sw_rel views, flows into sw_core.

Unit tests run anywhere. The integration test needs PostgreSQL + TimescaleDB (deploy/docker-compose.dev.yml) and
runs only with SWCOLLECT_IT=1 (DSN from SWCOLLECT_PG_DSN, default the dev port 15432). It uses its own instance key,
so it does not touch other instances in the same database.
"""
import os
import unittest
import uuid

from swcollect.config import Network
from swcollect.core import load_core, topo_order
from swcollect.core_rules import normalize_interface_name
from swcollect.jobs.inventory import row_hash, to_text
from swcollect.jobs.retry import retry
from swcollect.rel_views import view_name
from swcollect.swis.client import SwisError

IT = os.environ.get("SWCOLLECT_IT") == "1"
DSN = os.environ.get("SWCOLLECT_PG_DSN", "postgresql://swcollect:swcollect@127.0.0.1:15432/swcollect")


class ValuesTest(unittest.TestCase):
    def test_null_stays_null(self):
        for t in ("integer", "text", "timestamp", "boolean", "jsonb"):
            self.assertEqual(to_text(None, t), (None, True))

    def test_fits(self):
        self.assertEqual(to_text(5, "integer"), ("5", True))
        self.assertEqual(to_text(True, "boolean"), ("true", True))
        self.assertEqual(to_text("2026-11-18T11:17:33.246", "timestamp"), ("2026-11-18T11:17:33.246", True))
        self.assertEqual(to_text("2026-11-18T09:17:33Z", "timestamp")[1], True)        # as received
        self.assertEqual(to_text("", "text"), ("", True))                               # "" stays ""
        self.assertEqual(to_text(["a", "b"], "jsonb"), ('["a", "b"]', True))

    def test_misfit_is_flagged_not_coerced(self):
        self.assertEqual(to_text("", "integer"), (None, False))
        self.assertEqual(to_text("yesterday", "timestamp"), (None, False))
        self.assertEqual(to_text(1.5, "integer"), (None, False))
        self.assertEqual(to_text("not-a-guid", "uuid"), (None, False))

    def test_row_hash_stable(self):
        self.assertEqual(row_hash({"a": 1, "b": 2}), row_hash({"b": 2, "a": 1}))
        self.assertNotEqual(row_hash({"a": 1}), row_hash({"a": 2}))


class RetryTest(unittest.TestCase):
    def test_connection_errors_retry_4xx_do_not(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise SwisError("Connection failed")
            return "ok"
        self.assertEqual(retry(flaky, sleep=lambda s: None), "ok")
        self.assertEqual(len(calls), 3)
        calls.clear()

        def denied():
            calls.append(1)
            raise SwisError("Access denied", 403)
        with self.assertRaises(SwisError):
            retry(denied, sleep=lambda s: None)
        self.assertEqual(len(calls), 1)


class CoreModelTest(unittest.TestCase):
    def test_parents_first(self):
        order = [s["table"] for s in topo_order(load_core())]
        for child, parent in (("interface", "device"), ("link", "interface"), ("config_version", "ncm_node"),
                              ("ncm_node", "device"), ("ip_address", "subnet"),
                              ("custom_poller_assignment", "custom_poller")):
            self.assertLess(order.index(parent), order.index(child), child)

    def test_cycle_is_refused(self):
        with self.assertRaises(ValueError):
            topo_order([{"table": "a", "refs": [{"to": "b"}]}, {"table": "b", "refs": [{"to": "a"}]}])

    def test_every_ref_points_to_a_core_table(self):
        tables = {s["table"] for s in load_core()}
        for s in load_core():
            for r in s.get("refs") or []:
                self.assertIn(r["to"], tables, s["table"])
            for d in s.get("derived") or []:
                self.assertIn(d["from"], s["fields"], s["table"])

    def test_view_names(self):
        self.assertEqual(view_name("orion_nodes", "Interfaces"), "orion_nodes__interfaces")
        self.assertEqual(view_name("orion_nodes", "NodeID:Nodes"), "orion_nodes__nodeid_nodes")
        self.assertLessEqual(len(view_name("x" * 60, "y" * 30)), 63)


@unittest.skipUnless(IT, "integration: set SWCOLLECT_IT=1 with PostgreSQL + TimescaleDB running")
class InventoryCurateIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        from swcollect.mock.mock_swis import create_app
        from swcollect.steps import discover_step
        from swcollect.swis.client import SwisClient

        from .flask_session import FlaskSession
        cls.app = create_app(schema_all=True)
        cls.net = Network(name="it", host=f"it-{uuid.uuid4().hex[:8]}", port=17774, scheme="http",
                          username="readonly", password="mock", network_id="it")
        cls.client = SwisClient(cls.net, "mock", session=FlaskSession(cls.app), capture=False)
        cls.conn = psycopg.connect(DSN, autocommit=True)
        cls.disc = discover_step(cls.client, cls.net, None, cls.conn)
        cls.inst = cls.disc["instance_id"]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def inventory(self):
        from swcollect.jobs import inventory
        return inventory.run(self.conn, self.client, self.net, 5000, log=lambda *a: None)

    def admin(self, body):
        return self.app.test_client().post("/mock/admin/inventory", json=body, auth=("readonly", "mock")).get_json()

    def test_full_cycle(self):
        from swcollect.steps import curate_step
        self.assertEqual(self.disc["errors"], {})
        # 1. first load: coverage SWIS COUNT = received = stored for every pulled entity
        t1 = self.inventory()
        self.assertEqual(set(t1["status"]), {"ok", "empty"})
        bad = self.q("SELECT entity FROM sw_meta.coverage WHERE run_id = %s AND status = 'ok' AND "
                     "NOT (swis_count = received AND stored = received - quarantined)", t1["run_id"])
        self.assertEqual(bad, [])
        none = self.q("SELECT count(*) FROM sw_meta.entity_catalog e WHERE instance_id = %s AND NOT EXISTS "
                      "(SELECT 1 FROM sw_meta.coverage c WHERE c.run_id = %s AND c.entity = e.entity)", self.inst,
                      t1["run_id"])[0][0]
        self.assertEqual(none, 0, "every entity has a coverage row in the run (nothing skipped silently)")
        raw = self.q('SELECT _raw FROM sw_raw.orion_nodes WHERE instance_id = %s AND "NodeID" = 3', self.inst)[0][0]
        self.assertEqual(raw["NodeID"], 3)                                  # the row exactly as received

        # 2. unchanged source → no changes
        t2 = self.inventory()
        self.assertEqual(self.q("SELECT count(*) FROM sw_meta.change_log WHERE run_id = %s", t2["run_id"])[0][0], 0)

        # 3. curate: views, flows balanced, the planted orphan link in the dead-letter queue
        c1 = curate_step(self.conn, self.client, self.net)
        self.assertEqual(c1["unbalanced"], [])
        self.assertGreater(c1["views"]["views"], 20)
        dl = self.q("SELECT rule, field, value FROM sw_core.dead_letter WHERE instance_id = %s AND core_table = 'link' "
                    "AND resolved_at IS NULL", self.inst)
        self.assertIn(("ref", "DestInterfaceID", "99999"), dl)
        st = self.q("SELECT stored_status FROM sw_meta.relationship WHERE instance_id = %s AND "
                    "source_entity = 'Orion.Nodes' AND name = 'Interfaces'", self.inst)[0][0]
        self.assertIn(st, ("match", "match_with_orphans"))
        fk = self.q("SELECT count(*) FROM pg_constraint WHERE conname = 'interface_device_id_fkey'")[0][0]
        self.assertEqual(fk, 1)
        orphans = self.q('SELECT count(*) FROM sw_core.interface i WHERE i.instance_id = %s AND i.device_id IS NULL',
                         self.inst)[0][0]
        self.assertEqual(orphans, 0)
        ba = self.q("SELECT raw_row, record FROM sw_core.v_device_before_after WHERE instance_id = %s LIMIT 1", self.inst)
        self.assertIn("NodeID", ba[0][0])
        self.assertIn("stable_id", ba[0][1])

        # 4. source changes: add a node, change one, remove one, hide an entity
        self.admin({"add_node": True, "change_node": 3, "remove_node": 5, "hide": ["Orion.NodeVlans"]})
        t3 = self.inventory()
        ch = {(c, k["NodeID"]): f for c, k, f in self.q(
            "SELECT change, row_key, fields FROM sw_meta.change_log WHERE run_id = %s AND entity = 'Orion.Nodes'",
            t3["run_id"])}
        self.assertIn(("insert", 41), ch)
        self.assertIn(("soft_delete", 5), ch)
        self.assertEqual(ch[("update", 3)]["Caption"][1], "P-CORE-03-CHANGED")
        vl = self.q("SELECT status, stored FROM sw_meta.coverage WHERE run_id = %s AND entity = 'Orion.NodeVlans'",
                    t3["run_id"])[0]
        self.assertEqual(vl[0], "empty")
        self.assertGreater(vl[1], 0, "an empty result never deletes")

        # 5. curate again: the removed node's children go to the dead-letter queue, nothing is lost
        c2 = curate_step(self.conn, self.client, self.net)
        self.assertEqual(c2["unbalanced"], [])
        gone = self.q("SELECT deleted_at IS NOT NULL FROM sw_core.device WHERE instance_id = %s AND \"NodeID\" = 5",
                      self.inst)[0][0]
        self.assertTrue(gone)
        dlq_if = self.q("SELECT count(*) FROM sw_core.dead_letter WHERE instance_id = %s AND core_table = 'interface' "
                        "AND rule = 'ref' AND resolved_at IS NULL", self.inst)[0][0]
        raw_if = self.q('SELECT count(*) FROM sw_raw.orion_npm_interfaces WHERE instance_id = %s AND "NodeID" = 5 '
                        "AND _deleted_at IS NULL", self.inst)[0][0]
        self.assertEqual(dlq_if, raw_if)

        # 6. the node comes back: its children leave the dead-letter queue (resolved, not deleted)
        self.admin({"unhide": ["Orion.NodeVlans"], "restore_node": 5})
        self.inventory()
        curate_step(self.conn, self.client, self.net)
        open_if = self.q("SELECT count(*) FROM sw_core.dead_letter WHERE instance_id = %s AND core_table = 'interface' "
                         "AND resolved_at IS NULL", self.inst)[0][0]
        resolved = self.q("SELECT count(*) FROM sw_core.dead_letter WHERE instance_id = %s AND core_table = 'interface' "
                          "AND resolved_at IS NOT NULL", self.inst)[0][0]
        self.assertEqual(open_if, 0)
        self.assertEqual(resolved, dlq_if)

    def test_interface_name_sql_matches_python(self):
        from swcollect.ddl import interface_name_function
        self.conn.execute(interface_name_function())
        for name in ("Gi0/0/1", "GigabitEthernet0/0/1", "te1/1", "Hu0/0/0/0", "xe-0/0/1", "Loopback0", " po12 ",
                     "Bundle-Ether10", "mgmt", None):
            got = self.conn.execute("SELECT sw_core.normalize_interface_name(%s)", (name,)).fetchone()[0]
            self.assertEqual(got, normalize_interface_name(name), name)


if __name__ == "__main__":
    unittest.main()

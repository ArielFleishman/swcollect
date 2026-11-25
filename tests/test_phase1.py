"""Phase 1: discovery, classification, identity, DDL generation, relationships and their proof (against the mock)."""
import unittest

from swcollect.config import Network
from swcollect.ddl import column_name, entity_ddl, pg_type, raw_table_ddl, table_name, ts_table_ddl
from swcollect.discover import classify, discover, load_rules
from swcollect.mock.mock_swis import create_app
from swcollect.relationships import Relationship, candidates, discover_relationships, join_options, prove
from swcollect.swis.client import SwisClient

from .flask_session import FlaskSession

RULES = load_rules()
NET = Network(name="mock", host="mock", port=17774, scheme="http", username="readonly", password="mock")


def mock_client(schema_all=True) -> SwisClient:
    return SwisClient(NET, "mock", session=FlaskSession(create_app(schema_all=schema_all)), capture=False)


class TypesAndNamesTest(unittest.TestCase):
    def test_type_map(self):
        self.assertEqual(pg_type("System.Int32"), ("integer", True))
        self.assertEqual(pg_type("System.DateTime"), ("timestamp", True))     # as received, no zone
        self.assertEqual(pg_type("System.Guid"), ("uuid", True))
        self.assertEqual(pg_type("System.Weird"), ("text", False))            # unknown → text, needs proof

    def test_names(self):
        self.assertEqual(table_name("Orion.NPM.Interfaces"), "orion_npm_interfaces")
        long = "Orion." + "X" * 80
        self.assertLessEqual(len(table_name(long)), 63)
        self.assertNotEqual(table_name(long), table_name(long + "Y"))
        self.assertEqual(column_name("NodeID"), "NodeID")                     # verbatim


class ClassifyTest(unittest.TestCase):
    def test_rules(self):
        f = {"NodeID": "System.Int32", "DateTime": "System.DateTime", "Archive": "System.Byte"}
        self.assertEqual(classify("Orion.CPULoad", {}, f, RULES)[0], "metrics")
        self.assertEqual(classify("Orion.Events", {}, {"EventID": "System.Int32"}, RULES)[0], "events")
        self.assertEqual(classify("Orion.Nodes", {}, {"NodeID": "System.Int32"}, RULES)[0], "inventory")
        self.assertEqual(classify("X.Base", {"IsAbstract": True}, {"A": "System.Int32"}, RULES)[0], "excluded")
        self.assertEqual(classify("Metadata.Entity", {}, {"A": "System.String"}, RULES)[0], "excluded")
        cls, why, _ = classify("Orion.Netflow.Flows", {}, {"A": "System.String"}, RULES)
        self.assertEqual(cls, "excluded")
        self.assertIn("needs proof", why)
        # A DateTime alone does not make a time series (e.g. LastSync on an inventory entity).
        self.assertEqual(classify("Orion.Thing", {}, {"ID": "System.Int32", "DateTime": "System.DateTime"},
                                  RULES)[0], "inventory")


class DiscoverMockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = mock_client()
        cls.catalog = discover(cls.client, NET, RULES)
        cls.E = cls.catalog["entities"]
        cls.rels = {(r.source, r.name): r for r in discover_relationships(cls.client, cls.catalog, RULES)}

    def test_every_entity_has_a_status(self):
        names = {r["FullName"] for r in self.client.query("SELECT e.FullName FROM Metadata.Entity e").rows}
        self.assertEqual(set(self.E), names)
        for e in self.E.values():
            self.assertTrue(e["reason"], e["entity"])
            if e["collect"]:
                self.assertIn(e["class"], ("inventory", "metrics", "events"))
                self.assertTrue(e["key"] or e["key_status"] == "row_hash", e["entity"])
            else:
                self.assertEqual(e["class"], "excluded")
        self.assertGreater(self.catalog["summary"]["collected"], 200)       # all doc pages, not only checks.yaml

    def test_classes_and_identity(self):
        self.assertEqual((self.E["Orion.Nodes"]["key"], self.E["Orion.Nodes"]["key_status"]), (["NodeID"], "metadata"))
        cpu = self.E["Orion.CPULoad"]
        self.assertEqual((cpu["class"], cpu["sinks"], cpu["key"]), ("metrics", ["ts"], ["NodeID", "DateTime"]))
        self.assertEqual(cpu["key_status"], "sample_unique")
        ev = self.E["Orion.Events"]
        self.assertEqual((ev["class"], ev["sinks"], ev["watermark"]), ("events", ["loki"], {"kind": "id", "field": "EventID"}))
        ncm = self.E["NCM.ConfigArchive"]
        self.assertEqual(ncm["sinks"], ["raw", "loki"])
        self.assertEqual(ncm["watermark"]["kind"], "time")
        self.assertFalse(self.E["Orion.AlertTriggered"]["collect"])                       # indication
        self.assertFalse(self.E["Orion.Netflow.Flows"]["collect"])                         # named exclusion

    def test_inherited_and_binary_fields(self):
        nodes = {c["name"] for c in self.E["Orion.Nodes"]["columns"]}
        self.assertIn("InstanceSiteId", nodes)                                             # from System.Entity
        events = self.E["Orion.Events"]
        self.assertIn("TimeStamp", events["excluded_fields"])                              # Byte[] rowversion
        self.assertNotIn("TimeStamp", {c["name"] for c in events["columns"]})

    def test_doc_check(self):
        dc = self.E["Orion.Nodes"]["doc_check"]
        self.assertEqual(dc["missing_on_server"], [])
        self.assertTrue(self.E["Orion.Nodes"]["doc"].endswith("/Orion.Nodes.html"))

    def test_navigation_relationships_proven(self):
        for key, cols in {("Orion.Nodes", "Interfaces"): ["NodeID"], ("Orion.NPM.Interfaces", "Node"): ["NodeID"],
                          ("Orion.Nodes", "IPAddresses"): ["NodeID"],
                          ("NCM.ConfigArchive", "Node"): ["NodeID"],
                          ("NCM.Nodes", "OrionNode"): ["CoreNodeID"]}.items():
            r = self.rels[key]
            self.assertEqual((r.status, r.method, r.source_columns), ("proven", "navigation", cols), r.detail)

    def test_wrong_inference_is_rejected_by_proof(self):
        # Orion.NodeIPAddresses' key is (IPAddress, NodeID); both are on Orion.Nodes too, so that set is a candidate.
        # Navigation proves only NodeID, and the larger set is never used.
        r = self.rels[("Orion.Nodes", "IPAddresses")]
        self.assertEqual(r.source_columns, ["NodeID"])

    def test_override_proven_by_values_and_folded(self):
        r = self.rels[("Orion.TopologyConnections", "SrcNode")]
        self.assertEqual((r.status, r.method, r.target_columns), ("proven", "values", ["NodeID"]))
        self.assertNotIn(("NCM.Nodes", "CoreNode"), self.rels)                             # folded into OrionNode
        self.assertIn("override", self.rels[("NCM.Nodes", "OrionNode")].basis)

    def test_unproven_links_are_kept_with_a_reason(self):
        statuses = {r.status for r in self.rels.values()}
        self.assertIn("mismatch", statuses)
        for r in self.rels.values():
            self.assertTrue(r.detail, (r.source, r.name))
            if r.status != "not_proven" or r.source_columns:
                self.assertTrue(r.proof_swql, (r.source, r.name))

    def test_ddl_for_every_collected_table(self):
        for e in self.E.values():
            stmts = entity_ddl(e)
            if e["collect"] and {"raw", "ts"} & set(e["sinks"]):
                self.assertTrue(stmts[0].startswith("CREATE TABLE IF NOT EXISTS"), e["entity"])
            else:
                self.assertEqual(stmts, [], e["entity"])
        raw = "\n".join(raw_table_ddl(self.E["Orion.Nodes"]))
        self.assertIn('PRIMARY KEY (instance_id, "NodeID")', raw)
        self.assertIn("_raw jsonb NOT NULL", raw)                                        # the row as received
        self.assertNotIn("FOREIGN KEY", raw)                                               # never in sw_raw
        ts = "\n".join(ts_table_ddl(self.E["Orion.CPULoad"]))
        self.assertIn("create_hypertable('sw_ts.\"orion_cpuload\"', by_range('DateTime')", ts)
        self.assertIn('UNIQUE (instance_id, "NodeID", "DateTime")', ts)


class JoinInferenceTest(unittest.TestCase):
    def entry(self, key, cols):
        return {"key": key, "key_status": "metadata", "time_field": None,
                "columns": [{"name": c, "pg_type": "integer"} for c in cols]}

    def test_both_directions_smallest_first(self):
        nodes = self.entry(["NodeID"], ["NodeID", "IPAddress"])
        ips = self.entry(["NodeID", "IPAddress"], ["NodeID", "IPAddress"])
        self.assertEqual(join_options(nodes, ips), [(["NodeID"], ["NodeID"]), (["NodeID", "IPAddress"], ["NodeID", "IPAddress"])])

    def test_type_mismatch_is_no_join(self):
        a = self.entry(["NodeID"], ["NodeID"])
        b = {"key": ["NodeID"], "key_status": "metadata", "time_field": None,
             "columns": [{"name": "NodeID", "pg_type": "uuid"}]}
        self.assertEqual(join_options(a, b), [])

    def test_no_columns_not_proven(self):
        r = prove(mock_client(False), Relationship("A", "b", "C", "navigation"), {}, RULES)
        self.assertEqual(r.status, "not_proven")
        self.assertIn("override", r.detail)

    def test_override_without_basis_is_ignored(self):
        entries = {n: {"collect": True, "class": "inventory", "columns": [], "navigation": {}, "key": [],
                       "key_status": "row_hash"} for n in ("A", "B")}
        rules = {"relationships": [{"source": "A", "target": "B", "columns": [["x", "y"]]}], "name_match": {"enabled": False}}
        self.assertEqual(candidates(entries, [], rules), [])


if __name__ == "__main__":
    unittest.main()

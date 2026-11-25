"""Grafana dashboards: the generated files are current and well-formed, and (with SWCOLLECT_IT=1) every panel query
runs against a loaded database and Loki.

The live test needs a database that holds a collected network with metrics, for example after
`swcollect mock --rich` and `swcollect run --network <it> --once all`. SWCOLLECT_PG_DSN and SWCOLLECT_LOKI_URL
point at it; SWCOLLECT_DASH_INSTANCE picks the instance (default: the one with the most samples).
"""
import json
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from swcollect import dashboards

ROOT = Path(__file__).resolve().parents[1]
PROV = ROOT / "deploy" / "grafana" / "provisioning"


class Static(unittest.TestCase):
    def test_files_are_current(self):
        built = dashboards.build()
        on_disk = {p.name for p in dashboards.OUT_DIR.glob("*.json")}
        self.assertEqual(set(built), on_disk, "run `swcollect dashboards`")
        for name, board in built.items():
            disk = json.loads((dashboards.OUT_DIR / name).read_text(encoding="utf-8"))
            self.assertEqual(disk, json.loads(json.dumps(board)), f"{name} is stale: run `swcollect dashboards`")

    def test_datasources_match_provisioning(self):
        prov = yaml.safe_load((PROV / "datasources" / "swcollect.yaml").read_text(encoding="utf-8"))
        uids = {d["uid"]: d["type"] for d in prov["datasources"]}
        for name, board in dashboards.build().items():
            for title, uid, query in dashboards.panel_queries(board):
                self.assertIn(uid, uids, f"{name} / {title}")
                self.assertTrue(query, f"{name} / {title}: empty query")

    def test_unique_ids_and_grid(self):
        for name, board in dashboards.build().items():
            ids = [p["id"] for p in board["panels"]]
            self.assertEqual(len(ids), len(set(ids)), name)
            for p in board["panels"]:
                g = p["gridPos"]
                self.assertLessEqual(g["x"] + g["w"], 24, f"{name} / {p['title']}")

    def test_no_external_urls(self):
        """The closed network has no internet: no link, image or script from outside, except documentation
        references in descriptions."""
        for name, board in dashboards.build().items():
            text = json.dumps(board)
            for url in re.findall(r"https?://[^\"' )]+", text):
                self.fail(f"{name}: external URL {url}")

    def test_sql_reads_only_swcollect_schemas(self):
        for name, board in dashboards.build().items():
            for title, uid, query in dashboards.panel_queries(board):
                if uid != "swcollect-pg":
                    continue
                self.assertNotRegex(query.upper(), r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT)\b",
                                    f"{name} / {title}")
                for schema in re.findall(r"\b(\w+)\.\"?\w+", query):
                    if schema in ("m", "n", "d", "c", "a", "p", "l", "r", "s", "f", "v", "o", "h", "i", "t", "x", "y",
                                  "z", "nn", "si", "di", "sm", "rp"):
                        continue
                    self.assertIn(schema, ("sw_meta", "sw_raw", "sw_core", "sw_ts", "sw_rel"), f"{name} / {title}")

    def test_no_dual_axis(self):
        for name, board in dashboards.build().items():
            for p in board["panels"]:
                for o in p.get("fieldConfig", {}).get("overrides", []):
                    for prop in o["properties"]:
                        self.assertNotEqual(prop["id"], "custom.axisPlacement", f"{name} / {p['title']}")


def substitute(query: str, inst: int, device: int, iface: int, key: str, frm: datetime, to: datetime) -> str:
    f, t = frm.strftime("%Y-%m-%dT%H:%M:%SZ"), to.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = query.replace("$__timeFrom()", f"'{f}'").replace("$__timeTo()", f"'{t}'")
    query = re.sub(r"\$__timeFilter\(([^)]+)\)", rf"\1 BETWEEN '{f}' AND '{t}'", query)
    query = query.replace("${instance:text}", key).replace("$__auto", "10m").replace("$search", "")
    return query.replace("$instance", str(inst)).replace("$device", str(device)).replace("$interface", str(iface))


@unittest.skipUnless(os.environ.get("SWCOLLECT_IT") == "1", "set SWCOLLECT_IT=1 (needs a loaded PostgreSQL + Loki)")
class Live(unittest.TestCase):
    # Panels that must return rows on the rich mock (others may legitimately be empty, e.g. no flaps).
    MUST_HAVE_ROWS = {"Devices", "Interfaces", "CPU load (%)", "Traffic (bits per second)", "Busiest interfaces",
                      "Physical topology (sw_core.link)", "Hardware components", "Jobs: latest run",
                      "Coverage of every entity", "Entries per interval, by kind", "Orion events"}

    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.conn = psycopg.connect(os.environ.get("SWCOLLECT_PG_DSN",
                                                  "postgresql://swcollect:swcollect@127.0.0.1:15432/swcollect"),
                                   autocommit=True)
        cls.loki = os.environ.get("SWCOLLECT_LOKI_URL", "http://127.0.0.1:13100")
        inst = os.environ.get("SWCOLLECT_DASH_INSTANCE")
        if inst:
            row = cls.conn.execute("SELECT instance_id, instance_key FROM sw_meta.instance WHERE instance_key = %s",
                                   (inst,)).fetchone()
        else:
            row = cls.conn.execute("SELECT i.instance_id, i.instance_key FROM sw_meta.instance i JOIN "
                                   "sw_ts.orion_npm_interfacetraffic t USING (instance_id) GROUP BY 1, 2 "
                                   "ORDER BY count(*) DESC LIMIT 1").fetchone()
        if not row:
            raise unittest.SkipTest("no instance with interface samples in this database")
        cls.inst, cls.key = row
        cls.iface, cls.device = cls.conn.execute(
            'SELECT "InterfaceID", "NodeID" FROM sw_ts.orion_npm_interfacetraffic WHERE instance_id = %s '
            'GROUP BY 1, 2 ORDER BY count(*) DESC LIMIT 1', (cls.inst,)).fetchone()
        newest = cls.conn.execute("SELECT max(started_at) FROM sw_meta.sync_run WHERE instance_id = %s",
                                  (cls.inst,)).fetchone()[0] or datetime.now(timezone.utc)
        cls.to = newest.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)
        cls.frm = cls.to - timedelta(days=3)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_every_query_runs(self):
        import requests
        empty, failures = [], []
        for name, board in dashboards.build().items():
            for title, uid, query in dashboards.panel_queries(board):
                q = substitute(query, self.inst, self.device, self.iface, self.key, self.frm, self.to)
                try:
                    if uid == "swcollect-pg":
                        rows = self.conn.execute(q).fetchall()
                        n = len(rows)
                    else:
                        kind = "query_range" if "count_over_time" in q or "{" in q else "query"
                        r = requests.get(f"{self.loki}/loki/api/v1/{kind}", timeout=30, params={
                            "query": q, "limit": 50, "start": f"{self.frm.timestamp() * 1e9:.0f}",
                            "end": f"{self.to.timestamp() * 1e9:.0f}", "step": "600"})
                        self.assertEqual(r.status_code, 200, r.text[:300])
                        n = sum(len(s.get("values", [])) for s in r.json()["data"]["result"])
                except Exception as e:                            # report every failing panel, not just the first
                    failures.append(f"{name} / {title}: {str(e).splitlines()[0]}")
                    continue
                if not n:
                    empty.append(f"{name} / {title}")
        self.assertEqual(failures, [], "\n".join(failures))
        missing = [e for e in empty if e.split(" / ", 1)[1] in self.MUST_HAVE_ROWS]
        self.assertEqual(missing, [], f"panels without rows: {missing}; all empty panels: {empty}")


if __name__ == "__main__":
    unittest.main()

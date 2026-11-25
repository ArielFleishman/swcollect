"""Phase 0: config (read-only profiles only), the copied mock answers SWQL, and the readiness check reports failures."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swcollect import config as cfg
from swcollect.check import check_loki, check_postgres
from swcollect.config import Network, Settings
from swcollect.mock.mock_swis import create_app
from swcollect.swis.client import QUERY_PATH

ROOT = Path(__file__).resolve().parent.parent


def _load(text: str) -> Settings:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.yaml"
        p.write_text(text, encoding="utf-8")
        return cfg.load(p)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("SWIS_URL", "SWCOLLECT_PG_DSN", "SWCOLLECT_LOKI_URL", "SWCOLLECT_HOOK_TOKEN"):
            os.environ.pop(k, None)
        cfg._from_file.clear()
        env_patch = mock.patch.object(cfg, "ENV_FILE", ROOT / "does-not-exist.env")
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_example_config_loads(self):
        s = cfg.load(ROOT / "config.example.yaml")
        net = s.network("mock")
        self.assertEqual(net.url, "http://127.0.0.1:17774/SolarWinds/InformationService/v3/Json")
        self.assertEqual(net.instance_key, "mock/127.0.0.1:17774")
        self.assertTrue(s.pg_dsn.startswith("postgresql://"))

    def test_actions_profile_is_refused(self):
        s = _load("networks:\n  - {name: act, host: h, actions_enabled: true}\n")
        with self.assertRaises(ValueError):
            s.network("act")

    def test_unknown_profile(self):
        with self.assertRaises(KeyError):
            _load("networks: []\n").network("nope")

    def test_env_overrides_sinks_and_adds_profile(self):
        os.environ.update({"SWIS_URL": "orion.x:17778", "SWIS_NETWORK": "p1", "SWIS_USERNAME": "ro",
                           "SWCOLLECT_PG_DSN": "postgresql://a@b/c", "SWCOLLECT_LOKI_URL": "http://loki:3100/"})
        s = _load("networks: []\n")
        net = s.network("p1")
        self.assertEqual((net.scheme, net.host, net.port, net.username), ("https", "orion.x", 17778, "ro"))
        self.assertEqual(s.pg_dsn, "postgresql://a@b/c")
        self.assertEqual(s.loki_url, "http://loki:3100")

    def test_stable_network_id_defaults_to_name(self):
        self.assertEqual(Network(name="n1", host="h", port=1).instance_key, "n1/h:1")
        self.assertEqual(Network(name="n1", host="h", port=1, network_id="isp12").instance_key, "isp12/h:1")


class MockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = create_app().test_client()

    def q(self, swql, params=None, user="readonly"):
        return self.http.post(QUERY_PATH, json={"query": swql, "parameters": params or {}},
                              auth=(user, "mock"))

    def test_metadata_entity(self):
        r = self.q("SELECT FullName, IsAbstract FROM Metadata.Entity")
        self.assertEqual(r.status_code, 200)
        names = {x["FullName"] for x in r.get_json()["results"]}
        self.assertIn("Orion.Nodes", names)
        self.assertIn("Orion.Events", names)

    def test_incremental_filter_and_paging(self):
        rows = self.q("SELECT EventID FROM Orion.Events WHERE EventID > @last ORDER BY EventID WITH ROWS 1 TO 3",
                      {"last": 0}).get_json()["results"]
        self.assertLessEqual(len(rows), 3)
        ids = [r["EventID"] for r in rows]
        self.assertEqual(ids, sorted(ids))

    def test_bad_password(self):
        r = self.http.post(QUERY_PATH, json={"query": "SELECT NodeID FROM Orion.Nodes"}, auth=("readonly", "x"))
        self.assertEqual(r.status_code, 401)


class CheckTest(unittest.TestCase):
    def test_unreachable_sinks_are_reported_not_raised(self):
        s = Settings(pg_dsn="postgresql://x:y@127.0.0.1:1/z?connect_timeout=1", loki_url="http://127.0.0.1:1")
        results = check_postgres(s) + check_loki(s)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not r["ok"] for r in results))


class DeployFilesTest(unittest.TestCase):
    def test_compose_network_is_internal_and_pinned(self):
        import yaml
        c = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))
        self.assertTrue(c["networks"]["swnet"]["internal"])
        for name, svc in c["services"].items():
            self.assertEqual(svc.get("networks"), ["swnet"], name)
            image = svc["image"]
            self.assertNotIn(":latest", image, name)
            self.assertIn(":", image, name)
            self.assertNotIn("ports", svc, f"{name}: the closed-network compose file publishes no ports")

    def test_no_telemetry(self):
        loki = (ROOT / "deploy" / "loki-config.yaml").read_text(encoding="utf-8")
        self.assertIn("reporting_enabled: false", loki)
        compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("timescaledb.telemetry_level=off", compose)


if __name__ == "__main__":
    unittest.main()

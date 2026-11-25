"""Phase 4: events into Loki, the push receiver, push/poll reconciliation.

Unit tests run anywhere. The integration test needs PostgreSQL + TimescaleDB and Loki (deploy/docker-compose.dev.yml)
and runs only with SWCOLLECT_IT=1 (SWCOLLECT_PG_DSN / SWCOLLECT_LOKI_URL, default the dev ports 15432 / 13100).
"""
import json
import os
import time
import unittest
import uuid

import psycopg

from swcollect.config import Network, Settings
from swcollect.discover import load_rules
from swcollect.jobs.events import to_epoch_ns
from swcollect.reconcile import key_of
from swcollect.sinks.loki import Loki, LokiError
from swcollect.webhook import create_app as hook_app

IT = os.environ.get("SWCOLLECT_IT") == "1"
DSN = os.environ.get("SWCOLLECT_PG_DSN", "postgresql://swcollect:swcollect@127.0.0.1:15432/swcollect")
LOKI = os.environ.get("SWCOLLECT_LOKI_URL", "http://127.0.0.1:13100")
RULES = load_rules()


class TimeRuleTest(unittest.TestCase):
    def test_rules(self):
        utc = to_epoch_ns("2026-11-18T10:00:00", "utc", None)[0]
        self.assertEqual(to_epoch_ns("2026-11-18T12:00:00", "server_local", 120)[0], utc)
        self.assertEqual(to_epoch_ns("2026-11-18T10:00:00Z", "server_local", 120)[0], utc)    # explicit zone wins
        self.assertEqual(to_epoch_ns("2026-11-18T12:00:00+02:00", "utc", None)[0], utc)
        self.assertIsNone(to_epoch_ns("2026-11-18T12:00:00", "server_local", None)[0])       # offset unknown
        self.assertIsNone(to_epoch_ns(None, "utc", 0)[0])

    def test_key_regex(self):
        rx = RULES["push"]["key_regex"]
        self.assertEqual(key_of('{"Action": "reset", "AlertObjectID": 205}', rx), "205")
        self.assertEqual(key_of("AlertObjectID=17&x=1", rx), "17")
        self.assertIsNone(key_of("no key here", rx))


class FakeResp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class FakeSession:
    """Loki stand-in: refuses any batch that contains a line with 'BAD', fails `fail` times with 503 first."""
    def __init__(self, fail=0):
        self.fail, self.posts = fail, []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        if self.fail:
            self.fail -= 1
            return FakeResp(503, "unavailable")
        lines = [v[1] for s in json["streams"] for v in s["values"]]
        if any("BAD" in x for x in lines):
            return FakeResp(400, "entry too far behind")
        return FakeResp(204)


class LokiSinkTest(unittest.TestCase):
    def entries(self, n, bad=()):
        return [({"job": "t"}, 1_000 + i, "BAD" if i in bad else f"ok {i}", {"i": i}) for i in range(n)]

    def test_rejected_entries_are_isolated_not_lost(self):
        s = FakeSession()
        r = Loki("http://x", session=s, batch_entries=8, sleep=lambda x: None).push(self.entries(20, bad={3, 11}))
        self.assertEqual(r.accepted, 18)
        self.assertEqual(sorted(x[1] for x in r.rejected), [1_003, 1_011])
        self.assertIn("too far behind", r.rejected[0][4])

    def test_sorted_per_stream_and_batched(self):
        s = FakeSession()
        Loki("http://x", session=s, batch_entries=5, sleep=lambda x: None).push(list(reversed(self.entries(12))))
        ts = [int(v[0]) for b in s.posts for v in b["streams"][0]["values"]]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(len(s.posts), 3)

    def test_unavailable_retries_then_raises(self):
        ok = Loki("http://x", session=FakeSession(fail=2), sleep=lambda x: None).push(self.entries(3))
        self.assertEqual(ok.accepted, 3)
        with self.assertRaises(LokiError):
            Loki("http://x", session=FakeSession(fail=9), sleep=lambda x: None).push(self.entries(3))


class WebhookTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(networks={"n1": Network(name="n1", host="h")}, hook_token="secret")

    def test_token_and_network(self):
        http = hook_app(self.settings, loki=Loki("http://127.0.0.1:1"),
                        connect=lambda: (_ for _ in ()).throw(AssertionError("no DB before auth"))).test_client()
        self.assertEqual(http.post("/hook/n1", data="{}").status_code, 401)
        self.assertEqual(http.post("/hook/n1", data="{}", headers={"X-Swcollect-Token": "wrong"}).status_code, 401)
        self.assertEqual(http.post("/hook/zz", data="{}", headers={"X-Swcollect-Token": "secret"}).status_code, 404)

    def test_postgres_down_is_503(self):
        def down():
            raise psycopg.OperationalError("down")
        http = hook_app(self.settings, loki=Loki("http://127.0.0.1:1"), connect=down).test_client()
        r = http.post("/hook/n1", data='{"AlertObjectID": 1}', headers={"X-Swcollect-Token": "secret"})
        self.assertEqual(r.status_code, 503)


@unittest.skipUnless(IT, "integration: set SWCOLLECT_IT=1 with PostgreSQL + TimescaleDB and Loki running")
class EventsIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from swcollect.jobs import inventory
        from swcollect.mock.mock_swis import create_app
        from swcollect.steps import discover_step
        from swcollect.swis.client import SwisClient

        from .flask_session import FlaskSession
        cls.app = create_app(schema_all=True)
        cls.net = Network(name="it", host=f"it-{uuid.uuid4().hex[:8]}", port=17774, scheme="http",
                          username="readonly", password="mock", network_id="it")
        cls.client = SwisClient(cls.net, "mock", session=FlaskSession(cls.app), capture=False)
        cls.conn = psycopg.connect(DSN, autocommit=True)
        cls.inst = discover_step(cls.client, cls.net, None, cls.conn)["instance_id"]
        inventory.run(cls.conn, cls.client, cls.net, 5000, log=lambda *a: None)
        cls.loki = Loki(LOKI, sleep=lambda x: None)
        cls.dead = Loki("http://127.0.0.1:1", retries=1, sleep=lambda x: None)
        cls.settings = Settings(networks={"it": cls.net}, pg_dsn=DSN, loki_url=LOKI, hook_token="t0k")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def q(self, sql, *a):
        return self.conn.execute(sql, a).fetchall()

    def events(self, loki=None):
        from swcollect.jobs import events
        return events.run(self.conn, self.client, loki or self.loki, self.net, 5000, RULES, log=lambda *a: None)

    def loki_ids(self, source, field):
        sel = f'{{job="swcollect",instance="{self.net.instance_key}",source="{source}"}}'
        now = time.time_ns()
        return [json.loads(line)[field] for _, line, _ in self.loki.query_range(sel, now - 40 * 86400 * 10**9,
                                                                                 now + 86400 * 10**9)]

    def wait_loki(self, source, field, expected: set, seconds=120) -> list:
        """Loki makes accepted entries queryable after a short delay (and, with the default
        query_ingesters_within, old ones only after their chunk is flushed): poll until complete or timeout."""
        t = time.monotonic()
        while True:
            got = self.loki_ids(source, field)
            if expected <= set(got) or time.monotonic() - t > seconds:
                return got
            time.sleep(5)

    def wm(self, entity):
        r = self.q("SELECT value FROM sw_meta.sync_state WHERE instance_id = %s AND entity = %s", self.inst, entity)
        return r[0][0] if r else None

    def test_events_push_and_reconcile(self):
        from swcollect.steps import curate_step
        # 1. backfill: every event in the window is in Loki once
        t1 = self.events()
        self.assertEqual(set(t1["status"]) - {"empty"}, {"ok"})
        wm = int(self.wm("Orion.Events"))
        swis = {r["EventID"] for r in self.client.query_all(
            f"SELECT e.EventID FROM Orion.Events e WHERE e.EventID <= {wm}").rows}
        loki = self.wait_loki("Orion.Events", "EventID", swis)
        self.assertEqual(set(loki), swis)
        self.assertEqual(len(loki), len(set(loki)), "no duplicates in Loki")
        kinds = {s["kind"] for _, _, s in self.loki.query_range(
            f'{{instance="{self.net.instance_key}",source="Orion.OLM.LogEntry"}}', time.time_ns() - 30 * 86400 * 10**9,
            time.time_ns() + 86400 * 10**9)}
        self.assertEqual(kinds, {"syslog", "trap"})                         # from Orion.OLM.LogEntryType

        # 2. alerts: 6 triggers + 3 resets, every 4th push lost; the rest arrive at the receiver
        out = self.app.test_client().post("/mock/admin/alerts", json={"trigger": 6, "reset": 3, "drop_every": 4},
                                          auth=("readonly", "mock")).get_json()
        hook = hook_app(self.settings, loki=self.loki).test_client()
        delivered = 0
        for p in out["pushes"]:
            if not p["dropped"]:
                r = hook.post("/hook/it", data=p["body"], headers={"X-Swcollect-Token": "t0k",
                                                                  "Content-Type": "application/json"})
                self.assertEqual(r.status_code, 200)
                delivered += 1
        dup = hook.post("/hook/it", data=out["pushes"][0]["body"], headers={"X-Swcollect-Token": "t0k"}).get_json()
        self.assertIsNotNone(dup["duplicate_of"])                           # same body again: kept, marked
        lost = [h for h, p in zip(out["history"], out["pushes"]) if p["dropped"]]

        # 3. the safety-net poll fills what the push lost; every delivered push is matched to its own row
        t2 = self.events()
        r = t2["reconcile"]
        self.assertEqual(r["push_only"], 0)
        st = dict((h, s) for h, s in self.q("SELECT alert_history_id, status FROM sw_meta.alert_reconcile WHERE "
                                            "instance_id = %s AND alert_key LIKE 'h:%%'", self.inst))
        for h, p in zip(out["history"], out["pushes"]):
            self.assertEqual(st[h], "poll_only" if p["dropped"] else "matched", h)
        self.assertEqual(len(lost), 2)
        lat = self.q("SELECT max(latency_sec) FROM sw_meta.alert_reconcile WHERE instance_id = %s AND "
                     "status = 'matched'", self.inst)[0][0]
        self.assertLess(lat, 60, "a reset push is paired with the reset row, not the old trigger")
        hist = set(self.wait_loki("Orion.AlertHistory", "AlertHistoryID", set(out["history"])))
        self.assertTrue(set(out["history"]) <= hist, "every history row, pushed or not, is in Loki")

        # 4. Loki down: the receiver keeps the message, the events run sends it later
        hook_down = hook_app(self.settings, loki=self.dead).test_client()
        r = hook_down.post("/hook/it", data='{"AlertObjectID": 999, "Action": "test"}',
                           headers={"X-Swcollect-Token": "t0k"}).get_json()
        self.assertFalse(r["loki"])
        # Loki down for the events job: watermark stays, nothing is lost
        self.app.test_client().post("/mock/admin/metrics", json={"advance_minutes": 5}, auth=("readonly", "mock"))  # within Loki's 10-min future tolerance
        before = self.wm("Orion.Events")
        t3 = self.events(loki=self.dead)
        self.assertIn("error", t3["status"])
        self.assertEqual(self.wm("Orion.Events"), before)
        t4 = self.events()
        self.assertGreaterEqual(t4["push_resent"], 1)
        self.assertEqual(self.q("SELECT count(*) FROM sw_meta.push_message WHERE NOT loki_pushed AND instance_id = %s",
                                self.inst)[0][0], 0)
        wm = int(self.wm("Orion.Events"))
        self.assertGreater(wm, int(before))
        swis = {r["EventID"] for r in self.client.query_all(
            f"SELECT e.EventID FROM Orion.Events e WHERE e.EventID <= {wm}").rows}
        self.assertEqual(set(self.wait_loki("Orion.Events", "EventID", swis)), swis)

        # 5. configuration versions: full text in sw_raw, linked in sw_core
        n = self.q("SELECT count(*), count(\"Config\") FROM sw_raw.ncm_configarchive WHERE instance_id = %s", self.inst)[0]
        self.assertGreater(n[0], 0)
        self.assertEqual(n[0], n[1])
        c = curate_step(self.conn, self.client, self.net)
        cv = next(f for f in c["flows"] if f["table"] == "config_version")
        self.assertEqual(cv["in"], cv["out"] + cv["dlq"])
        self.assertGreater(cv["out"], 0)


if __name__ == "__main__":
    unittest.main()

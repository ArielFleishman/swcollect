"""Phase 5: the scheduler (what is due, locks, failures) and verify. Integration parts need SWCOLLECT_IT=1."""
import os
import unittest
import uuid
from unittest import mock

import psycopg

from swcollect.config import Network, Settings
from swcollect.discover import load_rules
from swcollect.scheduler import ORDER, interval_minutes

IT = os.environ.get("SWCOLLECT_IT") == "1"
DSN = os.environ.get("SWCOLLECT_PG_DSN", "postgresql://swcollect:swcollect@127.0.0.1:15432/swcollect")
LOKI = os.environ.get("SWCOLLECT_LOKI_URL", "http://127.0.0.1:13100")
SCHED = load_rules()["schedule"]


class IntervalTest(unittest.TestCase):
    def test_intervals(self):
        self.assertEqual(interval_minutes("inventory", SCHED, None), 1440)              # daily_at
        self.assertEqual(interval_minutes("routing", SCHED, None), 15)
        self.assertEqual(interval_minutes("metrics", SCHED, {"every_minutes": 9}), 9)  # follows StatCollection
        self.assertEqual(interval_minutes("metrics", SCHED, None), 5)
        self.assertIsNone(interval_minutes("nope", SCHED, None))

    def test_every_job_is_scheduled(self):
        for job in ORDER:
            self.assertIn(job, SCHED, job)
        self.assertIn("curate", SCHED["inventory"]["then"])


@unittest.skipUnless(IT, "integration: set SWCOLLECT_IT=1 with PostgreSQL + TimescaleDB and Loki running")
class SchedulerIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from swcollect.mock.mock_swis import create_app
        from swcollect.scheduler import JobRunner
        from swcollect.swis.client import SwisClient

        from .flask_session import FlaskSession
        cls.app = create_app(schema_all=True)
        cls.net = Network(name="it", host=f"it-{uuid.uuid4().hex[:8]}", port=17774, scheme="http",
                          username="readonly", password="mock", network_id="it")
        cls.settings = Settings(networks={"it": cls.net}, pg_dsn=DSN, loki_url=LOKI)
        cls.runner = JobRunner(cls.settings, cls.net, log=lambda *a: None)
        cls.runner.client = SwisClient(cls.net, "mock", session=FlaskSession(cls.app), capture=False)
        cls.conn = psycopg.connect(DSN, autocommit=True)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def due(self):
        from swcollect.scheduler import due_jobs
        return [j for j, _ in due_jobs(self.conn, self.net, SCHED)]

    def test_schedule_lifecycle(self):
        # 1. a new network: only discover is due
        self.assertEqual(self.due(), ["discover"])
        with mock.patch("swcollect.scheduler.CATALOG_DIR", new=__import__("pathlib").Path(os.environ.get("TEMP", "/tmp"))):
            r = self.runner.run("discover")
        self.assertNotIn("error", r)
        # 2. after discover every other job is due, in order
        self.assertEqual(self.due(), [j for j in ORDER if j != "discover"])
        for job in ("inventory", "inventory_core", "routing", "metrics", "events", "verify"):
            r = self.runner.run(job)
            self.assertNotIn("error", r, job)
        self.assertIn("curate", self.runner.run("inventory"))                          # `then`
        # 3. nothing is due right after
        self.assertEqual(self.due(), [])
        # 4. another collector holds the lock: the job is skipped, not run twice
        other = psycopg.connect(DSN, autocommit=True)
        try:
            other.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"swcollect:{self.net.instance_key}:events",))
            self.assertIn("skipped", self.runner.run("events"))
        finally:
            other.close()
        # 5. a job that fails is recorded (status failed, error) and retried only after the wait
        self.conn.execute("""UPDATE sw_meta.sync_run SET started_at = started_at - interval '1 hour'
                             WHERE instance_id = (SELECT instance_id FROM sw_meta.instance WHERE instance_key = %s)
                             AND job = 'routing'""", (self.net.instance_key,))
        self.assertIn("routing", self.due())
        with mock.patch.object(type(self.runner), "_run", side_effect=RuntimeError("boom")):
            r = self.runner.run("routing")
        self.assertIn("boom", r["error"])
        st = self.conn.execute("""SELECT status, error FROM sw_meta.sync_run r JOIN sw_meta.instance i USING (instance_id)
                                  WHERE i.instance_key = %s AND job = 'routing' ORDER BY run_id DESC LIMIT 1""",
                               (self.net.instance_key,)).fetchone()
        self.assertEqual(st[0], "failed")
        self.assertIn("boom", st[1])
        self.assertNotIn("routing", self.due())                                        # waits retry_after_failure
        # 6. verify ran and found nothing failing on the mock
        v = self.runner.run("verify")
        self.assertNotEqual(v["overall"], "fail", v["checks"])


if __name__ == "__main__":
    unittest.main()

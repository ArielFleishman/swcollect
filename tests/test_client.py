# Copied from swcheck tests/test_client.py at commit 0958074 (2026-11-18). Local changes: import paths only.
import json
import unittest

from swcollect.config import Network
from swcollect.swis.client import (QUERY_PATH, ReadOnlyViolation, SwisClient, SwisError, VerbSignatureError, coerce_args,
                                 ensure_select)


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self):
        return self._payload


class FakeSession:
    """Serves pages from a list of rows, honoring WITH ROWS a TO b."""

    def __init__(self, rows, status=200):
        self.rows = rows
        self.status = status
        self.calls = []
        self.headers = {}
        self.auth = None
        self.verify = True
        self.gets = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        if self.status != 200:
            return FakeResponse(self.status, {"Message": "boom"})
        q = json["query"]
        if "WITH ROWS" in q:
            a, b = q.rsplit("WITH ROWS", 1)[1].split("TO")
            return FakeResponse(200, {"results": self.rows[int(a) - 1:int(b)]})
        return FakeResponse(200, {"results": self.rows})

    get_payload = None

    def get(self, url, timeout=None):
        self.gets.append(url)
        if self.status != 200:
            return FakeResponse(self.status, {"Message": "boom"})
        return FakeResponse(200, self.get_payload)


class VerbSession(FakeSession):
    """Answers the Metadata.Verb / Metadata.VerbArgument queries, then the Invoke itself."""

    def __init__(self, signature, invoke_result):
        super().__init__([])
        self.signature = signature
        self.invoke_result = invoke_result

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        if "/Invoke/" in url:
            return FakeResponse(200, self.invoke_result)
        if "FROM Metadata.Verb " in json["query"]:
            return FakeResponse(200, {"results": [{"EntityName": "x", "Name": "y"}] if self.signature is not None else []})
        return FakeResponse(200, {"results": self.signature or []})


def client(session):
    return SwisClient(Network(name="t", host="h", username="u"), "p", session=session)


class ClientTests(unittest.TestCase):
    def test_only_select_allowed(self):
        for q in ("DELETE FROM Orion.Nodes", "UPDATE x SET y=1", "  -- hi\nINSERT INTO a"):
            with self.assertRaises(ReadOnlyViolation):
                ensure_select(q)
        ensure_select("-- comment\n SELECT NodeID FROM Orion.Nodes")

    def test_non_query_paths_blocked(self):
        s = FakeSession([])
        c = client(s)
        for path in ("/SolarWinds/InformationService/v3/Json/Invoke/Orion.Nodes/Unmanage",
                     "/SolarWinds/InformationService/v3/Json/Create/Orion.Nodes",
                     "/SolarWinds/InformationService/v3/Json/swis://x/Orion/Orion.Nodes/NodeID=1"):
            with self.assertRaises(ReadOnlyViolation):
                c._post(path, {})
        self.assertEqual(s.calls, [])

    def test_client_has_no_write_methods(self):
        for name in ("create", "update", "delete", "bulk_update", "bulk_delete"):
            self.assertFalse(hasattr(SwisClient, name), name)

    def test_invoke_outside_allowlist_is_never_sent(self):
        s = FakeSession([])
        c = client(s)
        for ent, verb in (("Orion.Nodes", "Unmanage"), ("Cirrus.ConfigArchive", "DownloadConfig"),
                          ("Cirrus.ConfigArchive", "ExecuteScript"), ("Cirrus.PolicyReports", "StartCaching")):
            with self.assertRaises(ReadOnlyViolation):
                c.invoke(ent, verb, [])
        self.assertEqual(s.calls, [])

    def test_invoke_checks_signature_before_sending(self):
        sig = [{"EntityName": "Cirrus.ConfigArchive", "VerbName": "Diff", "Position": 0, "Name": "a", "Type": "System.Guid",
                "IsOptional": False},
               {"EntityName": "Cirrus.ConfigArchive", "VerbName": "Diff", "Position": 1, "Name": "b", "Type": "System.Guid",
                "IsOptional": False}]
        s = VerbSession(sig, "--- a\n+++ b")
        c = client(s)
        with self.assertRaises(VerbSignatureError):
            c.invoke("Cirrus.ConfigArchive", "Diff", ["only-one"])
        self.assertFalse(any("/Invoke/" in url for url, _ in s.calls))
        data, seq = c.invoke("cirrus.configarchive", "diff", ["x", "y"])
        self.assertEqual(data, "--- a\n+++ b")
        url, body = s.calls[-1]
        self.assertEqual(url, "https://h:17774/SolarWinds/InformationService/v3/Json/Invoke/Cirrus.ConfigArchive/Diff")
        self.assertEqual(body, ["x", "y"])
        rec = c.log[-1]
        self.assertEqual((rec["seq"], rec["kind"], rec["target"]), (seq, "invoke", "Cirrus.ConfigArchive.Diff"))
        self.assertEqual(rec["response"]["body"], "--- a\n+++ b")
        self.assertIsNone(rec["response"]["results"])

    def test_invoke_refuses_verb_missing_from_metadata(self):
        s = VerbSession(None, None)
        with self.assertRaises(VerbSignatureError):
            client(s).invoke("Cirrus.ConfigArchive", "Diff", ["x", "y"])
        self.assertFalse(any("/Invoke/" in url for url, _ in s.calls))

    def test_coerce_args(self):
        sig = [{"Name": "ids", "Type": "System.Int32[]"}, {"Name": "flag", "Type": "System.Boolean"},
               {"Name": "n", "Type": "System.Int64", "IsOptional": True}]
        self.assertEqual(coerce_args(sig, ["1, 2,3", "true"]), [[1, 2, 3], True])
        self.assertEqual(coerce_args(sig, ["[4]", "0", "7"]), [[4], False, 7])
        for bad in (["1"], ["1", "maybe"], ["x", "true"], ["1", "true", "2", "3"]):
            with self.assertRaises(VerbSignatureError):
                coerce_args(sig, bad)

    def test_read_uri(self):
        s = FakeSession([])
        c = client(s)
        for bad in ("Orion.Nodes/NodeID=1", "swis://h/../Query", "swis://h", "http://h/x"):
            with self.assertRaises(ReadOnlyViolation):
                c.read_uri(bad)
        self.assertEqual(s.gets, [])
        s.get_payload = {"NodeID": 1, "Caption": "PE-01"}
        data, seq = c.read_uri("swis://orion/Orion/Orion.Nodes/NodeID=1")
        self.assertEqual(data, {"NodeID": 1, "Caption": "PE-01"})
        self.assertEqual(s.gets, ["https://h:17774/SolarWinds/InformationService/v3/Json/swis://orion/Orion/Orion.Nodes/NodeID=1"])
        rec = c.log[-1]
        self.assertEqual((rec["seq"], rec["kind"], rec["request"]["method"]), (seq, "read", "GET"))
        self.assertEqual(rec["response"]["results"], [{"NodeID": 1, "Caption": "PE-01"}])

    def test_read_uri_error_is_logged(self):
        c = client(FakeSession([], 404))
        with self.assertRaises(SwisError):
            c.read_uri("swis://orion/Orion/Orion.Nodes/NodeID=99")
        self.assertEqual(c.log[-1]["response"]["status"], 404)

    def test_paging(self):
        rows = [{"i": i} for i in range(23)]
        s = FakeSession(rows)
        res = client(s).query_all("SELECT i FROM X ORDER BY i", page_size=10, max_rows=100)
        self.assertEqual(len(res.rows), 23)
        self.assertEqual(res.stat.pages, 3)
        self.assertFalse(res.truncated)
        self.assertTrue(all(url.endswith(QUERY_PATH) for url, _ in s.calls))

    def test_paging_truncates_at_max_rows(self):
        s = FakeSession([{"i": i} for i in range(50)])
        res = client(s).query_all("SELECT i FROM X ORDER BY i", page_size=10, max_rows=20)
        self.assertEqual(len(res.rows), 20)
        self.assertTrue(res.truncated)

    def test_errors(self):
        for status in (400, 401, 403, 500):
            with self.assertRaises(SwisError):
                client(FakeSession([], status)).query("SELECT a FROM b")

    def test_log_records_request_and_full_response(self):
        rows = [{"i": i, "name": f"n{i}"} for i in range(23)]
        c = client(FakeSession(rows))
        res = c.query_all("SELECT i, name FROM X ORDER BY i", {"p": 1}, page_size=10, max_rows=100)
        rec = c.log[-1]
        self.assertEqual(rec["seq"], res.seq)
        self.assertEqual(rec["request"]["url"], "https://h:17774" + QUERY_PATH)
        self.assertEqual(rec["request"]["body"], {"query": "SELECT i, name FROM X ORDER BY i", "parameters": {"p": 1}})
        self.assertEqual(rec["request"]["headers"]["Authorization"], "Basic <REDACTED>")
        self.assertEqual([p["status"] for p in rec["pages"]], [200, 200, 200])
        self.assertEqual(rec["response"]["results"], rows)
        self.assertEqual(rec["response"]["rows"], 23)

    def test_log_never_contains_the_password(self):
        c = SwisClient(Network(name="t", host="h", username="u"), "S3cret-pw", session=FakeSession([{"a": 1}]))
        c.query("SELECT a FROM b")
        self.assertNotIn("S3cret-pw", json.dumps(c.log))

    def test_log_keeps_error_status_and_body(self):
        c = client(FakeSession([], 400))
        with self.assertRaises(SwisError):
            c.query("SELECT a FROM b")
        rec = c.log[-1]
        self.assertEqual(rec["response"]["status"], 400)
        self.assertIn("boom", rec["response"]["error_body"])

    def test_custom_base_path_and_capture_off(self):
        s = FakeSession([{"a": 1}])
        c = SwisClient(Network(name="t", host="h", port=443, base_path="/proxy/swis/v3/Json", username="u"), "p",
                       session=s, capture=False)
        c.query("SELECT a FROM b")
        self.assertEqual(s.calls[0][0], "https://h:443/proxy/swis/v3/Json/Query")
        self.assertEqual(c.log, [])
        with self.assertRaises(ReadOnlyViolation):
            c._post(QUERY_PATH, {})       # the default path is not the configured one


if __name__ == "__main__":
    unittest.main()

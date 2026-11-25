# Copied from swcheck swcheck/mock/mock_swis.py at commit 0958074 (2026-11-18). Local changes: import paths;
# entity alias + one level of navigation in SWQL, Metadata.Relationship, --schema-all (swcollect phase 1);
# test-only POST /mock/admin/inventory to add, change, remove a node and hide an entity (swcollect phase 2);
# a movable clock, metrics that keep sampling, POST /mock/admin/metrics (swcollect phase 3);
# POST /mock/admin/alerts: alerts trigger/reset, written to AlertHistory and pushed like an Orion alert action
# ("Send a GET or POST Request to a Web Server"), with deliberate drops (swcollect phase 4);
# rich mode (--rich, swcollect): realistic rows for every schema entity and metric history from mock/rich.py,
# documented types in Metadata.Property, and a time-index fast path for time-bounded metric queries.
# swcheck does not need these changes.
"""Mock SWIS REST endpoint with a small SWQL engine, CRUD read by Uri, the read-only verbs, and the actions.

Two accounts: readonly/mock may only read (an action gets 403, like a read-only Orion account); actions/mock may
also run the action verbs, Create and Update. Transfers move Queued → Transferring → Complete/Error over a few
seconds, and one node (AGG-02) always fails, so partial success in a group can be shown.

Supports: SELECT [TOP n] expr [AS alias], ... FROM Entity [[AS] e] [WHERE a op b AND ...]
          [ORDER BY f [ASC|DESC], ...] [WITH ROWS a TO b]
expr: field | e.field | e.Navigation.field | COUNT(*) | COUNT(f) | MIN(f) | MAX(f) | GETDATE() | GETUTCDATE()
      (navigation: one level, a left join per fixtures.NAV_RELATIONS; WHERE / ORDER BY use source fields)
op:   = != <> < > <= >= LIKE | IS [NOT] NULL
"""
from __future__ import annotations

import bisect
import difflib
import fnmatch
import uuid
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

from ..swis.client import QUERY_PATH
from . import fixtures, rich as rich_mode

USER, PASSWORD = "readonly", "mock"


# Series that keep sampling as the mock's clock moves: entity -> (key fields, minutes between samples).
# Same intervals as the history in fixtures.build (ResponseTime 2 min, the others 10 min = StatCollection).
METRIC_SERIES = {"Orion.ResponseTime": (["NodeID"], 2), "Orion.CPULoad": (["NodeID"], 10),
                 "Orion.NPM.InterfaceTraffic": (["InterfaceID"], 10), "Orion.NPM.InterfaceErrors": (["InterfaceID"], 10)}


class SwqlError(Exception):
    pass


_Q = re.compile(
    r"^\s*SELECT\s+(?:TOP\s+(?P<top>\d+)\s+)?(?P<cols>.+?)\s+FROM\s+(?P<ent>[\w.]+)"
    r"(?:\s+(?:AS\s+)?(?!(?:WHERE|ORDER|WITH)\b)(?P<alias>\w+))?"
    r"(?:\s+WHERE\s+(?P<where>.+?))?(?:\s+ORDER\s+BY\s+(?P<order>.+?))?"
    r"(?:\s+WITH\s+ROWS\s+(?P<r1>\d+)\s+TO\s+(?P<r2>\d+))?\s*$", re.I | re.S)
_COND = re.compile(r"^\s*([\w.]+)\s*(=|!=|<>|<=|>=|<|>|\s+LIKE\s+|\s+IS\s+NOT\s+NULL|\s+IS\s+NULL)\s*(.*?)\s*$", re.I)
_FUNC = re.compile(r"^(COUNT|MIN|MAX)\s*\(\s*(\*|[\w.]+)\s*\)$", re.I)


def _split_top(s: str, sep: str) -> list[str]:
    out, depth, quote, cur = [], 0, False, ""
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "'":
            quote = not quote
        if not quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if depth == 0 and s[i:i + len(sep)].upper() == sep.upper():
                out.append(cur)
                cur = ""
                i += len(sep)
                continue
        cur += ch
        i += 1
    out.append(cur)
    return [x.strip() for x in out if x.strip()]


class MockSwis:
    def __init__(self, latency_ms: int = 0, scale: int = 1, schema_all: bool = False, rich: bool = False,
                 history_days: int = 7, hourly_days: int = 7):
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        self.data = fixtures.build(now_utc=now_utc, scale=scale)
        self.rich = rich
        self.rich_info: dict = {}
        self.series = dict(METRIC_SERIES)
        if rich:                               # rich mode implies --schema-all
            schema_all = True
            self.rich_info = rich_mode.extend(self.data, now_utc, now_utc + fixtures.TZ_OFFSET, history_days,
                                              hourly_days)
            self.series = dict(rich_mode.RICH_SERIES)
        self._last: dict[str, dict] = {}       # rich: entity -> {key: newest row}
        self._cursor: dict[str, dict] = {}     # rich: entity -> {key: last time checked}
        self._tidx: dict[str, list] = {}       # rich: entity -> sorted DateTime strings (parallel to its rows)
        self.schema_all = schema_all
        self.latency_ms = latency_ms
        self.lock = threading.Lock()
        self.rnd = random.Random(7)
        self._uris()
        self._schema()
        self.jobs: dict[str, dict] = {}       # TransferID -> {"t0", "fail", "config"} for transfers started here
        self.caching: dict[str, float] = {}   # PolicyReportID -> start time of StartCaching
        self.hidden: set[str] = set()         # entities that answer with no rows (simulated permission filtering)
        self.removed: dict[int, dict] = {}    # nodes removed through the admin endpoint, for restore_node
        self.clock_offset = timedelta(0)
        last = self.data["Orion.Events"][-1]
        self.next_event_id = last["EventID"] + 1
        self.next_event_time = self._now_local() + timedelta(seconds=5)

    def _now_utc(self):
        # clock_offset lets tests move the mock's time forward (POST /mock/admin/metrics {"advance_minutes": n})
        return datetime.now(timezone.utc).replace(tzinfo=None) + getattr(self, "clock_offset", timedelta(0))

    def _now_local(self):
        return self._now_utc() + fixtures.TZ_OFFSET

    # ----- CRUD read -----
    # Like SWIS, every System.Entity row has a Uri. Its key part is the entity's key fields when the mock knows
    # them; otherwise the row's position (a Uri is opaque to the client, which only GETs it back).
    @staticmethod
    def _uri(ent: str, r: dict, i: int) -> str:
        if ent == "Orion.NodesCustomProperties":
            return f"swis://{fixtures.MOCK_HOST}/Orion/Orion.Nodes/NodeID={r['NodeID']}/CustomProperties"
        keys = [fixtures.URI_KEYS[ent]] if ent in fixtures.URI_KEYS else fixtures.KEYS.get(ent) or []
        ident = ",".join(f"{k}={r[k]}" for k in keys) if keys and all(k in r for k in keys) else f"Row={i}"
        return f"swis://{fixtures.MOCK_HOST}/Orion/{ent}/{ident}"

    def _stamp(self, ent: str, r: dict) -> dict:
        r["Uri"] = self._uri(ent, r, len(self.data.get(ent, [])))
        return r

    def _uris(self):
        for ent, rows in self.data.items():
            if ent.startswith(("Metadata.", "System.")):
                continue
            for i, r in enumerate(rows):
                r["Uri"] = self._uri(ent, r, i)

    def read(self, uri: str) -> dict | None:
        m = re.match(r"swis://[^/]+/Orion/([^/]+)/", uri, re.I)
        ent = m and ("Orion.NodesCustomProperties" if uri.lower().endswith("/customproperties") else m.group(1))
        for r in self.data.get(ent, []) if ent else []:
            if r.get("Uri", "").lower() == uri.lower():
                return {k: v for k, v in r.items() if not k.startswith("_")}
        return None

    # ----- verbs -----
    READ_VERBS = {"diff", "getcompliancedatatable", "getalertsuppressionstate", "getconfigtypes", "getpolicyreport",
                  "getpageableeosdatatable", "getpageableeosrowcount"}

    def invoke(self, entity: str, verb: str, args: list):
        sig = [a for a in fixtures.VERB_ARGUMENTS if a["EntityName"].lower() == entity.lower()
               and a["VerbName"].lower() == verb.lower()]
        if not sig and not any(e.lower() == entity.lower() and v.lower() == verb.lower() for e, v in fixtures.VERBS):
            return None
        required = sum(1 for a in sig if not a["IsOptional"])
        if not required <= len(args) <= len(sig):
            raise SwqlError(f"Verb {entity}.{verb} takes {required}-{len(sig)} arguments, got {len(args)}")
        v = verb.lower()
        if v not in ("diff", "getcompliancedatatable"):
            with self.lock:
                return self._action(entity, v, args)
        if verb.lower() == "diff":
            by_id = {c["ConfigID"]: c for c in self.data["NCM.ConfigArchive"]}
            old, new = (by_id.get(str(a)) for a in args)
            if not old or not new:
                raise SwqlError("Config not found")
            # The 2026.2 schema documents Diff as a DataTable. The column names here are the mock's own.
            out, n_old, n_new = [], 0, 0
            for line in difflib.unified_diff(old["Config"].splitlines(), new["Config"].splitlines(), lineterm="", n=0):
                if line.startswith(("---", "+++", "@@")):
                    if line.startswith("@@"):
                        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
                        n_old, n_new = int(m[1]), int(m[2])
                    continue
                if line.startswith("-"):
                    out.append({"ChangeType": "Removed", "LeftLine": n_old, "RightLine": None, "Text": line[1:]})
                    n_old += 1
                elif line.startswith("+"):
                    out.append({"ChangeType": "Added", "LeftLine": None, "RightLine": n_new, "Text": line[1:]})
                    n_new += 1
            return out
        rid = str(args[0])
        return [{k: v for k, v in r.items() if not k.startswith("_")}
                for r in self.data["Cirrus.PolicyCacheResults"] if r["ReportID"] == rid]

    def _ncm_node(self, guid):
        return next((n for n in self.data["Cirrus.Nodes"] if n["NodeID"] == guid), None)

    def _transfer(self, guid, action, config_type="", script="", fail=False) -> str:
        tid = str(uuid.uuid4())
        node = self._ncm_node(guid)
        fail = fail or node is None or node.get("NodeCaption") == "AGG-02"
        self.data["NCM.TransferResults"].append(self._stamp("NCM.TransferResults", {
            "TransferID": tid, "NodeID": guid, "Action": action, "RequestedConfigType": config_type,
            "RequestedScript": script, "RequestedReboot": False, "ConfigID": None, "TransferProtocol": "SSH",
            "Status": 0, "ErrorMessage": None, "DeviceOutput": None, "UserName": fixtures.ACTION_USER,
            "DateTime": self._now_local().strftime("%Y-%m-%dT%H:%M:%S"), "JobId": 0}))
        self.jobs[tid] = {"t0": time.monotonic(), "fail": fail, "script": script, "config_type": config_type}
        return tid

    def _advance(self):
        """Transfers: Queued for 1 s, Transferring until 2 s, then Complete or Error. Reports: computing for 2 s."""
        now = time.monotonic()
        for r in self.data["NCM.TransferResults"]:
            job = self.jobs.get(r["TransferID"])
            if not job or r["Status"] in (2, 3):
                continue
            age = now - job["t0"]
            if age < 1:
                continue
            if age < 2:
                r["Status"] = 1
                continue
            if job["fail"]:
                r.update(Status=3, ErrorMessage="Connection refused by remote host (mock)")
                continue
            r["Status"] = 2
            if r["Action"] == 1:
                old = max((c for c in self.data["NCM.ConfigArchive"] if c["NodeID"] == r["NodeID"]
                           and c["ConfigType"] == job["config_type"]), key=lambda c: c["DownloadTime"], default=None)
                cid = str(uuid.uuid4())
                self.data["NCM.ConfigArchive"].append(self._stamp("NCM.ConfigArchive", {
                    "ConfigID": cid, "NodeID": r["NodeID"], "DownloadTime": self._now_local().strftime("%Y-%m-%dT%H:%M:%S"),
                    "ConfigType": job["config_type"], "ConfigTitle": "downloaded by API", "Comments": "", "Baseline": False,
                    "IsBinary": False, "Config": (old or {}).get("Config", "hostname unknown")}))
                r["ConfigID"] = cid
            elif r["Action"] == 3:
                r["DeviceOutput"] = "\n".join(self._cli(r["NodeID"], l.strip())
                                              for l in job["script"].splitlines() if l.strip()) + "\n"
            else:
                r["DeviceOutput"] = ""
        for rep in self.data["Cirrus.PolicyReports"]:
            t0 = self.caching.get(rep["PolicyReportID"])
            if t0 is None:
                continue
            if now - t0 < 1:
                rep["CacheStatus"] = 1
            elif now - t0 < 2:
                rep["CacheStatus"] = 2
            else:
                rep.update(CacheStatus=3, LastUpdated=self._now_local().strftime("%Y-%m-%dT%H:%M:%S"), LastError="")
                del self.caching[rep["PolicyReportID"]]

    def _cli(self, guid: str, line: str) -> str:
        """Mock CLI output. An OSPF neighbour command is answered from the node's Orion.Routing.Neighbors rows
        (IOS XE-like lines), minus one neighbour and plus one CLI-only neighbour (10.9.9.9), so a comparison
        shows both kinds of difference."""
        low = line.lower()
        if low == "show clock":
            return self._now_utc().strftime("%H:%M:%S.000 UTC %a %b %d %Y")
        if "ospf" not in low:
            return f"% mock output for: {line}"
        core = next((n["CoreNodeID"] for n in self.data["Cirrus.Nodes"] if n["NodeID"] == guid), None)
        ports = {i.get("Index"): i.get("Name") or i.get("IfName") or i.get("Caption")
                 for i in self.data["Orion.NPM.Interfaces"] if i.get("NodeID") == core}
        nbrs = [n for n in self.data["Orion.Routing.Neighbors"] if n.get("NodeID") == core and n.get("ProtocolID") == 13]
        rows = ["Neighbor ID     Pri   State           Dead Time   Address         Interface"]
        for n in nbrs[:-1] if len(nbrs) > 1 else nbrs:
            state = "FULL/ -" if n.get("ProtocolStatus") == 8 else "DOWN/ -"
            rows.append(f"{n.get('NeighborID') or n['NeighborIP']}  1  {state}  00:00:35  {n['NeighborIP']}  "
                        f"{ports.get(n.get('LocalProtocolInterfaceIndex'), 'unknown')}")
        rows.append("10.9.9.9  1  FULL/ -  00:00:31  10.9.9.9  Loopback99")
        return "\n".join(rows)

    def _action(self, entity: str, verb: str, args: list):
        if verb == "getconfigtypes":
            return ",".join(sorted({c["ConfigType"] for c in self.data["NCM.ConfigArchive"]}))
        if verb == "getpolicyreport":
            rep = next((r for r in self.data["Cirrus.PolicyReports"] if r["PolicyReportID"] == str(args[0])), None)
            if not rep:
                raise SwqlError(f"Policy report {args[0]} not found")
            return dict(rep, Policies=[{"Name": "ISP policy"}] if args[1] else [])
        if verb in ("getpageableeosdatatable", "getpageableeosrowcount"):
            ids = set(args[0])
            rows = [{"NodeID": n["NodeID"], "CoreNodeID": n["CoreNodeID"], "EndOfSales": n.get("EndOfSales") or None,
                     "EndOfSupport": n.get("EndOfSupport") or None, "EosType": n.get("EosType") or None}
                    for n in sorted(self.data["Cirrus.Nodes"], key=lambda n: n["NodeID"]) if n["NodeID"] in ids]
            if verb == "getpageableeosrowcount":
                return len(rows)
            start, size = int(args[1]), int(args[2])          # the mock counts rows from 1
            return rows[start - 1:start - 1 + size]
        if verb == "downloadconfig":
            return [self._transfer(g, 1, config_type=args[1]) for g in args[0]]
        if verb == "executescript":
            return [self._transfer(g, 3, script=args[1]) for g in args[0]]
        if verb == "uploadconfig":
            return [self._transfer(g, 2, config_type=args[1], script=args[2]) for g in args[0]]
        if verb == "canceltransfers":
            for r in self.data["NCM.TransferResults"]:
                if r["TransferID"] in args[0] and r["Status"] in (0, 1):
                    r.update(Status=3, ErrorMessage="Cancelled by user (mock)")
                    self.jobs.pop(r["TransferID"], None)
            return None
        if verb == "startcaching":
            ids = args[0] if args and args[0] else [x["PolicyReportID"] for x in self.data["Cirrus.PolicyReports"]]
            for rid in ids:
                self.caching[rid] = time.monotonic()
            return True
        if verb == "generateremediationscriptfornodes":
            return [{"NodeID": g, "Script": "! mock remediation script – not executed\nno ip http server"} for g in args[0]]
        if verb == "addnodetoncm":
            core = int(args[0])
            hit = next((n for n in self.data["Cirrus.Nodes"] if n["CoreNodeID"] == core), None)
            if hit:
                return hit["NodeID"]
            node = next((n for n in self.data["Orion.Nodes"] if n["NodeID"] == core), None)
            if not node:
                raise SwqlError(f"Node {core} not found")
            guid = str(uuid.uuid4())
            self.data["Cirrus.Nodes"].append(self._stamp("Cirrus.Nodes", {"NodeID": guid, "CoreNodeID": core, "NodeCaption": node["Caption"],
                                              "AgentIP": node["IPAddress"], "ConnectionProfile": 0, "LoginStatus": "",
                                              "StatusText": "Unknown"}))
            return guid
        if verb == "suppressalerts":
            rows = self.data["Orion.AlertSuppression"]
            for u in args[0]:
                rows[:] = [x for x in rows if x["EntityUri"] != u]
                rows.append({"ID": max((x["ID"] for x in rows), default=0) + 1, "EntityUri": u,
                             "SuppressFrom": args[1] if len(args) > 1 else self._now_utc().strftime("%Y-%m-%dT%H:%M:%S"),
                             "SuppressUntil": args[2] if len(args) > 2 else None})
            return None
        if verb == "resumealerts":
            self.data["Orion.AlertSuppression"][:] = [x for x in self.data["Orion.AlertSuppression"] if x["EntityUri"] not in args[0]]
            return None
        if verb == "getalertsuppressionstate":
            on = {x["EntityUri"] for x in self.data["Orion.AlertSuppression"]}
            return [{"EntityUri": u, "SuppressionMode": 1 if u in on else 0} for u in args[0]]
        if verb in ("unmanage", "remanage"):
            nid = int(str(args[0]).split(":")[-1])
            node = next((n for n in self.data["Orion.Nodes"] if n["NodeID"] == nid), None)
            if not node:
                raise SwqlError(f"Node {args[0]} not found")
            node["UnManaged"] = verb == "unmanage"
            node["Status"] = 9 if verb == "unmanage" else 1
            return None
        raise SwqlError(f"Mock does not implement {entity}.{verb}")

    def create(self, entity: str, props: dict) -> str:
        with self.lock:
            key = fixtures.URI_KEYS.get(entity)
            rows = self.data.get(entity)
            if key is None or rows is None:
                raise SwqlError(f"Create is not supported for {entity}")
            new = dict(props, **{key: max((x[key] for x in rows), default=0) + 1})
            new["Uri"] = f"swis://{fixtures.MOCK_HOST}/Orion/{entity}/{key}={new[key]}"
            rows.append(new)
            return new["Uri"]

    def update(self, uri: str, props: dict) -> bool:
        with self.lock:
            for r in self.data.get("Orion.NodesCustomProperties", []):
                if r.get("Uri", "").lower() == uri.lower():
                    r.update(props)
                    return True
            return False

    # ----- schema -----
    def _schema(self):
        ents, props = [], []
        for name in fixtures.EMPTY_SCHEMA:
            self.data.setdefault(name, [])
        all_names = sorted(set(self.data) | set(fixtures.EXTRA_ENTITIES))
        generated = set(self.data) - self.rich_info.get("curated", set(self.data))
        for name in all_names + fixtures.ABSTRACT:
            caps = fixtures.CAPS.get(name, {})
            base = "System.Indication" if name in fixtures.INDICATIONS else "System.Entity"
            if self.rich and (rich_mode.pages().get(name) or {}).get("bases"):
                base = rich_mode.pages()[name]["bases"][-1]
            ents.append({"FullName": name, "Namespace": name.rsplit(".", 1)[0], "Name": name.rsplit(".", 1)[-1],
                         "BaseType": base,
                         "IsAbstract": name in fixtures.ABSTRACT, "IsIndication": name in fixtures.INDICATIONS,
                         "CanCreate": caps.get("CanCreate", False), "CanUpdate": caps.get("CanUpdate", False),
                         "CanDelete": caps.get("CanDelete", False)})
            rows = self.data.get(name) or []
            keys: dict[str, object] = {}
            for r in rows[:200]:
                for k, v in r.items():
                    if not k.startswith("_") and (k not in keys or keys[k] is None):
                        keys[k] = v
            for k, v in fixtures.EMPTY_SCHEMA.get(name, {}).items():
                keys.setdefault(k, v)
            rkey = rich_mode.synthetic_key(name, (rich_mode.pages().get(name) or {}).get("fields") or {}) \
                if self.rich and name in generated else None
            for k, v in keys.items():
                t = self._type(v)
                if self.rich:                  # the documented type, when every value fits it
                    dt = rich_mode.doc_type(name, k)
                    if dt and all(rich_mode.fits(r.get(k), dt) for r in rows[:2000]):
                        t = dt
                props.append({"EntityName": name, "Name": k, "Type": t, "IsNavigable": False,
                              "IsKey": k in fixtures.KEYS.get(name, []) or k == rkey})
            for nav, target in fixtures.NAVIGATION.get(name, []):
                props.append({"EntityName": name, "Name": nav, "Type": target, "IsNavigable": True, "IsKey": False})
        if self.schema_all:
            self._schema_all(ents, props)
        self.data["Metadata.Entity"] = ents
        # Like SWIS: the relationship is listed, its join columns are not.
        self.data["Metadata.Relationship"] = [
            {"SourceType": s, "TargetType": t, "SourcePropertyName": p, "TargetPropertyName": None,
             "SourceCardinalityMin": sc.split("..")[0], "SourceCardinalityMax": sc.split("..")[-1],
             "TargetCardinalityMin": tc.split("..")[0], "TargetCardinalityMax": tc.split("..")[-1]}
            for s, p, t, _cols, sc, tc in fixtures.NAV_RELATIONS]
        self.data["Metadata.Property"] = props
        self.data["Metadata.Verb"] = [{"EntityName": e, "Name": v} for e, v in fixtures.VERBS]
        self.data["Metadata.VerbArgument"] = [dict(a) for a in fixtures.VERB_ARGUMENTS]

    # ----- metrics keep sampling: new rows at the series interval up to the mock's now (server-local) -----
    def _tick_metrics(self, entity: str) -> int:
        if self.rich:
            return self._tick_rich(entity)
        keys, step = METRIC_SERIES[entity]
        rows, now, added = self.data[entity], self._now_local(), 0
        last: dict = {}
        for r in rows:
            k = tuple(r.get(f) for f in keys)
            if k not in last or r["DateTime"] > last[k]["DateTime"]:
                last[k] = r
        for tmpl in last.values():
            t = datetime.strptime(tmpl["DateTime"][:19], "%Y-%m-%dT%H:%M:%S") + timedelta(minutes=step)
            while t <= now:
                new = dict(tmpl, DateTime=t.strftime("%Y-%m-%dT%H:%M:%S"))
                for f, v in tmpl.items():
                    if f in keys or f in ("DateTime", "Uri") or f.upper().endswith("ID") or isinstance(v, bool):
                        continue
                    if isinstance(v, int):
                        new[f] = max(0, v + self.rnd.randint(-3, 3))
                    elif isinstance(v, float):
                        new[f] = round(v * self.rnd.uniform(0.9, 1.1), 3)
                rows.append(self._stamp(entity, new))
                added += 1
                t += timedelta(minutes=step)
        return added

    def _tick_rich(self, entity: str) -> int:
        """Rich mode: new samples from the shared model (mock/rich.py), so the curves continue; a key that Orion
        would not poll (down, unmanaged, rebooting) gets no sample. Rows stay sorted by time for the index."""
        keys, step = self.series[entity]
        rows, now, model = self.data[entity], self._now_local(), self.rich_info["model"]
        last = self._last.get(entity)
        if last is None:
            last = {}
            for r in rows:
                k = tuple(r.get(f) for f in keys)
                if k not in last or r["DateTime"] > last[k]["DateTime"]:
                    last[k] = r
            self._last[entity] = last
            self._cursor[entity] = {k: datetime.strptime(r["DateTime"][:19], "%Y-%m-%dT%H:%M:%S")
                                    for k, r in last.items()}
        cursor, added = self._cursor[entity], 0
        for k in list(cursor):
            t = cursor[k] + timedelta(minutes=step)
            while t <= now:
                if model.alive(entity, k, t):
                    new = self._stamp(entity, model.sample(entity, k, t))
                    rows.append(new)
                    last[k] = new
                    added += 1
                cursor[k] = t
                t += timedelta(minutes=step)
        if added or entity not in self._tidx:
            rows.sort(key=lambda r: r["DateTime"])
            self._tidx[entity] = [r["DateTime"] for r in rows]
        return added

    def change_metrics(self, body: dict) -> dict:
        out: dict = {}
        with self.lock:
            if body.get("advance_minutes"):
                self.clock_offset += timedelta(minutes=int(body["advance_minutes"]))
                out["added"] = {e: self._tick_metrics(e) for e in self.series}
            re = body.get("reaggregate")
            if re:       # Orion recomputing recent samples: change a value on the newest rows
                rows = sorted(self.data[re["entity"]], key=lambda r: r["DateTime"])[-int(re.get("rows", 1)):]
                for r in rows:
                    r[re["field"]] = (r.get(re["field"]) or 0) + 1
                out["reaggregated"] = [{k: r.get(k) for k in self.series[re["entity"]][0] + ["DateTime"]}
                                       for r in rows]
            out["now_local"] = self._now_local().strftime("%Y-%m-%dT%H:%M:%S")
        return out

    # ----- alerts: trigger / reset, AlertHistory rows, and the push an Orion alert action would send -----
    # The EventType codes (0 trigger, 1 reset) and the push body are the MOCK's own. On a real Orion the body is
    # whatever the alert action template says (docs/orion-alert-action.md) and the EventType meaning needs proof.
    def change_alerts(self, body: dict) -> dict:
        import json as _json
        out = {"history": [], "pushes": []}
        drop_every = int(body.get("drop_every") or 0)
        with self.lock:
            active, hist = self.data["Orion.AlertActive"], self.data["Orion.AlertHistory"]
            nodes = self.data["Orion.Nodes"]
            now = self._now_local().strftime("%Y-%m-%dT%H:%M:%S")
            todo = [("trigger", None)] * int(body.get("trigger") or 0)
            todo += [("reset", a) for a in sorted(active, key=lambda a: a["AlertActiveID"])[:int(body.get("reset") or 0)]]
            for action, a in todo:
                if action == "trigger":
                    aid = max([x["AlertActiveID"] for x in active] + [0]) + 1
                    node = nodes[aid % len(nodes)]
                    a = {"AlertActiveID": aid, "AlertObjectID": 200 + aid, "TriggeredDateTime": now,
                         "TriggeredMessage": f"{node['Caption']}: mock alert {aid}", "Acknowledged": False,
                         "AcknowledgedBy": ""}
                    active.append(self._stamp("Orion.AlertActive", a))
                else:
                    active.remove(a)
                hid = max(h["AlertHistoryID"] for h in hist) + 1
                hist.append(self._stamp("Orion.AlertHistory", {
                    "AlertHistoryID": hid, "EventType": 0 if action == "trigger" else 1,
                    "Message": a["TriggeredMessage"] + (" (reset)" if action == "reset" else ""), "TimeStamp": now,
                    "AccountID": "", "AlertActiveID": a["AlertActiveID"], "AlertObjectID": a["AlertObjectID"],
                    "ActionID": 0}))
                out["history"].append(hid)
                self.push_seq = getattr(self, "push_seq", 0) + 1
                msg = {"Action": action, "AlertObjectID": a["AlertObjectID"], "AlertActiveID": a["AlertActiveID"],
                       "Message": a["TriggeredMessage"], "TimeStamp": now}
                dropped = bool(drop_every) and self.push_seq % drop_every == 0
                out["pushes"].append({"body": _json.dumps(msg), "dropped": dropped, "status": None})
        if body.get("push_url"):                         # send outside the lock, like the alert engine would
            import requests as _rq
            headers = {"Content-Type": "application/json"}
            if body.get("token"):
                headers["X-Swcollect-Token"] = body["token"]
            for p in out["pushes"]:
                if p["dropped"]:
                    continue
                try:
                    p["status"] = _rq.post(body["push_url"], data=p["body"], headers=headers, timeout=5).status_code
                except _rq.RequestException as e:
                    p["status"] = f"error: {e}"
        return out

    # ----- test-only inventory changes (POST /mock/admin/inventory; not a SWIS path) -----
    def change_inventory(self, body: dict) -> dict:
        out: dict = {}
        with self.lock:
            nodes, ifs = self.data["Orion.Nodes"], self.data["Orion.NPM.Interfaces"]
            if body.get("add_node"):
                nid = max(n["NodeID"] for n in nodes) + 1
                new = dict(nodes[0], NodeID=nid, Caption=f"NEW-RTR-{nid:02d}", IPAddress=f"10.99.0.{nid}")
                nodes.append(self._stamp("Orion.Nodes", new))
                iid = max(i["InterfaceID"] for i in ifs) + 1
                ifs.append(self._stamp("Orion.NPM.Interfaces", dict(ifs[0], InterfaceID=iid, NodeID=nid)))
                out["added_node"], out["added_interface"] = nid, iid
            if body.get("change_node"):
                n = next(n for n in nodes if n["NodeID"] == body["change_node"])
                n.update(body.get("fields") or {"Caption": n["Caption"] + "-CHANGED"})
                out["changed_node"] = n["NodeID"]
            if body.get("remove_node"):
                nid = body["remove_node"]
                self.removed.update({n["NodeID"]: n for n in nodes if n["NodeID"] == nid})
                self.data["Orion.Nodes"] = [n for n in nodes if n["NodeID"] != nid]
                out["removed_node"] = nid       # its interfaces stay: they now point to a missing node
            if body.get("restore_node") in self.removed:
                self.data["Orion.Nodes"].append(self.removed.pop(body["restore_node"]))
                out["restored_node"] = body["restore_node"]
            for ent in body.get("hide") or []:
                self.hidden.add(ent)
            for ent in body.get("unhide") or []:
                self.hidden.discard(ent)
            out["hidden"] = sorted(self.hidden)
        return out

    # ----- --schema-all: every entity page of catalog/swis-schema-2026.2.json, with synthetic typed rows -----
    def _schema_all(self, ents: list, props: list):
        import json
        from pathlib import Path
        path = Path(__file__).resolve().parents[2] / "catalog" / "swis-schema-2026.2.json"
        pages = json.loads(path.read_text(encoding="utf-8"))["pages"]
        known = {e["FullName"] for e in ents}
        have = {(p["EntityName"], p["Name"]) for p in props}
        node_ids = [n["NodeID"] for n in self.data["Orion.Nodes"]]
        if_ids = [i["InterfaceID"] for i in self.data["Orion.NPM.Interfaces"]]
        for name, page in sorted(pages.items()):
            fields = page.get("fields") or {}
            bases = page.get("bases") or []
            key = None
            if name not in known:
                abstract = name.startswith("System.")
                indication = "System.Indication" in bases
                ents.append({"FullName": name, "Namespace": name.rsplit(".", 1)[0], "Name": name.rsplit(".", 1)[-1],
                             "BaseType": bases[-1] if bases else "System.Entity", "IsAbstract": abstract,
                             "IsIndication": indication, "CanCreate": False, "CanUpdate": False, "CanDelete": False})
                key = self._synthetic_key(name, fields)
                if not abstract and not indication:
                    self.data[name] = [self._synthetic_row(name, fields, i, node_ids, if_ids) for i in range(5)]
                    for i, r in enumerate(self.data[name]):
                        r["Uri"] = self._uri(name, r, i)
            for f, t in fields.items():
                if (name, f) not in have:
                    props.append({"EntityName": name, "Name": f, "Type": t, "IsNavigable": False, "IsKey": f == key})

    @staticmethod
    def _synthetic_key(name: str, fields: dict) -> str | None:
        short = name.rsplit(".", 1)[-1]
        for k in (f"{short}ID", f"{short.rstrip('s')}ID", "ID"):
            if k in fields:
                return k
        return None

    def _synthetic_row(self, name: str, fields: dict, i: int, node_ids: list, if_ids: list) -> dict:
        now = self._now_local()
        row = {}
        for f, t in fields.items():
            if t == "System.Byte[]":
                row[f] = None                      # binary: not served by the mock
            elif f == "NodeID" and t == "System.Int32":
                row[f] = node_ids[i % len(node_ids)]
            elif f == "InterfaceID" and t == "System.Int32":
                row[f] = if_ids[i % len(if_ids)]
            elif t in ("System.Int32", "System.Int16", "System.Int64", "System.Byte"):
                row[f] = 1000 + i if f.upper().endswith("ID") else self.rnd.randint(0, 100)
            elif t in ("System.Double", "System.Single", "System.Decimal"):
                row[f] = round(self.rnd.uniform(0, 100), 3)
            elif t == "System.Boolean":
                row[f] = i % 2 == 0
            elif t == "System.DateTime":
                row[f] = (now - timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            elif t == "System.Guid":
                row[f] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{name}/{f}/{i}"))
            elif t == "System.String[]":
                row[f] = [f"{f}-{i}-a", f"{f}-{i}-b"]
            elif t == "SolarWinds.InformationService.PropertyBag":
                row[f] = {"Key": f"{f}-{i}"}
            elif t == "System.Char":
                row[f] = "A"
            else:
                row[f] = f"{f}-{i}"
        return row

    def _navigate(self, entity: str, prop: str):
        for s, p, t, cols, *_ in fixtures.NAV_RELATIONS:
            if s.lower() == entity.lower() and p.lower() == prop.lower():
                return t, cols
        raise SwqlError(f"Cannot resolve property {prop} on {entity}")

    @staticmethod
    def _type(v) -> str:
        if isinstance(v, bool):
            return "System.Boolean"
        if isinstance(v, int):
            return "System.Int32"
        if isinstance(v, float):
            return "System.Double"
        if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}T", v):
            return "System.DateTime"
        return "System.String"

    # ----- live events -----
    def _tick_events(self):
        now = self._now_local()
        with self.lock:
            while self.next_event_time <= now:
                n = self.rnd.choice(self.data["Orion.Nodes"])
                et = self.rnd.choice([10, 11, 5000, 5001])
                delay = self.rnd.uniform(2, 12)  # simulated ingestion delay
                row = {
                    "EventID": self.next_event_id, "EventTime": self.next_event_time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "NetworkNode": n["NodeID"], "NetObjectID": n["NodeID"], "NetObjectType": "N", "EventType": et,
                    "Message": f"{n['Caption']}: event type {et}", "Acknowledged": False,
                    "_visible_at": (self.next_event_time + timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%S")}
                if self.rich:                  # same fields and message style as the rich history
                    row.update(Message=f"Node {n['Caption']}: " + {10: "an interface is Down", 11: "an interface is Up",
                                                                   5000: "alert triggered", 5001: "alert reset"}[et],
                               EngineID=n["EngineID"], NetObjectValue=n["Caption"], TimeStamp=None)
                self.data["Orion.Events"].append(self._stamp("Orion.Events", row))
                self.next_event_id += 1
                self.next_event_time += timedelta(seconds=self.rnd.randint(8, 25))

    # ----- SWQL -----
    def _rows(self, entity: str) -> tuple[str, list[dict]]:
        if entity.lower() in ("ncm.transferresults", "cirrus.policyreports"):
            with self.lock:
                self._advance()
        for k in self.data:
            if k.lower() == entity.lower():
                if k in self.hidden:
                    return k, []
                if k in self.series:
                    with self.lock:
                        self._tick_metrics(k)
                if k == "Orion.Events":
                    self._tick_events()
                    now = self._now_local().strftime("%Y-%m-%dT%H:%M:%S")
                    return k, [r for r in self.data[k] if r.get("_visible_at", "") <= now]
                return k, self.data[k]
        if any(e.lower() == entity.lower() for e in fixtures.EXTRA_ENTITIES):
            return entity, []
        raise SwqlError(f"Source entity [{entity}] not found in catalog")

    def _fields(self, entity: str) -> dict[str, str]:
        out = {}
        # Like SWIS: an entity also has the properties of its base types (Metadata.Entity.BaseType chain).
        bases = {e["FullName"]: e.get("BaseType") for e in self.data.get("Metadata.Entity", [])}
        chain, cur = [], entity
        while cur and cur not in chain:
            chain.append(cur)
            cur = bases.get(cur)
        for p in self.data["Metadata.Property"]:
            if p["EntityName"] in chain and not p["IsNavigable"]:
                out.setdefault(p["Name"].lower(), p["Name"])
        if entity.startswith("Metadata."):
            for r in self.data[entity][:1]:
                out.update({k.lower(): k for k in r})
        return out

    def _field(self, fmap, name, entity):
        real = fmap.get(name.lower())
        if not real:
            raise SwqlError(f"Cannot resolve property {name} on {entity}")
        return real

    @staticmethod
    def _value(tok: str, params: dict):
        tok = tok.strip()
        if tok.startswith("@"):
            key = tok[1:]
            for k, v in params.items():
                if k.lower() == key.lower():
                    return v
            raise SwqlError(f"Parameter {tok} was not supplied")
        if tok.startswith("'") and tok.endswith("'"):
            return tok[1:-1].replace("''", "'")
        if tok.lower() in ("true", "false"):
            return tok.lower() == "true"
        try:
            return int(tok)
        except ValueError:
            return float(tok)

    @staticmethod
    def _cmp(a, op, b) -> bool:
        if op in ("ISNULL", "ISNOTNULL"):
            return (a is None) == (op == "ISNULL")
        if a is None:
            return False
        if op == "LIKE":
            return fnmatch.fnmatchcase(str(a).lower(), str(b).replace("%", "*").replace("_", "?").lower())
        if isinstance(a, bool) and isinstance(b, int):
            b = bool(b)
        if isinstance(a, str) and not isinstance(b, str):
            b = str(b)
        if isinstance(b, str) and not isinstance(a, str):
            try:
                b = type(a)(b)
            except (TypeError, ValueError):
                a = str(a)
        try:
            return {"=": a == b, "!=": a != b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
        except TypeError:
            return False

    def _time_slice(self, entity, rows, where, params, strip):
        """Rich mode: narrow the time-sorted metric rows to the DateTime bounds in WHERE with bisect. The full WHERE
        is still applied afterwards, so this only saves work; it never changes the result."""
        idx, lo, hi = self._tidx[entity], 0, len(rows)
        for cond in _split_top(where, " AND "):
            c = _COND.match(cond)
            if not c or strip(c[1]).lower() != "datetime":
                continue
            op = c[2].strip().upper()
            if op not in (">", ">=", "<", "<="):
                continue
            try:
                v = self._value(c[3], params)
            except (SwqlError, ValueError):
                continue
            if not isinstance(v, str):
                continue
            if op == ">":
                lo = max(lo, bisect.bisect_right(idx, v))
            elif op == ">=":
                lo = max(lo, bisect.bisect_left(idx, v))
            elif op == "<":
                hi = min(hi, bisect.bisect_left(idx, v))
            else:
                hi = min(hi, bisect.bisect_right(idx, v))
        return rows[lo:hi] if lo < hi else []

    def query(self, swql: str, params: dict) -> list[dict]:
        m = _Q.match(swql)
        if not m:
            raise SwqlError(f"mismatched input near '{swql[:60]}'")
        entity, rows = self._rows(m["ent"])
        fmap = self._fields(entity)
        ent_alias = (m["alias"] or "").lower()

        def strip(name: str) -> str:
            name = name.strip()
            return name[len(ent_alias) + 1:] if ent_alias and name.lower().startswith(ent_alias + ".") else name

        if m["where"] and entity in self._tidx and len(self._tidx[entity]) == len(rows):
            rows = self._time_slice(entity, rows, m["where"], params, strip)
        if m["where"]:
            for cond in _split_top(m["where"], " AND "):
                c = _COND.match(cond)
                if not c:
                    raise SwqlError(f"Unsupported condition '{cond}'")
                f = self._field(fmap, strip(c[1]), entity)
                op = c[2].strip().upper().replace(" ", "")
                val = None if op in ("ISNULL", "ISNOTNULL") else self._value(c[3], params)
                rows = [r for r in rows if self._cmp(r.get(f), op, val)]
        cols = []
        for item in _split_top(m["cols"], ","):
            parts = re.split(r"\s+AS\s+", item, flags=re.I)
            expr, alias = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else None)
            cols.append((expr, alias))
        fn_cols = [c for c in cols if _FUNC.match(c[0])]
        if fn_cols:
            out = {}
            for expr, alias in cols:
                fm = _FUNC.match(expr)
                if not fm:
                    raise SwqlError("Mixing aggregates and fields needs GROUP BY (not supported by the mock)")
                fn, arg = fm[1].upper(), strip(fm[2])
                if arg == "*":
                    vals = rows
                else:
                    real = self._field(fmap, arg, entity)
                    vals = [r[real] for r in rows if r.get(real) is not None]
                key = alias or f"{fn}"
                out[key] = len(vals) if fn == "COUNT" else (min(vals) if vals and fn == "MIN" else max(vals) if vals else None)
            return [out]
        if m["order"]:
            for term in reversed(_split_top(m["order"], ",")):
                p = term.split()
                real = self._field(fmap, strip(p[0]), entity)
                desc = len(p) > 1 and p[1].upper() == "DESC"
                rows = sorted(rows, key=lambda r: (r.get(real) is None, r.get(real) if r.get(real) is not None else 0), reverse=desc)
        resolved, nav = [], None
        for expr, col_alias in cols:
            up = expr.upper().replace(" ", "")
            if up in ("GETDATE()", "GETUTCDATE()"):
                resolved.append((None, up, col_alias or expr))
                continue
            name = strip(expr)
            if "." in name:                                    # Navigation.Field (one level)
                prop, field = name.split(".", 1)
                target, join = self._navigate(entity, prop)
                if nav and nav[0].lower() != prop.lower():
                    raise SwqlError("The mock supports one navigation property per query")
                nav = (prop, target, join)
                resolved.append(("nav", self._field(self._fields(target), field, target), col_alias or field))
            else:
                resolved.append((None, self._field(fmap, name, entity), col_alias or name))
        pairs = [(r, None) for r in rows]
        if nav:                                                # left join, one row per child
            _, target, join = nav
            _, trows = self._rows(target)
            index: dict = {}
            for t in trows:
                index.setdefault(tuple(t.get(tc) for _, tc in join), []).append(t)
            pairs = [(r, t) for r in rows for t in (index.get(tuple(r.get(sc) for sc, _ in join)) or [None])]
        if m["top"]:
            pairs = pairs[: int(m["top"])]
        if m["r1"]:
            pairs = pairs[int(m["r1"]) - 1: int(m["r2"])]
        out = []
        now_local, now_utc = self._now_local(), self._now_utc()
        for r, t in pairs:
            o = {}
            for kind, real, name in resolved:
                if real == "GETDATE()":
                    o[name] = now_local.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                elif real == "GETUTCDATE()":
                    o[name] = now_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                elif kind == "nav":
                    o[name] = t.get(real) if t else None
                else:
                    o[name] = r.get(real)
            out.append(o)
        return out


def create_app(latency_ms: int = 0, scale: int = 1, schema_all: bool = False, rich: bool = False,
               history_days: int = 7, hourly_days: int = 7) -> Flask:
    app = Flask(__name__)
    app.json.sort_keys = False      # real SWIS returns fields in SELECT order
    app.url_map.merge_slashes = False   # keep the "//" of swis:// URIs in the path
    swis = MockSwis(latency_ms, scale, schema_all, rich, history_days, hourly_days)
    app.config["MOCK_SWIS"] = swis

    def auth_ok():
        a = request.authorization
        return a and a.username in (USER, fixtures.ACTION_USER) and a.password == PASSWORD

    def action_user():
        return request.authorization.username == fixtures.ACTION_USER

    def forbidden(what):
        # Like an Orion account without the right: the read-only account cannot run actions (spec §3.1 #1).
        return jsonify({"Message": f"Access denied: account {USER} may not run {what} (mock)"}), 403

    @app.post(QUERY_PATH)
    def query():
        if not auth_ok():
            return jsonify({"Message": "Authentication failed"}), 401
        body = request.get_json(force=True, silent=True) or {}
        if swis.latency_ms:
            time.sleep(swis.latency_ms / 1000 * random.uniform(0.5, 1.5))
        try:
            return jsonify({"results": swis.query(body.get("query", ""), body.get("parameters") or {})})
        except SwqlError as e:
            return jsonify({"Message": str(e), "ExceptionType": "SolarWinds.Data.Query.ParserException"}), 400

    base = QUERY_PATH.rsplit("/", 1)[0]

    @app.post(base + "/Invoke/<entity>/<verb>")
    def invoke(entity, verb):
        if not auth_ok():
            return jsonify({"Message": "Authentication failed"}), 401
        args = request.get_json(force=True, silent=True)
        if verb.lower() not in MockSwis.READ_VERBS and not action_user() and \
                any(e.lower() == entity.lower() and v.lower() == verb.lower() for e, v in fixtures.VERBS):
            return forbidden(f"{entity}.{verb}")
        try:
            out = swis.invoke(entity, verb, args if isinstance(args, list) else [])
        except SwqlError as e:
            return jsonify({"Message": str(e)}), 400
        if out is None:
            if any(e.lower() == entity.lower() and v.lower() == verb.lower() for e, v in fixtures.VERBS):
                return app.response_class(status=200)        # a verb with no return value: empty body
            return refuse(f"Invoke/{entity}/{verb}")
        return jsonify(out)

    @app.post(base + "/Create/<entity>")
    def create(entity):
        if not auth_ok():
            return jsonify({"Message": "Authentication failed"}), 401
        if not action_user():
            return forbidden(f"Create {entity}")
        try:
            return jsonify(swis.create(entity, request.get_json(force=True, silent=True) or {}))
        except SwqlError as e:
            return jsonify({"Message": str(e)}), 400

    @app.get(base + "/<path:rest>")
    def read(rest):
        if not auth_ok():
            return jsonify({"Message": "Authentication failed"}), 401
        if not rest.lower().startswith("swis://"):
            return refuse(rest)
        row = swis.read(rest)
        if row is None:
            return jsonify({"Message": f"Entity {rest} not found"}), 404
        return jsonify(row)

    @app.route(base + "/<path:rest>", methods=["POST", "PUT", "DELETE"])
    def other(rest):
        if request.method == "POST" and rest.lower().startswith("swis://") and rest.lower().endswith("/customproperties"):
            if not auth_ok():
                return jsonify({"Message": "Authentication failed"}), 401
            if not action_user():
                return forbidden(f"Update {rest}")
            if swis.update(rest, request.get_json(force=True, silent=True) or {}):
                return app.response_class(status=200)        # SWIS answers an update with an empty body
            return jsonify({"Message": f"Entity {rest} not found"}), 404
        return refuse(rest)

    @app.post("/mock/admin/metrics")
    def admin_metrics():
        if not auth_ok():
            return jsonify({"Message": "Authentication failed"}), 401
        return jsonify(swis.change_metrics(request.get_json(force=True, silent=True) or {}))

    @app.post("/mock/admin/alerts")
    def admin_alerts():
        if not auth_ok():
            return jsonify({"Message": "Authentication failed"}), 401
        return jsonify(swis.change_alerts(request.get_json(force=True, silent=True) or {}))

    @app.post("/mock/admin/inventory")
    def admin_inventory():
        if not auth_ok():
            return jsonify({"Message": "Authentication failed"}), 401
        return jsonify(swis.change_inventory(request.get_json(force=True, silent=True) or {}))

    def refuse(rest):
        # A real SWIS would accept these; the mock rejects them so tests prove the client never calls them.
        return jsonify({"Message": f"Mock SWIS: write endpoint {rest} called – the checker must never do this"}), 418

    return app

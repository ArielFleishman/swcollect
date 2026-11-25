# Copied from swcheck swcheck/swis_client.py at commit 0958074 (2026-11-18). Local changes: import paths only.
"""Read-only SWIS REST client.

Three request kinds, none of which changes anything in SolarWinds:
  Query   POST .../Query                   SWQL, SELECT only
  Read    GET  .../swis://host/...         CRUD read of one entity by its Uri
  Invoke  POST .../Invoke/<Entity>/<Verb>  only verbs in READ_ONLY_VERBS, and only after the arguments
                                           match the signature in Metadata.VerbArgument
There is deliberately no create / update / delete method, and every request path is checked before
it is sent, so the tool cannot change anything in SolarWinds even by mistake.

Every logical request (all its pages) is recorded in `client.log`: the request as sent
(credentials redacted), the HTTP status of each page, and the response as SWIS returned it.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote

import requests
import urllib3

from ..config import DEFAULT_BASE_PATH, Network
from .timeutil import utcnow

QUERY_PATH = DEFAULT_BASE_PATH + "/Query"
ERROR_BODY_MAX = 20000

# Verbs that compute on data SolarWinds already holds: no device connection, no new archive rows, no
# recalculation. Verbs that download, execute, upload or recalculate (DownloadConfig, ExecuteScript,
# StartCaching, ...) are left out on purpose. Keys are (entity, verb); matching ignores case.
# Each entry is a retrieval or comparison over stored data per the OrionSDK 2026.2 schema pages
# (https://solarwinds.github.io/OrionSDK/2026.2/schema/<Entity>.html). Left out on purpose: GetNode and
# GetAllConnectionProfiles (they return device credentials), CompareConfigs and ConfigSearch2 (side effects not
# documented), and everything that downloads, executes, uploads or recalculates.
READ_ONLY_VERBS = {
    ("Cirrus.ConfigArchive", "Diff"): "השוואה בין שתי תצורות שכבר שמורות בארכיון NCM. לא פונה לציוד. מחזיר DataTable.",
    ("Cirrus.ConfigArchive", "GetConfigTypes"): "סוגי התצורה הזמינים ב־NCM. ללא ארגומנטים.",
    ("Cirrus.PolicyReports", "GetComplianceDataTable"):
        "פירוט תוצאות תאימות שכבר חושבו (רכיב, כלל, הפרה). לא מפעיל חישוב חדש.",
    ("Cirrus.PolicyReports", "GetPolicyReport"): "הגדרת דוח תאימות (ולפי דגל: המדיניות והכללים שלו). קריאה בלבד.",
    ("Cirrus.Nodes", "GetPageableEosDataTable"): "נתוני End-of-Sale/Support לרכיבי NCM, בעימוד. גישת everyone.",
    ("Cirrus.Nodes", "GetPageableEosRowCount"): "מספר שורות ה־EoS לרכיבי NCM. גישת everyone.",
    ("Orion.AlertSuppression", "GetAlertSuppressionState"): "מצב השתקת התראות לרשימת ישויות. גישת everyone.",
}
# Values of these fields are never stored or shown: SNMP communities, device and SNMPv3 passwords, license keys.
SECRET_FIELD = re.compile(r"(password|passphrase|community|communityreadwrite|authpass|encryptpass|privpass|secret|"
                          r"licensekey|authkey|privkey|token)$", re.I)
REDACTED = "<REDACTED>"

_WITH_ROWS = re.compile(r"\bWITH\s+ROWS\b", re.I)
_LEADING_COMMENTS = re.compile(r"^\s*(--[^\n]*\n\s*)*")
_URI = re.compile(r"^swis://[^/\s?#]+/[^\s?#]+$", re.I)
_REDACTED_HEADERS = {"Content-Type": "application/json", "Accept": "application/json",
                     "Authorization": "Basic <REDACTED>"}


def redact(obj):
    """Replaces the value of every secret-named field, at any depth. The field itself stays, so it is visible that
    the API exposes it (spec §3.4: response bodies are stored redacted)."""
    if isinstance(obj, dict):
        return {k: (REDACTED if isinstance(k, str) and SECRET_FIELD.search(k) and v not in (None, "")
                    and not isinstance(v, bool) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def _iso(d) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SwisError(Exception):
    def __init__(self, message: str, status: int | None = None, query: str | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.query = query
        self.body = body
        self.seq: int | None = None


class ReadOnlyViolation(SwisError):
    pass


class VerbSignatureError(SwisError):
    """The arguments do not match Metadata.VerbArgument, so nothing was sent."""


@dataclass
class QueryStat:
    query: str
    ms: float
    rows: int
    bytes: int
    pages: int = 1
    error: str | None = None
    seq: int | None = None
    kind: str = "query"


@dataclass
class QueryResult:
    rows: list[dict]
    stat: QueryStat
    truncated: bool = False
    seq: int | None = None


def ensure_select(swql: str) -> str:
    body = _LEADING_COMMENTS.sub("", swql).strip()
    if not body.upper().startswith("SELECT"):
        raise ReadOnlyViolation("Only SELECT statements are allowed", query=swql)
    return swql


def ensure_uri(uri: str) -> str:
    uri = (uri or "").strip()
    if not _URI.match(uri) or ".." in uri:
        raise ReadOnlyViolation("A CRUD read needs an entity Uri such as swis://host/Orion/Orion.Nodes/NodeID=1",
                                query=uri)
    return uri


def read_only_verb(entity: str, verb: str) -> tuple[str, str] | None:
    """The allowlist key for entity.verb (any case), or None when the verb is not allowed."""
    for e, v in READ_ONLY_VERBS:
        if e.lower() == (entity or "").lower() and v.lower() == (verb or "").lower():
            return e, v
    return None


def _coerce(type_name: str, value):
    t = (type_name or "").strip()
    if t.endswith("[]"):
        if isinstance(value, str):
            v = value.strip()
            value = json.loads(v) if v.startswith("[") else [x.strip() for x in v.split(",") if x.strip()]
        if not isinstance(value, list):
            value = [value]
        return [_coerce(t[:-2], x) for x in value]
    if not isinstance(value, str):
        return value
    low = t.lower()
    if low.endswith(("int16", "int32", "int64", "byte")):
        return int(value)
    if low.endswith(("double", "single", "decimal")):
        return float(value)
    if low.endswith("boolean"):
        if value.strip().lower() not in ("true", "false", "1", "0"):
            raise ValueError(f"'{value}' is not a boolean")
        return value.strip().lower() in ("true", "1")
    return value


def coerce_args(signature: list[dict], args: list) -> list:
    """Checks args against Metadata.VerbArgument rows (ordered by Position) and converts form strings to
    the declared types. Raises VerbSignatureError, so nothing is sent on a mismatch."""
    required = sum(1 for a in signature if not a.get("IsOptional"))
    if not required <= len(args) <= len(signature):
        names = ", ".join(f"{a.get('Name')}: {a.get('Type')}" for a in signature) or "none"
        raise VerbSignatureError(f"The verb takes {required}–{len(signature)} arguments ({names}); got {len(args)}")
    out = []
    for a, v in zip(signature, args):
        try:
            out.append(_coerce(a.get("Type") or "", v))
        except ValueError as e:          # json.JSONDecodeError is a ValueError
            raise VerbSignatureError(f"Argument {a.get('Name')} ({a.get('Type')}): {e}") from e
    return out


def record_label(rec: dict) -> str:
    """One line for the log index: the SWQL, or the method and target of a Read / Invoke."""
    body = (rec.get("request") or {}).get("body")
    kind = rec.get("kind", "query")
    if kind == "invoke":
        return f"INVOKE {rec.get('target', '')} {json.dumps(body, ensure_ascii=False, default=str)}"
    if kind == "action":
        return f"ACTION {rec.get('target', '')} {json.dumps(body, ensure_ascii=False, default=str)}"
    if kind == "read":
        return f"READ {rec.get('target', '')}"
    return (body or {}).get("query") or ""


def _tabular(data):
    """Rows for the table view: a list of objects, a single object, or a {"results": [...]} envelope."""
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        data = data["results"]
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        return data
    return None


class SwisClient:
    def __init__(self, network: Network, password: str | None, session: requests.Session | None = None,
                 capture: bool = True):
        self.network = network
        self.base = f"{network.scheme}://{network.host}:{network.port}"
        self.base_path = (getattr(network, "base_path", None) or DEFAULT_BASE_PATH).rstrip("/")
        self.query_path = self.base_path + "/Query"
        self.query_url = self.base + self.query_path
        self.session = session or requests.Session()
        self.session.auth = (network.username, password or "")
        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        if network.ca_bundle:
            self.session.verify = network.ca_bundle
        else:
            self.session.verify = network.verify_tls
            if not network.verify_tls:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.stats: list[QueryStat] = []
        self.capture = capture       # False for long-lived pollers, so the log does not grow forever
        self.log: list[dict] = []
        self._seq = 0
        self._signatures: dict[tuple[str, str], list[dict]] = {}

    def invoke_path(self, entity: str, verb: str) -> str:
        return f"{self.base_path}/Invoke/{quote(entity, safe='.')}/{quote(verb, safe='')}"

    def allowed_post_paths(self) -> set[str]:
        """Every POST path this client may send. The actions client (swcheck/actions.py) extends it."""
        return {self.query_path} | {self.invoke_path(e, v) for e, v in READ_ONLY_VERBS}

    def _post(self, path: str, payload) -> requests.Response:
        if path not in self.allowed_post_paths():
            raise ReadOnlyViolation(f"Blocked request to {path}: this tool is read-only")
        return self.session.post(self.base + path, json=payload, timeout=self.network.timeout)

    def _get(self, uri: str) -> requests.Response:
        return self.session.get(f"{self.base}{self.base_path}/{ensure_uri(uri)}", timeout=self.network.timeout)

    @staticmethod
    def _check(resp: requests.Response, what: str):
        """Raises SwisError on an HTTP error; returns the parsed JSON body (None when the body is empty)."""
        status = resp.status_code
        if status >= 400:
            body = resp.text[:ERROR_BODY_MAX]
            if status == 401:
                raise SwisError("Authentication failed (401): check user name and password", 401, what, body)
            if status == 403:
                raise SwisError("Access denied (403): the account lacks permission", 403, what, body)
            msg = resp.text
            try:
                j = resp.json()
                if isinstance(j, dict):
                    msg = j.get("Message") or j.get("message") or msg
            except ValueError:
                pass
            raise SwisError(f"SWIS error {status}: {msg[:800]}", status, what, body)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as e:
            raise SwisError("SWIS returned a response that is not JSON", status, what, resp.text[:ERROR_BODY_MAX]) from e

    def _query_once(self, swql: str, params: dict | None) -> tuple[list[dict], int, int]:
        try:
            resp = self._post(self.query_path, {"query": swql, "parameters": params or {}})
        except requests.RequestException as e:
            raise SwisError(f"Connection failed: {e}", query=swql) from e
        data = self._check(resp, swql) or {}
        return redact(data.get("results", [])), len(resp.content), resp.status_code

    def _page(self, pages: list, swql: str, params: dict | None) -> tuple[list[dict], int]:
        t = time.perf_counter()
        entry = {"query": swql, "status": None, "bytes": 0, "rows": 0, "ms": None}
        pages.append(entry)
        try:
            rows, size, status = self._query_once(swql, params)
        except SwisError as e:
            entry.update(status=e.status, ms=round((time.perf_counter() - t) * 1000, 1), error=str(e))
            raise
        entry.update(status=status, bytes=size, rows=len(rows), ms=round((time.perf_counter() - t) * 1000, 1))
        return rows, size

    def _record(self, swql, params, started, ms, pages, rows, truncated, error=None, body=None) -> int:
        return self._append("query", "Query", "POST", self.query_url, {"query": swql, "parameters": params or {}},
                            started, ms, pages, rows, truncated, error, body)

    def _append(self, kind, target, method, url, req_body, started, ms, pages, rows, truncated=False,
                error=None, error_body=None, raw=None, has_raw=False) -> int:
        self._seq += 1
        if not self.capture:
            return self._seq
        last = pages[-1] if pages else {}
        response = {
            "status": last.get("status"),
            "rows": len(rows) if rows is not None else None,
            "bytes": sum(p.get("bytes") or 0 for p in pages),
            "truncated": truncated,
            "error": error,
            "error_body": error_body,
            "results": rows,
        }
        if has_raw:
            response["body"] = raw          # Read / Invoke: the JSON exactly as SWIS returned it
        self.log.append({
            "seq": self._seq,
            # Tracking fields of our system (spec §3.4): not fields SWIS returns. NodeID is only unique per instance.
            "request_id": str(uuid.uuid4()),
            "instance": self.base,
            "kind": kind,
            "target": target,
            "started_utc": _iso(started),
            "finished_utc": _iso(utcnow()),
            "ms": round(ms, 1),
            "network": self.network.name,
            "request": {"method": method, "url": url, "headers": dict(_REDACTED_HEADERS), "body": req_body,
                        "user": self.network.username},
            "pages": pages,
            "response": response,
        })
        return self._seq

    def _single(self, kind, target, method, url, req_body, send) -> tuple[object, int]:
        """Sends one Read / Invoke request and logs it. Returns (parsed body, log seq)."""
        started, t0 = utcnow(), time.perf_counter()
        page = {"query": f"{method} {url}", "status": None, "bytes": 0, "rows": 0, "ms": None}
        try:
            try:
                resp = send()
            except requests.RequestException as e:
                raise SwisError(f"Connection failed: {e}", query=target) from e
            page.update(status=resp.status_code, bytes=len(resp.content))
            data = redact(self._check(resp, target))
        except SwisError as e:
            ms = (time.perf_counter() - t0) * 1000
            page.update(status=page["status"] or e.status, ms=round(ms, 1), error=str(e))
            e.seq = self._append(kind, target, method, url, req_body, started, ms, [page], None,
                                 error=str(e), error_body=e.body)
            self.stats.append(QueryStat(f"{method} {target}", ms, 0, 0, error=str(e), seq=e.seq, kind=kind))
            raise
        ms = (time.perf_counter() - t0) * 1000
        rows = _tabular(data)
        page.update(ms=round(ms, 1), rows=len(rows or []))
        seq = self._append(kind, target, method, url, req_body, started, ms, [page], rows, raw=data, has_raw=True)
        self.stats.append(QueryStat(f"{method} {target}", ms, len(rows or []), page["bytes"], seq=seq, kind=kind))
        return data, seq

    def query(self, swql: str, params: dict | None = None) -> QueryResult:
        """Single request, no paging."""
        ensure_select(swql)
        started, t0, pages = utcnow(), time.perf_counter(), []
        try:
            rows, size = self._page(pages, swql, params)
        except SwisError as e:
            ms = (time.perf_counter() - t0) * 1000
            e.seq = self._record(swql, params, started, ms, pages, [], False, str(e), e.body)
            self.stats.append(QueryStat(swql, ms, 0, 0, error=str(e), seq=e.seq))
            raise
        ms = (time.perf_counter() - t0) * 1000
        seq = self._record(swql, params, started, ms, pages, rows, False)
        stat = QueryStat(swql, ms, len(rows), size, seq=seq)
        self.stats.append(stat)
        return QueryResult(rows, stat, seq=seq)

    def query_all(self, swql: str, params: dict | None = None, page_size: int = 5000,
                  max_rows: int = 50000) -> QueryResult:
        """Pages with WITH ROWS a TO b until a short page or max_rows."""
        ensure_select(swql)
        if _WITH_ROWS.search(swql):
            return self.query(swql, params)
        rows: list[dict] = []
        total_bytes = 0
        truncated = False
        started, t0, pages = utcnow(), time.perf_counter(), []
        start = 1
        try:
            while True:
                end = min(start + page_size - 1, max_rows)
                page, size = self._page(pages, f"{swql} WITH ROWS {start} TO {end}", params)
                total_bytes += size
                rows.extend(page)
                if len(page) < end - start + 1:
                    break
                if end >= max_rows:
                    truncated = True
                    break
                start = end + 1
        except SwisError as e:
            ms = (time.perf_counter() - t0) * 1000
            e.seq = self._record(swql, params, started, ms, pages, rows, truncated, str(e), e.body)
            self.stats.append(QueryStat(swql, ms, len(rows), total_bytes, len(pages), str(e), seq=e.seq))
            raise
        ms = (time.perf_counter() - t0) * 1000
        seq = self._record(swql, params, started, ms, pages, rows, truncated)
        stat = QueryStat(swql, ms, len(rows), total_bytes, len(pages), seq=seq)
        self.stats.append(stat)
        return QueryResult(rows, stat, truncated, seq=seq)

    # ---------- CRUD read ----------
    def read_uri(self, uri: str) -> tuple[object, int]:
        """GET one entity by its Uri. Returns (its properties as SWIS returned them, log seq)."""
        uri = ensure_uri(uri)
        return self._single("read", uri, "GET", f"{self.base}{self.base_path}/{uri}", None, lambda: self._get(uri))

    # ---------- read-only verbs ----------
    def verb_signature(self, entity: str, verb: str) -> list[dict]:
        """Metadata.VerbArgument rows for the verb, ordered by Position. Raises VerbSignatureError when the
        verb is not in Metadata.Verb on this server. Read once per client."""
        if (entity, verb) in self._signatures:
            return self._signatures[(entity, verb)]
        params = {"e": entity, "v": verb}
        found = self.query("SELECT EntityName, Name FROM Metadata.Verb WHERE EntityName = @e AND Name = @v", params)
        if not found.rows:
            raise VerbSignatureError(f"{entity}.{verb} is not in Metadata.Verb on this server", query=found.stat.query)
        base = ("SELECT EntityName, VerbName, Position, Name, Type{} FROM Metadata.VerbArgument "
                "WHERE EntityName = @e AND VerbName = @v ORDER BY Position")
        try:
            rows = self.query(base.format(", IsOptional"), params).rows
        except SwisError:
            rows = self.query(base.format(""), params).rows      # SWIS versions without IsOptional
        self._signatures[(entity, verb)] = rows
        return rows

    def invoke(self, entity: str, verb: str, args: list) -> tuple[object, int]:
        """Invokes a verb from READ_ONLY_VERBS after checking args against Metadata.VerbArgument.
        Returns (the response as SWIS returned it, log seq of the invoke)."""
        key = read_only_verb(entity, verb)
        if not key:
            raise ReadOnlyViolation(f"{entity}.{verb} is not in the read-only verb list; nothing was sent")
        entity, verb = key
        typed = coerce_args(self.verb_signature(entity, verb), list(args))
        path = self.invoke_path(entity, verb)
        return self._single("invoke", f"{entity}.{verb}", "POST", self.base + path, typed,
                            lambda: self._post(path, typed))

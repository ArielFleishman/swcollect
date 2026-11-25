"""Loki HTTP sink: push (POST /loki/api/v1/push, JSON) and query (query_range) for verification.

Entries are pushed per stream in time order (Loki 3 accepts out-of-order writes only within a window behind a
stream's newest entry). A 204 is the confirmation the caller waits for before moving a watermark.
If Loki rejects a batch (4xx), the batch is split until each rejected entry is isolated; accepted entries stay
accepted and the rejected ones are returned with Loki's reason, so the caller can keep them (nothing is dropped).
Connection errors, timeouts and 5xx are retried with backoff; if they persist, LokiError is raised.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import requests


class LokiError(Exception):
    pass


@dataclass
class PushResult:
    accepted: int = 0
    rejected: list = field(default_factory=list)    # [(labels, ts_ns, line, metadata, reason)]
    requests: int = 0


class Loki:
    def __init__(self, url: str, session: requests.Session | None = None, timeout: int = 30,
                 batch_entries: int = 1000, max_batch_bytes: int = 3_000_000, retries: int = 3, sleep=time.sleep):
        self.url = url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.batch_entries = batch_entries
        self.max_batch_bytes = max_batch_bytes
        self.retries = retries
        self.sleep = sleep

    # ---------------- push ----------------
    def push(self, entries: list[tuple[dict, int, str, dict]]) -> PushResult:
        """entries: (labels, timestamp in ns, line, structured metadata). Returns accepted / rejected."""
        res = PushResult()
        streams: dict[tuple, list] = {}
        for labels, ts, line, meta in entries:
            streams.setdefault(tuple(sorted(labels.items())), []).append((ts, line, meta))
        for key, vals in streams.items():
            vals.sort(key=lambda v: v[0])
            batch, size = [], 0
            for v in vals:
                n = len(v[1]) + 100
                if batch and (len(batch) >= self.batch_entries or size + n > self.max_batch_bytes):
                    self._send(dict(key), batch, res)
                    batch, size = [], 0
                batch.append(v)
                size += n
            if batch:
                self._send(dict(key), batch, res)
        return res

    def _body(self, labels: dict, vals: list) -> dict:
        return {"streams": [{"stream": labels, "values": [
            [str(ts), line, {k: str(v) for k, v in meta.items() if v is not None}] if meta else [str(ts), line]
            for ts, line, meta in vals]}]}

    def _post(self, labels: dict, vals: list, res: PushResult) -> tuple[int, str]:
        for i in range(self.retries):
            res.requests += 1
            try:
                r = self.session.post(f"{self.url}/loki/api/v1/push", json=self._body(labels, vals), timeout=self.timeout)
            except requests.RequestException as e:
                err = f"connection: {e}"
            else:
                if r.status_code < 500:
                    return r.status_code, r.text.strip()[:500]
                err = f"HTTP {r.status_code}: {r.text.strip()[:200]}"
            if i < self.retries - 1:
                self.sleep((1, 3, 9)[min(i, 2)])
        raise LokiError(f"Loki push failed after {self.retries} attempts: {err}")

    def _send(self, labels: dict, vals: list, res: PushResult):
        status, text = self._post(labels, vals, res)
        if status in (200, 204):
            res.accepted += len(vals)
            return
        if len(vals) == 1:                       # isolated: this entry is rejected, keep it with the reason
            ts, line, meta = vals[0]
            res.rejected.append((labels, ts, line, meta, f"HTTP {status}: {text}"))
            return
        mid = len(vals) // 2                     # a 4xx batch: split to keep what Loki accepts
        self._send(labels, vals[:mid], res)
        self._send(labels, vals[mid:], res)

    # ---------------- query (verification) ----------------
    def query_range(self, query: str, start_ns: int, end_ns: int, limit: int = 5000) -> list[tuple[int, str, dict]]:
        """All entries of a LogQL stream selector between start and end, oldest first. Loki limits one query's
        range (max_query_length, 30d1h by default), so longer ranges are read in 29-day windows."""
        window = 29 * 86400 * 10**9
        out = []
        a = start_ns
        while a < end_ns:
            out += self._query_window(query, a, min(a + window, end_ns), limit)
            a += window
        seen, uniq = set(), []
        for e in out:
            k = (e[0], e[1])
            if k not in seen:
                seen.add(k)
                uniq.append(e)
        return uniq

    def _query_window(self, query: str, start_ns: int, end_ns: int, limit: int) -> list[tuple[int, str, dict]]:
        out, cursor = [], start_ns
        while True:
            r = self.session.get(f"{self.url}/loki/api/v1/query_range", timeout=self.timeout, params={
                "query": query, "start": str(cursor), "end": str(end_ns), "limit": str(limit), "direction": "forward"})
            if r.status_code != 200:
                raise LokiError(f"query_range HTTP {r.status_code}: {r.text[:300]}")
            page = []
            for st in r.json()["data"]["result"]:
                for v in st["values"]:
                    page.append((int(v[0]), v[1], dict(st["stream"])))
            page.sort(key=lambda x: x[0])
            out += page
            if len(page) < limit:
                break
            cursor = page[-1][0] + 1
        seen, uniq = set(), []
        for e in out:
            k = (e[0], e[1])
            if k not in seen:
                seen.add(k)
                uniq.append(e)
        return uniq

    def ready(self) -> bool:
        try:
            return self.session.get(f"{self.url}/ready", timeout=self.timeout).status_code == 200
        except requests.RequestException:
            return False


def line_json(row: dict, omit: list[str] | None = None) -> str:
    return json.dumps({k: v for k, v in row.items() if k not in (omit or [])}, ensure_ascii=False, default=str)

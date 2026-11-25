"""Push vs poll for alerts (plan §5; spec stage A §3.1 #10, as in swcheck's listener reconciliation).

The Orion alert action pushes a message per trigger/reset; the safety-net poll reads Orion.AlertHistory. Each side
gets a row in sw_meta.alert_reconcile and they are paired by a key the alert action writes into the body
(collector.yaml push.key_regex → Orion.AlertHistory.<history_field>), the earliest unused push at or after the
history time (minus a clock-skew tolerance). No EventType meaning is assumed.
  matched    both sides, with the push latency
  poll_only  a history row with no push (yet): the push was lost or not configured; the poll filled it into Loki
  push_only  a message with no history row (yet), or with no key
Statuses converge: a row that is poll_only or push_only now is paired in a later run when its partner arrives.
History times are corrected for the Orion clock's skew against ours (GETUTCDATE() vs the database clock), so a server
clock that runs ahead or behind does not break the pairing or the latency (as in swcheck's live-events check).
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta, timezone

import psycopg

from .config import Network
from .ddl import literal
from .sinks.loki import Loki, LokiError
from .swis.client import SwisClient, SwisError
from .swis.timeutil import parse_dt


def push_entry(net: Network, message_id: int, received_at: datetime, body: str, sha: str, dup) -> tuple:
    labels = {"job": "swcollect", "network": net.stable_network_id, "instance": net.instance_key, "source": "push",
              "kind": "alert_push"}
    meta = {"message_id": message_id, "sha256": sha, "duplicate_of": dup}
    return labels, int(received_at.timestamp() * 1e9), body, meta


def resend_pushes(conn: psycopg.Connection, loki: Loki, net: Network, inst: int | None = None) -> int:
    """Messages stored while Loki was unreachable are sent now (the message itself was kept in PostgreSQL)."""
    rows = conn.execute("""SELECT message_id, received_at, body, body_sha256, duplicate_of FROM sw_meta.push_message
                           WHERE (instance_id = %s OR (instance_id IS NULL AND network = %s)) AND NOT loki_pushed
                           ORDER BY message_id""", (inst, net.name)).fetchall()
    if not rows:
        return 0
    try:
        pr = loki.push([push_entry(net, *r) for r in rows])
    except LokiError:
        return 0
    if not pr.rejected:
        conn.execute("UPDATE sw_meta.push_message SET loki_pushed = true WHERE message_id = ANY(%s)",
                     ([r[0] for r in rows],))
    return pr.accepted


def key_of(body: str, regex: str) -> str | None:
    m = re.search(regex, body or "")
    return m.group(1) if m else None


def reconcile(conn: psycopg.Connection, client: SwisClient, net: Network, inst: int, cfg: dict, offset: int | None,
              run_id: int, history_rule: str = "server_local") -> dict:
    regex = cfg.get("key_regex") or r"AlertObjectID\D{0,3}(\d+)"
    field = cfg.get("history_field") or "AlertObjectID"
    tol = timedelta(seconds=int(cfg.get("tolerance_seconds", 120)))
    hours = int(cfg.get("match_within_hours", 24))
    out = {"history_new": 0, "push_new": 0, "matched_now": 0}
    # 1. history side (bounded by time; the events job already sent these rows to Loki)
    try:
        clk = client.query("SELECT TOP 1 GETUTCDATE() AS U, GETDATE() AS L FROM Orion.Engines").rows[0]
        ours = conn.execute("SELECT now()").fetchone()[0]
        clock = clk["L"]
        skew = parse_dt(clk["U"]).replace(tzinfo=timezone.utc) - ours        # + : the Orion clock is ahead
        out["server_clock_skew_sec"] = round(skew.total_seconds(), 1)
        floor = (parse_dt(clock) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        hist = client.query_all(f"SELECT e.AlertHistoryID, e.{field} AS K, e.TimeStamp FROM Orion.AlertHistory e "
                                f"WHERE e.TimeStamp >= {literal(floor)} ORDER BY e.AlertHistoryID",
                                max_rows=1_000_000).rows
    except (SwisError, IndexError, KeyError) as e:
        return {"error": f"Orion.AlertHistory not readable for reconciliation: {e}"}
    with conn.transaction():
        for h in hist:
            t = parse_dt(h.get("TimeStamp"))
            if t is not None and history_rule == "server_local" and offset is not None:
                t = t - timedelta(minutes=offset)
            poll_at = (t.replace(tzinfo=timezone.utc) - skew) if t else None       # on our clock
            cur = conn.execute("""INSERT INTO sw_meta.alert_reconcile (instance_id, alert_key, alert_history_id, status,
                                      poll_at, key_value, detail) VALUES (%s, %s, %s, 'poll_only', %s, %s, %s)
                                  ON CONFLICT (instance_id, alert_key) DO NOTHING""",
                               (inst, f"h:{h['AlertHistoryID']}", h["AlertHistoryID"], poll_at,
                                None if h.get("K") is None else str(h["K"]),
                                f"TimeStamp as received {h.get('TimeStamp')}; server clock skew "
                                f"{skew.total_seconds():+.0f} s corrected"))
            out["history_new"] += cur.rowcount
        # 2. push side (first copies only; a duplicate body is not a new alert)
        # This instance's messages; one stored before the instance existed (instance_id NULL) is adopted by network.
        for mid, received, body in conn.execute(
                """SELECT m.message_id, m.received_at, m.body FROM sw_meta.push_message m
                   WHERE (m.instance_id = %s OR (m.instance_id IS NULL AND m.network = %s))
                   AND m.duplicate_of IS NULL AND NOT EXISTS (SELECT 1 FROM sw_meta.alert_reconcile r
                   WHERE r.instance_id = %s AND r.alert_key = 'p:' || m.message_id)""", (inst, net.name, inst)).fetchall():
            k = key_of(body, regex)
            conn.execute("UPDATE sw_meta.push_message SET key_value = %s, instance_id = %s WHERE message_id = %s",
                         (k, inst, mid))
            conn.execute("""INSERT INTO sw_meta.alert_reconcile (instance_id, alert_key, push_message_id, status, push_at,
                                key_value, detail) VALUES (%s, %s, %s, 'push_only', %s, %s, %s)""",
                         (inst, f"p:{mid}", mid, received, k, None if k else f"no key matched {regex}"))
            out["push_new"] += 1
        # 3. pair
        hs = conn.execute("""SELECT alert_key, key_value, poll_at, alert_history_id FROM sw_meta.alert_reconcile
                             WHERE instance_id = %s AND status = 'poll_only' AND key_value IS NOT NULL
                             ORDER BY poll_at, alert_key""", (inst,)).fetchall()
        ps = conn.execute("""SELECT alert_key, key_value, push_at, push_message_id FROM sw_meta.alert_reconcile
                             WHERE instance_id = %s AND status = 'push_only' AND key_value IS NOT NULL
                             ORDER BY push_at, alert_key""", (inst,)).fetchall()
        # A push follows its event: each push (oldest first) takes the most recent unpaired history row with the
        # same key at or before it (plus the tolerance). An older row of the same alert (its trigger, when the push
        # is a reset) stays unpaired instead of getting a wrong, hours-long latency.
        free: dict[str, list] = {}
        for h in hs:
            free.setdefault(h[1], []).append(h)
        for pkey, k, push_at, mid in ps:
            cands = [h for h in free.get(k, []) if h[2] is None or h[2] <= push_at + tol]
            if not cands:
                continue
            hkey, _k, poll_at, hid = max(cands, key=lambda h: (h[2] is not None, h[2]))
            free[k].remove((hkey, _k, poll_at, hid))
            p = (pkey, k, push_at, mid)
            lat = (p[2] - poll_at).total_seconds() if poll_at else None
            conn.execute("""UPDATE sw_meta.alert_reconcile SET status = 'matched', push_message_id = %s, push_at = %s,
                                latency_sec = %s, updated_at = now() WHERE instance_id = %s AND alert_key = %s""",
                         (p[3], p[2], lat, inst, hkey))
            conn.execute("""UPDATE sw_meta.alert_reconcile SET status = 'matched', alert_history_id = %s, poll_at = %s,
                                latency_sec = %s, updated_at = now() WHERE instance_id = %s AND alert_key = %s""",
                         (hid, poll_at, lat, inst, p[0]))
            out["matched_now"] += 1
    counts = dict(conn.execute("""SELECT status, count(*) FROM sw_meta.alert_reconcile WHERE instance_id = %s
                                  AND alert_key LIKE 'h:%%' GROUP BY status""", (inst,)).fetchall())
    counts["push_only"] = conn.execute("""SELECT count(*) FROM sw_meta.alert_reconcile WHERE instance_id = %s
                                          AND alert_key LIKE 'p:%%' AND status = 'push_only'""", (inst,)).fetchone()[0]
    lats = sorted(r[0] for r in conn.execute(
        """SELECT latency_sec FROM sw_meta.alert_reconcile WHERE instance_id = %s AND alert_key LIKE 'h:%%'
           AND status = 'matched' AND latency_sec IS NOT NULL""", (inst,)).fetchall())
    out.update(history={"matched": counts.get("matched", 0), "poll_only": counts.get("poll_only", 0)},
               push_only=counts["push_only"],
               latency_sec={"min": lats[0], "median": statistics.median(lats), "max": lats[-1]} if lats else None)
    return out

"""Push receiver for the Orion alert action "Send a GET or POST Request to a Web Server" (plan §5).

POST|PUT|GET /hook/<network>
  token     when SWCOLLECT_HOOK_TOKEN is set, the X-Swcollect-Token header, or the `token` query parameter when the
            alert action cannot set headers, must match (401 otherwise). The token is never stored.
  stored    the message exactly as received in sw_meta.push_message (body, method, headers without secrets,
            query args, sha256); an identical body seen before is stored with duplicate_of
  Loki      one entry, kind alert_push, at the receive time; if Loki is down the message stays in PostgreSQL with
            loki_pushed = false and the next `swcollect events` run sends it
  answer    200 once PostgreSQL has it. If PostgreSQL is down the answer is 503, so the alert action logs a
            failure; the safety-net poll of Orion.AlertHistory still brings the alert in.
GET /hook/health  readiness of PostgreSQL and Loki.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import psycopg
from flask import Flask, jsonify, request

from .config import Settings
from .reconcile import push_entry
from .sinks.loki import Loki, LokiError

SECRET_HEADERS = {"authorization", "cookie", "x-swcollect-token", "proxy-authorization"}


def create_app(settings: Settings, loki: Loki | None = None, connect=None) -> Flask:
    app = Flask(__name__)
    loki = loki or Loki(settings.loki_url)
    connect = connect or (lambda: psycopg.connect(settings.pg_dsn, autocommit=True, connect_timeout=5))

    @app.get("/hook/health")
    def health():
        try:
            with connect() as c:
                c.execute("SELECT 1")
            pg = True
        except psycopg.Error:
            pg = False
        return jsonify({"postgres": pg, "loki": loki.ready()}), (200 if pg else 503)

    @app.route("/hook/<network>", methods=["POST", "PUT", "GET"])
    def hook(network):
        given = request.headers.get("X-Swcollect-Token") or request.args.get("token") or ""
        if settings.hook_token and not hmac.compare_digest(given, settings.hook_token):
            return jsonify({"error": "bad or missing X-Swcollect-Token"}), 401
        if network not in settings.networks:
            return jsonify({"error": f"unknown network {network}"}), 404
        net = settings.networks[network]
        raw = request.get_data(cache=False) or b""
        body = raw.decode("utf-8", errors="replace")
        args = {k: v for k, v in request.args.to_dict(flat=False).items() if k != "token"}
        sha = hashlib.sha256(raw + json.dumps(args, sort_keys=True).encode()).hexdigest()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in SECRET_HEADERS}
        headers["_method"], headers["_remote_addr"], headers["_args"] = request.method, request.remote_addr, args
        try:
            with connect() as c:
                inst = c.execute("SELECT instance_id FROM sw_meta.instance WHERE instance_key = %s",
                                 (net.instance_key,)).fetchone()
                dup = c.execute("SELECT min(message_id) FROM sw_meta.push_message WHERE network = %s AND "
                                "body_sha256 = %s", (network, sha)).fetchone()[0]
                mid, received = c.execute(
                    """INSERT INTO sw_meta.push_message (instance_id, network, body_sha256, duplicate_of, headers, body)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING message_id, received_at""",
                    (inst[0] if inst else None, network, sha, dup, json.dumps(headers), body)).fetchone()
                pushed = False
                try:
                    pr = loki.push([push_entry(net, mid, received, body, sha, dup)])
                    pushed = pr.accepted == 1
                except LokiError:
                    pass
                if pushed:
                    c.execute("UPDATE sw_meta.push_message SET loki_pushed = true WHERE message_id = %s", (mid,))
        except psycopg.Error as e:
            return jsonify({"error": f"not stored: {str(e).strip().splitlines()[0]}"}), 503
        return jsonify({"message_id": mid, "duplicate_of": dup, "loki": pushed})

    return app

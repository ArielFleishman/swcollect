"""Readiness check: can we reach SWIS (read-only), PostgreSQL + TimescaleDB and Loki? Sends no writes to SWIS."""
from __future__ import annotations

import time

import requests

from .config import Settings
from .swis.client import SwisClient, SwisError


def _step(name, fn) -> dict:
    t = time.perf_counter()
    try:
        detail = fn()
        return {"check": name, "ok": True, "detail": detail, "ms": round((time.perf_counter() - t) * 1000)}
    except Exception as e:  # noqa: BLE001 – every failure is reported, none is fatal
        return {"check": name, "ok": False, "detail": f"{type(e).__name__}: {e}",
                "ms": round((time.perf_counter() - t) * 1000)}


def check_swis(settings: Settings, network: str) -> list[dict]:
    net = settings.network(network)
    client = SwisClient(net, net.resolve_password(), capture=False)

    def clock():
        r = client.query("SELECT TOP 1 GETUTCDATE() AS U, GETDATE() AS L FROM Orion.Engines").rows
        if not r:
            raise SwisError("Orion.Engines returned no rows (permissions?)")
        return f"server UTC {r[0]['U']}, server local {r[0]['L']}"

    def entities():
        rows = client.query("SELECT COUNT(FullName) AS N FROM Metadata.Entity").rows
        return f"{rows[0]['N']} entities in Metadata.Entity"

    return [_step(f"SWIS {net.url}: clock", clock), _step("SWIS: Metadata.Entity", entities)]


def check_postgres(settings: Settings) -> list[dict]:
    def version():
        import psycopg
        with psycopg.connect(settings.pg_dsn, connect_timeout=10) as conn:
            pg = conn.execute("SHOW server_version").fetchone()[0]
            ts = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'").fetchone()
            schemas = [r[0] for r in conn.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'sw!_%' ESCAPE '!' ORDER BY 1").fetchall()]
        if not ts:
            raise RuntimeError(f"PostgreSQL {pg} is up, but the timescaledb extension is not installed")
        return f"PostgreSQL {pg}, TimescaleDB {ts[0]}, schemas: {', '.join(schemas) or 'none'}"

    return [_step("PostgreSQL + TimescaleDB", version)]


def check_loki(settings: Settings) -> list[dict]:
    def ready():
        r = requests.get(f"{settings.loki_url}/ready", timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text.strip()[:200]}")
        b = requests.get(f"{settings.loki_url}/loki/api/v1/status/buildinfo", timeout=10)
        version = b.json().get("version") if b.ok else "?"
        return f"Loki {version} ready at {settings.loki_url}"

    return [_step("Loki", ready)]


def run(settings: Settings, network: str | None) -> list[dict]:
    out = []
    if network:
        out += check_swis(settings, network)
    out += check_postgres(settings)
    out += check_loki(settings)
    return out

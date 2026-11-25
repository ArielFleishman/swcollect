"""Discovery: everything the installed SWIS exposes, classified for collection (plan §1).

Reads Metadata.Entity, Metadata.Property and Metadata.Relationship from the live server (not from checks.yaml),
classifies every entity as inventory / metrics / events / excluded using catalog/collector.yaml, settles each
entity's identity (Metadata IsKey, else the smallest column set unique on a sample, else the row hash), and
writes catalog/discovered/<network_id>.yaml. Nothing is skipped silently: an excluded entity keeps its reason.
Read-only: SELECT queries only.
"""
from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import CATALOG_DIR, Network
from .ddl import column_name, pg_type, table_name
from .swis.client import SwisClient, SwisError
from .swis.timeutil import utcnow

DOC_URL = "https://solarwinds.github.io/OrionSDK/2026.2/schema/{}.html"
DOC_SCHEMA = CATALOG_DIR / "swis-schema-2026.2.json"
NO_LIMIT = 50_000_000


def load_rules(path: Path | None = None) -> dict:
    return yaml.safe_load((path or CATALOG_DIR / "collector.yaml").read_text(encoding="utf-8")) or {}


def load_doc_schema() -> dict:
    """The OrionSDK 2026.2 schema pages bundled with the repo (offline copy of the documentation)."""
    if not DOC_SCHEMA.exists():
        return {"entities": [], "pages": {}}
    return json.loads(DOC_SCHEMA.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------------------------
# Reading the server's schema
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class Schema:
    entities: dict[str, dict]
    properties: dict[str, list[dict]]
    relationships: list[dict]
    notes: list[str] = field(default_factory=list)
    queries: int = 0


def _first_ok(client: SwisClient, variants: list[str], notes: list[str], what: str) -> list[dict]:
    last = None
    for i, swql in enumerate(variants):
        try:
            res = client.query_all(swql, page_size=5000, max_rows=NO_LIMIT)
            if res.truncated:
                raise SwisError(f"{what}: result truncated")
            if i:
                notes.append(f"{what}: the server rejected richer variants; used: {swql}")
            return res.rows
        except SwisError as e:
            last = e
    notes.append(f"{what}: every variant failed ({last})")
    return []


def read_schema(client: SwisClient) -> Schema:
    notes: list[str] = []
    ents = _first_ok(client, [
        "SELECT e.FullName, e.BaseType, e.IsAbstract, e.IsIndication FROM Metadata.Entity e ORDER BY e.FullName",
        "SELECT e.FullName FROM Metadata.Entity e ORDER BY e.FullName",
    ], notes, "Metadata.Entity")
    props = _first_ok(client, [
        "SELECT p.EntityName, p.Name, p.Type, p.IsNavigable, p.IsKey, p.IsInherited FROM Metadata.Property p "
        "ORDER BY p.EntityName, p.Name",
        "SELECT p.EntityName, p.Name, p.Type, p.IsNavigable, p.IsKey FROM Metadata.Property p "
        "ORDER BY p.EntityName, p.Name",
        "SELECT p.EntityName, p.Name, p.Type, p.IsNavigable FROM Metadata.Property p ORDER BY p.EntityName, p.Name",
    ], notes, "Metadata.Property")
    rels = _first_ok(client, [
        "SELECT r.SourceType, r.TargetType, r.SourcePropertyName, r.TargetPropertyName, r.SourceCardinalityMin, "
        "r.SourceCardinalityMax, r.TargetCardinalityMin, r.TargetCardinalityMax FROM Metadata.Relationship r "
        "ORDER BY r.SourceType, r.TargetType",
        "SELECT r.SourceType, r.TargetType, r.SourcePropertyName FROM Metadata.Relationship r "
        "ORDER BY r.SourceType, r.TargetType",
        "SELECT r.SourceType, r.TargetType FROM Metadata.Relationship r ORDER BY r.SourceType, r.TargetType",
    ], notes, "Metadata.Relationship")
    by_entity: dict[str, list[dict]] = {}
    for p in props:
        by_entity.setdefault(p["EntityName"], []).append(p)
    return Schema({e["FullName"]: e for e in ents}, by_entity, rels, notes, queries=3)


def all_properties(schema: Schema, entity: str) -> list[dict]:
    """The entity's own properties plus those of its base types (Metadata.Entity.BaseType chain), by name."""
    out: dict[str, dict] = {}
    seen, cur = set(), entity
    while cur and cur not in seen:
        seen.add(cur)
        for p in schema.properties.get(cur, []):
            out.setdefault(p["Name"], p)
        cur = (schema.entities.get(cur) or {}).get("BaseType")
    return list(out.values())


# ---------------------------------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------------------------------
def classify(name: str, ent: dict, fields: dict[str, str], rules: dict) -> tuple[str, str, dict | None]:
    """(class, reason, event rule). class: excluded | events | metrics | inventory."""
    ex = rules.get("exclude") or {}
    if ex.get("abstract", True) and ent.get("IsAbstract"):
        return "excluded", "abstract base type (IsAbstract)", None
    if ex.get("indication", True) and ent.get("IsIndication"):
        return "excluded", "indication (subscription event type, not queryable data)", None
    for prefix, why in (ex.get("prefixes") or {}).items():
        if name.startswith(prefix):
            return "excluded", why, None
    if name in (ex.get("entities") or {}):
        return "excluded", ex["entities"][name], None
    ev = (rules.get("events") or {}).get(name)
    if ev is not None:
        return "events", f"event source (kind {ev.get('kind', 'event')})", ev
    if not fields:
        return "excluded", "no scalar properties in Metadata.Property", None
    if time_field_of(fields, rules):
        m = rules.get("metrics") or {}
        marker = any(f in fields for f in m.get("marker_fields") or [])
        pattern = m.get("name_pattern")
        named = bool(pattern and re.search(pattern, name.rsplit(".", 1)[-1]))
        if marker or named:
            return "metrics", "time series: " + ("has " + "/".join(
                f for f in m.get("marker_fields") or [] if f in fields) if marker else f"name matches {pattern}"), None
    return "inventory", "state / inventory (default class)", None


def time_field_of(fields: dict[str, str], rules: dict) -> str | None:
    dt = [f for f, t in fields.items() if t in ("System.DateTime", "System.DateTimeOffset")]
    for cand in (rules.get("metrics") or {}).get("time_fields") or []:
        for f in dt:
            if f.lower() == cand.lower():
                return f
    return None


# ---------------------------------------------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------------------------------------------
def key_candidates(entity: str, fields: dict[str, str], rules: dict, time_field: str | None) -> list[str]:
    suffixes = tuple((rules.get("keys") or {}).get("candidate_suffixes") or ["ID", "Id", "Index", "Guid"])
    short = entity.rsplit(".", 1)[-1]
    preferred = {f"{short}ID".lower(), f"{short.rstrip('s')}ID".lower(), "id"}
    cands = [f for f, t in fields.items() if f.endswith(suffixes) and f != time_field
             and pg_type(t)[0] in ("smallint", "integer", "bigint", "uuid", "text")]
    return sorted(cands, key=lambda f: (f.lower() not in preferred, f))


def _unique(rows: list[dict], cols: tuple[str, ...]) -> bool:
    seen = set()
    for r in rows:
        k = tuple(r.get(c) for c in cols)
        if any(v is None for v in k) or k in seen:
            return False
        seen.add(k)
    return True


def infer_key(client: SwisClient, entity: str, fields: dict[str, str], time_field: str | None,
              rules: dict) -> tuple[list[str], str, str]:
    """(key columns, status, detail) for an entity without Metadata IsKey. Never invents: a set is kept only
    when it is unique (and non-null) on a sample of real rows; otherwise the row hash is the identity."""
    cands = key_candidates(entity, fields, rules, time_field)[:12]
    if not cands and not time_field:
        return [], "row_hash", "no candidate key columns; the row hash is the identity"
    n = int((rules.get("keys") or {}).get("sample_rows") or 2000)
    cols = cands + ([time_field] if time_field else [])
    swql = f"SELECT TOP {n} " + ", ".join(f"e.{c}" for c in cols) + f" FROM {entity} e"
    try:
        rows = client.query(swql).rows
    except SwisError as e:
        return [], "row_hash", f"sample query failed ({e}); the row hash is the identity"
    if not rows:
        return [], "row_hash", ("no rows to test candidate keys (an empty result may be permission filtering); "
                                "the row hash is the identity until discovery sees rows")
    combos: list[tuple[str, ...]] = [(c,) for c in cands]
    if time_field:
        combos += [(c, time_field) for c in cands]
    combos += [tuple(x) for x in itertools.combinations(cands, 2)]
    if time_field:
        combos += [(a, b, time_field) for a, b in itertools.combinations(cands, 2)]
    combos.append(tuple(cols))
    for combo in combos:
        if _unique(rows, combo):
            return list(combo), "sample_unique", f"unique and non-null on a sample of {len(rows)} rows (not proven)"
    return [], "row_hash", f"no candidate set is unique on {len(rows)} sampled rows; the row hash is the identity"


# ---------------------------------------------------------------------------------------------------------------
# One entity
# ---------------------------------------------------------------------------------------------------------------
def build_entry(client: SwisClient | None, schema: Schema, name: str, rules: dict, doc: dict) -> dict:
    ent = schema.entities[name]
    props = all_properties(schema, name)
    excluded_types = rules.get("excluded_types") or {}
    fields: dict[str, str] = {}
    excluded_fields: dict[str, str] = {}
    navigation: dict[str, str] = {}
    meta_key: list[str] = []
    for p in sorted(props, key=lambda x: x["Name"]):
        if p.get("IsNavigable"):
            navigation[p["Name"]] = p.get("Type")
            continue
        t = p.get("Type") or ""
        if t in excluded_types:
            excluded_fields[p["Name"]] = excluded_types[t]
            continue
        fields[p["Name"]] = t
        if p.get("IsKey"):
            meta_key.append(p["Name"])
    cls, reason, ev = classify(name, ent, fields, rules)
    page = (doc.get("pages") or {}).get(name)
    entry = {
        "entity": name,
        "class": cls,
        "collect": cls != "excluded",
        "reason": reason,
        "doc": DOC_URL.format(name) if name in set(doc.get("entities") or []) else None,
        "fields": len(fields),
        "columns": [{"name": f, "column": column_name(f), "swis_type": t, "pg_type": pg_type(t)[0],
                     "known": pg_type(t)[1]} for f, t in fields.items()],
        "excluded_fields": excluded_fields,
        "navigation": navigation,
    }
    if page:
        documented = set(page.get("fields") or {})
        entry["doc_check"] = {"missing_on_server": sorted(documented - set(fields) - set(excluded_fields)),
                              "not_in_doc": sorted(set(fields) - documented - _inherited_doc(doc, page))}
    if cls == "excluded":
        entry.update(sinks=[], table=None, key=[], key_status=None, time_field=None, watermark=None)
        return entry

    time_field = (ev or {}).get("time") if ev else time_field_of(fields, rules)
    if time_field and time_field not in fields:
        time_field = time_field_of(fields, rules)
    override = ((rules.get("keys") or {}).get("overrides") or {}).get(name)
    if override:
        key, status, detail = list(override), "override", "key from collector.yaml keys.overrides"
    elif meta_key:
        key, status, detail = meta_key, "metadata", "Metadata.Property.IsKey"
    elif client is not None:
        key, status, detail = infer_key(client, name, fields, time_field, rules)
        schema.queries += 1
    else:
        key, status, detail = [], "row_hash", "not sampled (offline)"

    sinks = {"inventory": ["raw"], "metrics": ["ts"], "events": ["loki"]}[cls]
    if ev and ev.get("also_raw"):
        sinks = ["raw", "loki"]
    if cls == "metrics" and status != "row_hash" and time_field not in key:
        key = key + [time_field]          # a sample is identified by its key at a point in time
    watermark = None
    if cls == "events":
        wm = next((f for f in ev.get("watermark") or [] if f in fields), None)
        if wm:
            watermark = {"kind": "id", "field": wm}
        elif time_field:
            watermark = {"kind": "time", "field": time_field, "overlap": "2 x poll interval"}
        else:
            entry["reason"] += "; no watermark field on this server: full pull each run (needs proof)"
    elif cls == "metrics":
        watermark = {"kind": "time", "field": time_field, "overlap": "2 x StatCollection"}
    entry.update(sinks=sinks, table=table_name(name), key=key, key_status=status, key_detail=detail,
                 time_field=time_field, watermark=watermark, event_kind=(ev or {}).get("kind"))
    return entry


def _inherited_doc(doc: dict, page: dict) -> set[str]:
    out: set[str] = set()
    for base in page.get("bases") or []:
        out |= set(((doc.get("pages") or {}).get(base) or {}).get("fields") or {})
    return out


# ---------------------------------------------------------------------------------------------------------------
# The whole server
# ---------------------------------------------------------------------------------------------------------------
def server_info(client: SwisClient) -> dict:
    info: dict = {}
    try:
        r = client.query("SELECT TOP 1 GETUTCDATE() AS U, GETDATE() AS L FROM Orion.Engines").rows[0]
        from .swis.timeutil import parse_dt
        u, l = parse_dt(r["U"]), parse_dt(r["L"])
        if u and l:
            info["utc_offset_minutes"] = round((l.replace(tzinfo=None) - u.replace(tzinfo=None)).total_seconds() / 60)
        info["clock"] = r
    except (SwisError, IndexError, KeyError) as e:
        info["clock_error"] = str(e)
    try:
        info["modules"] = client.query("SELECT m.Name, m.Version FROM Orion.InstalledModule m").rows
    except SwisError as e:
        info["modules_error"] = str(e)
    return info


def discover(client: SwisClient, net: Network, rules: dict | None = None) -> dict:
    """Returns the discovered catalog (entities + server info). Relationships are added by relationships.py."""
    rules = rules or load_rules()
    doc = load_doc_schema()
    started = utcnow()
    schema = read_schema(client)
    info = server_info(client)
    entries = {name: build_entry(client, schema, name, rules, doc) for name in sorted(schema.entities)}
    summary: dict[str, int] = {}
    for e in entries.values():
        summary[e["class"]] = summary.get(e["class"], 0) + 1
    return {
        "generated": {"at": started.isoformat(timespec="seconds"), "network": net.name,
                      "instance": net.instance_key, "swis_url": net.url, "notes": schema.notes,
                      "doc_source": (doc.get("source") or "")},
        "server": info,
        "summary": {"entities": len(entries), **summary,
                    "collected": sum(1 for e in entries.values() if e["collect"]),
                    "metadata_relationships": len(schema.relationships)},
        "entities": entries,
        "_schema": schema,           # not written to the file
    }


def write_catalog(catalog: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in catalog.items() if not k.startswith("_")}
    header = ("# GENERATED by `swcollect discover` from the server's Metadata. Do not edit:\n"
              "# change catalog/collector.yaml and run discover again.\n")
    path.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
    return path

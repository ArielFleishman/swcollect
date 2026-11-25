"""sw_core: canonical entities with real foreign keys, built by SQL flows from sw_raw (plan §2, §2b step 5).

catalog/core.yaml declares each canonical entity: its SWIS source, id, fields, derived fields and references.
A reference becomes a FOREIGN KEY only through a relationship PROVEN in sw_meta.relationship between the two SWIS
entities on the same columns; an unproven reference is not enforced (its FK stays NULL) and the flow says so.

Each flow, in topological order (parents first), for one instance:
  1. _flow: every live sw_raw row → canonical columns, stable ID, derived fields, parent ids, lineage
  2. dead-letter: key_not_null, key_unique and ref failures go to sw_core.dead_letter with the raw row and the rule;
     the record is not curated. A failure seen again moves last_run; one that stops happening gets resolved_at.
  3. upsert the passing records by stable_id; curated records whose source is gone (or now fails) get deleted_at
  4. one sw_meta.flow row: sources, target, relationships used, rows in = out + dead-letter
Each record's `changes` lists every change from its raw row (renames, derived values); the raw row is one join away
(raw_table, raw_key) and sw_core.v_<table>_before_after shows them side by side.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import yaml

from .config import CATALOG_DIR
from .ddl import interface_name_function, literal, q

TRANSFORM_SQL = {"interface_name": "sw_core.normalize_interface_name({})"}


def load_core(path: Path | None = None) -> list[dict]:
    return (yaml.safe_load((path or CATALOG_DIR / "core.yaml").read_text(encoding="utf-8")) or {}).get("entities") or []


@dataclass
class Ref:
    fk: str
    to: str
    child_cols: list[str]            # SWIS columns in the child's source
    parent_fields: list[str]         # canonical fields of the parent
    parent_cols: list[str] = field(default_factory=list)   # the parent's SWIS columns for those fields
    relationship_id: int | None = None
    proven: bool = False
    note: str = ""


@dataclass
class Plan:
    table: str
    spec: dict
    source: str | None = None
    entry: dict | None = None
    mapping: dict = field(default_factory=dict)       # canonical → SWIS column (None = missing on this server)
    types: dict = field(default_factory=dict)         # canonical → pg type
    refs: list[Ref] = field(default_factory=list)
    skipped: str | None = None


def topo_order(specs: list[dict]) -> list[dict]:
    by = {s["table"]: s for s in specs}
    deps = {s["table"]: {r["to"] for r in s.get("refs") or [] if r["to"] in by and r["to"] != s["table"]} for s in specs}
    out, done = [], set()
    while len(out) < len(specs):
        ready = [t for t in by if t not in done and deps[t] <= done]
        if not ready:
            raise ValueError(f"cycle in core.yaml references: {sorted(set(by) - done)}")
        for t in sorted(ready, key=lambda t: [s["table"] for s in specs].index(t)):
            out.append(by[t])
            done.add(t)
    return out


def find_proven(proven: list, source: str, target: str, source_cols: list[str], target_cols: list[str]) -> int | None:
    """The id of a proven relationship joining source(source_cols) to target(target_cols), forward or reverse
    (the same join proven from the other side, e.g. the Orion.Nodes.IPAddresses navigation)."""
    for rid, s, t, sc, tc in proven:
        if (s == source and t == target and list(sc) == list(source_cols) and list(tc) == list(target_cols)) or                 (s == target and t == source and list(sc) == list(target_cols) and list(tc) == list(source_cols)):
            return rid
    return None


def make_plans(conn: psycopg.Connection, instance_id: int, entries: dict[str, dict], specs: list[dict]) -> list[Plan]:
    proven = conn.execute(
        """SELECT relationship_id, source_entity, target_entity, source_columns, target_columns
           FROM sw_meta.relationship WHERE instance_id = %s AND status = 'proven'""", (instance_id,)).fetchall()
    plans: dict[str, Plan] = {}
    for spec in topo_order(specs):
        p = Plan(spec["table"], spec)
        plans[p.table] = p
        for src in spec["source"]:
            e = entries.get(src)
            if e and e["collect"] and (e.get("sink_table") or "").startswith("sw_raw."):
                p.source, p.entry = src, e
                break
        if not p.source:
            p.skipped = f"none of {spec['source']} is collected into sw_raw on this server"
            continue
        cols = {c["name"]: c["pg_type"] for c in p.entry["columns"]}
        for canon, swis in spec["fields"].items():
            alts = swis if isinstance(swis, list) else [swis]
            hit = next((a for a in alts if a in cols), None)
            p.mapping[canon] = hit
            p.types[canon] = cols.get(hit, "text")
        for r in spec.get("refs") or []:
            ref = Ref(r["fk"], r["to"], list(r["columns"].keys()), list(r["columns"].values()))
            parent = plans.get(ref.to)
            if not parent or parent.skipped:
                ref.note = f"parent {ref.to} not built on this server"
            else:
                ref.parent_cols = [parent.mapping.get(f) for f in ref.parent_fields]
                missing = [c for c in ref.child_cols if c not in cols]
                if missing or None in ref.parent_cols:
                    ref.note = f"columns missing on this server: {missing or ref.parent_fields}"
                else:
                    rid = find_proven(proven, p.source, parent.source, ref.child_cols, ref.parent_cols)
                    if rid:
                        ref.relationship_id, ref.proven = rid, True
                    if not ref.proven:
                        ref.note = (f"no proven relationship {p.source}{ref.child_cols} -> {parent.source}"
                                    f"{ref.parent_cols} in sw_meta.relationship: not enforced (needs proof)")
            p.refs.append(ref)
    return list(plans.values())


# ---------------------------------------------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------------------------------------------
def core_ddl(p: Plan) -> list[str]:
    t = f"sw_core.{q(p.table)}"
    lines = ["id bigserial PRIMARY KEY", "stable_id text NOT NULL UNIQUE",
             "instance_id integer NOT NULL REFERENCES sw_meta.instance"]
    lines += [f"{q(c)} {p.types[c]}" for c in p.spec["fields"]]
    lines += [f"{q(d['name'])} text" for d in p.spec.get("derived") or []]
    lines += [f"{q(r.fk)} bigint REFERENCES sw_core.{q(r.to)}(id)" for r in p.refs]
    lines += ["raw_table text NOT NULL", "raw_key jsonb NOT NULL", "changes jsonb NOT NULL DEFAULT '[]'",
              "run_id bigint REFERENCES sw_meta.sync_run", "first_seen timestamptz NOT NULL DEFAULT now()",
              "last_seen timestamptz NOT NULL DEFAULT now()", "deleted_at timestamptz"]
    out = [f"CREATE TABLE IF NOT EXISTS {t} (\n    " + ",\n    ".join(lines) + ")"]
    for c in p.spec["fields"]:
        out.append(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {q(c)} {p.types[c]}")
    for r in p.refs:
        out.append(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {q(r.fk)} bigint REFERENCES sw_core.{q(r.to)}(id)")
        out.append(f"CREATE INDEX IF NOT EXISTS {q(p.table + '_' + r.fk)} ON {t} ({q(r.fk)})")
    ids = ", ".join(q(c) for c in p.spec["id"])
    out.append(f"CREATE INDEX IF NOT EXISTS {q(p.table + '_ident')} ON {t} (instance_id, {ids})")
    out.append(f"COMMENT ON TABLE {t} IS " + literal(
        f"{p.spec.get('contract')} (contracts.yaml); source {p.source}; id {p.spec['id']}; lineage raw_table + raw_key"))
    # before / after: the raw row as SWIS returned it, our record, and the changes
    raw = f"{p.entry['sink_table'].split('.')[0]}.{q(p.entry['sink_table'].split('.', 1)[1])}"
    rk = ("r._row_hash = c.raw_key ->> '_row_hash'" if p.entry["key_status"] == "row_hash" else
          " AND ".join(f"r._raw -> {literal(k)} = c.raw_key -> {literal(k)}" for k in p.entry["key"]))
    out.append(f"DROP VIEW IF EXISTS sw_core.{q('v_' + p.table + '_before_after')}")
    out.append(f"""CREATE VIEW sw_core.{q('v_' + p.table + '_before_after')} AS
        SELECT c.id, c.stable_id, c.instance_id, r._raw AS raw_row,
               to_jsonb(c) - ARRAY['raw_table', 'raw_key', 'changes', 'run_id', 'first_seen', 'last_seen'] AS record,
               c.changes, c.raw_table, c.raw_key
        FROM {t} c LEFT JOIN {raw} r ON r.instance_id = c.instance_id AND {rk}""")
    return out


# ---------------------------------------------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------------------------------------------
def _text(expr: str) -> str:
    return f"lower(({expr})::text)"


def flow_sql(p: Plan, raw_table: str) -> tuple[str, list[str]]:
    """SELECT that builds _flow for one instance (%(inst)s), and the list of check expressions."""
    e, spec = p.entry, p.spec
    raw = f"sw_raw.{q(raw_table)}"
    sel = ["r.instance_id"]
    sid = " || ';' || ".join(f"{literal(c + '=')} || coalesce(({_canon_expr(p, c)})::text, '')" for c in spec["id"])
    sel.append(f"i.instance_key || {literal('/' + p.source + '/')} || {sid} AS stable_id")
    for c in spec["fields"]:
        sel.append(f"{_canon_expr(p, c)} AS {q(c)}")
    for d in spec.get("derived") or []:
        sel.append(f"{TRANSFORM_SQL[d['rule']].format(_canon_expr(p, d['from']))} AS {q(d['name'])}")
    joins = []
    for n, r in enumerate(p.refs):
        if r.proven:
            on = " AND ".join(f"{_text(f'p{n}.' + q(pf))} = {_text('r.' + q(cc))}"
                              for pf, cc in zip(r.parent_fields, r.child_cols))
            joins.append(f"LEFT JOIN sw_core.{q(r.to)} p{n} ON p{n}.instance_id = r.instance_id "
                         f"AND p{n}.deleted_at IS NULL AND {on}")
            sel.append(f"p{n}.id AS {q(r.fk)}")
            sel.append(f"({' AND '.join('r.' + q(cc) + ' IS NOT NULL' for cc in r.child_cols)}) AS {q('_has_' + r.fk)}")
        else:
            sel.append(f"NULL::bigint AS {q(r.fk)}")
    key = (["_row_hash"] if e["key_status"] == "row_hash" else e["key"])
    raw_key = ("jsonb_build_object('_row_hash', r._row_hash)" if e["key_status"] == "row_hash" else
               "jsonb_build_object(" + ", ".join(f"{literal(k)}, r._raw -> {literal(k)}" for k in key) + ")")
    sel.append(f"{raw_key} AS raw_key")
    sel.append("r._raw")
    sel.append(f"{_changes_expr(p)} AS changes")
    sql = (f"SELECT {', '.join(sel)} FROM {raw} r JOIN sw_meta.instance i ON i.instance_id = r.instance_id "
           f"{' '.join(joins)} WHERE r.instance_id = %(inst)s AND r._deleted_at IS NULL")
    return sql, key


def _canon_expr(p: Plan, canon: str) -> str:
    col = p.mapping.get(canon)
    return f"r.{q(col)}" if col else f"NULL::{p.types.get(canon, 'text')}"


def _changes_expr(p: Plan) -> str:
    parts = []
    for canon, col in p.mapping.items():
        if col and col != canon:
            parts.append(f"jsonb_build_object('field', {literal(canon)}, 'from', {literal(col)}, 'change', 'renamed')")
    for d in p.spec.get("derived") or []:
        src = _canon_expr(p, d["from"])
        val = TRANSFORM_SQL[d["rule"]].format(src)
        parts.append(f"CASE WHEN {val} IS DISTINCT FROM {src} THEN jsonb_build_object('field', {literal(d['name'])}, "
                     f"'from', {literal(d['from'])}, 'change', 'derived', 'rule', {literal(d['rule'])}, "
                     f"'raw', to_jsonb({src}), 'value', to_jsonb({val})) END")
    if not parts:
        return "'[]'::jsonb"
    return f"(SELECT coalesce(jsonb_agg(x) FILTER (WHERE x IS NOT NULL), '[]') FROM unnest(ARRAY[{', '.join(parts)}]) x)"


def run_flow(conn: psycopg.Connection, p: Plan, instance_id: int, run_id: int, position: int) -> dict:
    t = f"sw_core.{q(p.table)}"
    raw_table = p.entry["sink_table"].split(".", 1)[1]
    sql, _ = flow_sql(p, raw_table)
    prm = {"inst": instance_id, "run": run_id, "t": p.table, "raw": p.entry["sink_table"]}
    checks = [(f"{q(c)} IS NULL", "key_not_null", c, f"{q(c)}::text") for c in p.spec["id"]]
    for r in p.refs:
        if r.proven:
            checks.append((f"{q('_has_' + r.fk)} AND {q(r.fk)} IS NULL", "ref", ",".join(r.child_cols),
                           " || ',' || ".join(f"coalesce((_raw ->> {literal(c)}), '')" for c in r.child_cols)))
    with conn.transaction():
        conn.execute(f"CREATE TEMP TABLE _flow ON COMMIT DROP AS {sql}", prm)
        conn.execute("ALTER TABLE _flow ADD COLUMN _fail boolean NOT NULL DEFAULT false")
        rows_in = conn.execute("SELECT count(*) FROM _flow").fetchone()[0]
        for cond, rule, fld, val in checks:
            detail = ("an id field is null" if rule == "key_not_null" else
                      f"no live parent in sw_core for this value (orphan, or the parent is in the dead-letter queue)")
            _dlq(conn, f"SELECT raw_key, {val} AS value, _raw FROM _flow WHERE {cond}", rule, fld, detail, prm)
            conn.execute(f"UPDATE _flow SET _fail = true WHERE {cond}")
        _dlq(conn, "SELECT raw_key, stable_id AS value, _raw FROM _flow WHERE stable_id IN "
                   "(SELECT stable_id FROM _flow GROUP BY stable_id HAVING count(*) > 1)",
             "key_unique", ",".join(p.spec["id"]), "the same stable ID from more than one raw row", prm)
        conn.execute("UPDATE _flow SET _fail = true WHERE stable_id IN "
                     "(SELECT stable_id FROM _flow GROUP BY stable_id HAVING count(*) > 1)")
        cols = list(p.spec["fields"]) + [d["name"] for d in p.spec.get("derived") or []] + [r.fk for r in p.refs]
        names = ", ".join(q(c) for c in cols)
        upd = ", ".join([f"{q(c)} = EXCLUDED.{q(c)}" for c in cols] +
                        ["raw_key = EXCLUDED.raw_key", "changes = EXCLUDED.changes", "run_id = EXCLUDED.run_id",
                         "last_seen = now()", "deleted_at = NULL"])
        conn.execute(f"""INSERT INTO {t} (stable_id, instance_id, {names}, raw_table, raw_key, changes, run_id)
                         SELECT stable_id, instance_id, {names}, %(raw)s, raw_key, changes, %(run)s FROM _flow
                         WHERE NOT _fail ON CONFLICT (stable_id) DO UPDATE SET {upd}""", prm)
        conn.execute(f"UPDATE {t} SET deleted_at = now() WHERE instance_id = %(inst)s AND deleted_at IS NULL "
                     f"AND run_id IS DISTINCT FROM %(run)s", prm)
        conn.execute("UPDATE sw_core.dead_letter SET resolved_at = now() WHERE core_table = %(t)s AND "
                     "instance_id = %(inst)s AND resolved_at IS NULL AND last_run IS DISTINCT FROM %(run)s", prm)
        rows_dlq = conn.execute("SELECT count(*) FROM _flow WHERE _fail").fetchone()[0]
        rows_out = conn.execute(f"SELECT count(*) FROM {t} WHERE instance_id = %(inst)s AND run_id = %(run)s",
                                prm).fetchone()[0]
        by_rule = dict(conn.execute("SELECT rule, count(*) FROM sw_core.dead_letter WHERE core_table = %(t)s AND "
                                    "instance_id = %(inst)s AND last_run = %(run)s GROUP BY rule", prm).fetchall())
        detail = {"source": p.source, "missing_fields": [c for c, v in p.mapping.items() if v is None],
                  "renamed": {c: v for c, v in p.mapping.items() if v and v != c},
                  "refs": [{"fk": r.fk, "to": r.to, "proven": r.proven, "relationship_id": r.relationship_id,
                            "note": r.note} for r in p.refs],
                  "dead_letter_by_rule": by_rule}
        rule = "; ".join([f"id {p.spec['id']}"] + [f"{r.fk} → {r.to}" + ("" if r.proven else " (not enforced)")
                                                   for r in p.refs])
        conn.execute("""INSERT INTO sw_meta.flow (run_id, position, source_tables, target_table, relationships, rule,
                            rows_in, rows_out, rows_dlq, detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                     (run_id, position, [p.entry["sink_table"]], f"sw_core.{p.table}",
                      [r.relationship_id for r in p.refs if r.proven], rule, rows_in, rows_out, rows_dlq,
                      json.dumps(detail)))
    return {"table": p.table, "in": rows_in, "out": rows_out, "dlq": rows_dlq, "rules": by_rule,
            "unproven": [r.fk for r in p.refs if not r.proven]}


def _dlq(conn, select_sql: str, rule: str, fld: str, detail: str, prm: dict):
    conn.execute(f"""
        INSERT INTO sw_core.dead_letter (core_table, instance_id, raw_table, raw_key, rule, field, value, detail, raw,
                                         first_run, last_run)
        SELECT %(t)s, %(inst)s, %(raw)s, x.raw_key, {literal(rule)}, {literal(fld)}, x.value, {literal(detail)}, x._raw,
               %(run)s, %(run)s FROM ({select_sql}) x
        ON CONFLICT (core_table, instance_id, raw_key, rule, field) DO UPDATE SET last_run = EXCLUDED.last_run,
            last_seen = now(), resolved_at = NULL, value = EXCLUDED.value, raw = EXCLUDED.raw""", prm)


def run(conn: psycopg.Connection, instance_id: int, run_id: int, entries: dict[str, dict], log=print) -> list[dict]:
    conn.execute(interface_name_function())
    plans = make_plans(conn, instance_id, entries, load_core())
    results = []
    for pos, p in enumerate(plans, 1):
        if p.skipped:
            results.append({"table": p.table, "skipped": p.skipped})
            continue
        with conn.transaction():
            for s in core_ddl(p):
                conn.execute(s)
        results.append(run_flow(conn, p, instance_id, run_id, pos))
    return results

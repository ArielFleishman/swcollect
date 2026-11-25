"""ERD of sw_raw: the relationships between SolarWinds entities that swcollect proved, as documents (no database change).

sw_raw has no foreign keys between its tables on purpose (a raw row is never refused for a missing parent), so a
database ERD tool shows the tables unconnected. The connections exist: `discover` proves them against SolarWinds and
records them in sw_meta.relationship (and as views in sw_rel). This module draws them:

  docs/raw-model.html   one self-contained page (offline): hubs, bridges between hubs, every join, sw_raw → sw_core
  docs/raw-model.md     the same as Mermaid erDiagrams, one per hub (renders in VS Code and on Git hosting)
  docs/raw-model.dbml   DBML for ERD tools that import it, with composite references (instance_id + key)
  docs/raw-joins.csv    one row per join: tables, columns, how it was proven, the sw_rel view and a ready JOIN

Only relationships with status `proven` are drawn. Every join also matches instance_id: a NodeID is local to one
Orion server.
"""
from __future__ import annotations

import csv
import html
import io
import json
from collections import defaultdict
from pathlib import Path

import psycopg

LOC = {"sw_raw": "sw_raw", "sw_ts": "sw_ts"}


def model(conn: psycopg.Connection, instance_id: int) -> dict:
    inst = conn.execute("SELECT instance_key FROM sw_meta.instance WHERE instance_id = %s", (instance_id,)).fetchone()[0]
    cat = {e: {"sink": s, "key": list(k or []), "class": c, "key_status": ks}
           for e, s, k, c, ks in conn.execute(
               """SELECT entity, sink_table, key_columns, class, key_status FROM sw_meta.entity_catalog
                  WHERE instance_id = %s""", (instance_id,)).fetchall()}
    rels = conn.execute(
        """SELECT source_entity, name, target_entity, source_columns, target_columns, origin, method, pairs_checked,
                  mismatches, view_name, detail, basis
           FROM sw_meta.relationship WHERE instance_id = %s AND status = 'proven'
           ORDER BY target_entity, source_entity, name""", (instance_id,)).fetchall()
    counts = dict(conn.execute(
        "SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE schemaname IN ('sw_raw', 'sw_ts')").fetchall())
    types: dict[str, dict[str, str]] = defaultdict(dict)
    ncols: dict[str, int] = defaultdict(int)
    for schema, table, col, udt in conn.execute(
            """SELECT table_schema, table_name, column_name, udt_name FROM information_schema.columns
               WHERE table_schema IN ('sw_raw', 'sw_ts') ORDER BY table_name, ordinal_position""").fetchall():
        types[f"{schema}.{table}"][col] = udt
        if not col.startswith("_") and col != "instance_id":
            ncols[f"{schema}.{table}"] += 1

    edges: dict[tuple, dict] = {}
    for src, name, tgt, scols, tcols, origin, method, pairs, mism, view, detail, basis in rels:
        scols, tcols = list(scols or []), list(tcols or [])
        skey, tkey = cat.get(src, {}).get("key", []), cat.get(tgt, {}).get("key", [])
        tkey = [k for k in tkey if k != "DateTime"]
        # Direction: the parent is the side whose key the join columns are. Child → parent is many-to-one.
        # A time series (sw_ts) is always the child: its rows are samples of the parent.
        if cat.get(tgt, {}).get("class") == "metrics" and cat.get(src, {}).get("class") != "metrics":
            child, ccols, parent, pcols = tgt, tcols, src, scols
        elif cat.get(src, {}).get("class") == "metrics":
            child, ccols, parent, pcols = src, scols, tgt, tcols
        elif tcols == tkey or (tcols and set(tcols) <= set(tkey) and scols != skey):
            child, ccols, parent, pcols = src, scols, tgt, tcols
        elif scols == skey:
            child, ccols, parent, pcols = tgt, tcols, src, scols
        else:
            child, ccols, parent, pcols = src, scols, tgt, tcols
        k = (child, tuple(ccols), parent, tuple(pcols))
        e = edges.setdefault(k, {"child": child, "ccols": ccols, "parent": parent, "pcols": pcols, "names": [],
                                 "origin": set(), "method": set(), "pairs": 0, "mismatches": 0, "views": [],
                                 "detail": [], "basis": None})
        e["names"].append(f"{src}.{name}")
        e["origin"].add(origin)
        e["method"].add(method)
        e["pairs"] = max(e["pairs"], pairs or 0)
        e["mismatches"] += mism or 0
        if view:
            e["views"].append(view)
        e["detail"].append(detail or "")
        e["basis"] = e["basis"] or basis
    def twins(a: str, b: str) -> bool:
        """The same records under two names: NCM.X and its older name Cirrus.X, or a node and its custom properties."""
        sa, sb = a.split(".", 1), b.split(".", 1)
        return ({sa[0], sb[0]} == {"NCM", "Cirrus"} and sa[1] == sb[1]) or \
            {a, b} == {"Orion.Nodes", "Orion.NodesCustomProperties"}

    def canon(e):
        """Twins point at the current name: Cirrus.X → NCM.X, Orion.NodesCustomProperties → Orion.Nodes."""
        if twins(e["child"], e["parent"]) and (e["child"].startswith("NCM.") or e["child"] == "Orion.Nodes"):
            e["child"], e["parent"], e["ccols"], e["pcols"] = e["parent"], e["child"], e["pcols"], e["ccols"]
        return e

    merged: dict[tuple, dict] = {}
    for e in map(canon, edges.values()):
        k = (e["child"], tuple(e["ccols"]), e["parent"], tuple(e["pcols"]))
        if k in merged:                                # the same join recorded from both ends
            m0 = merged[k]
            m0["names"] += e["names"]
            m0["origin"] |= e["origin"]
            m0["method"] |= e["method"]
            m0["views"] += e["views"]
            m0["pairs"] = max(m0["pairs"], e["pairs"])
        else:
            merged[k] = e
    edges_l = sorted(merged.values(), key=lambda e: (e["parent"], e["child"]))
    for e in edges_l:
        e["origin"], e["method"] = sorted(e["origin"]), sorted(e["method"])
        # Keys inferred from a sample are not proof of uniqueness, so only twins are called one-to-one.
        e["card"] = "one-to-one" if twins(e["child"], e["parent"]) else "many-to-one"

    def where(entity):
        sink = cat.get(entity, {}).get("sink")
        if sink:
            return sink
        return "Loki (event source, no table)" if cat.get(entity, {}).get("class") == "events" else "not stored"

    tables = sorted({e["child"] for e in edges_l} | {e["parent"] for e in edges_l})
    info = {}
    for t in tables:
        sink = where(t)
        info[t] = {"sink": sink, "key": cat.get(t, {}).get("key", []), "rows": counts.get(sink.split(".", 1)[-1]),
                   "fields": ncols.get(sink), "types": types.get(sink, {})}
    # Hubs: parents with the most children. A "bridge" is a join between two hubs.
    kids = defaultdict(list)
    for e in edges_l:
        kids[e["parent"]].append(e)
    hubs = sorted(kids, key=lambda p: (-len(kids[p]), p))
    lineage = conn.execute(
        """SELECT f.target_table, f.source_tables, f.rows_in, f.rows_out, f.rows_dlq FROM sw_meta.flow f
           WHERE f.run_id = (SELECT max(r.run_id) FROM sw_meta.sync_run r WHERE r.instance_id = %s AND r.job = 'curate'
                             AND EXISTS (SELECT 1 FROM sw_meta.flow x WHERE x.run_id = r.run_id))
           ORDER BY f.position""", (instance_id,)).fetchall()
    return {"instance": inst, "edges": edges_l, "tables": tables, "info": info, "hubs": hubs, "kids": kids,
            "lineage": lineage, "n_rels": len(rels)}


# ---------------------------------------------------------------------------------------------------------------
def _t(m, entity) -> str:
    return m["info"][entity]["sink"]


def _stored(m, entity) -> bool:
    return m["info"][entity]["sink"].startswith(("sw_raw.", "sw_ts."))


def join_sql(m, e) -> str:
    c, p = _t(m, e["child"]), _t(m, e["parent"])
    if not (_stored(m, e["child"]) and _stored(m, e["parent"])):
        return ""
    on = " AND ".join(["p.instance_id = c.instance_id"] + [f'p."{pc}" = c."{cc}"' for cc, pc in zip(e["ccols"], e["pcols"])])
    live = [f"{a}._deleted_at IS NULL" for a, t in (("c", c), ("p", p)) if t.startswith("sw_raw.")]
    return f"SELECT * FROM {c} c JOIN {p} p ON {on}" + (f" WHERE {' AND '.join(live)}" if live else "")


def csv_text(m) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["child_entity", "child_table", "child_columns", "parent_entity", "parent_table", "parent_columns",
                "cardinality", "relationship_names", "proven_by", "values_checked", "mismatches", "sw_rel_views",
                "join_sql"])
    for e in m["edges"]:
        w.writerow([e["child"], _t(m, e["child"]), "instance_id + " + " + ".join(e["ccols"]), e["parent"],
                    _t(m, e["parent"]), "instance_id + " + " + ".join(e["pcols"]), e["card"],
                    "; ".join(e["names"]), "/".join(e["origin"]), e["pairs"], e["mismatches"],
                    "; ".join(e["views"]), join_sql(m, e)])
    return out.getvalue()


def _mid(entity: str) -> str:
    return entity.replace(".", "_")


def _mtype(udt: str | None) -> str:
    return (udt or "unknown").replace(" ", "_")


def markdown(m) -> str:
    L = ["# sw_raw relationships (ERD)", "",
         f"Generated by `swcollect erd` from the relationships proven against SolarWinds (instance `{m['instance']}`). "
         "Do not edit: run it again.", "",
         "sw_raw has no foreign keys between its tables on purpose: a raw row is never refused because its parent is "
         "missing. These are the connections swcollect proved instead. Every join also matches `instance_id`.", "",
         f"{len(m['edges'])} joins between {len(m['tables'])} entities. Full list with ready SQL: "
         "[raw-joins.csv](raw-joins.csv). Interactive version: [raw-model.html](raw-model.html).", ""]
    big = [h for h in m["hubs"] if len(m["kids"][h]) >= 4]
    rest = [h for h in m["hubs"] if h not in big]
    groups = [(f"Around {h}", [h]) for h in big] + ([("Smaller groups", rest)] if rest else [])
    for title, hs in groups:
        L += [f"## {title}", "", "```mermaid", "erDiagram"]
        ents = set()
        for h in hs:
            for e in m["kids"][h]:
                lab = " + ".join(e["ccols"]) + (" = " + " + ".join(e["pcols"]) if e["ccols"] != e["pcols"] else "")
                rel = "||--||" if e["card"] == "one-to-one" else "||--o{"
                L.append(f'    {_mid(e["parent"])} {rel} {_mid(e["child"])} : "{lab}"')
                ents |= {(e["parent"], tuple(e["pcols"])), (e["child"], tuple(e["ccols"]))}
        cols = defaultdict(list)
        for ent, cs in ents:
            for c in cs:
                if c not in cols[ent]:
                    cols[ent].append(c)
        for ent in sorted(cols):
            inf = m["info"][ent]
            keys = set(inf["key"])
            L.append(f"    {_mid(ent)} {{")
            for c in list(dict.fromkeys(inf["key"] + cols[ent])):
                mark = " PK" if c in keys else " FK"
                L.append(f"        {_mtype(inf['types'].get(c))} {c}{mark}")
            L.append("    }")
        L += ["```", ""]
    L += ["## Where each entity is stored", "", "| Entity | Table | Key | Fields | Rows |", "|---|---|---|---|---|"]
    for t in m["tables"]:
        inf = m["info"][t]
        L.append(f"| `{t}` | `{inf['sink']}` | {', '.join(inf['key']) or 'row hash'} | {inf['fields'] or ''} | "
                 f"{inf['rows'] if inf['rows'] is not None else ''} |")
    return "\n".join(L) + "\n"


def dbml(m) -> str:
    L = [f"// sw_raw relationships proven by swcollect (instance {m['instance']}). Generated by `swcollect erd`.",
         "// Only key and join columns are listed; each note says how many SolarWinds fields the table has.",
         "// A reference is a proven relationship, not a database constraint.", ""]
    cols = defaultdict(list)
    for e in m["edges"]:
        for ent, cs in ((e["child"], e["ccols"]), (e["parent"], e["pcols"])):
            for c in cs:
                if c not in cols[ent]:
                    cols[ent].append(c)
    for t in m["tables"]:
        inf = m["info"][t]
        if not _stored(m, t):
            continue
        schema, table = inf["sink"].split(".", 1)
        L.append(f'Table "{schema}"."{table}" {{')
        L.append('  "instance_id" int4 [not null, note: "which Orion server (sw_meta.instance)"]')
        for c in list(dict.fromkeys(inf["key"] + cols[t])):
            L.append(f'  "{c}" {inf["types"].get(c, "text")}')
        if inf["key"]:
            L.append("  indexes {")
            L.append("    (" + ", ".join(['"instance_id"'] + [f'"{k}"' for k in inf["key"]]) + ") [unique, note: 'key']")
            L.append("  }")
        L.append(f"  Note: 'SolarWinds entity {t}; {inf['fields'] or 0} fields; rows: {inf['rows']}'")
        L.append("}")
        L.append("")
    for e in m["edges"]:
        if not (_stored(m, e["child"]) and _stored(m, e["parent"])):
            continue
        c, p = _t(m, e["child"]), _t(m, e["parent"])
        cq = ".".join(f'"{x}"' for x in c.split("."))
        pq = ".".join(f'"{x}"' for x in p.split("."))
        cc = ", ".join(['"instance_id"'] + [f'"{x}"' for x in e["ccols"]])
        pc = ", ".join(['"instance_id"'] + [f'"{x}"' for x in e["pcols"]])
        op = "-" if e["card"] == "one-to-one" else ">"
        L.append(f"Ref: {cq}.({cc}) {op} {pq}.({pc}) // {'/'.join(e['origin'])}, {e['pairs']} values checked")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------------------------------------------
def page(m) -> str:
    data = {
        "instance": m["instance"],
        "tables": {t: {"sink": i["sink"], "key": i["key"], "rows": i["rows"], "fields": i["fields"]}
                   for t, i in m["info"].items()},
        "edges": [{"child": e["child"], "parent": e["parent"], "ccols": e["ccols"], "pcols": e["pcols"],
                   "origin": e["origin"], "pairs": e["pairs"], "views": e["views"], "names": e["names"], "card": e["card"],
                   "sql": join_sql(m, e)} for e in m["edges"]],
        "hubs": m["hubs"],
        "lineage": [{"target": t, "sources": s, "in": a, "out": b, "dlq": d} for t, s, a, b, d in m["lineage"]],
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return PAGE.replace("__DATA__", blob).replace("__INSTANCE__", html.escape(m["instance"]))


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>sw_raw Relationship Map</title>
<style>
/* Layout: left list of hubs and tables, right detail of the selected table; summary bands above. Offline: system fonts. */
:root {
  --bg: #f5f6f8; --surface: #ffffff; --ink: #15181d; --ink-2: #4b525d; --ink-3: #757d89; --line: #dce0e6;
  --raw: #a86200; --raw-bg: #fcf1e1; --core: #1f5fbf; --core-bg: #e8f0fc; --ts: #13805a; --ts-bg: #e3f4ec;
  --loki: #7a4fb8; --loki-bg: #f1ebfa; --hi: #fff4c2; --code: #eff1f4;
  --sans: "Segoe UI", system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif;
  --mono: "Cascadia Mono", Consolas, "SFMono-Regular", Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #0f1216; --surface: #171b21; --ink: #e7eaee; --ink-2: #b3bac5; --ink-3: #8a929e; --line: #2a3038;
  --raw: #efa443; --raw-bg: #2b2113; --core: #74a7f2; --core-bg: #15243b; --ts: #4cc796; --ts-bg: #11291f;
  --loki: #b593ef; --loki-bg: #241c37; --hi: #3a3313; --code: #1d222a; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #0f1216; --surface: #171b21; --ink: #e7eaee; --ink-2: #b3bac5; --ink-3: #8a929e; --line: #2a3038;
  --raw: #efa443; --raw-bg: #2b2113; --core: #74a7f2; --core-bg: #15243b; --ts: #4cc796; --ts-bg: #11291f;
  --loki: #b593ef; --loki-bg: #241c37; --hi: #3a3313; --code: #1d222a; color-scheme: dark }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.55 var(--sans);
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  padding-block: 28px 56px; }
.wrap { max-width: 1180px; margin: 0 auto; display: flex; flex-direction: column; gap: 28px; }
h1, h2, h3 { margin: 0; line-height: 1.2; text-wrap: balance; }
h1 { font-size: clamp(1.7rem, 4vw, 2.4rem); letter-spacing: -0.02em; }
h2 { font-size: 1.3rem; }
h3 { font-size: 1rem; }
p { margin: 0; }
.eyebrow { font-size: 12px; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink-3); }
.lede { color: var(--ink-2); max-width: 75ch; }
code, .mono { font-family: var(--mono); font-size: 0.86em; }
code { background: var(--code); padding: 1px 5px; border-radius: 4px; overflow-wrap: anywhere; }
section { display: flex; flex-direction: column; gap: 12px; }
.stats { display: flex; flex-wrap: wrap; gap: 10px; }
.stat { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; min-width: 140px; }
.stat b { display: block; font-size: 1.5rem; font-variant-numeric: tabular-nums; }
.stat span { font-size: 0.82rem; color: var(--ink-3); }
.loc { display: inline-block; font: 500 0.75rem var(--mono); padding: 1px 7px; border-radius: 999px; white-space: nowrap; }
.loc.sw_raw { color: var(--raw); background: var(--raw-bg); } .loc.sw_ts { color: var(--ts); background: var(--ts-bg); }
.loc.loki { color: var(--loki); background: var(--loki-bg); } .loc.sw_core { color: var(--core); background: var(--core-bg); }
/* hub map */
.hubmap { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 12px; }
.hub { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.hub header { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.hub header button { font: 600 0.95rem var(--mono); }
.hub .n { font-size: 0.8rem; color: var(--ink-3); white-space: nowrap; }
.hub ul { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 4px 6px; }
.hub li button { font: 400 0.8rem var(--mono); }
button.ent { background: none; border: 0; padding: 0; color: var(--core); cursor: pointer; text-align: left; overflow-wrap: anywhere; }
button.ent:hover { text-decoration: underline; }
button.ent:focus-visible { outline: 2px solid var(--core); outline-offset: 2px; border-radius: 3px; }
.via { font: 400 0.72rem var(--mono); color: var(--ink-3); }
/* explorer */
.explorer { display: grid; grid-template-columns: minmax(220px, 300px) 1fr; gap: 16px; align-items: start; }
.side { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 12px; display: flex; flex-direction: column; gap: 10px; position: sticky; top: 12px; max-height: calc(100vh - 24px); }
.side input { width: 100%; font: inherit; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--ink); }
.side input:focus-visible { outline: 2px solid var(--core); }
.list { overflow-y: auto; display: flex; flex-direction: column; gap: 2px; min-height: 120px; }
.list button { display: flex; justify-content: space-between; gap: 8px; width: 100%; background: none; border: 0; border-radius: 5px; padding: 5px 8px; font: 400 0.82rem var(--mono); color: var(--ink); cursor: pointer; text-align: left; }
.list button:hover { background: var(--bg); }
.list button[aria-current="true"] { background: var(--core-bg); color: var(--core); }
.list button span:last-child { color: var(--ink-3); font-variant-numeric: tabular-nums; }
.detail { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 18px; display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.detail .head { display: flex; flex-direction: column; gap: 6px; }
.detail .head h2 { font-family: var(--mono); font-size: 1.15rem; overflow-wrap: anywhere; }
.kv { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 0.88rem; color: var(--ink-2); }
.kv b { color: var(--ink); font-weight: 600; }
.joins { display: flex; flex-direction: column; gap: 10px; }
.join { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.join .line { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 0.9rem; }
.join .arrow { color: var(--ink-3); }
.join .cols { font: 500 0.82rem var(--mono); background: var(--hi); padding: 1px 6px; border-radius: 4px; }
.join pre { margin: 0; background: var(--code); border-radius: 6px; padding: 8px 10px; overflow-x: auto; font: 400 0.78rem/1.5 var(--mono); }
.join .meta { font-size: 0.78rem; color: var(--ink-3); }
.join .row { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
button.copy { font: 500 0.75rem var(--sans); color: var(--ink); background: var(--bg); border: 1px solid var(--line); border-radius: 5px; padding: 3px 8px; cursor: pointer; }
.empty { color: var(--ink-3); font-size: 0.9rem; }
/* tables */
.tbl { overflow-x: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; }
table { border-collapse: collapse; width: 100%; font-size: 0.86rem; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 0.72rem; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-3); background: var(--bg); }
tr:last-child td { border-bottom: 0; }
td.num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
@media (max-width: 760px) { .explorer { grid-template-columns: 1fr; } .side { position: static; max-height: 360px; } }
</style>
</head>
<body>
<div class="wrap">
<header style="display:flex;flex-direction:column;gap:10px">
  <span class="eyebrow">swcollect · <span class="mono">__INSTANCE__</span></span>
  <h1>sw_raw relationship map</h1>
  <p class="lede">sw_raw keeps one table per SolarWinds entity and has no foreign keys between them on purpose: a raw row is never refused because its parent is missing. swcollect proves the connections against SolarWinds instead. This map shows every proven connection, the columns it joins on, and a ready-made JOIN. Every join also matches <code>instance_id</code>, because a NodeID belongs to one Orion server.</p>
  <div class="stats" id="stats"></div>
</header>

<section>
  <span class="eyebrow">Overview</span>
  <h2>Hubs: the tables everything points to</h2>
  <p class="lede">Each box is a parent table and the tables that reference it. Click any name to open it below.</p>
  <div class="hubmap" id="hubmap"></div>
</section>

<section>
  <span class="eyebrow">Bridges</span>
  <h2>How the hubs connect to each other</h2>
  <p class="lede">These joins link one hub to another, so you can walk from Orion's view of a router to NCM's view of the same router, or from a node to its interfaces.</p>
  <div class="tbl"><table><thead><tr><th>From</th><th>Columns</th><th>To</th><th>Kind</th><th>Proven by</th></tr></thead><tbody id="bridges"></tbody></table></div>
</section>

<section>
  <span class="eyebrow">Explore</span>
  <h2>Every table and its joins</h2>
  <div class="explorer">
    <aside class="side">
      <label for="q" class="eyebrow">Find a table</label>
      <input id="q" type="search" placeholder="e.g. interfaces, ncm, nodeid" autocomplete="off">
      <div class="list" id="list" role="list"></div>
    </aside>
    <div class="detail" id="detail"></div>
  </div>
</section>

<section>
  <span class="eyebrow">The flow onward</span>
  <h2>sw_raw → sw_core</h2>
  <p class="lede">The canonical tables in sw_core are built from these raw tables (latest curate run). sw_core has real foreign keys, so pgAdmin's ERD of sw_core shows its connections directly.</p>
  <div class="tbl"><table><thead><tr><th>sw_core table</th><th>Built from</th><th>Rows in</th><th>Out</th><th>Dead letter</th></tr></thead><tbody id="lineage"></tbody></table></div>
</section>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  var D = JSON.parse(document.getElementById("data").textContent);
  var T = D.tables, E = D.edges;
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    for (var k in attrs || {}) { if (k === "text") n.textContent = attrs[k]; else n.setAttribute(k, attrs[k]); }
    (kids || []).forEach(function (c) { if (c) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return n;
  }
  function locOf(t) { var s = (T[t] || {}).sink || ""; return s.indexOf("sw_raw.") === 0 ? "sw_raw" : s.indexOf("sw_ts.") === 0 ? "sw_ts" : "loki"; }
  function locChip(t) { var s = (T[t] || {}).sink || ""; return el("span", {"class": "loc " + locOf(t), text: s.indexOf("sw_") === 0 ? s : "Loki, not a table"}); }
  function entBtn(t) { var b = el("button", {"class": "ent", type: "button", text: t}); b.addEventListener("click", function () { select(t, true); }); return b; }
  function cols(a) { return a.join(" + "); }
  var parentsOf = {}, childrenOf = {};
  E.forEach(function (e) { (parentsOf[e.child] = parentsOf[e.child] || []).push(e); (childrenOf[e.parent] = childrenOf[e.parent] || []).push(e); });
  var names = Object.keys(T).sort();

  // stats
  var hubs = D.hubs.filter(function (h) { return (childrenOf[h] || []).length >= 2; });
  [[E.length, "proven joins"], [names.length, "tables involved"], [hubs.length, "hubs (2+ children)"], [D.lineage.length, "sw_core tables built from them"]]
    .forEach(function (s) { document.getElementById("stats").appendChild(el("div", {"class": "stat"}, [el("b", {text: String(s[0])}), el("span", {text: s[1]})])); });

  // hub map
  var hm = document.getElementById("hubmap");
  D.hubs.forEach(function (h) {
    var ks = childrenOf[h] || [];
    var ul = el("ul");
    ks.forEach(function (e) { ul.appendChild(el("li", {}, [entBtn(e.child), el("span", {"class": "via", text: " " + cols(e.ccols)})])); });
    hm.appendChild(el("div", {"class": "hub"}, [
      el("header", {}, [entBtn(h), el("span", {"class": "n", text: ks.length + (ks.length === 1 ? " child" : " children")})]),
      locChip(h), ul]));
  });

  // bridges: joins where both sides are hubs
  var hubSet = {}; D.hubs.forEach(function (h) { hubSet[h] = 1; });
  var tb = document.getElementById("bridges");
  E.filter(function (e) { return hubSet[e.child] && hubSet[e.parent]; }).forEach(function (e) {
    tb.appendChild(el("tr", {}, [el("td", {}, [entBtn(e.child)]), el("td", {"class": "mono", text: cols(e.ccols) + (cols(e.ccols) !== cols(e.pcols) ? " = " + cols(e.pcols) : "")}),
      el("td", {}, [entBtn(e.parent)]), el("td", {text: e.card}), el("td", {text: e.origin.join(", ")})]));
  });

  // lineage
  var lt = document.getElementById("lineage");
  D.lineage.forEach(function (f) {
    lt.appendChild(el("tr", {}, [el("td", {}, [el("span", {"class": "loc sw_core", text: f.target})]), el("td", {"class": "mono", text: (f.sources || []).join(", ")}),
      el("td", {"class": "num", text: String(f["in"])}), el("td", {"class": "num", text: String(f.out)}), el("td", {"class": "num", text: String(f.dlq)})]));
  });

  // explorer
  var list = document.getElementById("list"), q = document.getElementById("q"), detail = document.getElementById("detail");
  var current = null;
  function renderList() {
    var s = q.value.trim().toLowerCase();
    list.textContent = "";
    names.filter(function (t) { return !s || t.toLowerCase().indexOf(s) >= 0 || (T[t].sink || "").toLowerCase().indexOf(s) >= 0 ||
      (parentsOf[t] || []).some(function (e) { return cols(e.ccols).toLowerCase().indexOf(s) >= 0; }); })
      .forEach(function (t) {
        var n = (parentsOf[t] || []).length + (childrenOf[t] || []).length;
        var b = el("button", {type: "button", role: "listitem", "aria-current": String(t === current)}, [el("span", {text: t}), el("span", {text: String(n)})]);
        b.addEventListener("click", function () { select(t, false); });
        list.appendChild(b);
      });
    if (!list.children.length) list.appendChild(el("p", {"class": "empty", text: "No table matches."}));
  }
  function joinCard(e, otherIsParent) {
    var other = otherIsParent ? e.parent : e.child;
    var line = el("div", {"class": "line"}, otherIsParent
      ? [el("span", {"class": "cols", text: cols(e.ccols)}), el("span", {"class": "arrow", text: "→"}), entBtn(e.parent), el("span", {"class": "cols", text: cols(e.pcols)})]
      : [entBtn(e.child), el("span", {"class": "cols", text: cols(e.ccols)}), el("span", {"class": "arrow", text: "→"}), el("span", {"class": "cols", text: cols(e.pcols)})]);
    var kids = [line, el("span", {"class": "meta", text: (e.card === "one-to-one" ? "One-to-one (the same records) · " : "Many-to-one · ") + "Proven by " + e.origin.join(", ") + " · " + e.pairs + " values checked" + (e.views.length ? " · view " + e.views[0] : "")})];
    if (e.sql) {
      var pre = el("pre", {text: e.sql});
      var btn = el("button", {"class": "copy", type: "button", text: "Copy JOIN"});
      btn.addEventListener("click", function () {
        var done = function () { btn.textContent = "Copied"; setTimeout(function () { btn.textContent = "Copy JOIN"; }, 1400); };
        var sel = function () { var r = document.createRange(); r.selectNodeContents(pre); var s = window.getSelection(); s.removeAllRanges(); s.addRange(r); btn.textContent = "Selected: Ctrl+C"; };
        try { navigator.clipboard.writeText(e.sql).then(done, sel); } catch (x) { sel(); }
      });
      kids.push(el("div", {"class": "row"}, [el("span", {"class": "meta", text: "Ready JOIN"}), btn]), pre);
    } else {
      kids.push(el("span", {"class": "meta", text: "No SQL: " + other + " is not a table (events go to Loki)."}));
    }
    return el("div", {"class": "join"}, kids);
  }
  function select(t, scroll) {
    current = t;
    var i = T[t];
    detail.textContent = "";
    detail.appendChild(el("div", {"class": "head"}, [el("span", {"class": "eyebrow", text: "SolarWinds entity"}), el("h2", {text: t}),
      el("div", {"class": "kv"}, [locChip(t), el("span", {}, ["Key: ", el("b", {text: (i.key && i.key.length) ? i.key.join(" + ") : "row hash (no key)"})]),
        el("span", {}, ["Fields: ", el("b", {text: i.fields == null ? "–" : String(i.fields)})]), el("span", {}, ["Rows: ", el("b", {text: i.rows == null ? "–" : String(i.rows)})])])]));
    var ps = parentsOf[t] || [], cs = childrenOf[t] || [];
    detail.appendChild(el("h3", {text: "Points to (" + ps.length + ")"}));
    detail.appendChild(ps.length ? el("div", {"class": "joins"}, ps.map(function (e) { return joinCard(e, true); })) : el("p", {"class": "empty", text: "This table does not reference another table."}));
    detail.appendChild(el("h3", {text: "Referenced by (" + cs.length + ")"}));
    detail.appendChild(cs.length ? el("div", {"class": "joins"}, cs.map(function (e) { return joinCard(e, false); })) : el("p", {"class": "empty", text: "No table references this one."}));
    renderList();
    if (scroll) detail.scrollIntoView({behavior: "smooth", block: "start"});
  }
  q.addEventListener("input", renderList);
  select(D.hubs[0] || names[0], false);
})();
</script>
</body>
</html>
"""


def write(conn: psycopg.Connection, instance_id: int, out_dir: Path) -> list[Path]:
    m = model(conn, instance_id)
    files = {"raw-model.html": page(m), "raw-model.md": markdown(m), "raw-model.dbml": dbml(m),
             "raw-joins.csv": csv_text(m)}
    out = []
    for name, text in files.items():
        p = out_dir / name
        p.write_bytes(text.encode("utf-8"))
        out.append(p)
    return out

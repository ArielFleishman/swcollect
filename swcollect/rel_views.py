"""sw_rel: one view per PROVEN relationship, joining the stored tables along it (plan §2b, step 4).

Each view returns, for every live source row, its related live target rows by the proven join columns:
  instance_id, s_<source key>…, t_<target key>…
Then the stored rows are checked against SWIS (stored_status on sw_meta.relationship):
  navigation  for a few source rows, the target keys in the view = the target keys SWIS returns by navigation
  orphans     stored source rows whose join value has no stored target row (e.g. a link to a removed interface)
"""
from __future__ import annotations

import re
from collections import Counter

import psycopg

from .ddl import _short, literal, q
from .swis.client import SwisClient, SwisError


def _swis_param(v):
    """A key value read back from PostgreSQL, as the SWIS JSON parameter it was received as: a uuid column gives
    uuid.UUID, a timestamp column datetime, a numeric column Decimal. None of them is JSON as such."""
    import datetime as _dt
    import decimal
    import uuid
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, _dt.datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] if v.microsecond else v.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def view_name(source_table: str, name: str) -> str:
    return _short(f"{source_table}__{re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')}")


def _ident_cols(entry: dict) -> list[str]:
    return ["_row_hash"] if entry["key_status"] == "row_hash" else list(entry["key"])


def _live(alias: str, sink: str) -> str:
    return f"{alias}._deleted_at IS NULL" if sink.startswith("sw_raw.") else "TRUE"


def build(conn: psycopg.Connection, client: SwisClient | None, instance_id: int, entries: dict[str, dict],
          log=print) -> dict:
    rels = conn.execute(
        """SELECT relationship_id, source_entity, name, target_entity, source_columns, target_columns, method, basis
           FROM sw_meta.relationship WHERE instance_id = %s AND status = 'proven' ORDER BY 2, 3""",
        (instance_id,)).fetchall()
    out = {"views": 0, "skipped": [], "stored": {}}
    for rid, src, name, tgt, scols, tcols, method, basis in rels:
        se, te = entries.get(src), entries.get(tgt)
        if not se or not te or not se.get("sink_table") or not te.get("sink_table"):
            out["skipped"].append(f"{src}.{name}: a side has no stored table")
            continue
        s_sink, t_sink = se["sink_table"], te["sink_table"]
        s_tab, t_tab = s_sink.split(".", 1)[1], t_sink.split(".", 1)[1]
        vname = view_name(s_tab, name)
        s_id, t_id = _ident_cols(se), _ident_cols(te)
        cols = ["s.instance_id"] + [f"s.{q(c)} AS {q('s_' + c)}" for c in s_id]
        cols += [f"s.{q(c)} AS {q('s_' + c)}" for c in scols if c not in s_id]
        cols += [f"t.{q(c)} AS {q('t_' + c)}" for c in t_id]
        join = " AND ".join(f"t.{q(b)} = s.{q(a)}" for a, b in zip(scols, tcols))
        s_from = f"{s_sink.split('.')[0]}.{q(s_tab)}"
        t_from = f"{t_sink.split('.')[0]}.{q(t_tab)}"
        sql = (f"CREATE VIEW sw_rel.{q(vname)} AS SELECT {', '.join(cols)} FROM {s_from} s "
               f"JOIN {t_from} t ON t.instance_id = s.instance_id AND {join} "
               f"WHERE {_live('s', s_sink)} AND {_live('t', t_sink)}")
        try:
            with conn.transaction():
                conn.execute(f"DROP VIEW IF EXISTS sw_rel.{q(vname)}")
                conn.execute(sql)
                conn.execute(f"COMMENT ON VIEW sw_rel.{q(vname)} IS " + literal(
                    f"{src}.{name} -> {tgt} on {list(zip(scols, tcols))}; proven by {method}; basis: {basis}"))
            out["views"] += 1
        except psycopg.Error as e:
            out["skipped"].append(f"{src}.{name}: {str(e).strip().splitlines()[0]}")
            continue
        status, detail, orphans = check_stored(conn, client, instance_id, src, name, se, te, s_sink, t_sink, scols,
                                               tcols, method, vname)
        conn.execute("""UPDATE sw_meta.relationship SET view_name = %s, stored_status = %s, stored_detail = %s,
                            stored_orphans = %s WHERE relationship_id = %s""",
                     (f"sw_rel.{vname}", status, detail, orphans, rid))
        out["stored"][status] = out["stored"].get(status, 0) + 1
    return out


def check_stored(conn, client, instance_id, src, name, se, te, s_sink, t_sink, scols, tcols, method, vname):
    s_schema, s_tab = s_sink.split(".", 1)
    t_schema, t_tab = t_sink.split(".", 1)
    nn = " AND ".join(f"s.{q(c)} IS NOT NULL" for c in scols)
    join = " AND ".join(f"t.{q(b)} = s.{q(a)}" for a, b in zip(scols, tcols))
    orphans = conn.execute(
        f"""SELECT count(*) FROM {s_schema}.{q(s_tab)} s WHERE s.instance_id = %s AND {_live('s', s_sink)} AND {nn}
            AND NOT EXISTS (SELECT 1 FROM {t_schema}.{q(t_tab)} t WHERE t.instance_id = s.instance_id AND {join}
                            AND {_live('t', t_sink)})""", (instance_id,)).fetchone()[0]
    # Orphans only make sense when the source refers to the target's key (many → one). When the join columns are the
    # source's own key (one → many, e.g. Orion.Nodes.Interfaces), a source row without related rows is allowed.
    to_parent = list(tcols) == _ident_cols(te) and list(scols) != _ident_cols(se)
    if to_parent:
        note = f"{orphans} stored source rows have no stored target row" if orphans else "no orphans in the stored rows"
    else:
        note = f"{orphans} stored source rows have no related rows (allowed: one-to-many)"
        orphans = 0
    if method != "navigation" or client is None or se["key_status"] == "row_hash" or te["key_status"] == "row_hash":
        return ("orphans" if orphans else "consistent"), note, orphans
    skey, tkey = list(se["key"]), [k for k in te["key"] if k != te.get("time_field")] or list(te["key"])
    sample = conn.execute(
        f"SELECT DISTINCT {', '.join(q('s_' + k) for k in skey)} FROM sw_rel.{q(vname)} WHERE instance_id = %s "
        f"LIMIT 3", (instance_id,)).fetchall()
    if not sample:
        return "not_checked", f"no stored related rows to compare with SWIS navigation; {note}", orphans
    diffs, kept = [], 0
    for kv in sample:
        where = " AND ".join(f"s.{k} = @k{i}" for i, k in enumerate(skey))
        try:
            rows = client.query(f"SELECT s.{name}.{tkey[0]} AS T FROM {src} s WHERE {where}",
                                {f"k{i}": _swis_param(v) for i, v in enumerate(kv)}).rows
        except SwisError as e:
            return "error", f"navigation query failed: {e}; {note}", orphans
        swis = Counter(str(r["T"]).lower() for r in rows if r.get("T") is not None)
        cond = " AND ".join(f"{q('s_' + k)} = %s" for k in skey)
        stored = Counter(str(r[0]).lower() for r in conn.execute(
            f"SELECT {q('t_' + tkey[0])} FROM sw_rel.{q(vname)} WHERE instance_id = %s AND {cond}",
            (instance_id, *kv)).fetchall())
        # A metrics target keeps history SWIS no longer has: there everything SWIS returns must be stored (subset).
        missing = swis - stored
        if (t_schema == "sw_ts" and missing) or (t_schema != "sw_ts" and swis != stored):
            diffs.append(f"{dict(zip(skey, kv))}: SWIS {sum(swis.values())} vs stored {sum(stored.values())}")
        elif t_schema == "sw_ts" and stored - swis:
            kept += sum((stored - swis).values())
    if kept:
        note = f"{note}; {kept} stored samples SWIS no longer returns (kept)"
    if diffs:
        return "differs", "stored pairs differ from SWIS navigation: " + "; ".join(diffs) + f"; {note}", orphans
    return ("match" if not orphans else "match_with_orphans"), (
        f"stored pairs = SWIS navigation for {len(sample)} source rows; {note}"), orphans

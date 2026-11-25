"""Relationships between entities, taken from SolarWinds itself and proven before use (plan §2b, steps 1–3).

Candidates:
  navigation  a navigation property in Metadata.Property (IsNavigable, Type = target entity)
  metadata    a Metadata.Relationship row with no navigation property on this server
  override    catalog/collector.yaml `relationships` (columns given, each with a documentation basis)
  name_match  a column named like an owner entity's single key (NodeID → Orion.Nodes), same type

SWIS does not publish the join columns. For navigation/metadata they are inferred (the target's key present by
name and type in the source, or the source's key present in the target) and then proven:
  navigation  SELECT … s.<Nav>.<col> … FROM <Source> s on a sample: every pair must agree on the join columns,
              and for a few source rows the number of related rows must equal COUNT on the target
  values      a few distinct source values must each exist in the target (overrides, name matches)
A link is used (phase 2) only with status `proven`. Nothing here writes to SWIS.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .swis.client import SwisClient, SwisError

COLLECTABLE = ("inventory", "metrics", "events")


@dataclass
class Relationship:
    source: str
    name: str
    target: str
    origin: str
    source_columns: list[str] = field(default_factory=list)
    target_columns: list[str] = field(default_factory=list)
    columns_origin: str = "none"
    cardinality: str | None = None
    basis: str | None = None
    method: str | None = None
    status: str = "not_proven"
    pairs_checked: int = 0
    mismatches: int = 0
    sources_checked: int = 0
    detail: str = ""
    proof_swql: list = field(default_factory=list)
    options: list = field(default_factory=list)       # [(source cols, target cols)], smallest first; first proven wins

    def as_dict(self) -> dict:
        return asdict(self)


def _types(entry: dict) -> dict[str, str]:
    return {c["name"]: c["pg_type"] for c in entry.get("columns") or []}


def _key(entry: dict) -> list[str]:
    if entry.get("key_status") in (None, "row_hash"):
        return []
    return [k for k in entry.get("key") or [] if k != entry.get("time_field")]


def join_options(src: dict, tgt: dict) -> list[tuple[list[str], list[str]]]:
    """Column sets shared by name and type: the source's key in the target (one → many) and the target's key in the
    source (many → one), smallest first. Each is only a candidate until the proof agrees with navigation."""
    st, tt = _types(src), _types(tgt)
    out: list[tuple[list[str], list[str]]] = []
    for key in (_key(src), _key(tgt)):
        if key and all(k in st and k in tt and st[k] == tt[k] for k in key) and (list(key), list(key)) not in out:
            out.append((list(key), list(key)))
    return sorted(out, key=lambda o: len(o[0]))


def infer_join(src: dict, tgt: dict) -> tuple[list[str], list[str]]:
    opts = join_options(src, tgt)
    return (list(opts[0][0]), list(opts[0][1])) if opts else ([], [])


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def candidates(entries: dict[str, dict], md_rels: list[dict], rules: dict) -> list[Relationship]:
    live = {n: e for n, e in entries.items() if e.get("collect") and e["class"] in COLLECTABLE}
    card = {}
    for r in md_rels:
        card[(r.get("SourceType"), r.get("SourcePropertyName"), r.get("TargetType"))] = r
    out: list[Relationship] = []
    seen: set[tuple] = set()

    def add(rel: Relationship):
        sig = (rel.source, rel.target, tuple(rel.source_columns), tuple(rel.target_columns))
        if rel.source_columns and sig in seen:
            return
        seen.add(sig)
        out.append(rel)

    # 1. navigation properties
    for name, e in live.items():
        for prop, target in (e.get("navigation") or {}).items():
            if target not in live:
                continue
            md = card.get((name, prop, target)) or {}
            rel = Relationship(name, prop, target, "navigation", cardinality=_card(md),
                               basis="Metadata.Property (IsNavigable)" + (" + Metadata.Relationship" if md else ""))
            rel.options = join_options(e, live[target])
            rel.source_columns, rel.target_columns = infer_join(e, live[target])
            rel.columns_origin = "inferred" if rel.source_columns else "none"
            add(rel)
    # 2. Metadata.Relationship rows without a navigation property here
    nav_pairs = {(r.source, r.name) for r in out}
    for r in md_rels:
        s, t, p = r.get("SourceType"), r.get("TargetType"), r.get("SourcePropertyName")
        if s in live and t in live and (s, p) not in nav_pairs:
            rel = Relationship(s, p or t.rsplit(".", 1)[-1], t, "metadata", cardinality=_card(r),
                               basis="Metadata.Relationship")
            rel.options = join_options(live[s], live[t])
            rel.source_columns, rel.target_columns = infer_join(live[s], live[t])
            rel.columns_origin = "inferred" if rel.source_columns else "none"
            add(rel)
    # 3. overrides
    for o in rules.get("relationships") or []:
        if o["source"] not in live or o["target"] not in live:
            continue
        if not o.get("basis"):
            continue                              # a link without a basis is not added
        add(Relationship(o["source"], o.get("name") or o["target"].rsplit(".", 1)[-1], o["target"], "override",
                         [c[0] for c in o["columns"]], [c[1] for c in o["columns"]], "override", basis=o["basis"]))
    # A navigation property whose columns cannot be inferred by name may match an override for the same pair of
    # entities (e.g. NCM.Nodes.CoreNodeID → Orion.Nodes.NodeID): navigation then proves the override's columns,
    # which is stronger than values alone, and the override entry is folded into it.
    for o in [r for r in out if r.origin == "override"]:
        for nav in out:
            if nav.origin == "navigation" and nav.source == o.source and nav.target == o.target and not nav.options:
                nav.options = [(o.source_columns, o.target_columns)]
                nav.source_columns, nav.target_columns = list(o.source_columns), list(o.target_columns)
                nav.columns_origin = "override"
                nav.basis = f"{nav.basis}; columns from override {o.name}: {o.basis}"
                out.remove(o)
                break
    # 4. name matches
    nm = rules.get("name_match") or {}
    if nm.get("enabled", True):
        ignore = {c.lower() for c in nm.get("ignore_columns") or []}
        owners: dict[str, list[str]] = {}
        for name, e in live.items():
            k = _key(e)
            if len(k) == 1 and k[0].lower() == f"{_singular(name.rsplit('.', 1)[-1])}ID".lower():
                owners.setdefault(k[0].lower(), []).append(name)
        for name, e in live.items():
            types = _types(e)
            for col, typ in types.items():
                # The owner itself is excluded below (o != name). An entity whose own key is the same column
                # (Orion.NodesCustomProperties, Orion.CPULoad: NodeID + time) is still linked to its owner.
                if col.lower() in ignore or col.lower() not in owners:
                    continue
                cands = [o for o in owners[col.lower()] if o != name and _types(live[o]).get(_key(live[o])[0]) == typ]
                same_ns = [o for o in cands if o.split(".")[0] == name.split(".")[0]]
                for o in same_ns or cands:
                    tk = _key(live[o])[0]
                    add(Relationship(name, f"{col}:{o.rsplit('.', 1)[-1]}", o, "name_match", [col], [tk],
                                     "inferred", basis=f"column {col} = key of {o} (same name and type)"))
    return out


def _card(md: dict) -> str | None:
    if not md or md.get("SourceCardinalityMin") is None:
        return None
    return (f"{md.get('SourceCardinalityMin')}..{md.get('SourceCardinalityMax')} -> "
            f"{md.get('TargetCardinalityMin')}..{md.get('TargetCardinalityMax')}")


# ---------------------------------------------------------------------------------------------------------------
# Proof
# ---------------------------------------------------------------------------------------------------------------
def _norm(v):
    return str(v).lower() if isinstance(v, str) else v


def _q(client: SwisClient, rel: Relationship, swql: str, params: dict | None = None) -> list[dict]:
    rel.proof_swql.append({"swql": swql, "parameters": params or {}})
    return client.query(swql, params).rows


def prove(client: SwisClient, rel: Relationship, entries: dict[str, dict], rules: dict) -> Relationship:
    if not rel.source_columns:
        rel.status, rel.detail = "not_proven", ("no join columns could be inferred from key names and types; "
                                                "add an override with a documentation basis")
        return rel
    p = rules.get("proof") or {}
    try:
        if rel.origin == "navigation":
            rel.method = "navigation"
            tried = []
            for opt in rel.options or [(rel.source_columns, rel.target_columns)]:
                rel.source_columns, rel.target_columns = list(opt[0]), list(opt[1])
                rel.pairs_checked = rel.mismatches = rel.sources_checked = 0
                _prove_navigation(client, rel, entries[rel.source], int(p.get("sample_pairs", 500)),
                                  int(p.get("check_sources", 3)))
                if rel.status == "proven":
                    break
                tried.append(f"{opt}: {rel.status} ({rel.detail})")
            if rel.status != "proven" and len(tried) > 1:
                first = (rel.options or [([], [])])[0]
                rel.source_columns, rel.target_columns = list(first[0]), list(first[1])
                rel.detail = "no inferred column set agrees with navigation. Tried " + " | ".join(tried)
            elif tried and rel.status == "proven":
                rel.detail += ". Rejected first: " + " | ".join(tried)
        else:
            rel.method = "values"
            _prove_values(client, rel, int(p.get("sample_pairs", 500)), int(p.get("check_values", 5)))
    except SwisError as e:
        rel.status, rel.detail = "error", f"SWIS error while proving: {e}"
    return rel


def _target_count(client, rel, values: tuple) -> int:
    where = " AND ".join(f"t.{c} = @v{i}" for i, c in enumerate(rel.target_columns))
    rows = _q(client, rel, f"SELECT COUNT(t.{rel.target_columns[0]}) AS N FROM {rel.target} t WHERE {where}",
              {f"v{i}": v for i, v in enumerate(values)})
    return int((rows[0] if rows else {}).get("N") or 0)


def _prove_navigation(client: SwisClient, rel: Relationship, src: dict, n: int, check: int):
    skey = _key(src)
    sel = [f"s.{c} AS S_{i}" for i, c in enumerate(rel.source_columns)]
    sel += [f"s.{rel.name}.{c} AS T_{i}" for i, c in enumerate(rel.target_columns)]
    sel += [f"s.{c} AS K_{i}" for i, c in enumerate(skey)]
    rows = _q(client, rel, f"SELECT TOP {n} " + ", ".join(sel) + f" FROM {rel.source} s")
    k = len(rel.source_columns)
    linked = [r for r in rows if r.get("T_0") is not None]
    rel.pairs_checked = len(linked)
    rel.mismatches = sum(1 for r in linked if any(_norm(r[f"S_{i}"]) != _norm(r[f"T_{i}"]) for i in range(k)))
    if not linked:
        rel.status, rel.detail = "not_proven", ("navigation returned no related rows in the sample "
                                                "(an empty result does not prove absence)")
        return
    if rel.mismatches:
        rel.status = "mismatch"
        rel.detail = f"{rel.mismatches} of {len(linked)} navigation pairs disagree on the inferred join columns"
        return
    if not skey:
        rel.status, rel.detail = "partial", (f"{len(linked)} navigation pairs agree on the join columns; "
                                             "completeness not checked (source has no key)")
        return
    seen, diffs = [], []
    for r in linked:
        kv = tuple(r[f"K_{i}"] for i in range(len(skey)))
        if kv in seen:
            continue
        seen.append(kv)
        where = " AND ".join(f"s.{c} = @k{i}" for i, c in enumerate(skey))
        nav = _q(client, rel, f"SELECT s.{rel.name}.{rel.target_columns[0]} AS T FROM {rel.source} s WHERE {where}",
                 {f"k{i}": v for i, v in enumerate(kv)})
        nav_n = sum(1 for x in nav if x.get("T") is not None)
        tgt_n = _target_count(client, rel, tuple(r[f"S_{i}"] for i in range(k)))
        if nav_n != tgt_n:
            diffs.append(f"{dict(zip(skey, kv))}: navigation {nav_n}, join {tgt_n}")
        if len(seen) >= check:
            break
    rel.sources_checked = len(seen)
    if diffs:
        rel.status, rel.detail = "mismatch", "related-row counts differ: " + "; ".join(diffs)
    else:
        rel.status = "proven"
        on = ", ".join(f"{a} = {b}" for a, b in zip(rel.source_columns, rel.target_columns))
        rel.detail = (f"{len(linked)} navigation pairs agree on {on}; related-row counts match the join for "
                      f"{len(seen)} source rows")


def _prove_values(client: SwisClient, rel: Relationship, n: int, check: int):
    cols = ", ".join(f"s.{c} AS S_{i}" for i, c in enumerate(rel.source_columns))
    nn = " AND ".join(f"s.{c} IS NOT NULL" for c in rel.source_columns)
    rows = _q(client, rel, f"SELECT TOP {n} {cols} FROM {rel.source} s WHERE {nn}")
    values: list[tuple] = []
    for r in rows:
        v = tuple(r[f"S_{i}"] for i in range(len(rel.source_columns)))
        if v not in values:
            values.append(v)
    rel.pairs_checked = len(rows)
    if not values:
        rel.status, rel.detail = "not_proven", "no non-null source values in the sample"
        return
    missing = [v for v in values[:check] if _target_count(client, rel, v) == 0]
    rel.sources_checked = min(check, len(values))
    rel.mismatches = len(missing)
    if not missing:
        rel.status = "proven"
        rel.detail = f"{rel.sources_checked} distinct source values all found in {rel.target}"
    elif len(missing) < rel.sources_checked:
        rel.status = "partial"
        rel.detail = (f"{len(missing)} of {rel.sources_checked} source values not found in {rel.target} "
                      f"(orphans or permission filtering): {missing}")
    else:
        rel.status = "mismatch"
        rel.detail = f"none of {rel.sources_checked} source values found in {rel.target}: {missing}"


def discover_relationships(client: SwisClient, catalog: dict, rules: dict, prove_them: bool = True) -> list[Relationship]:
    schema = catalog.get("_schema")
    rels = candidates(catalog["entities"], schema.relationships if schema else [], rules)
    if prove_them:
        for r in rels:
            prove(client, r, catalog["entities"], rules)
    return rels

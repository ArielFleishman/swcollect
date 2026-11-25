# Copied from swcheck swcheck/metadata.py at commit 0958074 (2026-11-18). Local changes: import paths only.
"""SWIS schema discovery through the Metadata.* entities (read-only)."""
from __future__ import annotations

import fnmatch

from .client import SwisClient, SwisError


def like_to_glob(pattern: str) -> str:
    return pattern.replace("%", "*").replace("_", "?")


class Metadata:
    def __init__(self, client: SwisClient):
        self.client = client
        self.entities: dict[str, dict] = {}
        self._props: dict[str, list[dict]] = {}
        self._verbs: dict[str, list[str]] | None = None

    def load_entities(self) -> dict[str, dict]:
        try:
            rows = self.client.query(
                "SELECT FullName, BaseType, IsAbstract, IsIndication, CanCreate, CanUpdate, CanDelete "
                "FROM Metadata.Entity").rows
        except SwisError:
            rows = self.client.query("SELECT FullName FROM Metadata.Entity").rows
        self.entities = {r["FullName"]: r for r in rows}
        return self.entities

    def _lookup(self, name: str) -> str | None:
        if name in self.entities:
            return name
        low = name.lower()
        for k in self.entities:
            if k.lower() == low:
                return k
        return None

    def has_entity(self, name: str) -> bool:
        return self._lookup(name) is not None

    def canonical(self, name: str) -> str | None:
        return self._lookup(name)

    def match(self, pattern: str) -> list[str]:
        g = like_to_glob(pattern).lower()
        out = []
        for name, e in self.entities.items():
            if e.get("IsAbstract") or e.get("IsIndication"):
                continue
            if fnmatch.fnmatchcase(name.lower(), g):
                out.append(name)
        return sorted(out)

    def properties(self, entity: str) -> list[dict]:
        entity = self._lookup(entity) or entity
        if entity not in self._props:
            try:
                rows = self.client.query(
                    "SELECT Name, Type, IsNavigable, IsKey FROM Metadata.Property WHERE EntityName = @e",
                    {"e": entity}).rows
            except SwisError:
                rows = self.client.query(
                    "SELECT Name, Type FROM Metadata.Property WHERE EntityName = @e", {"e": entity}).rows
            self._props[entity] = rows
        return self._props[entity]

    def scalar_fields(self, entity: str) -> list[str]:
        out = []
        for p in self.properties(entity):
            if p.get("IsNavigable"):
                continue
            t = (p.get("Type") or "").lower()
            # Skip large blobs; they are fetched explicitly when needed.
            if "byte[]" in t:
                continue
            out.append(p["Name"])
        return out

    def key_fields(self, entity: str) -> list[str]:
        """The entity's key properties (Metadata.Property.IsKey), used to make paging order stable."""
        return [p["Name"] for p in self.properties(entity) if p.get("IsKey") and not p.get("IsNavigable")]

    def field_types(self, entity: str) -> dict[str, str]:
        return {p["Name"]: p.get("Type") or "" for p in self.properties(entity)}

    def field_map(self, entity: str) -> dict[str, str]:
        """lower-case name -> real name for scalar fields."""
        return {f.lower(): f for f in self.scalar_fields(entity)}

    def verbs(self) -> dict[str, list[str]]:
        if self._verbs is None:
            rows = self.client.query("SELECT EntityName, Name FROM Metadata.Verb").rows
            v: dict[str, list[str]] = {}
            for r in rows:
                v.setdefault(r["EntityName"], []).append(r["Name"])
            self._verbs = v
        return self._verbs

    def has_verb(self, entity: str, verb: str) -> bool:
        ent = self._lookup(entity) or entity
        return verb in self.verbs().get(ent, [])

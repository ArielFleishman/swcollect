# swcollect: high-level design

## 1. Purpose and scope

swcollect continuously collects everything SolarWinds Orion exposes through SWIS, read-only, and stores each kind of data where it fits. Each stored item can be traced back to what SolarWinds returned.

It covers the SolarWinds side of stage B (SPEC-B):
- canonical entities (§4)
- stable identity per network and instance (§6)
- what is pulled and how often (§7, proposals)
- raw → normalized → curated, with a dead-letter queue (§9)
- quality measurements (§11)
- the data dictionary and lineage (§13.3, §15)

It does **not** include:
- plan‑e, IPAM or Muse adapters, or the plan-vs-reality engine (§10)
- the one-way-network exporter (§8)

Stage A (swcheck) remains the tool that proves API coverage. swcollect is the continuous collector that the proof enables.

## 2. Architecture

```
 Orion SWIS :17774 (read-only account)                     Orion alert action (HTTP POST)
   │  POST /Query  — SWQL, WITH ROWS paging, always filtered      │
   ▼                                                              ▼
 ┌──────────────────────────── swcollect serve ─────────────────────────────┐
 │ scheduler (one thread per network, jobs in order, advisory locks)       │
 │   discover → inventory(+curate) → inventory_core → routing → metrics    │
 │   → events(+reconcile) → verify                                         │
 │ push receiver  /hook/<network>                                          │
 └───────┬───────────────────────────┬──────────────────────────┬──────────┘
         ▼                           ▼                          ▼
  PostgreSQL 16                TimescaleDB (same DB)          Loki 3
  sw_meta  bookkeeping         sw_ts  one hypertable          events, alerts, syslog,
  sw_raw   1 table/entity             per metrics entity      traps, audit, config lines,
  sw_rel   proven joins                                       pushed alerts
  sw_core  canonical + FKs
         ▲                           ▲                          ▲
         └───────── Grafana (read-only: role swcollect_ro, and Loki) ─────────┘
```

All components run as containers on one internal Docker network with no route out (`deploy/docker-compose.yml`). The images are pinned, and the bundle is installed from local files (`scripts/bundle_offline.ps1` / `load_offline.*`).

## 3. What is collected: "everything", with reasons

`discover` reads `Metadata.Entity`, `Metadata.Property` (including the properties inherited through `BaseType`) and `Metadata.Relationship` from the installed server. Every entity is then classified by [catalog/collector.yaml](../catalog/collector.yaml):

| Class | Rule | Sink |
|---|---|---|
| excluded | abstract, indication, `Metadata.*`, `System.*`, or a named exclusion (e.g. `Orion.Netflow.Flows`), each with its reason | — (listed in coverage) |
| events | `Orion.Events`, `Orion.AlertHistory`, `Orion.OLM.LogEntry`, `Orion.AuditingEvents`, `NCM.ConfigArchive` | Loki (+ `sw_raw` for configs) |
| metrics | a time field + `Archive`, or a statistics name | `sw_ts` |
| inventory | everything else | `sw_raw` → `sw_core` |

The result is written per network to `catalog/discovered/<network_id>.yaml`, because modules differ per network. It is also stored in `sw_meta.entity_catalog`.

**Identity of each entity:**
- `Metadata.Property.IsKey`, when the server gives one.
- Otherwise, the smallest set of ID columns (+ the time field) that is unique on a sample of real rows. This is marked `sample_unique`: it is not proven.
- Otherwise, the row hash.

Each entity is also compared with the bundled OrionSDK 2026.2 documentation (`doc_check`).

## 4. Data flows

| Kind | Pull | Store | Change handling |
|---|---|---|---|
| Inventory | full: `COUNT(*)`, then `SELECT` of every column, paged in key order; daily, plus hourly for core entities and every 15 min for routing | `sw_raw.<entity>`: SWIS names verbatim, `_raw jsonb` exactly as received, UPSERT by key | field diff to `change_log`; soft delete only after a complete, non-empty pull; null/duplicate keys go to `raw_quarantine` |
| Metrics | first run backfills everything Orion holds (24 h slices); then `time > watermark − overlap AND time <= MAX`; interval from Orion's `StatCollection` | `sw_ts.<entity>` hypertable, UNIQUE (instance, key, time); compression after 7 days | a recomputed sample is updated and diffed; samples SWIS no longer returns are kept |
| Events | `id > @LastID AND time >= last − 24 h AND id <= MAX(id)`; first run 30 days; configs by time + overlap | Loki: labels `job, network, instance, source, kind`; line = the row as received; structured metadata = id, node, time as received, the time rule | watermark moves only after Loki 204 + commit; refused entries go to `loki_rejected` and are re-sent |
| Alerts (push) | Orion alert action → `/hook/<network>` | `push_message` exactly as received + Loki `kind=alert_push` | reconciled with `Orion.AlertHistory`: matched / poll_only / push_only |

**Every query is filtered and paged.** None of them is a `SELECT *`: each lists its columns and uses `WITH ROWS`. Each query that reads data has a time or ID filter, except the full inventory pulls, which read the whole entity with `COUNT(*)` as their bound.

## 5. Relationships and the canonical model

**Relationship candidates** come from SolarWinds itself:
- navigation properties and `Metadata.Relationship`
- overrides in `collector.yaml`, each with a documentation basis
- name matches (e.g. `NodeID` → `Orion.Nodes`)

**Proof.** SWIS does not publish join columns, so they are inferred and then proven against SWIS:
- **Navigation:** the inferred join must agree with `s.<Nav>.<col>` pairs and with related-row counts.
- **Overrides and name matches:** by values.

Only `proven` relationships are used. The results are in `sw_meta.relationship`, with the SWQL used for each proof.

| Layer | Content |
|---|---|
| `sw_rel` | one view per proven relationship over the stored tables. The stored pairs are re-checked against SWIS navigation (for metrics targets: everything SWIS returns must be stored). |
| `sw_core` | canonical entities from [catalog/core.yaml](../catalog/core.yaml) (names, stable IDs and refs from swcheck's `contracts.yaml`), built by SQL flows in topological order. A reference becomes a real FOREIGN KEY only when a proven relationship backs it. Records that break `key_not_null`, `key_unique` or `ref` go to `sw_core.dead_letter` with the raw row; they are resolved, never deleted. |
| lineage | every record keeps `raw_table`, `raw_key` and the list of `changes`. `sw_core.v_<table>_before_after` shows the raw row, our record and the changes side by side. `sw_meta.flow` records each flow (in = out + dead-letter). |
| `sw_ts.v_<table>` | metrics joined to `sw_core.device` / `interface` along a proven relationship. There is no hard FK, so inventory lag never drops a sample. |

**Stable ID:** `<network_id>/<host:port>/<SWIS entity>/<key=value;…>`. A local NodeID is not global.

## 6. Time

**Storage.** Times are stored exactly as received: `timestamp` without time zone, and the original value in `_raw`.

**Loki.** Loki needs epoch time, so each event source declares `time_zone: server_local | utc`.
- `server_local` subtracts the measured `GETDATE() − GETUTCDATE()` offset. A value with `Z` or an offset is read as such.
- The rule needs proof per version. A source whose newest event lands in the future of the server clock is flagged.

**Reconciliation** corrects history times by the Orion clock's skew against ours.

## 7. Reliability: nothing is lost silently

**Recorded per run:**
- Every job writes a `sync_run`.
- Every entity gets a `coverage` row (SWIS COUNT / received / stored / status / note), including excluded ones, with their reason.

**Watermarks** move only after the sink confirmed: a PostgreSQL commit, a Loki 204.
- PostgreSQL or Loki down: status `error`, the watermark stays, and the next run fills the gap.
- If PostgreSQL is down, the push receiver answers 503 (Orion logs it) and the poll still brings the alert in.

**Retries.** Connection errors, timeouts and 5xx are retried with backoff. 4xx are not.

**Parked rows** are kept, never dropped:
- `raw_quarantine`: rows the key cannot place
- `loki_rejected`: entries Loki refused (re-sent each run)
- `sw_core.dead_letter`: records that break a rule
- `alert_reconcile` `poll_only`: pushes that never arrived

**Scheduler.** One thread per network runs its jobs in order. An advisory lock per (instance, job) prevents double runs across collectors. A failure is recorded and retried after 5 minutes; it never stops the service.

## 8. Security

**Read-only.** The client sends only `POST /Query` with SWQL that starts with `SELECT` (plus `GET` by `swis://` Uri, and the read-only verbs that are allowed). A profile with `actions_enabled` is refused.

**Secrets:**
- SWIS passwords come from the environment or the config. Secret fields in responses are redacted before storage (swcheck's `redact`).
- The push token (`X-Swcollect-Token` or `?token=`) is never stored or logged.

**Network:**
- No telemetry: TimescaleDB uses `telemetry_level=off`, and Loki uses `reporting_enabled: false`.
- The compose network is `internal: true`. Only the push receiver can be published, through `docker-compose.hook.yml`.

## 9. Verification

| Tool | What it checks |
|---|---|
| `swcollect verify` (hourly) | catalog reasons, inventory and metrics coverage, duplicate samples, Loki completeness by ID per event source, relationships whose stored rows differ from SWIS, flow balance, parked rows, push vs poll, job freshness → `reports/verify-*.json` |
| `swcollect e2e` / `scripts/e2e.ps1` | the full cycle on the closed network against the mock: first run, coverage, source changes, metrics re-aggregation, lost pushes filled by the poll, Loki completeness, a Loki outage, and no route out |
| unit + integration tests | `tests/`; the integration tests run with `SWCOLLECT_IT=1` against PostgreSQL + Loki |
| `swcollect dbstats` | rows per schema and table; every empty collected table with its coverage status |
| Grafana dashboards | `swcollect dashboards` → `deploy/grafana/dashboards`; `tests/test_dashboards.py` runs every panel query |
| rich mock (`mock --rich`) | realistic rows for every schema entity, 14 days of history, events; feeds the demo through the normal jobs |

## 10. Proposals and items that need proof

**Proposals, not approved** ("מוצע – טרם אושר"):
- every schedule interval
- compression after 7 days
- retention (off)
- backfill depth (30 days)
- overlap windows

**Needs proof on a real Orion:**
- the entity alias + navigation SWQL form (`FROM Orion.Nodes s` … `s.Interfaces.X`)
- the column names of `Metadata.Relationship` and the `IsInherited` property (discovery falls back and notes it)
- time literals in SWQL filters
- each event source's time zone
- the alert action's variable syntax, and whether it can send headers

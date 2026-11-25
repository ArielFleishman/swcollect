# swcollect

swcollect is a read-only collector that pulls everything SolarWinds Orion (SWIS) exposes and stores it by kind:

| Kind | Examples | Sink | Method |
|---|---|---|---|
| Inventory and state | nodes, interfaces, IPs, OS versions, topology | PostgreSQL | full pull daily or hourly, UPSERT by key |
| Metrics | CPU, memory, interface bandwidth, latency | TimescaleDB | every 5 minutes (aligned to Orion's StatCollection), append with a timestamp |
| Events and alerts | alert history, syslog, traps, configuration changes | Loki | incremental (`ID > @LastID`), plus push from an Orion alert action with a safety-net poll |

It is a separate project from [swcheck](../swcheck). The SWIS client, the metadata reader and the mock SWIS are copied from swcheck, and each copied file names the source commit in its first line.

## Status: all five phases built

| Phase | What | State |
|---|---|---|
| 0 | skeleton, Docker stack on an internal network, offline bundle | done |
| 1 | discovery, classification, generated tables, relationships proven against SWIS | done |
| 2 | inventory → `sw_raw` (UPSERT, change log, soft delete) → `sw_core` (FKs, DLQ), `sw_rel` views, flows | done |
| 3 | metrics → TimescaleDB | done |
| 4 | events → Loki, webhook + safety-net poll | done |
| 5 | scheduler (`serve`), `verify`, docs, e2e | done |
| + | rich mock data through the collector into PostgreSQL, Grafana dashboards | done |

## Discovery: everything the server exposes

```powershell
.\.venv\Scripts\python -m swcollect discover --network mock        # add --no-db to only write the YAML
.\.venv\Scripts\python -m swcollect ddl --network mock --entity Orion.Nodes
```

`discover` works from what the installed server says about itself, not from a fixed list.

1. **Reads the server's schema:**
   - `Metadata.Entity`, `Metadata.Property` (with the properties inherited through `BaseType`) and `Metadata.Relationship`
   - the server clock (its UTC offset) and `Orion.InstalledModule`
2. **Classifies every entity** as inventory, metrics, events or excluded, by the rules in [catalog/collector.yaml](catalog/collector.yaml). An excluded entity keeps its reason.
3. **Settles each entity's identity:**
   - `Metadata.Property.IsKey`, when the server gives one
   - otherwise, the smallest set of `*ID` / `*Index` columns (+ the time field) that is unique on a sample of real rows; this is marked `sample_unique` (not proven)
   - otherwise, the row hash
4. **Compares each entity with the OrionSDK 2026.2 documentation** (`doc_check`: documented fields missing on the server, and server fields not in the docs).
5. **Finds and proves the relationships between entities:**
   - **Candidates:** navigation properties, `Metadata.Relationship`, overrides in `collector.yaml` (each with a documentation basis), and name matches (`NodeID` → `Orion.Nodes`).
   - **Join columns:** SWIS does not publish them, so they are inferred and then proven. Navigation links are proven by comparing `s.<Nav>.<col>` pairs and related-row counts with the inferred join. Overrides and name matches are proven by checking that sampled source values exist in the target.
   - **What is stored:** status, detail and the exact SWQL used, in `sw_meta.relationship`. Only `proven` links will be used for views and foreign keys.
6. **Writes `catalog/discovered/<network_id>.yaml`** (generated, one per network, because modules differ per network). It also writes `sw_meta.entity_catalog` and creates or evolves:
   - one `sw_raw.<entity>` table per inventory entity: SWIS names verbatim, `_raw jsonb`, no content FKs
   - one `sw_ts.<entity>` hypertable per metrics entity

The mock can serve every entity page of the bundled documentation with synthetic rows (`swcollect mock --schema-all`), so discovery of "everything" is tested offline. Against it, the run finds:
- 231 entities: 214 collected, 17 excluded with a reason
- 79 relationship candidates: 57 proven. The rest are kept with their reason, for example random synthetic values or no rows to sample.

## Inventory, relationships and the canonical model

```powershell
.\.venv\Scripts\python -m swcollect inventory --network mock     # sw_raw: full pull, UPSERT, change log
.\.venv\Scripts\python -m swcollect curate --network mock        # sw_rel views + flows into sw_core
.\.venv\Scripts\python -m swcollect erd --network mock           # docs/data-model.* (sw_core) + docs/raw-model.* (sw_raw)
```

**inventory** pulls every inventory entity in full (COUNT, then `SELECT` of every collected column, paged in key order) and writes `sw_raw.<entity>`:

- **UPSERT by key.** The raw row is kept as received in `_raw`. A changed row is diffed field by field into `sw_meta.change_log` (`update` with `{field: [old, new]}`, `insert`, `soft_delete`, `undelete`). A second run with no changes at the source writes nothing.
- **Quarantine.** A row whose key is null, or whose key appears twice in one pull, goes to `sw_meta.raw_quarantine` with the raw row. Neither duplicate is chosen.
- **Soft delete** (`_deleted_at`) happens only after a complete pull: no error, received = SWIS COUNT, no duplicate keys, and a non-empty result. An empty result never deletes anything, because it may be permission filtering.
- **Coverage.** Every entity in the catalog gets a row in `sw_meta.coverage` for the run: SWIS COUNT, received, stored, quarantined, status (`ok`, `empty`, `mismatch`, `error`, `excluded`, `other_job`) and a note.
- **Retries.** Connection errors, timeouts and 5xx are retried with backoff. 401, 403 and other 4xx are not. If SWIS rejects a column, that column is found and left out, and the coverage note names it.

**curate** builds the relationship layer and the canonical layer from the stored rows. It does not read the raw data again.

1. **`sw_rel`: one view per proven relationship.** Each view gives `instance_id, s_<source key>, t_<target key>` over live rows. The stored rows are then checked against SWIS and the result goes to `sw_meta.relationship.stored_status`:
   - **Navigation links:** for sampled source rows, the pairs in the view must equal the pairs SWIS navigation returns.
   - **Child → parent links:** orphans are counted. A one-to-many link (a node without interfaces) is not an orphan.
2. **`sw_core`: the canonical entities from [catalog/core.yaml](catalog/core.yaml).** The names, stable IDs and references come from swcheck's `contracts.yaml`. Each one is built by a SQL flow, in topological order (parents first):
   - **Stable ID:** `<network_id>/<host:port>/<SWIS entity>/<id=value;…>`
   - **Foreign keys:** a reference becomes a real `FOREIGN KEY` only when a proven relationship backs it on the same columns, forward or reverse. Otherwise it is "not enforced" and the flow says so.
   - **Dead-letter queue:** a record that breaks `key_not_null`, `key_unique` or `ref` goes to `sw_core.dead_letter` with its raw row and the rule. When the failure stops, it gets `resolved_at`; it is never deleted.
   - **Lineage:** every record keeps `raw_table` and `raw_key`. `changes` lists every rename and derived value (for example `NameNormalized`, SPEC-B §6). `sw_core.v_<table>_before_after` shows the raw row, our record and the changes side by side.
   - **`sw_meta.flow`:** one row per flow, with the sources, the target, the relationships used, and rows in = out + dead-letter.

**What the mock run shows:**
- 205 inventory entities; a second run with no source changes logs zero changes.
- 53 `sw_rel` views; the stored pairs match SWIS navigation.
- 19 `sw_core` tables, every flow balanced.
- The planted topology link to interface 99999 is in the dead-letter queue.

**Changing the mock** (test only, `POST /mock/admin/inventory`) shows the full cycle:
- Adding a node and changing one appear in the change log as `insert` and `update` (`Caption` old → new).
- Removing a node marks it `soft_delete` and sends its interfaces and neighbours to the dead-letter queue (rule `ref`).
- Restoring the node resolves those dead-letter rows.
- Hiding an entity gives `empty` and deletes nothing.

**Integration test:** `SWCOLLECT_IT=1 .\.venv\Scripts\python -m unittest discover -s tests -t .` runs this whole cycle against PostgreSQL, using its own instance key.

## Metrics into TimescaleDB

```powershell
.\.venv\Scripts\python -m swcollect metrics --network mock     # first run = backfill, then incremental
```

**metrics** pulls every metrics entity (`Orion.CPULoad`, `Orion.ResponseTime`, `Orion.NPM.InterfaceTraffic`, `Orion.NPM.InterfaceErrors`, `Orion.NPM.CustomPollerStatistics`, …) into its hypertable in `sw_ts`.

- **Cadence follows Orion's own sampling.**
  - The smallest `StatCollection` on `Orion.Nodes` / `Orion.NPM.Interfaces` gives the interval: every max(5, StatCollection) minutes, clamped to 1–15.
  - The overlap is 2 × the largest StatCollection.
  - The result is stored in `sw_meta.instance.metrics_cadence` for the scheduler.
  - These values are in `metrics_policy` in [catalog/collector.yaml](catalog/collector.yaml) and are a proposal ("מוצע – טרם אושר") until approved.
- **First run: backfill.** It reads `MIN`, `MAX` and `COUNT` of the time field, then pulls everything Orion still holds (detail, hourly, daily) in 24-hour slices up to that `MAX`.
- **Next runs: incremental.** Each run reads `WHERE time > watermark − overlap AND time <= MAX`, paged in (time, key) order. The overlap re-reads samples that Orion writes late or recomputes. The same upper bound is used for `COUNT` and the pull, so coverage compares the same set.
- **Store.**
  - `INSERT … ON CONFLICT (instance, key, time) DO UPDATE`, only when the sample changed.
  - A recomputed sample is logged in `sw_meta.change_log` with its old and new values (re-aggregation).
  - Samples whose key or time is null, or appear twice in one pull, go to quarantine.
  - Times are stored exactly as received.
- **Watermark.** `sw_meta.sync_state` holds the newest time received. It moves only after the commit. If PostgreSQL rejects a batch, the entity is marked `error`, its watermark stays, and the next run pulls the same window again.
- **Policies.** Compression after 7 days (`compress_segmentby` = instance + key). Retention is off until a period is approved. Both are proposals.
- **Link to the model.** `sw_ts.v_<table>` adds `device_id` / `interface_id`, their stable IDs, and the caption or name from `sw_core`. It joins only along a relationship proven between the metrics entity and `Orion.Nodes` / `Orion.NPM.Interfaces`. There is no hard FK, so a sample is never lost to inventory lag. Samples whose parent is not in `sw_core` are counted in the coverage note.

**What the mock run shows:**
- **Backfill:** 41,093 samples, coverage exact.
- **30 minutes later:** 96 new samples inserted, plus the 2 that the mock recomputed, updated with `AvgLoad` old → new.
- **Run with nothing new at the source:** it re-reads the overlap and writes nothing.
- **Duplicates:** none.

The test-only `POST /mock/admin/metrics` moves the mock's clock (`advance_minutes`) and recomputes samples (`reaggregate`).

## Events into Loki, push and the safety net

```powershell
.\.venv\Scripts\python -m swcollect events --network mock      # first run = backfill, then incremental
.\.venv\Scripts\python -m swcollect hook                       # push receiver: POST /hook/<network>
```

**events** pulls every event source in [catalog/collector.yaml](catalog/collector.yaml) `events` into Loki: `Orion.Events`, `Orion.AlertHistory`, `Orion.OLM.LogEntry` (syslog and traps), `Orion.AuditingEvents` and `NCM.ConfigArchive`.

- **Incremental by ID.** Each run reads `WHERE id > @LastID AND time >= last seen time − 24 h`, bounded by `MAX(id)` read first. It is never an unfiltered read.
  - **First run:** a backfill of the 30 days before the newest event.
  - **`NCM.ConfigArchive`:** its `ConfigID` is a Guid, so it uses a time watermark with a 60-minute overlap. Loki drops exact duplicates.
- **One Loki entry per row.**
  - **Labels** (low cardinality): `job, network, instance, source, kind`. The kind is `event`, `alert`, `syslog` / `trap` (from `Orion.OLM.LogEntryType`, read from SWIS), `audit` or `config_change`.
  - **Line:** the row exactly as received, as JSON.
  - **Structured metadata:** the ID, the node, the time as received, and the rule that placed it on Loki's time axis.
- **Time on Loki's axis.** Loki needs epoch time, so each entry is placed by a declared per-source rule: `server_local` minus the measured server offset, or `utc`. A value with `Z` or an offset is always read as such.
  - This is the one declared exception to "times as received". The line itself keeps the original time.
  - The rules need proof per version. A source whose newest event lands in the future of the server clock is flagged in the coverage note.
- **Configuration versions.** The full text goes to `sw_raw.ncm_configarchive` (so `sw_core.config_version` is filled and linked to the device). Loki gets the change line without the text; the metadata says where the text is.
- **Nothing is lost.**
  - The watermark moves only after Loki answers 204 and PostgreSQL commits.
  - Connection errors and 5xx are retried. If Loki is down, the source is marked `error`, its watermark stays, and the next run fills the gap.
  - An entry Loki refuses is isolated by splitting the batch, and kept in `sw_meta.loki_rejected` with Loki's reason.
- **Push vs poll.** After the sources, the run reconciles alerts in `sw_meta.alert_reconcile`: `matched` with latency, `poll_only` (push lost, filled by the poll), or `push_only`. It also re-sends pushes stored while Loki was down. See [docs/orion-alert-action.md](docs/orion-alert-action.md) for the Orion alert action and the body template.

**hook** receives the Orion alert action "Send a GET or POST Request to a Web Server":
- **Token:** `X-Swcollect-Token`, or `?token=` when the action cannot set headers. It is never stored or logged.
- **Stored:** every message exactly as received in `sw_meta.push_message`, with a sha256 and `duplicate_of` for repeats.
- **Loki:** each message is sent as `kind="alert_push"`.
- **If PostgreSQL is down:** the answer is 503.

**What the mock run shows** (`POST /mock/admin/alerts` triggers and resets alerts and pushes them, deliberately losing every 4th push):
- 250 / 250 `Orion.Events` in Loki, with no gap and no duplicate.
- Syslog 200 / trap 50.
- 7 pushes delivered and matched to their own rows (latency < 1 s). The 2 lost pushes are `poll_only`.
- 144 configuration versions, with their text, in `sw_raw`.

The Loki config sets `querier.query_ingesters_within: 0s`. Backfilled events are older than Loki's default 3-hour ingester window, and without this setting they are not queryable until their chunks are flushed.

## The service, verification and the end-to-end check

```powershell
.\.venv\Scripts\python -m swcollect serve                        # every job on its schedule + the push receiver
.\.venv\Scripts\python -m swcollect schedule --network mock      # what is due and why
.\.venv\Scripts\python -m swcollect run --network mock --once all
.\.venv\Scripts\python -m swcollect verify --network mock        # SWIS vs what we hold → reports/verify-*.json
.\scripts\e2e.ps1                                                 # the full cycle on the closed Docker network
```

**serve** runs the jobs in `schedule` in [catalog/collector.yaml](catalog/collector.yaml), all of them proposals:

| Job | When | Notes |
|---|---|---|
| discover | daily 01:30 | and at once for a new network |
| inventory | daily 02:00 | then curate |
| inventory_core | hourly | then curate |
| routing | every 15 minutes | |
| metrics | at Orion's own cadence | from `StatCollection` |
| events | every 10 minutes | |
| verify | hourly | |

How it runs:
- **Due:** a job is due when its last run in `sw_meta.sync_run` is older than its interval. Times come from the database clock.
- **Threads:** one per network, with its jobs one after another, which keeps the load on each Orion bounded.
- **Locks:** an advisory lock per (instance, job), so two collectors never run the same job at once.
- **Failures:** a failure is recorded on the run and retried after 5 minutes. It never stops the service.
- **Push receiver:** runs in the same process.

**verify** checks, per network:
- the catalog: every excluded entity has a reason
- inventory and metrics coverage (SWIS COUNT = received = stored)
- duplicate samples
- Loki completeness: every ID SWIS has in the last 24 h is in Loki
- relationships whose stored rows differ from SWIS
- flow balance and open dead-letter rows
- parked rows (quarantine, refused by Loki)
- push vs poll
- job freshness

Each check is ok / warn / fail with its evidence.

**e2e** ([swcollect/e2e.py](swcollect/e2e.py)) drives the whole cycle over HTTP against the mock. It refuses to run against anything that does not answer `/mock/admin`. The 16 steps:
1. first run of every job
2. coverage, the planted orphan in the dead-letter queue, core relationships proven, flows balanced
3. a node added, changed and removed → insert / update with the field diff / soft delete, its children in the dead-letter queue; a hidden entity reported empty and nothing deleted
4. metrics appended and re-aggregated, no duplicates
5. 9 alerts with every 4th push lost → the poll fills exactly the lost ones
6. Loki completeness
7. Loki down → error and the watermark kept, then the gap filled

`scripts/e2e.ps1` runs it inside the closed compose network, then checks there is no route out.

**Documents:**
- [docs/HLD.md](docs/HLD.md): the design
- [docs/closed-network.md](docs/closed-network.md): install, configure, operate, back up
- [docs/orion-alert-action.md](docs/orion-alert-action.md): the Orion push
- `docs/data-model.md` / `.html` (`swcollect erd`): the live data model of sw_core
- `docs/raw-model.html` / `.md` / `.dbml` and `docs/raw-joins.csv` (`swcollect erd`): the ERD of sw_raw. sw_raw has no
  foreign keys between its tables on purpose (a raw row is never refused for a missing parent), so these draw the
  relationships proven against SolarWinds instead: hubs, bridges, every join with ready SQL, and the flow into sw_core

## Mock data and dashboards

```powershell
.\scripts\demo.ps1                 # the stack with the rich mock + Grafana on http://127.0.0.1:13000; -Down removes it
```

**The rich mock** (`swcollect mock --rich`, [swcollect/mock/rich.py](swcollect/mock/rich.py)) turns the sample ISP into
one consistent network for every entity page of the OrionSDK 2026.2 schema:
- **Rows that fit the schema.** Values fit each field's name and SWIS type, and every ID points at a real row: the
  Orion NodeID, InterfaceID, the NCM node GUID, ConfigID and the custom poller assignment. `Metadata.Property`
  lists the documented types.
- **No rows where none would exist,** each with its reason: a module `Orion.InstalledModule` does not list (NTA,
  UDT, SecObs), a device family the ISP does not have (F5, Brocade, Catalyst, Nexus, Windows), or a feature nobody used.
- **History.** 7 days of 10-minute samples and 7 days of hourly ones. They cover every Up node and every interface on
  a link or LAG, plus the optical Rx/Tx custom pollers. The values follow a daily curve.
- **Gaps where Orion would have none:**
  - AGG-06 is down: no SNMP samples, and response rows with 100 % loss.
  - ACC-11 rebooted.
  - ACC-14 has been unmanaged for 2 days.
  - A missing sample is never written as zero.
- **Planted findings:**
  - a busy core link, CRC errors on an AGG uplink, and a drifting Rx level on PE-04
  - a BGP session that is not Established
  - an LLDP neighbor not in Orion
  - an old config backup, EoL dates, CVEs, an IP conflict
  - the dead-letter link
- **Events.** 7 days of events, vendor-style syslog / traps, and alert history.
- **Deterministic.** The same clock gives the same rows. The default mock is unchanged, and the e2e uses it.

**Into PostgreSQL.** The data goes only through the normal jobs (discover → inventory → curate → metrics → events), so
raw rows, lineage, coverage and the change log are real. A host run against the rich mock gives:
- 205 inventory entities: 153 with rows (12,526), 52 empty for their stated reason, and coverage exact
- 124 relationships proven (the plain mock: 57), 119 `sw_rel` views, and the 19 `sw_core` tables filled, every flow
  balanced
- 480,844 samples in `sw_ts`, in about 4 minutes
- 4,343 Loki entries

`swcollect dbstats --network <n>` prints the rows per schema and table. It lists every empty collected table with its
coverage status, and exits 1 if one is unexplained.

**Grafana** (`grafana` service, pinned `grafana/grafana-oss:11.4.0`, offline). It reads PostgreSQL only as the
read-only role `swcollect_ro` ([deploy/init-ro.sh](deploy/init-ro.sh)), and reads Loki. The seven dashboards in folder
"swcollect" are generated by `swcollect dashboards` ([swcollect/dashboards.py](swcollect/dashboards.py)):

| Dashboard | Shows |
|---|---|
| Network overview | devices by status / vendor / model / role, stale, rebooted and unmanaged nodes, the device list with stable IDs |
| Node performance | CPU, memory, response time and loss for a device; the busiest CPUs; devices whose newest sample is old |
| Interfaces | traffic and utilization (SolarWinds' own PercentUtil), errors and discards; the busiest and erroring interfaces; optical power and its change |
| Topology and routing | the link map (node graph), neighbors by protocol and state, sessions not in their main state, flaps, LLDP neighbors not in Orion |
| Inventory, configurations and IP addresses | hardware and missing serials, backup age, versions per day, compliance violations, EoS/EoL, CVEs, subnet use, IP conflicts |
| Events and logs | Loki volume by kind, the event / syslog / trap / alert / config-change streams with a text filter, push vs poll, active alerts |
| Collector health and data quality | jobs, coverage SWIS vs stored, empty entities, flows, dead letter, parked rows, relationship proofs, changes (SPEC-B §11) |

The same rules hold in the dashboards:
- **Times in tables** are exactly as received.
- **Graphs** place each point at the received time minus the measured server offset, the same declared rule as Loki.
- **A status code gets a name** only from SolarWinds (StatusDescription, RoutingProtocolStateMapping) or a named
  standard. Otherwise it is shown as a code.
- **Gaps stay gaps.**

`tests/test_dashboards.py` checks that the files are current, the data sources match the provisioning, there is no
external URL, the SQL only reads `sw_*`, and no panel has a second axis. With `SWCOLLECT_IT=1`, it also runs every
panel query against a loaded database and Loki.

**One database per mock.** `sw_raw` tables are shared by all networks of a database, and each table takes its key and
column types from the first network that creates it. Data whose inferred keys differ (the plain and the rich mock)
must not share a database: the demo runs as its own compose project, and `tests/test_rich_mock.py` uses its own
database.

## Layout

| Path | What |
|---|---|
| `swcollect/swis/` | read-only SWIS client, metadata, time helpers (from swcheck) |
| `swcollect/mock/` | mock SWIS (from swcheck); `rich.py`: the rich mode (swcollect's own) |
| `swcollect/config.py` | network profiles (same format as swcheck) and sinks |
| `swcollect/check.py` | readiness check: SWIS, PostgreSQL + TimescaleDB, Loki |
| `swcollect/discover.py` | reads Metadata, classifies entities, infers identity, writes the discovered catalog |
| `swcollect/relationships.py` | relationship candidates and their proof against SWIS |
| `swcollect/ddl.py`, `swcollect/db.py` | type map, generated DDL, `sw_meta` schema, persistence |
| `swcollect/jobs/inventory.py`, `jobs/retry.py` | full pull into `sw_raw`, change log, quarantine, soft delete, coverage |
| `swcollect/jobs/metrics.py` | metrics into `sw_ts`: cadence, backfill, watermark + overlap, policies, views |
| `swcollect/jobs/events.py`, `reconcile.py`, `webhook.py`, `sinks/loki.py` | events into Loki, push/poll reconciliation, the push receiver, the Loki client |
| `swcollect/rel_views.py` | `sw_rel` views and the stored-rows check against SWIS |
| `swcollect/core.py`, `core_rules.py` | `sw_core` DDL, flows, dead-letter queue, lineage views |
| `swcollect/erd.py`, `raw_erd.py` | `docs/data-model.*` (sw_core) and `docs/raw-model.*` / `raw-joins.csv` (sw_raw relationships) from the database (offline) |
| `swcollect/dashboards.py`, `dbstats.py` | the Grafana dashboards (generated JSON), rows per schema and table |
| `swcollect/steps.py` | the steps the CLI runs (discover, curate), shared with the tests |
| `swcollect/scheduler.py`, `verify.py`, `e2e.py` | the service (due jobs, locks, failures), the quality report, the end-to-end check |
| `catalog/` | `collector.yaml` (rules and overrides, hand-edited), `core.yaml` (canonical model, hand-edited), `discovered/<network_id>.yaml` (generated), `contracts.yaml` and `swis-schema-2026.2.json` (from swcheck) |
| `deploy/` | `docker-compose.yml` (internal network only), the `.dev.yml` override, `Dockerfile`, Loki config, `init.sql`, `init-ro.sh`, `grafana/` (provisioning + dashboards), the `.ui.yml` and `.demo.yml` overrides, offline wheels and images |
| `scripts/` | `bundle_offline.ps1` (outside), `load_offline.ps1` / `.sh` (inside), `smoke.ps1`, `e2e.ps1` / `.sh`, `demo.ps1` / `.sh` |

## Run it locally

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt     # or --no-index --find-links wheels
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml up -d --build db loki mock   # db 15432, loki 13100, mock 17774, grafana 13000
.\.venv\Scripts\python -m swcollect check --network mock --wait 60
.\.venv\Scripts\python -m unittest discover -s tests -t .
```

## Closed network

1. **Outside the closed network** (with internet and Docker), run `.\scripts\bundle_offline.ps1`. It writes:
   - the wheels into `deploy/wheels` (Linux) and `wheels` (Windows)
   - the images, via `docker save`, into `deploy/images`
   - a SHA256 manifest into `deploy/MANIFEST.sha256`
2. Carry the whole folder in.
3. **Inside the closed network**, run `.\scripts\load_offline.ps1` (or `scripts/load_offline.sh`). It:
   1. verifies the manifest
   2. runs `docker load`
   3. builds the swcollect image from the local wheels only
   4. starts the stack
   5. runs the readiness check

`deploy/docker-compose.yml` puts every service on `swnet`, which is an `internal: true` network with no route out. `scripts/smoke.ps1` checks two things on that network:
- the readiness check passes;
- a container cannot reach the internet.

Telemetry is off: TimescaleDB uses `telemetry_level=off`, Loki uses `analytics.reporting_enabled: false`, and
Grafana has reporting, update / news checks, the plugin catalog and preinstalled plugins turned off.

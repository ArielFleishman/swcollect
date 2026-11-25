# swcollect: instructions for Claude

swcollect continuously collects **everything** SolarWinds SWIS exposes, read-only, and stores each kind of data in its own sink:

| Kind | Sink | How |
|---|---|---|
| Inventory and state | PostgreSQL (`sw_raw` → `sw_core`) | full pull, UPSERT by key |
| Metrics | TimescaleDB (`sw_ts`) | incremental by time, append and dedupe |
| Events, alerts, syslog, configuration changes | Loki | incremental by ID, plus push with a safety-net poll |

The approved plan is in `~/.claude/plans/dynamic-weaving-canyon.md`. All five phases are built (see README → Status); for new work, plan it and stop for approval as before. Write code, plans and docs in English. The design is in docs/HLD.md.

swcollect is a separate repo from swcheck, which sits next to it (`../swcheck`; both are in `c:\Users\ariel\swchecker`). Code copied from swcheck says so in its first line, with the source commit. If a bug is fixed in a copied file, say whether swcheck needs the same fix.

## Rules that are easy to break

These come from swcheck's spec (stage A) and SPEC-B §6, §9, §15.

- **Read-only.** The client sends only `POST …/Query` (SWQL that starts with `SELECT`), `GET` by `swis://` Uri, and Invoke only for the verbs in `READ_ONLY_VERBS`. There is no write path, and the collector refuses a profile with `actions_enabled`.
- **Talk only to SolarWinds.** Never talk to network devices.
- **Raw rows are never modified, and every change is visible.**
  - Every stored record keeps its raw row as received (`_raw jsonb`).
  - A changed row is diffed field by field into `sw_meta.change_log`.
  - A transformation is made only by a declared rule.
- **Null stays null.** It is never turned into 0 or `""`.
- **Times are kept exactly as received** (`timestamp` without time zone).
  - The one declared exception is the Loki entry timestamp, which needs epoch time. It uses the measured server UTC offset, and the line keeps the original time.
- **An empty result does not prove deletion or absence,** because it may be permission filtering.
  - A row is soft-deleted (`_deleted_at`) only after a complete, successful full pull.
  - Rows are never physically deleted.
- **The stable ID includes network and instance:** `<network_id>/<host:port>/<entity>/<key=value;…>`. A local NodeID is not global.
- **Never invent a signature, relationship, join column, enum mapping or alert-action variable name.** Unknown means "needs proof" (טעון הוכחה). A relationship is used only after it is proven against SWIS (`sw_meta.relationship.status = proven`).
- **Nothing is skipped silently.**
  - An entity that is excluded or fails is listed with its reason in `sw_meta.coverage` / `catalog/collector.yaml`.
  - The dead-letter queue drops nothing.
- **Never `SELECT *` without a filter on large tables.** Use paging (`WITH ROWS`) and a time or ID filter.
- **Watermarks advance only after the sink confirms the write.**
- **Frequencies, owners and retention are proposals until approved:** "מוצע – טרם אושר".
- **The closed network has no internet.**
  - No CDN, telemetry or download at run time.
  - Everything installs from `deploy/wheels` and `deploy/images`.

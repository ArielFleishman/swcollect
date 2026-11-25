# Pushing alerts from Orion to swcollect

swcollect gets alerts two ways:
- **Push:** in real time, through an Orion alert action.
- **Poll:** every 10–15 minutes, from `Orion.AlertHistory`, as a safety net.

`swcollect events` pairs the two sides in `sw_meta.alert_reconcile`. It reports which pushes arrived, which were lost and filled in by the poll, and how long each push took.

## 1. Run the receiver

```powershell
$env:SWCOLLECT_HOOK_TOKEN = "<a long random value>"
.\.venv\Scripts\python -m swcollect hook            # listens on hook_host:hook_port (config.yaml; default 0.0.0.0:18090)
```

`GET /hook/health` reports whether PostgreSQL and Loki are reachable.

**What happens to each message:**
- It is stored exactly as received in `sw_meta.push_message`. Secret headers and the token are left out.
- It is sent to Loki with labels `{source="push", kind="alert_push"}`.
- If Loki is down, the message stays in PostgreSQL and the next `swcollect events` run sends it.
- If PostgreSQL is down, the receiver answers 503, so Orion's alert log shows the failure. The poll still brings the alert in.

## 2. Add the alert action in Orion

These steps are done in the Orion web console. The option names below come from the action's title; check them against the installed version.

1. Open the alert definition. Under **Trigger Actions**, add an action of type **Send a GET or POST Request to a Web Server**.
2. **URL:** `http://<swcollect host>:18090/hook/<network>`, where `<network>` is the profile name in `config.yaml`.
   - If the action can set a header, send the token as `X-Swcollect-Token: <token>`.
   - If it cannot, append `?token=<token>` to the URL. The receiver accepts both, and never stores or logs the token.
3. **Method:** POST. **Content type:** `application/json`.
4. **Body:** a JSON object built with the action's **Insert Variable** picker. It must contain the alert object ID: by default, reconciliation finds the key with `push.key_regex` (`AlertObjectID\D{0,3}(\d+)`) and compares it with `Orion.AlertHistory.AlertObjectID`. A body that also carries the action, the alert name and the time is easier to read in Loki:

   ```json
   {"Action": "trigger", "AlertObjectID": <Alert Object ID>, "AlertActiveID": <Alert Active ID>,
    "AlertName": <Alert Name>, "Node": <Node Name>, "TimeStamp": <Alert Trigger Time>}
   ```

   Each `<…>` is a variable picked from **Insert Variable**. The exact variable syntax depends on the Orion version: **needs proof (טעון הוכחה)**. Take it from the picker; don't copy it from elsewhere.
5. Add the same action under **Reset Actions**, with `"Action": "reset"`.
6. Use **Simulate** / **Test** on the action, then check:
   - `sw_meta.push_message`: the body as received
   - Loki: `{kind="alert_push", network="<network>"}`

If the key lives in another field, set `push.key_regex` and `push.history_field` in [catalog/collector.yaml](../catalog/collector.yaml).

## 3. How reconciliation works

`swcollect events` reads `Orion.AlertHistory` for the last `match_within_hours` (24 h), and the stored push messages. It pairs each push with the most recent unpaired history row that has the same key, at or before the push time (with `tolerance_seconds` of clock skew allowed).

| Status | Meaning |
|---|---|
| `matched` | both sides, with `latency_sec` (push received − history time) |
| `poll_only` | a history row with no push (yet): the push was lost, or this alert has no push action. The poll already put it in Loki. |
| `push_only` | a message with no history row (yet), or with no key |

**How rows change over time:**
- A row in `poll_only` or `push_only` is paired in a later run when its partner arrives.
- A body identical to an earlier one is kept, but marked `duplicate_of`, and is not counted as a new alert.
- No meaning is assumed for `Orion.AlertHistory.EventType`.

**Time zone:** history times are read as server-local (`events.Orion.AlertHistory.time_zone`). This needs proof per version. If latencies come out about one server offset too high or too low, the declared zone is wrong.

## 4. Testing on the closed network (mock)

The mock simulates both sides:

```powershell
# 6 triggers + 3 resets; every 4th push is "lost" on purpose
curl.exe -u readonly:mock -X POST -H "Content-Type: application/json" `
  -d '{"trigger":6,"reset":3,"drop_every":4,"push_url":"http://127.0.0.1:18090/hook/mock","token":"<token>"}' `
  http://127.0.0.1:17774/mock/admin/alerts
.\.venv\Scripts\python -m swcollect events --network mock
```

The two dropped pushes appear as `poll_only`, and every delivered push is `matched` to its own row (latency under a second).

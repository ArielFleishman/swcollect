"""Grafana dashboards for swcollect, generated as provisioned JSON (`swcollect dashboards`).

The dashboards read only what swcollect stored: PostgreSQL (sw_meta, sw_raw, sw_core, sw_ts) through the read-only
role swcollect_ro, and Loki. They change nothing.

Two rules from the project hold here too:

  time   Stored times are exactly as received (server-local, `timestamp` without zone). Graphs need an absolute
         time axis, so a graph places each point at "DateTime" minus the server offset measured in
         sw_meta.instance.utc_offset_minutes: the same declared rule as the Loki entry time. Tables show the times
         exactly as received. Nothing stored changes.
  codes  A status code gets a name only from SolarWinds itself (StatusDescription, Orion.Routing.Routing-
         ProtocolStateMapping, Orion.EventTypes, Orion.OLM.LogEntryType) or from a published standard named in the
         panel description (ifOperStatus, RFC 2863). Any other code is shown as received.

Series colors are the dark-mode steps of the dataviz reference palette, in fixed order; status colors are reserved
for thresholds. No panel has two y-axes.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parents[1] / "deploy" / "grafana" / "dashboards"
PG = {"type": "grafana-postgresql-datasource", "uid": "swcollect-pg"}
LOKI = {"type": "loki", "uid": "swcollect-loki"}
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"]
GOOD, WARN, SERIOUS, CRITICAL = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"

# ---------------------------------------------------------------------------------------------------------------
# SQL building blocks
# ---------------------------------------------------------------------------------------------------------------
OFF = "(SELECT make_interval(mins => utc_offset_minutes) FROM sw_meta.instance WHERE instance_id = $instance)"
NOW_LOCAL = f"((now() AT TIME ZONE 'UTC') + {OFF})"       # the server's local clock now, like the stored times


def t_axis(col: str = 'm."DateTime"') -> str:
    """The declared time rule: a stored server-local time placed on Grafana's (UTC) time axis."""
    return f"{col} - {OFF} AS time"


def t_range(col: str = 'm."DateTime"') -> str:
    """The dashboard time range, moved to server-local time so the stored column is compared as received."""
    return (f"{col} >= ($__timeFrom()::timestamptz AT TIME ZONE 'UTC') + {OFF} AND "
            f"{col} < ($__timeTo()::timestamptz AT TIME ZONE 'UTC') + {OFF}")


TIME_NOTE = ("Times in tables are exactly as received from SolarWinds (server-local). Graphs place each point at the "
             "received time minus the server UTC offset measured by swcollect (sw_meta.instance.utc_offset_minutes).")


# ---------------------------------------------------------------------------------------------------------------
# Panel helpers
# ---------------------------------------------------------------------------------------------------------------
class Board:
    def __init__(self, uid: str, title: str, description: str, variables: list, tags: list[str]):
        self.uid, self.title, self.description, self.variables, self.tags = uid, title, description, variables, tags
        self.panels: list[dict] = []
        self.x = self.y = self.row_h = 0
        self.next_id = 1

    def row(self, title: str):
        if self.x:
            self.y += self.row_h
            self.x = self.row_h = 0
        self.panels.append({"type": "row", "title": title, "id": self._id(), "collapsed": False,
                            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []})
        self.y += 1

    def add(self, panel: dict, w: int, h: int):
        if self.x + w > 24:
            self.y += self.row_h
            self.x = self.row_h = 0
        panel["id"] = self._id()
        panel["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.panels.append(panel)
        self.x += w
        self.row_h = max(self.row_h, h)

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id - 1

    def json(self) -> dict:
        return {
            "uid": self.uid, "title": self.title, "description": self.description, "tags": ["swcollect"] + self.tags,
            "timezone": "browser", "editable": True, "graphTooltip": 1, "schemaVersion": 39, "version": 1,
            "time": {"from": "now-24h", "to": "now"}, "refresh": "5m", "fiscalYearStartMonth": 0,
            "liveNow": False, "weekStart": "", "annotations": {"list": []},
            "links": [{"type": "dashboards", "tags": ["swcollect"], "asDropdown": True, "title": "swcollect",
                       "includeVars": True, "keepTime": True}],
            "templating": {"list": self.variables}, "panels": self.panels,
        }


def sql(raw: str, fmt: str = "table", ref: str = "A") -> dict:
    return {"datasource": PG, "refId": ref, "rawQuery": True, "editorMode": "code", "format": fmt, "rawSql": raw}


def logql(expr: str, ref: str = "A", legend: str = "", kind: str = "range") -> dict:
    t = {"datasource": LOKI, "refId": ref, "expr": expr, "queryType": kind, "editorMode": "code"}
    if legend:
        t["legendFormat"] = legend
    return t


def thresholds(*steps) -> dict:
    """steps: (value or None, color)."""
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in steps]}


def stat(title: str, query: str, desc: str, unit: str = "short", steps=((None, "text"),), decimals=None) -> dict:
    d = {"unit": unit, "thresholds": thresholds(*steps), "color": {"mode": "thresholds"}}
    if decimals is not None:
        d["decimals"] = decimals
    return {"type": "stat", "title": title, "description": desc, "datasource": PG, "targets": [sql(query)],
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "value", "graphMode": "none", "textMode": "value_and_name", "justifyMode": "auto",
                        "orientation": "auto", "showPercentChange": False, "wideLayout": True}}


def table(title: str, query: str, desc: str, overrides=None, sort=None, ds=PG) -> dict:
    p = {"type": "table", "title": title, "description": desc, "datasource": ds, "targets": [sql(query)],
         "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": True},
                                      "thresholds": thresholds((None, "text"))}, "overrides": overrides or []},
         "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False, "reducer": ["sum"],
                                                                        "fields": ""}}}
    if sort:
        p["options"]["sortBy"] = [{"displayName": sort[0], "desc": sort[1]}]
    return p


def series(title: str, targets: list, desc: str, unit: str, colors: dict | None = None, bars: bool = False,
           stack: bool = False, max_=None, min_=None, steps=None, ds=PG, gap_ms: int = 1_200_000) -> dict:
    """A time series. Missing samples stay gaps: nulls are never joined, and a hole longer than gap_ms (two
    sampling intervals) is drawn as a gap. A missing sample is not zero."""
    custom = {"drawStyle": "bars" if bars else "line", "lineWidth": 2, "fillOpacity": 70 if bars else 8,
              "gradientMode": "none", "showPoints": "never", "pointSize": 8, "spanNulls": False,
              "insertNulls": gap_ms if not bars else False, "axisPlacement": "auto", "axisSoftMin": 0,
              "stacking": {"mode": "normal" if stack else "none", "group": "A"}, "lineInterpolation": "linear",
              "barAlignment": 0, "thresholdsStyle": {"mode": "line+area" if steps else "off"}}
    defaults = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"},
                "thresholds": thresholds(*(steps or ((None, "transparent"),)))}
    if max_ is not None:
        defaults["max"] = max_
    if min_ is not None:
        defaults["min"] = min_
        custom.pop("axisSoftMin")
    overrides = [{"matcher": {"id": "byName", "options": name},
                  "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": col}}]}
                 for name, col in (colors or {}).items()]
    return {"type": "timeseries", "title": title, "description": desc, "datasource": ds, "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": overrides},
            "options": {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom", "calcs": []},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def barchart(title: str, query: str, desc: str, x: str, unit: str = "short", horizontal: bool = True,
             color: str = SERIES[0]) -> dict:
    return {"type": "barchart", "title": title, "description": desc, "datasource": PG, "targets": [sql(query)],
            "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "fixed", "fixedColor": color},
                                         "custom": {"fillOpacity": 85, "lineWidth": 0, "gradientMode": "none",
                                                    "axisSoftMin": 0}}, "overrides": []},
            "options": {"xField": x, "orientation": "horizontal" if horizontal else "vertical", "barWidth": 0.7,
                        "groupWidth": 0.7, "showValue": "auto", "stacking": "none", "xTickLabelRotation": 0,
                        "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
                        "tooltip": {"mode": "single", "sort": "none"}}}


def logs(title: str, expr: str, desc: str) -> dict:
    return {"type": "logs", "title": title, "description": desc, "datasource": LOKI, "targets": [logql(expr)],
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True,
                        "dedupStrategy": "none", "prettifyLogMessage": False, "showLabels": False,
                        "showCommonLabels": False}}


def text(content: str) -> dict:
    return {"type": "text", "title": "", "options": {"mode": "markdown", "content": content},
            "transparent": True}


def cell_color(field: str, steps, mode: str = "color-background") -> dict:
    return {"matcher": {"id": "byName", "options": field},
            "properties": [{"id": "custom.cellOptions", "value": {"type": mode}},
                           {"id": "thresholds", "value": thresholds(*steps)}, {"id": "color", "value": {
                               "mode": "thresholds"}}]}


def unit_of(field: str, unit: str) -> dict:
    return {"matcher": {"id": "byName", "options": field}, "properties": [{"id": "unit", "value": unit}]}


# ---------------------------------------------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------------------------------------------
def var_query(name: str, label: str, query: str, desc: str = "", hide: int = 0) -> dict:
    return {"type": "query", "name": name, "label": label, "description": desc, "datasource": PG,
            "query": query, "definition": query, "refresh": 1, "sort": 1, "multi": False, "includeAll": False,
            "hide": hide, "current": {}, "options": [], "regex": ""}


V_INSTANCE = var_query("instance", "Orion instance",
                       "SELECT instance_key AS __text, instance_id AS __value FROM sw_meta.instance ORDER BY 1",
                       "<network_id>/<host:port>: one Orion instance (SPEC-B §6)")
V_DEVICE = var_query("device", "Device",
                     'SELECT "Caption" AS __text, "NodeID" AS __value FROM sw_core.device '
                     "WHERE instance_id = $instance AND deleted_at IS NULL ORDER BY 1")
V_INTERFACE = var_query("interface", "Interface (with samples)",
                        'SELECT i."Name" AS __text, i."InterfaceID" AS __value FROM sw_core.interface i '
                        'WHERE i.instance_id = $instance AND i."NodeID" = $device AND i.deleted_at IS NULL '
                        "AND EXISTS (SELECT 1 FROM sw_ts.orion_npm_interfacetraffic t WHERE t.instance_id = "
                        'i.instance_id AND t."InterfaceID" = i."InterfaceID") ORDER BY 1')
V_SEARCH = {"type": "textbox", "name": "search", "label": "Log text contains", "query": "", "current": {"text": "",
                                                                                                      "value": ""},
            "hide": 0, "description": "case-insensitive text filter for the log panels (LogQL |~ \"(?i)...\")"}
LIVE = "deleted_at IS NULL"
DEVICE_NAME = ('(SELECT d."Caption" FROM sw_core.device d WHERE d.instance_id = $instance AND d."NodeID" = {col} '
               "AND d.deleted_at IS NULL)")


# ---------------------------------------------------------------------------------------------------------------
# 1. Network overview
# ---------------------------------------------------------------------------------------------------------------
def overview() -> Board:
    b = Board("swc-overview", "swcollect · Network overview",
              "Devices and their state as SolarWinds reports them. " + TIME_NOTE, [V_INSTANCE], ["inventory"])
    raw = "sw_raw.orion_nodes n WHERE n.instance_id = $instance AND n._deleted_at IS NULL"
    b.row("Devices")
    b.add(stat("Devices", f"SELECT count(*) AS \"Devices\" FROM sw_core.device WHERE instance_id = $instance AND {LIVE}",
               "Live rows in sw_core.device (Orion.Nodes)."), 4, 4)
    b.add(stat("Interfaces", f"SELECT count(*) AS \"Interfaces\" FROM sw_core.interface WHERE instance_id = $instance "
                             f"AND {LIVE}", "Live rows in sw_core.interface (Orion.NPM.Interfaces)."), 4, 4)
    b.add(stat("Not synced for over 60 min", f'SELECT count(*) AS "Stale" FROM {raw} AND n."MinutesSinceLastSync" > 60',
               "Orion.Nodes.MinutesSinceLastSync > 60: data from this node is not being refreshed (stage A check "
               "#17). It does not say the node is down.", steps=((None, GOOD), (1, CRITICAL))), 4, 4)
    b.add(stat("Booted in the last 24 h", f'SELECT count(*) AS "Rebooted" FROM {raw} AND n."LastBoot" > '
                                          f"{NOW_LOCAL} - interval '24 hours'",
               "Orion.Nodes.LastBoot within 24 h of the server's clock: possible unplanned reboots (check #17).",
               steps=((None, GOOD), (1, WARN))), 4, 4)
    b.add(stat("Unmanaged", f'SELECT count(*) AS "Unmanaged" FROM {raw} AND n."UnManaged"',
               "Orion.Nodes.UnManaged = true: Orion does not poll these nodes, so they have no new samples.",
               steps=((None, GOOD), (1, WARN))), 4, 4)
    b.add(stat("Open dead-letter rows", "SELECT count(*) AS \"Dead letter\" FROM sw_core.dead_letter WHERE "
                                        "instance_id = $instance AND resolved_at IS NULL",
               "Records that broke a rule on the way to sw_core (SPEC-B §9). They are kept, never dropped.",
               steps=((None, GOOD), (1, SERIOUS))), 4, 4)
    b.row("Composition")
    b.add(barchart("Devices by status (StatusDescription as received)",
                   f'SELECT coalesce(n."StatusDescription", \'(null)\') AS "Status", count(*) AS "Devices" FROM {raw} '
                   "GROUP BY 1 ORDER BY 2 DESC", "Orion.Nodes.StatusDescription: the label SolarWinds gives.",
                   "Status"), 6, 8)
    b.add(barchart("Devices by vendor", f'SELECT CASE WHEN n."Vendor" = \'\' THEN \'(none reported)\' ELSE n."Vendor" END'
                                        f' AS "Vendor", count(*) AS "Devices" FROM {raw} GROUP BY 1 ORDER BY 2 DESC',
                   "Orion.Nodes.Vendor. ICMP-only nodes report none.", "Vendor"), 6, 8)
    b.add(barchart("Devices by model", f'SELECT CASE WHEN n."MachineType" = \'\' THEN \'(none reported)\' ELSE '
                                       f'n."MachineType" END AS "Model", count(*) AS "Devices" FROM {raw} '
                                       "GROUP BY 1 ORDER BY 2 DESC", "Orion.Nodes.MachineType.", "Model"), 6, 8)
    b.add(barchart("Devices by role (custom property)",
                   'SELECT coalesce(c."Role", \'(not set)\') AS "Role", count(*) AS "Devices" FROM sw_core.device d '
                   'LEFT JOIN sw_raw.orion_nodescustomproperties c ON c.instance_id = d.instance_id AND c."NodeID" = '
                   f'd."NodeID" AND c._deleted_at IS NULL WHERE d.instance_id = $instance AND d.{LIVE} '
                   "GROUP BY 1 ORDER BY 2 DESC", "Orion.NodesCustomProperties.Role, as set in Orion.", "Role"), 6, 8)
    b.row("Device list")
    b.add(table("Devices",
                'SELECT d."Caption", d."IPAddress", d."Vendor", d."MachineType" AS "Model", d."SoftwareVersion", '
                'd."Location", n."StatusDescription", d."MinutesSinceLastSync", d."LastBoot", d."LastSync", d.stable_id '
                'FROM sw_core.device d LEFT JOIN sw_raw.orion_nodes n ON n.instance_id = d.instance_id AND n."NodeID" = '
                f'd."NodeID" WHERE d.instance_id = $instance AND d.{LIVE} ORDER BY d."Caption"',
                "sw_core.device with the StatusDescription from the raw row. LastBoot / LastSync exactly as received. "
                "stable_id = <network>/<host:port>/<entity>/<key> (SPEC-B §6).",
                [cell_color("MinutesSinceLastSync", ((None, "transparent"), (15, WARN), (60, CRITICAL)), "color-text")]),
          24, 12)
    return b


# ---------------------------------------------------------------------------------------------------------------
# 2. Node performance
# ---------------------------------------------------------------------------------------------------------------
def node_perf() -> Board:
    b = Board("swc-node", "swcollect · Node performance",
              "CPU, memory and response time from Orion.CPULoad and Orion.ResponseTime (sw_ts). Gaps are samples "
              "Orion did not take: a down, rebooting or unmanaged node. A missing sample is not zero. " + TIME_NOTE,
              [V_INSTANCE, V_DEVICE], ["metrics"])
    b.row("Selected device")
    b.add(series("CPU load (%)", [sql(
        f'SELECT {t_axis()}, m."AvgLoad" AS "average", m."MaxLoad" AS "maximum" FROM sw_ts.v_orion_cpuload m '
        f'WHERE m.instance_id = $instance AND m."NodeID" = $device AND {t_range()} ORDER BY 1', "time_series")],
        "Orion.CPULoad AvgLoad / MaxLoad per sample.", "percent", {"average": SERIES[0], "maximum": SERIES[1]},
        max_=100, steps=((None, "transparent"), (85, "rgba(208,59,59,0.12)"))), 12, 8)
    b.add(series("Memory used (%)", [sql(
        f'SELECT {t_axis()}, m."AvgPercentMemoryUsed" AS "average", m."PeakPercentMemoryUsed" AS "peak" '
        f'FROM sw_ts.v_orion_cpuload m WHERE m.instance_id = $instance AND m."NodeID" = $device AND {t_range()} '
        "ORDER BY 1", "time_series")], "Orion.CPULoad AvgPercentMemoryUsed / PeakPercentMemoryUsed.", "percent",
        {"average": SERIES[0], "peak": SERIES[1]}, max_=100), 12, 8)
    b.add(series("Response time (ms)", [sql(
        f'SELECT {t_axis()}, m."AvgResponseTime" AS "average", m."MaxResponseTime" AS "maximum" '
        f'FROM sw_ts.v_orion_responsetime m WHERE m.instance_id = $instance AND m."NodeID" = $device AND {t_range()} '
        "ORDER BY 1", "time_series")],
        "Orion.ResponseTime. While a node does not answer, Orion stores null response times with 100 % loss.",
        "ms", {"average": SERIES[0], "maximum": SERIES[1]}), 12, 8)
    b.add(series("Packet loss (%)", [sql(
        f'SELECT {t_axis()}, m."PercentLoss" AS "loss" FROM sw_ts.v_orion_responsetime m WHERE m.instance_id = '
        f'$instance AND m."NodeID" = $device AND {t_range()} ORDER BY 1', "time_series")],
        "Orion.ResponseTime.PercentLoss.", "percent", {"loss": SERIES[1]}, max_=100, bars=True), 12, 8)
    b.row("All devices in the time range")
    b.add(table("Busiest CPUs",
                'SELECT m.device_caption AS "Device", round(avg(m."AvgLoad"), 1) AS "Average CPU %", '
                'max(m."MaxLoad") AS "Peak CPU %", round(avg(m."AvgPercentMemoryUsed")::numeric, 1) AS "Average memory %",'
                f' count(*) AS "Samples" FROM sw_ts.v_orion_cpuload m WHERE m.instance_id = $instance AND {t_range()} '
                'GROUP BY 1 ORDER BY 2 DESC LIMIT 10', "Top 10 by average AvgLoad over the dashboard time range.",
                [cell_color("Average CPU %", ((None, "transparent"), (70, WARN), (85, CRITICAL)), "gauge"),
                 unit_of("Average CPU %", "percent"), unit_of("Peak CPU %", "percent"),
                 unit_of("Average memory %", "percent")]), 12, 9)
    b.add(table("Devices whose newest sample is old",
                'SELECT d."Caption" AS "Device", max(m."DateTime") AS "Newest CPU sample (as received)", '
                f"round(extract(epoch FROM {NOW_LOCAL} - max(m.\"DateTime\")) / 60) AS \"Minutes ago\" "
                'FROM sw_core.device d LEFT JOIN sw_ts.orion_cpuload m ON m.instance_id = d.instance_id AND '
                f'm."NodeID" = d."NodeID" WHERE d.instance_id = $instance AND d.{LIVE} AND d."ObjectSubType" = \'SNMP\' '
                f'GROUP BY 1 HAVING max(m."DateTime") IS NULL OR max(m."DateTime") < {NOW_LOCAL} - interval \'30 minutes\''
                ' ORDER BY 2 NULLS FIRST',
                "SNMP devices with no CPU sample in the last 30 minutes of the server's clock: Orion stopped "
                "sampling (down, unmanaged) or the sample did not reach us. Not a zero load.",
                [cell_color("Minutes ago", ((None, WARN), (240, CRITICAL)), "color-text")]), 12, 9)
    return b


# ---------------------------------------------------------------------------------------------------------------
# 3. Interfaces
# ---------------------------------------------------------------------------------------------------------------
def interfaces() -> Board:
    b = Board("swc-interfaces", "swcollect · Interfaces",
              "Traffic, utilization and errors from Orion.NPM.InterfaceTraffic / InterfaceErrors, and optical power "
              "from the custom pollers (Orion.NPM.CustomPollerStatistics). " + TIME_NOTE,
              [V_INSTANCE, V_DEVICE, V_INTERFACE], ["metrics"])
    where = f'm.instance_id = $instance AND m."InterfaceID" = $interface AND {t_range()}'
    b.row("Selected interface")
    b.add(series("Traffic (bits per second)", [sql(
        f'SELECT {t_axis()}, m."InAveragebps" AS "in", m."OutAveragebps" AS "out" FROM sw_ts.v_orion_npm_interfacetraffic'
        f" m WHERE {where} ORDER BY 1", "time_series")], "InAveragebps / OutAveragebps per sample.", "bps",
        {"in": SERIES[0], "out": SERIES[1]}), 12, 8)
    b.add(series("Utilization (%)", [sql(
        f'SELECT {t_axis()}, m."InPercentUtil" AS "in", m."OutPercentUtil" AS "out" FROM '
        f"sw_ts.v_orion_npm_interfacetraffic m WHERE {where} ORDER BY 1", "time_series")],
        "InPercentUtil / OutPercentUtil as SolarWinds computes them (not derived here). Shaded above 80 %.",
        "percent", {"in": SERIES[0], "out": SERIES[1]}, max_=100,
        steps=((None, "transparent"), (80, "rgba(250,178,25,0.10)"), (90, "rgba(208,59,59,0.14)"))), 12, 8)
    b.add(series("Errors and discards (per sample)", [sql(
        f'SELECT {t_axis()}, m."InErrors" AS "in errors", m."CRCAlignErrors" AS "CRC errors", m."OutDiscards" AS '
        f'"out discards" FROM sw_ts.v_orion_npm_interfaceerrors m WHERE {where} ORDER BY 1', "time_series")],
        "Orion.NPM.InterfaceErrors counts per sample interval.", "short",
        {"in errors": SERIES[0], "CRC errors": SERIES[1], "out discards": SERIES[2]}, bars=True), 24, 7)
    b.row("All interfaces in the time range")
    b.add(table("Busiest interfaces",
                'SELECT m.device_caption AS "Device", m.interface_name AS "Interface", '
                'round(max(greatest(m."InPercentUtil", m."OutPercentUtil"))::numeric, 1) AS "Peak %", '
                'round(avg(m."InPercentUtil")::numeric, 1) AS "Average in %", round(avg(m."OutPercentUtil")::numeric, 1) '
                f'AS "Average out %" FROM sw_ts.v_orion_npm_interfacetraffic m WHERE m.instance_id = $instance AND '
                f"{t_range()} GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 15",
                "Top 15 by peak utilization (SolarWinds' InPercentUtil / OutPercentUtil).",
                [cell_color("Peak %", ((None, GOOD), (80, WARN), (90, CRITICAL)), "gauge"), unit_of("Peak %", "percent"),
                 unit_of("Average in %", "percent"), unit_of("Average out %", "percent")]), 12, 10)
    b.add(table("Interfaces with errors or discards",
                'SELECT m.device_caption AS "Device", m.interface_name AS "Interface", sum(m."InErrors") AS "In errors",'
                ' sum(m."CRCAlignErrors") AS "CRC errors", sum(m."OutDiscards") AS "Out discards" FROM '
                f"sw_ts.v_orion_npm_interfaceerrors m WHERE m.instance_id = $instance AND {t_range()} GROUP BY 1, 2 "
                'HAVING sum(m."InErrors") + sum(m."OutDiscards") > 0 ORDER BY 4 DESC, 3 DESC LIMIT 15',
                "Sums over the time range; only interfaces with any.",
                [cell_color("CRC errors", ((None, "transparent"), (100, WARN), (1000, CRITICAL)), "color-text")]),
          12, 10)
    b.row("Optical power (custom pollers)")
    # Joined as text: a server may list these IDs as Guid (uuid columns) or as strings (text), per entity.
    cp = ('FROM sw_ts.orion_npm_custompollerstatistics m JOIN sw_core.custom_poller_assignment a ON a.instance_id = '
          'm.instance_id AND a."CustomPollerAssignmentID"::text = m."CustomPollerAssignmentID"::text AND '
          'a.deleted_at IS NULL JOIN sw_core.custom_poller p ON p.instance_id = a.instance_id AND '
          'p."CustomPollerID"::text = a."CustomPollerID"::text AND p.deleted_at IS NULL')
    b.add(series("Optical power on the selected device (dBm)", [sql(
        f'SELECT {t_axis()}, p."UniqueName" AS metric, m."Status"::double precision AS value {cp} '
        f'WHERE m.instance_id = $instance AND a."NodeID" = $device AND {t_range()} '
        "AND m.\"Status\" ~ '^-?[0-9.]+$' ORDER BY 1", "time_series")],
        "CustomPollerStatistics.Status read as a number. Its unit is the poller's own (dBm for the Juniper DOM "
        "pollers, Orion.NPM.CustomPollers.Unit). Only devices with optical pollers have data.", "dBm",
        {"jnxDomCurrentRxLaserPower": SERIES[0], "jnxDomCurrentTxLaserOutputPower": SERIES[1]}, min_=None), 12, 8)
    b.add(table("Optical power change over the time range",
                f'SELECT {DEVICE_NAME.format(col="a.\"NodeID\"")} AS "Device", p."UniqueName" AS "Poller", '
                '(array_agg(m."Status" ORDER BY m."DateTime"))[1]::double precision AS "First dBm", '
                '(array_agg(m."Status" ORDER BY m."DateTime" DESC))[1]::double precision AS "Last dBm", '
                'round(((array_agg(m."Status" ORDER BY m."DateTime" DESC))[1]::numeric - '
                '(array_agg(m."Status" ORDER BY m."DateTime"))[1]::numeric), 2) AS "Change dB" '
                f"{cp} WHERE m.instance_id = $instance AND {t_range()} AND m.\"Status\" ~ '^-?[0-9.]+$' "
                'GROUP BY 1, 2 ORDER BY 5 LIMIT 20',
                "First and last sample per poller in the range. A falling Rx level predicts a failing link "
                "(SPEC §2 optical transceivers).",
                [cell_color("Change dB", ((None, CRITICAL), (-3, WARN), (-1, "transparent")), "color-text")]), 12, 8)
    return b


# ---------------------------------------------------------------------------------------------------------------
# 4. Topology and routing
# ---------------------------------------------------------------------------------------------------------------
def topology() -> Board:
    b = Board("swc-topology", "swcollect · Topology and routing",
              "Links from Orion.TopologyConnections, neighbors from LLDP and Orion.Routing.*. State names come from "
              "Orion.Routing.RoutingProtocolStateMapping, as SolarWinds publishes them. " + TIME_NOTE,
              [V_INSTANCE], ["inventory"])
    b.row("Map")
    nodes = ('SELECT d."NodeID"::text AS id, d."Caption" AS title, d."MachineType" AS subtitle, '
             'coalesce(n."StatusDescription", \'\') AS mainstat, d."IPAddress" AS secondarystat '
             'FROM sw_core.device d LEFT JOIN sw_raw.orion_nodes n ON n.instance_id = d.instance_id AND n."NodeID" = '
             f'd."NodeID" WHERE d.instance_id = $instance AND d.{LIVE}')
    edges = ('SELECT min(l.id)::text AS id, l."SrcNodeID"::text AS source, l."DestNodeID"::text AS target, '
             'min(si."Name" || \' ↔ \' || di."Name") AS mainstat FROM sw_core.link l '
             'JOIN sw_core.interface si ON si.id = l.src_interface_id JOIN sw_core.interface di ON di.id = '
             f'l.dest_interface_id WHERE l.instance_id = $instance AND l.{LIVE} AND l."SrcNodeID" < l."DestNodeID" '
             'GROUP BY 2, 3')
    b.add({"type": "nodeGraph", "title": "Physical topology (sw_core.link)",
           "description": "One node per device, one edge per device pair with a link whose both interfaces are in "
                          "Orion. A link to an interface Orion does not hold is in the dead-letter queue instead.",
           "datasource": PG, "targets": [sql(nodes, ref="nodes"), sql(edges, ref="edges")],
           "options": {"nodes": {"mainStatUnit": "", "secondaryStatUnit": ""}, "edges": {"mainStatUnit": ""}}}, 24, 16)
    b.row("Routing")
    state = ('LEFT JOIN sw_raw.orion_routing_routingprotocolstatemapping sm ON sm.instance_id = r.instance_id AND '
             'sm."ProtocolID" = r."ProtocolID" AND sm."ProtocolStatus" = r."ProtocolStatus" AND sm._deleted_at IS NULL '
             'LEFT JOIN sw_raw.orion_routing_routingprotocol rp ON rp.instance_id = r.instance_id AND rp."ProtocolID" = '
             'r."ProtocolID" AND rp._deleted_at IS NULL')
    b.add(table("Neighbors by protocol and state",
                'SELECT coalesce(rp."DisplayName", r."ProtocolID"::text) AS "Protocol", coalesce(sm."DisplayName", '
                '\'code \' || r."ProtocolStatus") AS "State", count(*) AS "Neighbors" FROM sw_core.routing_neighbor r '
                f"{state} WHERE r.instance_id = $instance AND r.{LIVE} GROUP BY 1, 2 ORDER BY 1, 3 DESC",
                "A state without a SolarWinds mapping is shown as its code."), 8, 9)
    b.add(table("Neighbors not in the protocol's main state",
                f'SELECT {DEVICE_NAME.format(col="r.\"NodeID\"")} AS "Device", rp."DisplayName" AS "Protocol", '
                'r."NeighborIP" AS "Neighbor", coalesce(sm."DisplayName", \'code \' || r."ProtocolStatus") AS "State", '
                'r."LastChange" AS "Last change (as received)" FROM sw_core.routing_neighbor r '
                f'{state} WHERE r.instance_id = $instance AND r.{LIVE} AND (r."ProtocolID", r."ProtocolStatus") NOT IN '
                '(SELECT x."ProtocolID", x."ProtocolStatus" FROM sw_core.routing_neighbor x WHERE x.instance_id = '
                '$instance AND x.deleted_at IS NULL GROUP BY 1, 2 HAVING count(*) = (SELECT max(c) FROM (SELECT '
                'count(*) c FROM sw_core.routing_neighbor y WHERE y.instance_id = $instance AND y.deleted_at IS NULL '
                'AND y."ProtocolID" = x."ProtocolID" GROUP BY y."ProtocolStatus") z)) ORDER BY 5 DESC',
                "Per protocol, every neighbor whose state is not the state most of its neighbors are in (for "
                "example a BGP session that is not Established). Rank, not a hard-coded state.",
                [cell_color("State", ((None, SERIOUS),), "color-text")]), 16, 9)
    b.add(table("Flap counts (last 7 days, Orion.Routing.NeighborsFlapCount)",
                f'SELECT {DEVICE_NAME.format(col="f.\"NodeID\"")} AS "Device", f."NeighborIP" AS "Neighbor", '
                'f."FlapCount" AS "Flaps" FROM sw_raw.orion_routing_neighborsflapcount f WHERE f.instance_id = $instance '
                'AND f._deleted_at IS NULL AND f."DaysBucket" = 7 AND f."FlapCount" > 0 ORDER BY 3 DESC LIMIT 15',
                "FlapCount for DaysBucket 7.",
                [cell_color("Flaps", ((None, "transparent"), (3, WARN), (8, CRITICAL)), "color-text")]), 8, 9)
    b.add(table("Route flaps (Orion.Routing.RoutingTableFlap)",
                f'SELECT f."DateTime" AS "Time (as received)", {DEVICE_NAME.format(col="f.\"NodeId\"")} AS "Device", '
                'f."RouteDestination" || \'/\' || f."CIDR" AS "Prefix", f."RouteNextHop" AS "Next hop" FROM '
                'sw_raw.orion_routing_routingtableflap f WHERE f.instance_id = $instance AND f._deleted_at IS NULL '
                "ORDER BY 1 DESC LIMIT 50", "Routes that changed, as SolarWinds recorded them."), 8, 9)
    b.add(table("LLDP neighbors that are not in Orion",
                f'SELECT {DEVICE_NAME.format(col="l.\"NodeID\"")} AS "Device", l."LocalPort" AS "Local port", '
                'l."RemoteSystemName" AS "Remote system", l."RemotePort" AS "Remote port", l."RemoteIP" AS "Remote IP" '
                'FROM sw_core.lldp_neighbor l WHERE l.instance_id = $instance AND l.deleted_at IS NULL AND NOT EXISTS '
                '(SELECT 1 FROM sw_core.node_ip_address a WHERE a.instance_id = l.instance_id AND a."IPAddress" = '
                'l."RemoteIP" AND a.deleted_at IS NULL) AND NOT EXISTS (SELECT 1 FROM sw_core.device d WHERE '
                'd.instance_id = l.instance_id AND d."Caption" = l."RemoteSystemName" AND d.deleted_at IS NULL)',
                "Neighbors seen by LLDP whose name and address match no Orion node: unmonitored equipment."), 8, 9)
    return b


# ---------------------------------------------------------------------------------------------------------------
# 5. Inventory, NCM and IPAM
# ---------------------------------------------------------------------------------------------------------------
def inventory() -> Board:
    b = Board("swc-inventory", "swcollect · Inventory, configurations and IP addresses",
              "Hardware (NCM entity MIB), configuration backups, end-of-life and vulnerabilities (NCM), compliance and "
              "IPAM, as SolarWinds holds them. " + TIME_NOTE, [V_INSTANCE], ["inventory", "ncm", "ipam"])
    ncm = ('JOIN sw_core.ncm_node nn ON nn.id = {fk} LEFT JOIN sw_core.device d ON d.id = nn.device_id')
    b.row("Hardware")
    b.add(stat("Hardware components", "SELECT count(*) AS \"Components\" FROM sw_core.hardware_component WHERE "
                                      f"instance_id = $instance AND {LIVE}", "sw_core.hardware_component (NCM.EntityPhysical)."),
          6, 4)
    b.add(stat("Components without a serial", "SELECT count(*) AS \"No serial\" FROM sw_core.hardware_component "
                                              f"WHERE instance_id = $instance AND {LIVE} AND coalesce(\"Serial\", '') = ''",
               "Serial empty or null as received (stage A check #3: does SolarWinds have it for every vendor?).",
               steps=((None, GOOD), (1, WARN))), 6, 4)
    b.add(stat("Devices with no hardware rows", "SELECT count(*) AS \"Devices\" FROM sw_core.device d WHERE d.instance_id "
                                                f"= $instance AND d.{LIVE} AND d.\"ObjectSubType\" = 'SNMP' AND NOT EXISTS "
                                                "(SELECT 1 FROM sw_core.hardware_component h JOIN sw_core.ncm_node nn ON "
                                                "nn.id = h.ncm_node_id WHERE nn.device_id = d.id AND h.deleted_at IS NULL)",
               "SNMP devices without NCM hardware inventory, e.g. vendors NCM does not cover here.",
               steps=((None, GOOD), (1, WARN))), 6, 4)
    b.add(stat("Configuration versions", "SELECT count(*) AS \"Versions\" FROM sw_core.config_version WHERE "
                                         f"instance_id = $instance AND {LIVE}", "sw_core.config_version (NCM.ConfigArchive)."),
          6, 4)
    b.add(table("Hardware components",
                'SELECT d."Caption" AS "Device", h."EntityClass" AS "Class", h."EntityName" AS "Name", h."Model", '
                'h."Serial", h."Manufacturer", h."HardwareRevision" AS "HW rev", h."SoftwareRevision" AS "SW rev" '
                f'FROM sw_core.hardware_component h {ncm.format(fk="h.ncm_node_id")} WHERE h.instance_id = $instance '
                f'AND h.{LIVE} ORDER BY 1, h."EntPhysicalIndex"', "NCM.EntityPhysical through sw_core.",
                [cell_color("Serial", ((None, "transparent"),), "color-text")]), 24, 9)
    b.row("Configurations")
    b.add(table("Newest configuration backup per device",
                'SELECT d."Caption" AS "Device", c."ConfigType" AS "Type", max(c."DownloadTime") AS "Newest (as received)", '
                f'round(extract(epoch FROM {NOW_LOCAL} - max(c."DownloadTime")) / 3600) AS "Age (hours)", '
                f'count(*) AS "Versions" FROM sw_core.config_version c {ncm.format(fk="c.ncm_node_id")} '
                f'WHERE c.instance_id = $instance AND c.{LIVE} GROUP BY 1, 2 ORDER BY 4 DESC',
                "Age = the server's clock now minus DownloadTime (both server-local).",
                [cell_color("Age (hours)", ((None, GOOD), (48, WARN), (168, CRITICAL)), "color-text")]), 12, 10)
    b.add(barchart("Configuration versions downloaded per day",
                   'SELECT to_char(date_trunc(\'day\', c."DownloadTime"), \'YYYY-MM-DD\') AS "Day (server-local)", '
                   'count(*) AS "Versions" FROM sw_core.config_version c WHERE c.instance_id = $instance AND '
                   f'c.{LIVE} GROUP BY 1 ORDER BY 1', "By the day of DownloadTime as received.", "Day (server-local)",
                   horizontal=False), 12, 10)
    b.add(table("Compliance violations (Cirrus.PolicyCacheResults)",
                'SELECT d."Caption" AS "Device", p."ReportName" AS "Report", p."RuleName" AS "Rule", p."ErrorLevel" AS '
                '"Error level (code)" FROM sw_raw.cirrus_policycacheresults p LEFT JOIN sw_core.ncm_node nn ON '
                'nn.instance_id = p.instance_id AND nn."NcmNodeID" = p."NodeID" LEFT JOIN sw_core.device d ON d.id = '
                'nn.device_id WHERE p.instance_id = $instance AND p._deleted_at IS NULL AND p."IsViolation" ORDER BY 1, 3',
                "Rows with IsViolation = true, as NCM cached them. ErrorLevel is shown as its code (no published "
                "mapping)."), 12, 9)
    b.row("Lifecycle and vulnerabilities")
    b.add(table("End of sale / support (NCM)",
                'SELECT n."NodeCaption" AS "Device", n."MachineType" AS "Model", n."EndOfSales" AS "End of sales", '
                'n."EndOfSupport" AS "End of support", n."EndOfSoftware" AS "End of software", '
                'n."ReplacementPartNumber" AS "Replacement" FROM sw_raw.cirrus_nodes n WHERE n.instance_id = $instance '
                'AND n._deleted_at IS NULL AND (n."EndOfSales" IS NOT NULL OR n."EndOfSupport" IS NOT NULL) '
                'ORDER BY n."EndOfSupport", 1', "Dates NCM matched per model (stage A check #22). Nodes without a "
                                                "match are not listed.",
                [cell_color("End of support", ((None, "transparent"),), "color-text")]), 12, 9)
    b.add(table("Vulnerabilities per device (NCM)",
                'SELECT d."Caption" AS "Device", v."EntryId" AS "CVE", a."Score", a."Severity", a."Summary", '
                'v."State" AS "State (code)" FROM sw_raw.ncm_vulnerabilitiesannouncementsnodes v JOIN '
                'sw_raw.ncm_vulnerabilitiesannouncements a ON a.instance_id = v.instance_id AND a."EntryId" = v."EntryId" '
                'AND a._deleted_at IS NULL LEFT JOIN sw_core.device d ON d.instance_id = v.instance_id AND d."NodeID" = '
                'v."CoreNodeID" AND d.deleted_at IS NULL WHERE v.instance_id = $instance AND v._deleted_at IS NULL '
                'ORDER BY a."Score" DESC, 1',
                "NCM.VulnerabilitiesAnnouncementsNodes with the announcement. State is shown as its code.",
                [cell_color("Score", ((None, "transparent"), (7, SERIOUS), (9, CRITICAL)), "color-text")]), 12, 9)
    b.row("IP addresses")
    b.add({"type": "bargauge", "title": "Subnet use (IPAM PercentUsed)",
           "description": "IPAM.Subnet PercentUsed as IPAM reports it.", "datasource": PG,
           "targets": [sql('SELECT s."Address" || \'/\' || s."CIDR" || \'  \' || s."FriendlyName" AS "Subnet", '
                           's."PercentUsed" AS "Used %" FROM sw_core.subnet s WHERE s.instance_id = $instance AND '
                           f's.{LIVE} ORDER BY 2 DESC')],
           "fieldConfig": {"defaults": {"unit": "percent", "min": 0, "max": 100, "color": {"mode": "thresholds"},
                                        "thresholds": thresholds((None, SERIES[0]), (80, WARN), (95, CRITICAL))},
                           "overrides": []},
           "options": {"reduceOptions": {"calcs": [], "fields": "/^Used %$/", "values": True},
                       "orientation": "horizontal", "displayMode": "basic", "showUnfilled": True, "valueMode": "color",
                       "namePlacement": "left", "sizing": "auto", "minVizHeight": 16, "maxVizHeight": 24}}, 12, 9)
    b.add(table("IP address conflicts (IPAM)",
                'SELECT c."IPAddress", c."AssignedMac" AS "Assigned MAC", c."ConflictingMac" AS "Conflicting MAC", '
                'c."ConflictTypeText" AS "Type", c."ConflictTimeUTC" AS "Detected (UTC, as received)" FROM '
                'sw_raw.ipam_conflict c WHERE c.instance_id = $instance AND c._deleted_at IS NULL ORDER BY 5 DESC',
                "IPAM.Conflict as IPAM reports it."), 12, 9)
    return b


# ---------------------------------------------------------------------------------------------------------------
# 6. Events and logs
# ---------------------------------------------------------------------------------------------------------------
def events() -> Board:
    sel = '{job="swcollect", instance="${instance:text}"'
    b = Board("swc-events", "swcollect · Events and logs",
              "Orion events, alert history, syslog / traps and configuration changes as stored in Loki: one entry "
              "per row, the line is the row exactly as received. Loki places each entry by the declared per-source "
              "time rule. Push vs poll comes from sw_meta.alert_reconcile.", [V_INSTANCE, V_SEARCH], ["events"])
    kinds = {"event": SERIES[0], "syslog": SERIES[1], "trap": SERIES[2], "alert": SERIES[3],
             "config_change": SERIES[4], "alert_push": SERIES[6], "audit": SERIES[5]}
    b.row("Volume")
    b.add(series("Entries per interval, by kind", [logql(
        f'sum by (kind) (count_over_time({sel}}} [$__auto]))', legend="{{kind}}")],
        "Loki entries per kind (label kind). Syslog and trap are told apart by Orion.OLM.LogEntryType.", "short",
        kinds, bars=True, stack=True, ds=LOKI), 16, 8)
    b.add(table("Alerts: push vs poll", "SELECT status AS \"Status\", count(*) AS \"Alerts\", "
                                        "round(avg(extract(epoch FROM poll_at - push_at))::numeric, 1) AS "
                                        "\"Average push lead (s)\" FROM sw_meta.alert_reconcile WHERE instance_id = "
                                        "$instance GROUP BY 1 ORDER BY 1",
                "matched = pushed by the Orion alert action and seen by the poll; poll_only = the push never arrived "
                "and the poll filled it; push_only = not (yet) in Orion.AlertHistory."), 8, 8)
    b.row("Streams")
    flt = '|~ "(?i)$search"'
    b.add(logs("Orion events", f'{sel}, kind="event"}} {flt}', "Orion.Events rows."), 12, 11)
    b.add(logs("Syslog and traps", f'{sel}, kind=~"syslog|trap"}} {flt}', "Orion.OLM.LogEntry rows."), 12, 11)
    b.add(logs("Alert history", f'{sel}, kind=~"alert|alert_push"}} {flt}',
               "Orion.AlertHistory rows, and alert pushes received by the hook (kind alert_push)."), 12, 9)
    b.add(logs("Configuration changes", f'{sel}, kind="config_change"}} {flt}',
               "One line per NCM.ConfigArchive version, without the configuration text (that is in "
               "sw_raw.ncm_configarchive)."), 12, 9)
    b.row("Active alerts (sw_core.alert_active)")
    b.add(table("Active alerts",
                'SELECT a."TriggeredDateTime" AS "Triggered (as received)", o."EntityCaption" AS "Object", '
                'o."RelatedNodeCaption" AS "Node", a."TriggeredMessage" AS "Message", a."Acknowledged", '
                'a."AcknowledgedBy" AS "By" FROM sw_core.alert_active a LEFT JOIN sw_raw.orion_alertobjects o ON '
                'o.instance_id = a.instance_id AND o."AlertObjectID" = a."AlertObjectID" AND o._deleted_at IS NULL '
                f'WHERE a.instance_id = $instance AND a.{LIVE} ORDER BY 1 DESC', "Orion.AlertActive with its alert object."),
          24, 8)
    return b


# ---------------------------------------------------------------------------------------------------------------
# 7. Collector health and data quality
# ---------------------------------------------------------------------------------------------------------------
def health() -> Board:
    b = Board("swc-health", "swcollect · Collector health and data quality",
              "How the collection itself is doing (SPEC-B §11): runs, coverage SWIS vs stored, parked rows, "
              "relationship proofs, flows and freshness. Collector times (started_at, checked_at) are the database "
              "clock (timestamptz).", [V_INSTANCE], ["quality"])
    latest_cov = ("SELECT DISTINCT ON (entity) entity, status, swis_count, received, stored, note, checked_at FROM "
                  "sw_meta.coverage WHERE instance_id = $instance ORDER BY entity, checked_at DESC")
    b.row("Now")
    b.add(stat("Entities collected (latest coverage ok)", f"SELECT count(*) FILTER (WHERE status = 'ok') AS \"ok\" "
                                                          f"FROM ({latest_cov}) c",
               "Entities whose latest coverage row is ok: SWIS COUNT = received = stored.",
               steps=((None, GOOD),)), 6, 4)
    b.add(stat("Coverage mismatch or error", f"SELECT count(*) FILTER (WHERE status IN ('mismatch', 'error')) AS "
                                             f"\"Problems\" FROM ({latest_cov}) c",
               "Latest coverage status mismatch or error.", steps=((None, GOOD), (1, CRITICAL))), 6, 4)
    b.add(stat("Empty entities", f"SELECT count(*) FILTER (WHERE status = 'empty') AS \"Empty\" FROM ({latest_cov}) c",
               "SWIS returned no rows. That may be permission filtering, a module that is not installed, or no "
               "such devices; it never deletes anything.", steps=((None, "text"),)), 6, 4)
    b.add(stat("Parked rows (24 h)", "SELECT (SELECT count(*) FROM sw_meta.raw_quarantine WHERE instance_id = $instance "
                                     "AND created_at > now() - interval '24 hours') + (SELECT count(*) FROM "
                                     "sw_meta.loki_rejected WHERE instance_id = $instance AND resolved_at IS NULL) AS "
                                     "\"Parked\"",
               "raw_quarantine (key null / duplicate) + loki_rejected not yet accepted. Kept, never dropped.",
               steps=((None, GOOD), (1, WARN))), 6, 4)
    b.add(table("Jobs: latest run",
                "SELECT DISTINCT ON (job) job AS \"Job\", status AS \"Status\", started_at AS \"Started\", "
                "round(extract(epoch FROM now() - started_at) / 60) AS \"Minutes ago\", rows AS \"Rows\", "
                "requests AS \"SWIS requests\", ms AS \"Duration\", error AS \"Error\" FROM sw_meta.sync_run WHERE "
                "instance_id = $instance ORDER BY job, started_at DESC",
                "The newest sync_run per job. The proposed schedule is in catalog/collector.yaml "
                "(מוצע – טרם אושר).",
                [cell_color("Status", ((None, "transparent"),), "color-text"), unit_of("Duration", "ms")]), 12, 8)
    b.add(series("Run duration by job", [sql(
        "SELECT started_at AS time, job AS metric, ms AS value FROM sw_meta.sync_run WHERE instance_id = $instance "
        "AND $__timeFilter(started_at) AND ms IS NOT NULL ORDER BY 1", "time_series")],
        "Duration of each run.", "ms", None, gap_ms=0), 12, 8)
    b.row("Coverage (latest per entity)")
    b.add(barchart("Entities by coverage status", f"SELECT status AS \"Status\", count(*) AS \"Entities\" FROM "
                                                  f"({latest_cov}) c GROUP BY 1 ORDER BY 2 DESC",
                   "ok / empty / mismatch / error / excluded / other_job.", "Status"), 6, 9)
    b.add(table("Entities that need a look",
                f"SELECT entity AS \"Entity\", status AS \"Status\", swis_count AS \"SWIS COUNT\", received AS "
                f"\"Received\", stored AS \"Stored\", note AS \"Note\" FROM ({latest_cov}) c WHERE status NOT IN "
                "('ok', 'excluded', 'other_job') ORDER BY status, entity",
                "Everything that is not plainly ok, with its note. Nothing is skipped silently."), 18, 9)
    b.add(table("Coverage of every entity",
                f"SELECT entity AS \"Entity\", status AS \"Status\", swis_count AS \"SWIS COUNT\", received AS "
                f"\"Received\", stored AS \"Stored\", checked_at AS \"Checked\" FROM ({latest_cov}) c ORDER BY entity",
                "SWIS vs what we hold (SPEC-B §11 coverage, §13.3)."), 24, 10)
    b.row("Canonical model and parked rows")
    b.add(table("Flows of the latest curate run (sw_raw → sw_core)",
                "SELECT f.target_table AS \"Target\", f.rows_in AS \"In\", f.rows_out AS \"Out\", f.rows_dlq AS "
                "\"Dead letter\", CASE WHEN f.rows_in = f.rows_out + f.rows_dlq THEN 'balanced' ELSE 'NOT balanced' END "
                "AS \"Balance\" FROM sw_meta.flow f WHERE f.run_id = (SELECT max(r.run_id) FROM sw_meta.sync_run r "
                "WHERE r.instance_id = $instance AND r.job = 'curate' AND EXISTS (SELECT 1 FROM sw_meta.flow x WHERE "
                "x.run_id = r.run_id)) ORDER BY f.position", "rows in = out + dead-letter for every flow.",
                [cell_color("Dead letter", ((None, "transparent"), (1, SERIOUS)), "color-text")]), 12, 10)
    b.add(table("Open dead-letter rows",
                "SELECT core_table AS \"Table\", rule AS \"Rule\", field AS \"Field\", value AS \"Value\", detail AS "
                "\"Detail\", first_seen AS \"First seen\" FROM sw_core.dead_letter WHERE instance_id = $instance AND "
                "resolved_at IS NULL ORDER BY first_seen DESC",
                "Each with its raw row in sw_core.dead_letter.raw; resolved rows stay with resolved_at."), 12, 10)
    b.add(table("Relationships by proof status",
                "SELECT status AS \"Status\", origin AS \"Origin\", count(*) AS \"Relationships\" FROM "
                "sw_meta.relationship WHERE instance_id = $instance GROUP BY 1, 2 ORDER BY 1, 3 DESC",
                "Only proven relationships become sw_rel views and foreign keys."), 8, 9)
    b.add(series("Changes recorded per hour", [sql(
        "SELECT date_trunc('hour', changed_at) AS time, change AS metric, count(*) AS value FROM sw_meta.change_log "
        "WHERE instance_id = $instance AND $__timeFilter(changed_at) GROUP BY 1, 2 ORDER BY 1", "time_series")],
        "sw_meta.change_log: insert / update (with the field diff) / soft_delete / undelete.", "short",
        {"insert": SERIES[0], "update": SERIES[1], "soft_delete": SERIES[2], "undelete": SERIES[3]}, bars=True,
        stack=True), 16, 9)
    return b


BOARDS = [overview, node_perf, interfaces, topology, inventory, events, health]


def build() -> dict[str, dict]:
    return {f"{b.uid}.json": b.json() for b in (f() for f in BOARDS)}


def panel_queries(board: dict):
    """(panel title, datasource uid, query) for every query of a dashboard, rows included. For tests."""
    for p in board["panels"]:
        for t in p.get("targets", []):
            yield p["title"], t["datasource"]["uid"], t.get("rawSql") or t.get("expr")
    for v in board["templating"]["list"]:
        if v.get("type") == "query":
            yield f"variable {v['name']}", v["datasource"]["uid"], v["query"]


def write(out_dir: Path = OUT_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, board in build().items():
        p = out_dir / name
        p.write_text(json.dumps(board, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        paths.append(p)
    return paths

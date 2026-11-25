"""Rich mode for the mock SWIS (`swcollect mock --rich`). swcollect's own code, not copied from swcheck.

The default mock (fixtures.build) holds a coherent sample ISP for about 50 entities, and `--schema-all` adds five
typed placeholder rows (`Caption-0`, random numbers) for every other entity page of the bundled OrionSDK schema.
Rich mode keeps the curated ISP and makes the rest of it look like one network:

  - every entity page of catalog/swis-schema-2026.2.json gets rows whose values fit each field's name and SWIS
    type, and whose IDs point at real rows (Orion NodeID, InterfaceID, the NCM node GUID, ConfigID, ...);
  - an entity gets no rows where none would exist: a module that Orion.InstalledModule does not list, a device
    family the sample ISP does not have, or a feature nobody used (EMPTY, each with its reason);
  - documented fields the curated rows lack are filled where a sensible value is known; otherwise they stay null;
  - metrics get history: 7 days of 10-minute samples and 7 more days of hourly ones (Orion.Settings retention), for
    every Up node and every interface on a topology link or LAG, with a daily curve, and gaps where Orion would have
    none (a down node, a reboot, an unmanaged node). A missing sample is never written as zero;
  - events, syslog / traps and alert history span 7 days with vendor-style messages.

Everything is deterministic: the same clock gives the same rows. All values are the MOCK's own, including status
codes and names in lookup tables. They prove nothing about a real Orion.
"""
from __future__ import annotations

import ipaddress
import json
import uuid
import zlib
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "catalog" / "swis-schema-2026.2.json"
NS = uuid.UUID("6f1c2a52-6b3e-4d4f-9a39-7d1bd6a1e0c1")     # namespace for the mock's derived GUIDs

DETAIL_STEP = 10           # minutes between detail samples (= StatCollection of the sample ISP)
HOURLY_STEP = 60

# Rich-mode metric series: entity -> (key fields, minutes between samples). Used by mock_swis to keep sampling.
RICH_SERIES = {
    "Orion.ResponseTime": (["NodeID"], DETAIL_STEP),
    "Orion.CPULoad": (["NodeID"], DETAIL_STEP),
    "Orion.NPM.InterfaceTraffic": (["InterfaceID"], DETAIL_STEP),
    "Orion.NPM.InterfaceErrors": (["InterfaceID"], DETAIL_STEP),
    "Orion.NPM.CustomPollerStatistics": (["CustomPollerAssignmentID"], DETAIL_STEP),
}

# Entities that get no rows in rich mode, and why. A key ending in "." is a prefix.
EMPTY = {
    "Orion.Netflow.": "NTA is not installed (not in Orion.InstalledModule)",
    "Orion.UDT.": "UDT is not installed (not in Orion.InstalledModule)",
    "Orion.SecObs.": "Security Observability is not installed (not in Orion.InstalledModule)",
    "Orion.NPM.MulticastRouting.": "multicast is not in use in the sample ISP",
    "Orion.Nexus.": "the sample ISP has no Nexus switches (vPC)",
    "Orion.NPM.SwitchStack": "the sample ISP has no switch stacks",
    "NCM.F5": "the sample ISP has no F5 devices",
    "NCM.Brocade": "the sample ISP has no Brocade devices",
    "NCM.CATOSPorts": "the sample ISP has no CatOS devices",
    "NCM.CatalystCards": "the sample ISP has no Catalyst switches",
    "Cirrus.CatalystCards": "the sample ISP has no Catalyst switches",
    "NCM.Windows": "the sample ISP has no Windows nodes in NCM",
    "Cirrus.Windows": "the sample ISP has no Windows nodes in NCM",
    "NCM.FirmwareOperation": "no firmware upgrade has been run",
    "NCM.FirmwareUpgrade": "no firmware upgrade has been run",
    "Cirrus.ApproveQueue": "no change-approval request is pending",
    "Cirrus.NCM_ApproveQueueView": "no change-approval request is pending",
    "NCM.AceShadowRuleDetectionResult": "no shadowed ACL rule was found",
    "NCM.ShadowRuleDetectionResult": "no shadowed ACL rule was found",
    "NCM.RuleDetection": "no shadowed ACL rule was found",
    "Cirrus.IgnoredNodes": "no node is ignored by NCM",
}


def empty_reason(entity: str, fields: dict) -> str | None:
    if not fields:
        return "the documentation lists no properties"
    for k, why in EMPTY.items():
        if (k.endswith(".") and entity.startswith(k)) or entity == k or (not k.endswith(".") and entity.startswith(k)):
            return why
    return None


@lru_cache(maxsize=1)
def pages() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["pages"]


def doc_fields(entity: str) -> dict[str, str]:
    """Documented fields of an entity, including the ones inherited from non-System base entities."""
    p = pages()
    out: dict[str, str] = {}
    for b in (p.get(entity) or {}).get("bases") or []:
        if b in p and not b.startswith("System."):
            out.update(doc_fields(b))
    out.update((p.get(entity) or {}).get("fields") or {})
    return out


def doc_type(entity: str, field: str) -> str | None:
    """SWIS type of a field per the documentation (own, any base, or System.*), or None."""
    p = pages()
    seen, todo = set(), [entity]
    while todo:
        e = todo.pop(0)
        if e in seen or e not in p:
            continue
        seen.add(e)
        t = (p[e].get("fields") or {}).get(field)
        if t:
            return t
        todo += p[e].get("bases") or []
    return None


def fits(value, swis_type: str) -> bool:
    """Whether a value fits a SWIS type (and so the PostgreSQL column the collector makes from it)."""
    if value is None:
        return True
    t = swis_type
    try:
        if t == "System.Guid":
            uuid.UUID(str(value))
            return isinstance(value, str)
        if t in ("System.Int16", "System.Byte", "System.Int32", "System.Int64", "System.SByte", "System.UInt16",
                 "System.UInt32"):
            if isinstance(value, bool) or not isinstance(value, int):
                return False
            lim = {"System.Int16": 2 ** 15, "System.Byte": 2 ** 15, "System.SByte": 2 ** 15, "System.UInt16": 2 ** 31,
                   "System.Int32": 2 ** 31, "System.UInt32": 2 ** 63, "System.Int64": 2 ** 63}[t]
            return -lim <= value < lim
        if t in ("System.Double", "System.Single", "System.Decimal"):
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if t == "System.Boolean":
            return isinstance(value, bool)
        if t == "System.DateTime":
            return isinstance(value, str) and len(value) >= 19 and value[4] == "-" and value[10] == "T"
        if t in ("System.String", "System.Char"):
            return isinstance(value, str)
        if t.endswith("[]"):
            return isinstance(value, list) or (t == "System.Byte[]" and value is None)
        if t == "SolarWinds.InformationService.PropertyBag":
            return isinstance(value, dict)
    except (ValueError, TypeError):
        return False
    return True


# ---------------------------------------------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------------------------------------------
def _u(*parts) -> float:
    """A stable number in [0, 1) for the parts (Python's str hash is salted, crc32 is not)."""
    return zlib.crc32("/".join(map(str, parts)).encode()) / 4294967296.0


def _ri(lo: int, hi: int, *parts) -> int:
    return lo + int(_u(*parts) * (hi - lo + 1))


def _guid(*parts) -> str:
    return str(uuid.uuid5(NS, "/".join(map(str, parts))))


def _fmt(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def _ip_int(ip: str) -> int:
    try:
        return int(ipaddress.IPv4Address(ip))
    except ValueError:
        return 0


def role(node: dict | None) -> str:
    return node["Caption"].rsplit("-", 1)[0] if node else ""


def vendor_key(node: dict | None) -> str:
    v = (node or {}).get("Vendor") or ""
    return "cisco" if v.startswith("Cisco") else "juniper" if v.startswith("Juniper") else \
        "nokia" if v == "Nokia" else "huawei" if v.startswith("Huawei") else ""


def serial(node: dict | None, *parts) -> str:
    vk = vendor_key(node)
    nid = (node or {}).get("NodeID", 0)
    if vk == "cisco":
        return f"FOX{_ri(1000, 2999, nid, *parts)}{'ABCDEFGHJK'[_ri(0, 9, 'a', nid, *parts)]}" \
               f"{'LMNPQRSTUV'[_ri(0, 9, 'b', nid, *parts)]}{_ri(10, 99, 'c', nid, *parts)}"
    if vk == "juniper":
        return f"JN{_ri(0x100000, 0xFFFFFF, nid, *parts):06X}AFA"
    if vk == "nokia":
        return f"NS{_ri(1000000000, 1999999999, nid, *parts)}"
    return f"SN{_ri(10 ** 7, 10 ** 8 - 1, nid, *parts)}"


IMAGES = {"cisco": ("asr9k-mini-x64-7.9.2.iso", "asr920-universalk9_npe.17.09.04.SPA.bin"),
          "juniper": ("junos-install-mx-x86-64-22.4R3.tgz",), "nokia": ("cpm.tim", "iom.tim", "both.tim"),
          "huawei": ("NE40E-V800R021C10SPC600.cc",)}
CARDS = {
    "Cisco ASR 9006": [("A9K-RSP880-SE", "ASR9K Route Switch Processor with 880G/slot Fabric and 16GB"),
                       ("A99-8X100GE-SE", "8-port 100GE Line Card, Service Edge Optimized"),
                       ("A9K-24X10GE-1G-SE", "24-port 10GE/1GE Line Card, Service Edge Optimized"),
                       ("A9K-AC-PEM-V3", "ASR 9000 AC Power Module Version 3")],
    "Cisco ASR-920-24SZ-M": [("ASR-920-24SZ-M", "Cisco ASR920 Series - 24GE and 4-10GE"),
                             ("ASR-920-PWR-A", "ASR 920 AC Power Supply"),
                             ("ASR-920-FAN-M", "ASR 920 Fan Tray")],
}
# Messages per vendor for interface state changes on syslog: (down, up).
LINK_MSG = {
    "cisco": ("%PKT_INFRA-LINK-3-UPDOWN : Interface {ifn}, changed state to Down",
              "%PKT_INFRA-LINK-3-UPDOWN : Interface {ifn}, changed state to Up"),
    "juniper": ("mib2d[2104]: SNMP_TRAP_LINK_DOWN: ifIndex {idx}, ifAdminStatus up(1), ifOperStatus down(2), "
                "ifName {ifn}", "mib2d[2104]: SNMP_TRAP_LINK_UP: ifIndex {idx}, ifAdminStatus up(1), ifOperStatus "
                "up(1), ifName {ifn}"),
    "nokia": ("TIMOS: SNMP #2004 Base Port {ifn} \"Interface {ifn} is not operational\"",
              "TIMOS: SNMP #2005 Base Port {ifn} \"Interface {ifn} is operational\""),
    "huawei": ("%%01IFNET/4/LINK_STATE(l)[12]:The line protocol IP on the interface {ifn} has entered the DOWN state.",
               "%%01IFNET/4/LINK_STATE(l)[13]:The line protocol IP on the interface {ifn} has entered the UP state."),
}
OTHER_SYSLOG = {
    "cisco": ["%SSHD_-6-INFO_SUCCESS : Successfully authenticated user 'netops' from 10.250.0.20",
              "%MGBL-CONFIG-6-DB_COMMIT : Configuration committed by user 'netops'.",
              "%ROUTING-ISIS-5-ADJCHANGE : Adjacency to {peer} (TenGigE0/0/0/1) (L2) Up, New adjacency",
              "%PLATFORM-ENVMON-4-ALARM : Inlet temperature above minor threshold"],
    "juniper": ["sshd[44121]: Accepted password for netops from 10.250.0.20 port 51322 ssh2",
                "mgd[7781]: UI_COMMIT: User 'netops' requested 'commit' operation",
                "rpd[2190]: RPD_BGP_NEIGHBOR_STATE_CHANGED: BGP peer {peer} (Internal AS 65000) changed state from "
                "OpenConfirm to Established", "chassisd[1901]: CHASSISD_FAN_SPEED_CHANGED: Fan speed set to normal"],
    "nokia": ["TIMOS: SECURITY #2001 Base SSH \"User netops from 10.250.0.20 logged in\"",
              "TIMOS: SYSTEM #2006 Base CONFIG \"Configuration saved by netops\"",
              "TIMOS: OSPF #2024 Base VR 1 OSPFv2 (0) \"Neighbor {peer} state changed to full\""],
    "huawei": ["%%01SHELL/5/LOGIN(s):The user netops succeeded in logging in to VTY0.",
               "%%01BGP/3/STATE_CHG_UPDOWN(l):The status of the peer {peer} changed from OPENCONFIRM to ESTABLISHED."],
}
TRAPS = ["IF-MIB::linkDown ifIndex.{idx} = {idx}, ifAdminStatus = up(1), ifOperStatus = down(2)",
         "IF-MIB::linkUp ifIndex.{idx} = {idx}, ifAdminStatus = up(1), ifOperStatus = up(1)",
         "BGP4-MIB::bgpBackwardTransition bgpPeerRemoteAddr = {peer}, bgpPeerState = idle(1)",
         "SNMPv2-MIB::coldStart", "ENTITY-MIB::entConfigChange"]
LEVELS = {3: "error", 4: "warning", 5: "notice", 6: "info"}


# ---------------------------------------------------------------------------------------------------------------
# Context: the curated ISP, indexed
# ---------------------------------------------------------------------------------------------------------------
class Ctx:
    def __init__(self, D: dict, now_utc: datetime, now: datetime):
        self.D, self.now_utc, self.now = D, now_utc, now
        self.nodes = D["Orion.Nodes"]
        self.node = {n["NodeID"]: n for n in self.nodes}
        self.byname = {n["Caption"]: n for n in self.nodes}
        self.snmp = [n for n in self.nodes if n["ObjectSubType"] == "SNMP"]
        self.ifaces = D["Orion.NPM.Interfaces"]
        self.iface = {i["InterfaceID"]: i for i in self.ifaces}
        self.if_by_node: dict[int, list[dict]] = {}
        for i in self.ifaces:
            self.if_by_node.setdefault(i["NodeID"], []).append(i)
        self.ncm_rows = D["Cirrus.Nodes"]
        self.guid_of = {x["CoreNodeID"]: x["NodeID"] for x in self.ncm_rows}
        self.core_of = {x["NodeID"]: x["CoreNodeID"] for x in self.ncm_rows}
        self.ips: dict[int, list[dict]] = {}
        self.node_of_ip: dict[str, int] = {}
        for r in D["Orion.NodeIPAddresses"]:
            self.ips.setdefault(r["NodeID"], []).append(r)
            self.node_of_ip.setdefault(r["IPAddress"], r["NodeID"])
        self.latest_running: dict[str, dict] = {}
        for c in D["NCM.ConfigArchive"]:
            if c["ConfigType"] == "Running" and c["DownloadTime"] > self.latest_running.get(c["NodeID"], {}).get(
                    "DownloadTime", ""):
                self.latest_running[c["NodeID"]] = c

    def if_by_index(self, nid: int, idx) -> dict | None:
        return next((i for i in self.if_by_node.get(nid, []) if i["Index"] == idx), None)

    def mac(self, node: dict | None, iface: dict | None, *parts) -> str:
        if iface and iface.get("PhysicalAddress"):
            return iface["PhysicalAddress"]
        nid = (node or {}).get("NodeID", 0)
        return "00:1A:{:02X}:{:02X}:{:02X}:{:02X}".format(nid % 256, _ri(0, 255, nid, *parts), _ri(0, 255, 'm', *parts),
                                                          _ri(0, 255, 'n', *parts))


class RowCtx:
    __slots__ = ("ctx", "i", "j", "node", "guid", "iface", "extra")

    def __init__(self, ctx: Ctx, node=None, guid=None, iface=None, j=0, extra=None):
        self.ctx, self.node, self.guid, self.iface, self.j, self.extra = ctx, node, guid, iface, j, extra or {}
        self.i = 0
        if self.guid is None and node is not None:
            self.guid = ctx.guid_of.get(node["NodeID"])

    @property
    def nid(self):
        return self.node["NodeID"] if self.node else None

    @property
    def ip(self):
        return self.extra.get("ip") or (self.node or {}).get("IPAddress")


# ---------------------------------------------------------------------------------------------------------------
# Field values by name and SWIS type
# ---------------------------------------------------------------------------------------------------------------
UNKNOWN = object()
FALSE_FLAGS = {"missing", "isdeleted", "isdisabled", "disabled", "ishidden", "ismissing", "isexcluded", "ringfailure",
               "reboot", "unmanaged", "isviolation", "iseval", "isexpired", "isrc", "ispollingerror", "external",
               "isserver", "isorionserver", "dynamicip", "hasobsoletedata", "custombandwidth", "usehttps",
               "usekeybinteractiveauth", "useuserdevicecredentials", "allowintermediary", "fipsmodeenabled",
               "isfree", "iscustom", "isbinary", "isblock", "baseline", "istransfererror", "istransfercanceling",
               "isactivetransfer", "customstatus", "unpluggable", "changetype", "exactmatching", "diffflag",
               "patternmustexist", "executescriptautomatically", "executeremediationscriptperblock", "acknowledged",
               "issubscription", "isscalable", "cmts", "requestedreboot", "isipv6", "iswlc", "jobendswithgeneralerror"}
TRUE_FLAGS = {"enabled", "isactive", "ismonitored", "managed", "iscomplete", "applicationenabled",
              "interfaceresponding", "allow64bitcounters", "enableorionimport", "includehistoricstatistics",
              "notifyenabled", "hashistoricaldata", "reliabledelivery", "showsummary", "showdetails", "postresult",
              "completevalues", "reportstatus", "canned", "preservewhitespace", "isindexed",
              "executescriptinconfigmode", "usecomparisoncriterias", "detailpageenabled", "datadringredundant"}
CREDENTIAL = {"password": "mock-secret-pw", "enablepassword": "mock-secret-enable", "snmpauthpass": "mock-secret-auth",
              "snmpencryptpass": "mock-secret-priv", "community": "mock-secret-ro",
              "communityreadwrite": "mock-secret-rw"}


def guess(entity: str, field: str, typ: str, c: RowCtx):
    """A value that fits the field's name and SWIS type for the row's context, or UNKNOWN."""
    f = field.lower()
    n, iface, ctx = c.node, c.iface, c.ctx
    now, now_utc = ctx.now, ctx.now_utc
    is_int = typ in ("System.Int16", "System.Byte", "System.Int32", "System.Int64", "System.SByte")
    is_num = is_int or typ in ("System.Double", "System.Single", "System.Decimal")
    if typ == "System.Byte[]":
        return None
    if f in c.extra:
        return c.extra[f]
    if f == "configid" and "_config" in c.extra:
        return c.extra["_config"]["ConfigID"]
    # ----- identity and links -----
    if f in ("nodeid", "ncmnodeid") and typ == "System.Guid":
        return c.guid
    if f in ("nodeid", "corenodeid", "networknode", "netobjectid", "parentobjectid", "srcorionnodeid",
             "datasourcenodeid") and is_int:
        return c.nid
    if f == "interfaceid":
        if typ == "System.Guid":
            return _guid("ncm-if", c.guid, iface["InterfaceID"]) if iface and c.guid else None
        return iface["InterfaceID"] if iface else None
    if f in ("interfaceindex", "ifindex", "portindex", "routeifindex", "localportnumber", "port") and is_int:
        return iface["Index"] if iface else None
    if f == "engineid":
        return (n or {}).get("EngineID", 1)
    if f in ("netobject", "entitynetobjectid"):
        return f"N:{c.nid}" if n else None
    if f == "netobjecttype":
        return "I" if iface else "N"
    if f == "orionidprefix":
        return "I:" if entity == "Orion.NPM.Interfaces" else "N:"
    if f == "orionidcolumn":
        return "InterfaceID" if entity == "Orion.NPM.Interfaces" else "NodeID"
    # ----- interface attributes -----
    if iface is not None:
        m = {"interfacename": iface["Name"], "ifname": iface["Name"], "portname": iface["Name"],
             "shortname": iface["Name"], "interfacealias": iface["Alias"], "interfacedescription": iface["Description"],
             "portdescription": iface["Alias"], "interfacecaption": iface["Caption"], "interfacemtu": iface["MTU"],
             "interfacespeed": iface["Speed"], "interfacehighspeed": iface["Speed"] / 1e6,
             "interfacetype": iface["Type"], "interfacetypename": iface["TypeName"],
             "interfacetypedescription": iface["TypeDescription"], "interfacelastchange": iface["LastChange"],
             "adminstatus": "up" if iface["AdminStatus"] == 1 else "down",
             "operstatus": "up" if iface["OperStatus"] == 1 else "down",
             "portstatus": "up" if iface["OperStatus"] == 1 else "down", "interfacesubtype": 0,
             "port": iface["Name"], "interfaceresponding": True}
        if f in m and (f not in ("adminstatus", "operstatus") or typ == "System.String"):
            v = m[f]
            return int(v) if is_int and isinstance(v, float) else v
        if f in ("speed",) and is_num:
            return iface["Speed"]
        if f in ("physicalinterface",):
            return "N" if iface["Type"] == 161 else "Y"
        if f in ("alias",):
            return iface["Alias"]
        if f in ("statusdescription",) and entity == "Orion.NPM.Interfaces":
            return "Up" if iface["OperStatus"] == 1 else "Down"
        if f in ("operstatusled", "statusled"):
            return "Up.gif" if iface["OperStatus"] == 1 else "Down.gif"
        if f == "adminstatusled":
            return "Up.gif"
        if f == "duplexmode":
            return 3
        if f in ("pollinterval",):
            return 120
        if f in ("statcollection",):
            return 10                      # the same 10 minutes as the nodes (and the rich samples)
    if f == "promiscuous":
        return "N"
    # ----- node attributes -----
    if n is not None:
        cap = n["Caption"]
        m = {"caption": cap, "nodecaption": cap, "nodename": cap, "sysname": cap, "displayname": cap,
             "parentobjectname": cap, "relatednodecaption": cap, "entitycaption": cap,
             "dns": f"{cap.lower()}.isp.local", "reversedns": f"{cap.lower()}.isp.local",
             "rdnslookup": f"{cap.lower()}.isp.local", "fullyqualifiedname": f"{cap.lower()}.isp.local",
             "vendor": n["Vendor"], "manufacturer": n["Vendor"], "parentobjectvendor": n["Vendor"],
             "machinetype": n["MachineType"], "model": n["MachineType"], "platforminfoname": n["MachineType"],
             "iosversion": n["IOSVersion"], "osversion": n["IOSVersion"], "swversion": n["IOSVersion"],
             "softwarerevision": n["IOSVersion"], "romsysversion": n["IOSVersion"], "location": n["Location"],
             "syslocation": n["Location"], "contact": n["Contact"], "syscontact": n["Contact"],
             "systemoid": n["SysObjectID"], "sysobjectid": n["SysObjectID"], "lastboot": n["LastBoot"],
             "sysdescr": f"{n['Vendor']} {n['MachineType']}, {n['IOSVersion']}".strip(", "),
             "nodedescription": f"{n['Vendor']} {n['MachineType']}, {n['IOSVersion']}".strip(", "),
             "parentobjectentity": "Orion.Nodes", "parentobjecttype": "Node", "parentobjectstatus": n["Status"],
             "agentip": n["IPAddress"], "nodeipaddress": n["IPAddress"]}
        if f in m:
            return m[f]
        if f in ("osimage", "iosimage", "softwareimage", "image") and typ == "System.String" and f != "image":
            return (IMAGES.get(vendor_key(n)) or ("",))[0]
        if f == "minutessincelastsync" or f == "minutessincelastsync":
            return n["MinutesSinceLastSync"]
        if f in ("avgresponsetime", "responsetime") and is_int:
            return n["ResponseTime"]
        if f in ("minresponsetime",):
            return max(1, n["ResponseTime"] - 2)
        if f in ("maxresponsetime",):
            return n["ResponseTime"] + 9
    if f == "lastsync" and typ == "System.DateTime":
        return (n or {}).get("LastSync") or _fmt(now - timedelta(minutes=_ri(1, 9, entity, c.i)))
    # ----- end-of-life (NCM matches these per model) -----
    if f in ("endofsales", "endofsupport", "endofsoftware", "eoslink", "replacementpartnumber", "eostype"):
        from . import fixtures
        eos = fixtures.EOS.get((n or {}).get("MachineType", ""), {})
        key = {"endofsales": "sales", "endofsupport": "support", "endofsoftware": "software", "eoslink": "link",
               "replacementpartnumber": "repl"}.get(f)
        if f == "eostype":
            return (1 if eos else 0) if is_int else ("Auto" if eos else "")
        v = eos.get(key)
        return v if v else (None if typ == "System.DateTime" else "")
    if f in ("eosmatchdate",):
        return _fmt(now - timedelta(days=40)) if n else None
    if f in ("eosentryid",):
        return _guid("eos", (n or {}).get("MachineType")) if n else None
    if f in ("eosversion",):
        return 3
    if f in ("eoscomments", "nodecomments", "comments", "comment", "eosmodel_comments"):
        return ""
    # ----- IP -----
    if f in ("ipaddress", "ip", "ip_address", "agentip", "nodeipaddress", "tcplocaladdress", "udpaddress", "taddress"):
        if f in ("taddress",):
            return f"{c.ip}:161" if c.ip else None
        return c.ip
    if f in ("ipaddressguid", "ipaddressn", "neighboripguid", "routedestinationguid", "routenexthopguid") or \
            (f.endswith("guid") and "ip" in f):
        return _guid("ip", c.ip) if c.ip else None
    if f in ("ipsort", "ipaddripsort", "agentipsort"):
        return float(_ip_int(c.ip or "")) if c.ip else None
    if f in ("subnetmask", "mask", "routemask", "addressmask"):
        return c.extra.get("mask") or "255.255.255.255"
    if f in ("ipversion", "ip_version"):
        return 4
    if f in ("ip_address_type", "ipaddresstype"):
        return "IPv4"
    if f in ("agentipv6",):
        return None
    if f in ("mac", "macaddress", "physicaladdress", "assignedrawmac") and typ == "System.String":
        return ctx.mac(n, iface, entity, c.i)
    # ----- serials, versions, parts -----
    if "serial" in f and typ == "System.String":
        return serial(n, entity, f, c.j)
    if f in ("hwversion", "hardwarerevision", "chassisversion", "jnxcontentsrevision"):
        return f"V0{_ri(1, 4, entity, c.nid, c.j)}"
    if f in ("fwversion", "firmwarerevision", "romversion"):
        return f"{_ri(1, 17, 'fw', c.nid)}.{_ri(0, 9, 'fw2', c.nid)}({_ri(1, 5, 'fw3', c.nid)})"
    if f in ("partnumber", "jnxcontentspartno"):
        return f"{_ri(700, 760, entity, c.nid, c.j)}-{_ri(10000, 99999, 'p', c.nid, c.j)}"
    # ----- times -----
    if typ == "System.DateTime" or (f == "datetime" and typ == "System.String"):
        if f in ("firstdiscovery", "created", "createdat", "creationdate", "installdate", "initialized",
                 "ncmjobdatecreated", "published", "startdate", "firstseenutc"):
            v = now - timedelta(days=_ri(120, 700, entity, f, c.nid, c.j))
        elif f in ("nextpoll", "nextrediscovery", "nextdaterunutc"):
            v = (now_utc if f.endswith("utc") else now) + timedelta(minutes=_ri(2, 600, entity, f, c.i))
        elif f in ("unmanagefrom", "unmanageuntil", "blockuntil", "expires", "lockouttime", "failoveractive",
                   "enddate", "suppressuntil", "acknowledgeddatetime", "leaseexpires", "passwordexpirationdate",
                   "expiration", "maintenanceexpiration"):
            return None
        elif f.startswith("lastdiscovery") or f in ("lastinventory", "laststatus", "lastpolltime", "lastupdatetime",
                                                     "lastrediscoverytime", "lastdownloadtime"):
            v = now - timedelta(hours=_ri(2, 20, entity, f, c.nid))
        else:
            v = now - timedelta(minutes=_ri(5, 7 * 1440, entity, f, c.nid, c.j, c.i))
        if f.endswith("utc"):
            v = v - (now - now_utc)
        return _fmt(v)
    # ----- flags -----
    if typ == "System.Boolean":
        if f in TRUE_FLAGS:
            return True
        return False
    # ----- status, severity -----
    if f in ("status", "objectstatus", "polledstatus", "groupstatusid") and is_int:
        return (n or {}).get("Status", 1) if f != "status" or not iface else iface["OperStatus"]
    if f == "statusdescription":
        return (n or {}).get("StatusDescription", "Up") if n else "Up"
    if f in ("statusled", "statusicon", "groupstatus"):
        st = (n or {}).get("Status", 1)
        return {1: "Up.gif", 2: "Down.gif", 9: "Unmanaged.gif"}.get(st, "Unknown.gif")
    if f in ("severity", "uiseverity", "childstatus", "skippedpollingcycles", "badpwdcount", "failedDeliveryAttempts"
             .lower()) and is_int:
        return 0
    if f == "loginstatus":
        return "Login OK"
    # ----- credentials and access -----
    if f in CREDENTIAL:
        return CREDENTIAL[f]
    if f in ("username", "snmpusername", "rtnusername"):
        return "svc_ncm"
    if f in ("account", "accountid", "approvedby", "author", "owner", "ncmjobcreator", "createdby",
             "acknowledgedby", "lasteditby"):
        return "netops"
    if f in ("commandprotocol", "execprotocol"):
        return "SSH"
    if f == "transferprotocol":
        return "SCP" if vendor_key(n) == "juniper" else "SSH"
    if f == "sshport":
        return "22"
    if f == "telnetport":
        return "23"
    if f in ("snmpport", "agentport"):
        return "161"
    if f in ("snmpversion",):
        return 2
    if f in ("snmplevel",):
        return 0
    if f in ("snmpauthtype", "snmpencrypttype", "snmpcontext"):
        return ""
    if f == "connectionprofile":
        return 1
    if f in ("enablelevel",):
        return "<No Enable Login>"
    if f in ("encryptionalgorithm", "managedprotocol"):
        return 0
    if f == "configtypes":
        return "Running,Startup"
    if f in ("nodegroup",):
        return role(n)
    # ----- network values -----
    if f in ("vlanid", "vlantag"):
        v = c.extra.get("vlan", 100)
        return v if is_int else str(v)
    if f in ("vlanname",):
        return f"VLAN{c.extra.get('vlan', 100)}"
    if f in ("vlanmtu", "mtu"):
        return 1500 if f == "vlanmtu" else 9216
    if f in ("vlanstate", "vlanstatus"):
        return 1
    if f in ("vlantype",):
        return 1 if is_int else ("access" if role(n) == "ACC" else "trunk")
    if f in ("neighborip", "remoteipaddress", "nexthop", "nexthopaddress", "routenexthop", "tcpremoteaddress",
             "remoteip"):
        return c.extra.get("peer_ip") or "0.0.0.0"
    if f in ("autonomoussystem", "localautonomoussystemnumber", "nexthopas", "sourceasid"):
        return 65000
    if f.startswith("metric") and f[-1].isdigit():
        return _ri(1, 40, entity, c.nid, c.i) if f == "metric1" else -1
    if f == "routeage":
        return _ri(600, 900000, entity, c.nid, c.i)
    if f in ("routeprotocol",):
        return {"P-CORE": "isis", "PE": "isis", "AGG": "ospf", "ACC": "ospf", "BR": "bgp"}.get(role(n), "local")
    if f in ("routetype",):
        return 4 if is_int else "remote"
    # ----- hardware and resources -----
    if f in ("cpucount", "activecpucount"):
        return 8
    if f in ("totalmemory", "memorytotal", "processorram") and is_num:
        v = {"P-CORE": 32, "PE": 64, "AGG": 16, "ACC": 4, "BR": 32}.get(role(n), 8) * 1024 ** 3
        return v // 1024 if f == "processorram" else float(v)
    if f in ("memoryused",) and is_num:
        tot = {"P-CORE": 32, "PE": 64, "AGG": 16, "ACC": 4, "BR": 32}.get(role(n), 8) * 1024 ** 3
        return float(round(tot * (n or {}).get("PercentMemoryUsed", 50) / 100))
    if f in ("memoryavailable",) and is_num:
        tot = {"P-CORE": 32, "PE": 64, "AGG": 16, "ACC": 4, "BR": 32}.get(role(n), 8) * 1024 ** 3
        return float(round(tot * (100 - (n or {}).get("PercentMemoryUsed", 50)) / 100))
    if f in ("percentmemoryavailable",):
        return 100 - (n or {}).get("PercentMemoryUsed", 50)
    if f in ("freemem",):
        return _ri(400000, 4000000, entity, c.nid)
    if f in ("nvramsize",):
        return 2048
    if f in ("nvramused",):
        return _ri(200, 1500, entity, c.nid)
    if f in ("loadaverage1", "loadaverage5", "loadaverage15"):
        return None
    # ----- presentation -----
    if f.endswith("url") or f == "requestlink":
        if iface is not None:
            return f"/Orion/Interfaces/InterfaceDetails.aspx?NetObject=I:{iface['InterfaceID']}"
        return f"/Orion/NetPerfMon/NodeDetails.aspx?NetObject=N:{c.nid}" if n else ""
    if f in ("vendoricon", "parentobjectvendoricon", "icon", "modernicon", "interfaceicon", "statusicon", "image"):
        return f"{vendor_key(n) or 'unknown'}.gif" if typ == "System.String" else None
    if f in ("description", "details", "assetid", "containedin", "alias", "hint"):
        return ""
    # ----- generic -----
    if c.extra.get("_fill") and (f.endswith("id") or f.endswith("index")):
        return UNKNOWN                     # a curated row: an unknown ID is never invented (it may be a reference)
    if is_int and (f.endswith("id") or f.endswith("index")):
        return c.i + 1
    if typ == "System.Guid" and f.endswith("id"):
        return _guid(entity, f, c.i)
    if f.endswith("count") and is_int:
        return _ri(0, 6, entity, f, c.nid, c.i)
    return UNKNOWN


# ---------------------------------------------------------------------------------------------------------------
# Row scopes: one row per what?
# ---------------------------------------------------------------------------------------------------------------
def is_cisco(n):
    return vendor_key(n) == "cisco"


def is_juniper(n):
    return vendor_key(n) == "juniper"


def has_eos(n):
    from . import fixtures
    return n["MachineType"] in fixtures.EOS


def is_routed(n):
    return role(n) in ("P-CORE", "PE", "AGG", "ACC", "BR")


def _ncm_nodes(ctx: Ctx, only=None):
    for x in ctx.ncm_rows:
        n = ctx.node.get(x["CoreNodeID"])
        if n and (only is None or only(n)):
            yield n, x["NodeID"]


def contexts(spec: dict, ctx: Ctx, D: dict):
    scope, only, n = spec["scope"], spec.get("only"), spec.get("n", 1)
    if scope == "ncm":
        for node, g in _ncm_nodes(ctx, only):
            for j in range(n):
                yield RowCtx(ctx, node=node, guid=g, j=j)
    elif scope == "ncm_if":
        for node, g in _ncm_nodes(ctx, only):
            for i in ctx.if_by_node.get(node["NodeID"], [])[:spec.get("max") or 999]:
                yield RowCtx(ctx, node=node, guid=g, iface=i)
    elif scope == "ncm_ip":
        for node, g in _ncm_nodes(ctx, only):
            for r in ctx.ips.get(node["NodeID"], []):
                yield RowCtx(ctx, node=node, guid=g, iface=ctx.if_by_index(node["NodeID"], r["InterfaceIndex"]),
                             extra={"ip": r["IPAddress"], "mask": r["SubnetMask"]})
    elif scope == "ncm_arp":                 # the peer of every point-to-point address, as an ARP entry
        for node, g in _ncm_nodes(ctx, only):
            for r in ctx.ips.get(node["NodeID"], []):
                if r["SubnetMask"] != "255.255.255.254":
                    continue
                peer = str(ipaddress.IPv4Address(_ip_int(r["IPAddress"]) ^ 1))
                pn = ctx.node.get(ctx.node_of_ip.get(peer))
                pi = next((x for x in ctx.ips.get(pn["NodeID"], []) if x["IPAddress"] == peer), None) if pn else None
                piface = ctx.if_by_index(pn["NodeID"], pi["InterfaceIndex"]) if pi else None
                yield RowCtx(ctx, node=node, guid=g, iface=ctx.if_by_index(node["NodeID"], r["InterfaceIndex"]),
                             extra={"ip": peer, "mac": ctx.mac(pn, piface, "arp", peer),
                                    "rdnslookup": f"{pn['Caption'].lower()}.isp.local" if pn else ""})
    elif scope in ("node", "node_all"):
        for node in (ctx.snmp if scope == "node" else ctx.nodes):
            if only is None or only(node):
                for j in range(n):
                    yield RowCtx(ctx, node=node, j=j)
    elif scope == "nbr":
        for nb in D["Orion.Routing.Neighbors"]:
            if only is None or only(nb):
                yield RowCtx(ctx, node=ctx.node[nb["NodeID"]], extra={"peer_ip": nb["NeighborIP"], "_nbr": nb})
    elif scope == "vrf":
        for v in D["Orion.Routing.VRF"]:
            for j in range(n):
                node = ctx.node[v["NodeID"]]
                yield RowCtx(ctx, node=node, iface=ctx.if_by_node[node["NodeID"]][j], j=j,
                             extra={"vrfindex": v["VrfIndex"], "_vrf": v})
    elif scope == "config":                  # the latest Running config of each NCM node
        for node, g in _ncm_nodes(ctx, only):
            cfg = ctx.latest_running.get(g)
            if cfg:
                for j in range(n):
                    yield RowCtx(ctx, node=node, guid=g, j=j, extra={"_config": cfg})
    elif scope == "items":
        for j, item in enumerate(spec["items"](ctx, D)):
            node = item.pop("_node", None)
            yield RowCtx(ctx, node=node, guid=item.pop("_guid", None), iface=item.pop("_iface", None), j=j,
                         extra=item)
    elif scope == "fixed":
        for j in range(n):
            yield RowCtx(ctx, j=j)
    else:
        raise ValueError(f"unknown scope {scope}")


def default_scope(fields: dict) -> dict:
    t = {k.lower(): v for k, v in fields.items()}
    if t.get("nodeid") == "System.Guid" or t.get("ncmnodeid") == "System.Guid":
        return {"scope": "ncm"}
    if t.get("nodeid") == "System.Int32" or t.get("corenodeid") == "System.Int32":
        return {"scope": "node"}
    if t.get("configid") == "System.Guid":
        return {"scope": "config"}
    return {"scope": "fixed", "n": 4}


# ---------------------------------------------------------------------------------------------------------------
# Per-entity specifications (scope + values that the generic guess cannot know)
# ---------------------------------------------------------------------------------------------------------------
def _cfg_line(c, *needles):
    for line in c.extra["_config"]["Config"].splitlines():
        if all(x in line for x in needles):
            return line
    return ""


def _cards(c):
    return CARDS.get(c.node["MachineType"]) or [("RP", "Route Processor"), ("LC", "Line Card")]


def _policy_rules(ctx, D):
    names = sorted({r["RuleName"] for r in D["Cirrus.PolicyCacheResults"]})
    pats = {"No telnet": ("transport input telnet", False), "SNMP RO only": ("snmp-server community .* RW", False),
            "NTP configured": ("^ntp server", True), "Logging host set": ("^logging host", True)}
    return [{"policyruleid": _guid("rule", nm), "name": nm, "pattern": pats.get(nm, ("", True))[0],
             "patternmustexist": pats.get(nm, ("", True))[1], "errorlevel": 2 if nm == "No telnet" else 1,
             "patterntype": "RegEx", "grouping": "Security", "remediatescripttype": "CLI",
             "remediatescript": "! review manually"} for nm in names]


def _policies(ctx, D):
    return [{"policyid": _guid("policy", "ISP policy"), "name": "ISP policy", "configtype": "Running",
             "grouping": "ISP", "nodeselection": "All NCM nodes"}]


def _vlans(ctx, D):
    out = []
    for r in D["Orion.NodeVlans"]:
        g = ctx.guid_of.get(r["NodeID"])
        if g:
            out.append({"_node": ctx.node[r["NodeID"]], "_guid": g, "vlan": r["VlanId"], "vlanname": r["VlanName"]})
    return out


def _cdp(ctx, D):
    out = []
    for e in D["Orion.NodeCdpEntry"]:
        n = ctx.node[e["NodeID"]]
        out.append({"_node": n, "_guid": ctx.guid_of.get(n["NodeID"]), "_iface": ctx.if_by_index(n["NodeID"], e["IfIndex"]),
                    "cdpindex": f"{e['IfIndex']}.1", "ifindex": e["IfIndex"], "remotedevice": e["DeviceId"],
                    "remoteport": e["DevicePort"], "remoteipaddress": e["IpAddress"], "remoteplatform": e["Platform"],
                    "remotecapability": "Router", "remoteduplex": "full", "remotenativevlan": 0,
                    "remoteversion": next((x["IOSVersion"] for x in ctx.nodes if x["IPAddress"] == e["IpAddress"]), "")})
    return out


def _routes(ctx, D):
    out = []
    for r in D["Orion.Routing.RoutingTable"]:
        g = ctx.guid_of.get(r["NodeID"])
        if g:
            n = ctx.node[r["NodeID"]]
            bits = (0xFFFFFFFF << (32 - r["RouteMaskLen"])) & 0xFFFFFFFF
            out.append({"_node": n, "_guid": g, "_iface": ctx.if_by_index(n["NodeID"], r["InterfaceIndex"]),
                        "destination": r["RouteDestination"], "mask": str(ipaddress.IPv4Address(bits)),
                        "peer_ip": r["RouteNextHop"], "metric1": r["Metric"],
                        "routeprotocol": {9: "isis", 13: "ospf", 14: "bgp"}.get(r["ProtocolID"], "local")})
    return out


def _l2(ctx, D):
    return [{"_node": ctx.node[r["NodeID"]], "_guid": ctx.guid_of.get(r["NodeID"]), "mac": r["MACAddress"],
             "port": r["PortID"], "vlan": r["VlanId"], "source": "learned"}
            for r in D["Orion.NodeL2Connections"] if ctx.guid_of.get(r["NodeID"])]


def _tcp(ctx, D):
    out = []
    for n, g in _ncm_nodes(ctx):
        rows = [(22, "ssh", "0.0.0.0", 0, "listen"), (830, "netconf", "0.0.0.0", 0, "listen"),
                (22, "ssh", "10.250.0.10", 51000 + n["NodeID"], "established")]
        if role(n) == "PE":
            rows.append((179, "bgp", ctx.D["Orion.Nodes"][0]["IPAddress"], 179, "established"))
        for lp, name, ra, rp, st in rows:
            out.append({"_node": n, "_guid": g, "tcplocaladdress": n["IPAddress"] if st == "established" else "0.0.0.0",
                        "tcplocalport": lp, "tcplocalportname": name, "tcpremoteaddress": ra, "tcpremoteport": rp,
                        "tcpremoteportname": name if rp else "", "tcpstate": st})
    return out


def _udp(ctx, D):
    return [{"_node": n, "_guid": g, "udpaddress": "0.0.0.0", "udpport": p, "udpportname": nm}
            for n, g in _ncm_nodes(ctx) for p, nm in ((161, "snmp"), (123, "ntp"), (514, "syslog"), (646, "ldp"))]


def _acls(ctx, D):
    out = []
    for n, g in _ncm_nodes(ctx, is_cisco):
        cfg = ctx.latest_running.get(g)
        if cfg:
            for nm, cx in (("MGMT-ACCESS", 4), ("SNMP-RO", 2)):
                out.append({"_node": n, "_guid": g, "configid": cfg["ConfigID"], "name": nm, "complexity": cx,
                            "hash": _ri(1, 2 ** 30, nm, g), "interfaces": "Loopback0" if nm == "MGMT-ACCESS" else ""})
    return out


def _config_ifaces(ctx, D):
    out, k = [], 0
    for n, g in _ncm_nodes(ctx):
        cfg = ctx.latest_running.get(g)
        if not cfg:
            continue
        names = ["Loopback0"] + (["TenGigE0/0/1"] if "TenGigE0/0/1" in cfg["Config"] else [])
        for nm in names:
            k += 1
            out.append({"_node": n, "_guid": g, "configid": cfg["ConfigID"], "id": k, "identifier": nm,
                        "interfacename": nm, "_ip": n["IPAddress"] if nm == "Loopback0" else None})
    return out


def _snippets(ctx, D):
    return [{"id": i + 1, "name": nm, "description": d, "advancedscript": s}
            for i, (nm, d, s) in enumerate((("Show version", "basic show", "show version"),
                                            ("Show interfaces brief", "interface summary", "show ip interface brief"),
                                            ("Save config", "write memory", "write memory")))]


def _jobs(ctx, D):
    return [{"ncmjobid": i + 1, "ncmjobname": nm, "ncmjobtype": t, "ncmjobschedule": sch}
            for i, (nm, t, sch) in enumerate((("Nightly config backup", 1, "Daily 01:00"),
                                              ("Weekly inventory", 2, "Weekly Sun 03:00"),
                                              ("Compliance report refresh", 3, "Daily 04:00"),
                                              ("EoS/EoL refresh", 4, "Monthly 1st 05:00")))]


def _backup_daily(ctx, D):
    total = len(ctx.ncm_rows)
    return [{"timestamp": _fmt((ctx.now - timedelta(days=d)).replace(hour=0, minute=0, second=0)),
             "allnodes": total, "backupnodes": total - (1 if d < 20 else 0) - (1 if d % 6 == 0 else 0),
             "granularity": None} for d in range(14)]


def _log_fields(ctx, D):
    out, k = [], 0
    for r in [x for x in D["Orion.OLM.LogEntry"] if x["LogEntryTypeID"] == 1][:400]:
        for fid, nm, num, txt in ((1, "Facility", 23.0, "local7"), (2, "Severity", float(r["LogEntryLevelID"]),
                                                                    r["Level"])):
            k += 1
            out.append({"logentryfieldvalueid": k, "logentryid": r["LogEntryID"], "logentryfieldid": fid, "name": nm,
                        "numericvalue": num, "textvalue": txt, "datetimevalue": None})
    return out


def _flaps(ctx, D):
    out = []
    flapper = [nb for nb in D["Orion.Routing.Neighbors"] if nb["ProtocolStatus"] == 1]
    for nb in D["Orion.Routing.Neighbors"]:
        for days in (1, 7, 30):
            cnt = (6 if days == 1 else 9 if days == 7 else 14) if nb in flapper else \
                (_ri(0, 1, nb["NeighborID"], days) * (days // 7))
            out.append({"_node": ctx.node[nb["NodeID"]], "neighborip": nb["NeighborIP"], "protocolid": nb["ProtocolID"],
                        "daysbucket": days, "flapcount": float(cnt), "datetime": _fmt(ctx.now)})
    return out


def _default_routes(ctx, D):
    out, k = [], 0
    for n in ctx.snmp:
        if role(n) not in ("BR", "PE"):
            continue
        for h in (30, 200):
            k += 1
            out.append({"_node": n, "changeid": k, "changetype": h == 30, "ip": "0.0.0.0", "vrfindex": 0,
                        "datetimeutc": _fmt(ctx.now_utc - timedelta(hours=h)), "ipaddress": "0.0.0.0",
                        "description": "Default route " + ("added" if h == 30 else "removed")})
    return out


def _ftrans(ctx, D):
    return [{"_node": ctx.node[ctx.core_of[t["NodeID"]]], "_guid": t["NodeID"], "jobid": i + 1,
             "transferstate": t["Status"], "errormessage": t["ErrorMessage"] or "",
             "requestedconfigtype": t["RequestedConfigType"]} for i, t in enumerate(D["NCM.TransferResults"])]


def _cache_diff(ctx, D):
    out = []
    by_node: dict[str, list] = {}
    for c in D["NCM.ConfigArchive"]:
        if c["ConfigType"] == "Running":
            by_node.setdefault(c["NodeID"], []).append(c)
    for g, cfgs in by_node.items():
        cfgs.sort(key=lambda c: c["DownloadTime"])
        new, old = cfgs[-1], cfgs[-2] if len(cfgs) > 1 else cfgs[-1]
        diff = new["Config"] != old["Config"]
        out.append({"_node": ctx.node[ctx.core_of[g]], "_guid": g, "cacheid": 1, "comparisontype": 1,
                    "configid": new["ConfigID"], "configtitle": new["ConfigTitle"], "configtitlebefore": old["ConfigTitle"],
                    "configtype": "Running", "configtypebefore": "Running", "diffflag": diff, "diffwidth": 0,
                    "difflist": "+ interface TenGigE0/0/1" if diff else "", "rtnmessageafter": "",
                    "rtnmessagebefore": ""})
    return out


def _assignments(ctx, D):
    return [{"custompollerassignmentid": a["CustomPollerAssignmentID"], "custompollerid": a["CustomPollerID"],
             "_node": ctx.node[a["NodeID"]], "assignmentname": a["AssignmentName"],
             "_last": next((s for s in D["Orion.NPM.CustomPollerStatus"]
                            if s["CustomPollerAssignmentID"] == a["CustomPollerAssignmentID"]), None)}
            for a in D["Orion.NPM.CustomPollerAssignment"]]


def _cp_on_node(ctx, D):
    out = []
    for a in _assignments(ctx, D):
        last = a.pop("_last") or {}
        a.update(rowid=last.get("RowID", "0"), status=last.get("Status"), rawstatus=float(last.get("RawStatus") or 0),
                 datetime=last.get("DateTime"), displayname=a["assignmentname"], rowstatus=1, rate=None, total=None,
                 description="")
        out.append(a)
    return out


def _cp_tab(ctx, D):
    out = []
    for a in _assignments(ctx, D):
        a.pop("_last")
        out.append({"custompollerassignmentid": a["custompollerassignmentid"], "_node": a["_node"],
                    "compressedrowid": "0", "rowlabel": "et-0/0/0"})
    return out


def _alert_ind(ctx, D):
    return []


def _module_rows(ctx, D):
    return [{"name": m["Name"], "version": m["Version"], "isactive": True} for m in D["Orion.InstalledModule"]]


def _ipconflict(ctx, D):
    out = []
    for i, cf in enumerate(D["IPAM.Conflict"]):
        node = next((x for x in D["IPAM.IPNode"] if x["IPAddress"] == cf["IPAddress"]), None)
        out.append({"ipconflictid": i + 1, "ipaddress": cf["IPAddress"], "activeconflicts": 1,
                    "ipconflictmsg": f"IP address {cf['IPAddress']} is used by {cf['AssignedMac']} and "
                                     f"{cf['ConflictingMac']}", "ipnodeid": node["IpNodeId"] if node else None,
                    "firstseenutc": cf["ConflictTimeUTC"], "lastseenutc": _fmt(ctx.now_utc - timedelta(minutes=20)),
                    "grouptype": 8, "role": "Admin", "status": 1, "accountid": "admin"})
    return out


def _juniper_parts(c):
    parts = [("Chassis", "MX960", "750-017001"), ("Routing Engine 0", "RE-S-X6-64G", "750-072006"),
             ("FPC 0", "MPC7E 3D 40XGE", "750-056519"), ("PEM 0", "PS 4.1kW; 200-240V AC in", "740-027760"),
             ("PEM 1", "PS 4.1kW; 200-240V AC in", "740-027760")]
    return parts[c.j]


SPECS: dict[str, dict] = {
    # ---- NCM inventory (per NCM node; Cisco-only tables only on Cisco nodes) ----
    "NCM.CiscoChassis": {"scope": "ncm", "only": is_cisco, "values": {
        "ChassisID": lambda c: f"{c.node['Caption']}-chassis", "ChassisName": lambda c: c.node["MachineType"],
        "ChassisSlots": lambda c: 6 if "9006" in c.node["MachineType"] else 1, "ChassisType": lambda c: 1,
        "ProcessorRAM": lambda c: 16777216 if "9006" in c.node["MachineType"] else 4194304,
        "RomID": lambda c: f"System Bootstrap, Version {c.node['IOSVersion']}",
        "WhyReload": lambda c: "power-on" if c.node["Caption"] == "ACC-11" else "Reload Command"}},
    "NCM.CiscoCards": {"scope": "ncm", "only": is_cisco, "n": 3, "values": {
        "CardIndex": lambda c: c.j + 1, "Slot": lambda c: c.j, "SlotsOnCard": lambda c: 2,
        "CardName": lambda c: _cards(c)[c.j % len(_cards(c))][0],
        "CardDescr": lambda c: _cards(c)[c.j % len(_cards(c))][1], "CardType": lambda c: 800 + c.j,
        "OperStatus": lambda c: "ok", "Parent": lambda c: 0}},
    "NCM.CiscoFlash": {"scope": "ncm", "only": is_cisco, "n": 2, "values": {
        "Name": lambda c: ["bootflash:", "harddisk:"][c.j], "FlashDescription": lambda c: ["internal flash", "SSD"][c.j],
        "FlashSize": lambda c: [16 * 1024 ** 3, 128 * 1024 ** 3][c.j], "Controller": lambda c: "",
        "MaxPartitions": lambda c: 1, "MinPartitionSize": lambda c: 1024 ** 2, "PartitionCount": lambda c: 1,
        "Removable": lambda c: "false", "WriteProtectJumper": lambda c: "installed"}},
    "NCM.CiscoFlashFiles": {"scope": "ncm", "only": is_cisco, "n": 2, "values": {
        "FlashFileName": lambda c: [IMAGES["cisco"][0 if "9006" in c.node["MachineType"] else 1], "startup-config"][c.j],
        "FlashFileSize": lambda c: [1_400_000_000 if "9006" in c.node["MachineType"] else 680_000_000, 18_000][c.j],
        "FlashCheckSum": lambda c: f"{_ri(0, 2 ** 31, 'ck', c.nid, c.j):08x}", "FlashFileStatus": lambda c: "valid"}},
    "NCM.CiscoImageMIB": {"scope": "ncm", "only": is_cisco, "n": 4, "values": {
        "ImageIndex": lambda c: c.j + 1, "Name": lambda c: ["CW_BEGIN", "CW_FAMILY", "CW_VERSION", "CW_END"][c.j],
        "Value": lambda c: ["$", c.node["MachineType"].split()[1] if " " in c.node["MachineType"] else "ASR",
                            c.node["IOSVersion"], "$"][c.j]}},
    "NCM.CiscoMemoryPools": {"scope": "ncm", "only": is_cisco, "n": 2, "values": {
        "PoolName": lambda c: ["Processor", "I/O"][c.j], "PoolUsed": lambda c: _ri(300_000_000, 900_000_000, c.nid, c.j),
        "PoolFree": lambda c: _ri(900_000_000, 1_500_000_000, c.nid, c.j),
        "PoolLargestFree": lambda c: _ri(400_000_000, 800_000_000, c.nid, c.j)}},
    "NCM.CiscoBootloadImages": {"scope": "ncm", "only": is_cisco, "values": {
        "ImageIndex": lambda c: 1, "SysBootImageList": lambda c: "bootflash:" + IMAGES["cisco"][1],
        "SysKickstartImageList": lambda c: ""}},
    "NCM.CiscoCdp": {"scope": "items", "items": _cdp},
    "NCM.CiscoFruFanTrayStatus": {"scope": "ncm", "only": is_cisco, "n": 2, "values": {
        "FRUIndex": lambda c: 100 + c.j, "FanTrayOperStatus": lambda c: 2}},
    "NCM.CiscoFruPowerStatus": {"scope": "ncm", "only": is_cisco, "n": 2, "values": {
        "FRUIndex": lambda c: 200 + c.j, "FRUPowerAdminStatus": lambda c: 1, "FRUPowerOperStatus": lambda c: 2}},
    "NCM.CiscoFruPowerSupplyGroups": {"scope": "ncm", "only": is_cisco, "values": {
        "FRUIndex": lambda c: 300, "AdminPowerRedundancyMode": lambda c: 2, "OperPowerRedundancyMode": lambda c: 2,
        "PowerUnits": lambda c: "CentiAmps@12V", "TotalAvailableCurrent": lambda c: 3000,
        "TotalDrawnCurrent": lambda c: _ri(900, 1900, c.nid)}},
    "NCM.EntityPhysicalJuniper": {"scope": "ncm", "only": is_juniper, "n": 5, "values": {
        "JnxContainersIndex": lambda c: c.j + 1, "JnxContentsL1Index": lambda c: c.j, "JnxContentsL2Index": lambda c: 0,
        "JnxContentsL3Index": lambda c: 0, "JnxContentsDescr": lambda c: _juniper_parts(c)[1],
        "JnxContentsType": lambda c: _juniper_parts(c)[0], "JnxContentsPartNo": lambda c: _juniper_parts(c)[2],
        "JnxContentsChassisId": lambda c: f"{c.node['Caption']}-chassis",
        "JnxContentsChassisDescr": lambda c: c.node["MachineType"]}},
    "NCM.EntityLogical": {"scope": "ncm", "values": {"Name": lambda c: "default", "Type": lambda c: "snmpUDPDomain",
                                                     "TDomain": lambda c: "snmpUDPDomain"}},
    "NCM.Interfaces": {"scope": "ncm_if"},
    "NCM.IpAddresses": {"scope": "ncm_ip", "values": {"ICMPReachable": lambda c: "Yes", "SNMPReachable": lambda c:
                                                      "Yes" if c.extra.get("mask") == "255.255.255.255" else "No",
                                                      "ResponseTime": lambda c: c.node["ResponseTime"]}},
    "NCM.ArpTables": {"scope": "ncm_arp", "values": {"Source": lambda c: "ARP"}},
    "NCM.MacForwarding": {"scope": "items", "items": _l2},
    "NCM.BridgePorts": {"scope": "ncm_if", "only": lambda n: role(n) == "ACC", "values": {
        "SpanningTreeEnabled": lambda c: "true", "SpanningTreeState": lambda c: "forwarding",
        "VlanType": lambda c: "trunk"}},
    "NCM.VLANs": {"scope": "items", "items": _vlans},
    "NCM.RouteTable": {"scope": "items", "items": _routes},
    "NCM.PortsTcp": {"scope": "items", "items": _tcp},
    "NCM.PortsUdp": {"scope": "items", "items": _udp},
    "NCM.NodeProperties": {"scope": "ncm"},
    "NCM.ConfigBackupStatistic": {"scope": "ncm", "values": {
        "ConfigType": lambda c: "Running", "TimeRangeHours": lambda c: 24,
        "LastDownloadTime": lambda c: (c.ctx.latest_running.get(c.guid) or {}).get("DownloadTime")}},
    "NCM.InventoryStatistic": {"scope": "ncm", "values": {"TimeRangeDays": lambda c: 7}},
    "NCM.LatestTransferJobStatus": {"scope": "items", "items": _ftrans},
    "NCM.Baselines": {"scope": "fixed", "n": 2, "values": {
        "Id": lambda c: c.j + 1, "Name": lambda c: ["IOS XR golden", "Junos golden"][c.j],
        "Content": lambda c: ["logging host 10.250.0.10\nsnmp-server community ****** RO",
                              "system { syslog { host 10.250.0.10; } }"][c.j],
        "Description": lambda c: "golden configuration", "IgnoredLines": lambda c: "^! Last configuration change"}},
    "NCM.BaselineNodeMap": {"scope": "ncm", "only": lambda n: is_cisco(n) or is_juniper(n), "values": {
        "BaselineId": lambda c: 1 if is_cisco(c.node) else 2, "CacheState": lambda c: 1,
        "ConfigType": lambda c: "Running"}},
    "NCM.BaselineViolations": {"scope": "config", "only": lambda n: is_cisco(n) or is_juniper(n), "values": {
        "BaselineId": lambda c: 1 if is_cisco(c.node) else 2, "ConfigID": lambda c: c.extra["_config"]["ConfigID"],
        "ConfigType": lambda c: "Running", "IsViolation": lambda c: "NEW-CUSTOMER-LINK" in c.extra["_config"]["Config"]}},
    "NCM.FirmwareDefinitions": {"scope": "fixed", "n": 2, "values": {
        "ID": lambda c: c.j + 1, "Name": lambda c: ["Cisco IOS XE upgrade", "Junos upgrade"][c.j],
        "Description": lambda c: "canned firmware upgrade template", "Author": lambda c: "SolarWinds",
        "DefinitionXml": lambda c: None}},
    "NCM.ConfigTypes": {"scope": "fixed", "n": 2, "values": {"Id": lambda c: c.j + 1,
                                                            "Name": lambda c: ["Running", "Startup"][c.j]}},
    "NCM.ConfigTypeVendors": {"scope": "fixed", "n": 6, "values": {
        "ConfigTypeId": lambda c: c.j % 2 + 1, "Operation": lambda c: 1,
        "Vendor": lambda c: ["Cisco", "Juniper Networks, Inc.", "Nokia"][c.j // 2]}},
    "NCM.AccessList": {"scope": "items", "items": _acls},
    "NCM.ShadowRuleDetectionAclStatistics": {"scope": "items", "items": lambda ctx, D: [
        {"configid": a["ConfigId"],
         "accesslistname": a["Name"], "complexity": a["Complexity"], "fullyredundantcount": 0,
         "fullyshadowedcount": 0, "partiallyredundantcount": 0, "partiallyshadowedcount": 0,
         "calculationdurationinmiliseconds": _ri(3, 40, a["Name"], a["ConfigId"])} for a in D.get("NCM.AccessList", [])]},
    "NCM.ObjectDefinitionData": {"scope": "config", "only": is_cisco, "values": {
        "Name": lambda c: "MGMT-HOSTS", "Description": lambda c: "management hosts", "Type": lambda c: 1,
        "Hash": lambda c: _ri(1, 2 ** 30, "obj", c.guid)}},
    "NCM.ObjectDefinitionDataValue": {"scope": "config", "only": is_cisco, "n": 2, "values": {
        "ObjectName": lambda c: "MGMT-HOSTS", "Value": lambda c: ["10.250.0.10", "10.250.0.20"][c.j]}},
    "NCM.ObjectGroupData": {"scope": "config", "only": is_cisco, "values": {
        "Name": lambda c: "NMS-SERVICES", "Description": lambda c: "SNMP and syslog", "Type": lambda c: 2,
        "ServiceProtocol": lambda c: 17, "Hash": lambda c: _ri(1, 2 ** 30, "grp", c.guid)}},
    "NCM.ParsedConfigData": {"scope": "config", "values": {
        "Id": lambda c: c.i + 1, "ConfigContent": lambda c: c.extra["_config"]["Config"],
        "ConfigContentRaw": lambda c: c.extra["_config"]["Config"]}},
    "NCM.ConfigInterface": {"scope": "items", "items": _config_ifaces},
    "NCM.ConfigInterfaceIpAddress": {"scope": "items", "items": lambda ctx, D: [
        {"id": k + 1, "interfaceid": ci["ID"], "ipaddress": ip} for k, (ci, ip) in enumerate(
            (ci, next((n["IPAddress"] for n in ctx.nodes if ctx.guid_of.get(n["NodeID"]) and
                       ctx.latest_running.get(ctx.guid_of[n["NodeID"]], {}).get("ConfigID") == ci["ConfigID"]), None))
            for ci in D.get("NCM.ConfigInterface", []) if ci["InterfaceName"] == "Loopback0")]},
    "NCM.OneTimeOperations": {"scope": "ncm", "only": lambda n: role(n) == "PE", "values": {
        "ID": lambda c: c.i + 1, "Name": lambda c: "show version (ad hoc)", "ScriptContent": lambda c: "show version",
        "Type": lambda c: 1, "ApprovalStatus": lambda c: 2, "ApprovalStatusString": lambda c: "Approved",
        "NCMNodeID": lambda c: c.guid, "CoreNodeID": lambda c: c.nid}},
    "NCM.RTNAudit": {"scope": "ncm", "only": lambda n: n["NodeID"] % 2 == 0, "values": {
        "ID": lambda c: _guid("rtn", c.guid), "IP": lambda c: c.node["IPAddress"],
        "DateTime": lambda c: _fmt(c.ctx.now - timedelta(hours=_ri(1, 70, "rtn", c.nid))),
        "Message": lambda c: "Configuration change detected (real-time change notification)"}},
    # ---- Cirrus.* (older NCM names): same data as their NCM.* twin, or their own ----
    "Cirrus.Audit": {"scope": "ncm", "only": lambda n: role(n) == "PE", "values": {
        "ID": lambda c: _guid("audit", c.guid), "Action": lambda c: "Download config", "Type": lambda c: "Transfer",
        "ModuleName": lambda c: "NCM", "Details": lambda c: f"Running config downloaded from {c.node['Caption']}",
        "RequestID": lambda c: "", "RequestLink": lambda c: ""}},
    "Cirrus.Backup_vs_AllNodes": {"scope": "items", "items": _backup_daily},
    "Cirrus.CacheDiffResults": {"scope": "items", "items": _cache_diff},
    "Cirrus.CompareRegExs": {"scope": "fixed", "n": 2, "values": {
        "RegExID": lambda c: _guid("re", c.j), "Title": lambda c: ["Change timestamp", "NTP clock period"][c.j],
        "RegEx": lambda c: ["^! Last configuration change", "^ntp clock-period"][c.j],
        "DotNetRegEx": lambda c: ["^! Last configuration change", "^ntp clock-period"][c.j],
        "BlockEndRegEx": lambda c: "", "IsBlock": lambda c: False}},
    "Cirrus.ComparisonCache": {"scope": "fixed", "n": 1, "values": {"CacheID": lambda c: 1}},
    "Cirrus.PolicyCache": {"scope": "fixed", "n": 1, "values": {"CacheID": lambda c: 1}},
    "Cirrus.ConfigSnippets": {"scope": "items", "items": _snippets},
    "Cirrus.SnippetArchive": {"scope": "items", "items": lambda ctx, D: [
        {"configid": _guid("snip", s["ID"]), "configtitle": s["Name"], "config": s["AdvancedScript"],
         "downloadtime": s["Created"], "modifiedtime": s["LastModified"], "comments": ""}
        for s in D.get("Cirrus.ConfigSnippets", [])]},
    "Cirrus.Tags": {"scope": "items", "items": lambda ctx, D: [
        {"snippetid": s["ID"], "tagname": "show" if s["AdvancedScript"].startswith("show") else "write"}
        for s in D.get("Cirrus.ConfigSnippets", [])]},
    "Cirrus.GlobalSettings": {"scope": "fixed", "n": 4, "values": {
        "SettingName": lambda c: ["ArchiveConfigs", "ConfigArchiveDirectory", "RTNEnabled", "MaxConfigsPerNode"][c.j],
        "SettingValue": lambda c: ["True", r"C:\SolarWinds\Configs", "True", "50"][c.j]}},
    "Cirrus.Options": {"scope": "fixed", "n": 3, "values": {
        "OptionName": lambda c: ["DefaultTransferProtocol", "DefaultExecProtocol", "Comparison.IgnoreWhitespace"][c.j],
        "OptionValue": lambda c: ["SSH", "SSH", "True"][c.j]}},
    "Cirrus.LatestComparisonResults": {"scope": "items", "items": lambda ctx, D: [
        {"_node": r["_node"], "_guid": r["_guid"], "comparisontype": 1, "configtype": "Running",
         "diffflag": r["diffflag"]} for r in _cache_diff(ctx, D)]},
    "Cirrus.LatestPolicyReportViolations": {"scope": "items", "items": lambda ctx, D: [
        {"reportid": v["ReportID"], "info": v["Info"], "warning": v["Warning"], "error": v["Error"]}
        for v in D["Cirrus.PolicyReportViolations"]]},
    "Cirrus.MatrixTargets": {"scope": "fixed", "n": 1, "values": {
        "MatrixTargetID": lambda c: _guid("mt", 0), "TargetName": lambda c: "Loopback0 description",
        "TargetDescription": lambda c: "interface description", "TargetPattern": lambda c: "^interface Loopback0",
        "TargetParamater": lambda c: 0, "EnableCommand": lambda c: "", "DisableCommand": lambda c: ""}},
    "Cirrus.NCMNodeLicenseStatus": {"scope": "items", "items": lambda ctx, D: [
        {"nodeid": x["CoreNodeID"], "licensedbyncm": "True"} for x in ctx.ncm_rows]},
    "Cirrus.NCM_EosMatchQueue": {"scope": "ncm", "only": lambda n: has_eos(n), "values": {
        "ID": lambda c: c.i + 1, "EosModel": lambda c: c.node["MachineType"], "Certainty": lambda c: "High",
        "CertaintyStyle": lambda c: "high", "Rank": lambda c: 1, "PartNumber": lambda c: c.node["MachineType"]}},
    "Cirrus.NCM_NCMJobs": {"scope": "items", "items": _jobs},
    "Cirrus.NCM_NCMJobsView": {"scope": "items", "items": lambda ctx, D: [
        dict(j, allsubjobs=len(ctx.ncm_rows), completedsubjobs=len(ctx.ncm_rows) - (1 if j["ncmjobid"] == 1 else 0),
             status=1, joblogindicator=0, lastdaterun=_fmt(ctx.now - timedelta(hours=8 + j["ncmjobid"]))) for j in
        _jobs(ctx, D)]},
    "Cirrus.NCM_JobEngineNCMJobs": {"scope": "items", "items": lambda ctx, D: [
        {"ncmjobid": j["ncmjobid"], "jobenginejobid": 100 + j["ncmjobid"], "jobstatus": 2,
         "datetime": _fmt(ctx.now - timedelta(hours=8 + j["ncmjobid"]))} for j in _jobs(ctx, D)]},
    "Cirrus.Policies": {"scope": "items", "items": _policies},
    "Cirrus.PolicyRules": {"scope": "items", "items": _policy_rules},
    "Cirrus.PolicyAssignment": {"scope": "items", "items": lambda ctx, D: [
        {"policyid": _guid("policy", "ISP policy"), "policyreportid": r["PolicyReportID"]}
        for r in D["Cirrus.PolicyReports"]]},
    "Cirrus.PolicyRuleAssignment": {"scope": "items", "items": lambda ctx, D: [
        {"policyid": _guid("policy", "ISP policy"), "policyruleid": r["policyruleid"]} for r in _policy_rules(ctx, D)]},
    # ---- Orion platform / NPM ----
    "IPAM.IPConflict": {"scope": "items", "items": _ipconflict},
    "Orion.Module": {"scope": "items", "items": _module_rows},
    "Orion.NPM.CustomPollerStatusOnNode": {"scope": "items", "items": _cp_on_node},
    "Orion.NPM.CustomPollerStatusOnNodeTabular": {"scope": "items", "items": _cp_tab},
    "Orion.NPM.InterfacesRelationshipType": {"scope": "fixed", "n": 2, "values": {
        "RelationshipTypeID": lambda c: c.j + 1, "Name": lambda c: ["Aggregation", "Sub-interface"][c.j]}},
    "Orion.NodeL3RoutingData": {"scope": "items", "items": lambda ctx, D: [
        {"_node": ctx.node[r["NodeID"]], "routedestination": r["RouteDestination"],
         "routemask": str(ipaddress.IPv4Address((0xFFFFFFFF << (32 - r["RouteMaskLen"])) & 0xFFFFFFFF)),
         "nexthopaddress": r["RouteNextHop"], "routeifindex": r["InterfaceIndex"], "routetype": 4}
        for r in D["Orion.Routing.RoutingTable"] if int(r["RouteDestination"].split(".")[1]) < 5]},
    "Orion.NodeMACAddresses": {"scope": "items", "items": lambda ctx, D: [
        {"_node": ctx.node[i["NodeID"]], "mac": i["PhysicalAddress"], "datetime": i["LastSync"]}
        for i in ctx.ifaces if i["PhysicalAddress"]]},
    "Orion.NodeSettings": {"scope": "node", "n": 2, "values": {
        "NodeSettingID": lambda c: c.i + 1, "SettingName": lambda c: ["ROSNMPCredentialID", "NeedsInventory"][c.j],
        "SettingValue": lambda c: ["1", ""][c.j]}},
    "Orion.OLM.LogEntryFieldValue": {"scope": "items", "items": _log_fields},
    "Orion.OLM.LogEntryLevel": {"scope": "items", "items": lambda ctx, D: [
        {"logentrylevelid": k, "level": v.capitalize(), "levelkey": v} for k, v in LEVELS.items()]},
    "Orion.OLM.MessageSources": {"scope": "node_all", "values": {
        "MessageSourceID": lambda c: c.nid, "Caption": lambda c: c.node["Caption"], "Status": lambda c: 1,
        "Created": lambda c: _fmt(c.ctx.now - timedelta(days=300))}},
    "Orion.Routing.DefaultRouteChange": {"scope": "items", "items": _default_routes},
    "Orion.Routing.NeighborsFlapCount": {"scope": "items", "items": _flaps},
    "Orion.Routing.Router": {"scope": "node", "only": is_routed, "values": {
        "Caption": lambda c: c.node["Caption"], "Description": lambda c: f"{role(c.node)} router"}},
    "Orion.Routing.VRFInterface": {"scope": "vrf", "n": 2, "values": {
        "IfIndex": lambda c: c.iface["Index"], "VrfIndex": lambda c: c.extra["vrfindex"],
        "Description": lambda c: f"{c.extra['_vrf']['Name']} on {c.iface['Name']}",
        "VrfInterfaceType": lambda c: 1, "DistributionProtocol": lambda c: 2}},
    "Orion.SysLogSeverities": {"scope": "fixed", "n": 8, "values": {   # RFC 5424 §6.2.1
        "SeverityCode": lambda c: c.j, "SeverityName": lambda c: ["Emergency", "Alert", "Critical", "Error", "Warning",
                                                                  "Notice", "Informational", "Debug"][c.j]}},
    "Orion.LogEntity": {"scope": "items", "items": lambda ctx, D: [
        {"observationtimestamp": r["DateTime"], "observationseverity": r["LogEntryLevelID"],
         "observationseverityname": r["Level"]} for r in D["Orion.OLM.LogEntry"][:200]]},
    "Orion.MixedObjectType": {"scope": "node_all", "values": {
        "NetObjectID": lambda c: c.nid, "NetObjectType": lambda c: "N", "NetworkNode": lambda c: c.nid}},
}
# Same data as their twin (the twin is generated or curated first); fields only the copy has are guessed.
MIRRORS = {f"Cirrus.{x}": f"NCM.{x}" for x in (
    "ArpTables", "BridgePorts", "CiscoCards", "CiscoCdp", "CiscoChassis", "CiscoFlash", "CiscoFlashFiles",
    "CiscoImageMIB", "CiscoMemoryPools", "EntityLogical", "EntityPhysical", "Interfaces", "IpAddresses",
    "MacForwarding", "NodeProperties", "PortsTcp", "PortsUdp", "RouteTable", "VLANs")}
MIRRORS.update({"Cirrus.ConfigArchive": "NCM.ConfigArchive",
                "Orion.Topology.TopologyConnections": "Orion.TopologyConnections",
                "Orion.HardwareHealth.HardwareInfoBase": "Orion.HardwareHealth.HardwareInfo",
                "Orion.HardwareHealth.HardwareItemBase": "Orion.HardwareHealth.HardwareItem"})
ALIASES = {"Description": ["FlashDescription", "EntityDescription"], "Name": ["EntityName"],
           "ChassisSerialNumber": ["ChassisSerialNumberString"], "Type": ["EntityType", "EntityClass"]}
# Fill values for documented fields the curated rows lack, where the generic guess would be wrong.
FILL = {
    "Orion.TopologyConnections": {"SrcOrionNodeID": lambda c, r: r["SrcNodeID"],
                                  "DestOrionNodeID": lambda c, r: r["DestNodeID"], "DiscoveryProfileID": lambda c, r: 1},
    "Orion.NodeLldpEntry": {"RemoteIfIndex": lambda c, r: None},
    "Orion.Routing.Neighbors": {
        "ProtocolName": lambda c, r: {9: "IS-IS", 13: "OSPF", 14: "BGP"}.get(r["ProtocolID"]),
        "VrfList": lambda c, r: "", "Description": lambda c, r: "", "BgpRole": lambda c, r: "",
        "LocalAutonomousSystemNumber": lambda c, r: 65000 if r["ProtocolID"] == 14 else None,
        "BgpNeighborLastError": lambda c, r: "Hold Timer Expired" if r["ProtocolStatus"] == 1 else "",
        "ProtocolStatusDescription": lambda c, r: None, "ProtocolOrionStatus": lambda c, r: None,
        "ProtocolStatusLED": lambda c, r: None, "Status": lambda c, r: None,
        "BgpNeighborAdminStatus": lambda c, r: None, "BgpNeighborAdminStatusDescription": lambda c, r: None,
        "HelloSuppressed": lambda c, r: False},
    "Orion.Routing.RoutingTable": {
        "RouteID": lambda c, r: c.i + 1, "NodeCaption": lambda c, r: c.node["Caption"],
        "ProtocolName": lambda c, r: {9: "IS-IS", 13: "OSPF", 14: "BGP"}.get(r["ProtocolID"]),
        "VrfIndex": lambda c, r: 0, "VrfName": lambda c, r: "default", "NeighborID": lambda c, r: None,
        "NodeStatus": lambda c, r: c.node["Status"], "InterfaceStatus": lambda c, r: None,
        "NextHopStatus": lambda c, r: None, "ProtocolStatusDescription": lambda c, r: None,
        "LastChange": lambda c, r: _fmt(c.ctx.now - timedelta(hours=_ri(1, 400, "rt", c.i)))},
    "Orion.Routing.RoutingTableFlap": {"DaysBucket": lambda c, r: 1, "Ip_Version": lambda c, r: 4},
    "Orion.HardwareHealth.HardwareItem": {
        "ID": lambda c, r: c.i + 1,
        "HardwareInfoID": lambda c, r: next((h.get("ID") for h in c.ctx.D["Orion.HardwareHealth.HardwareInfo"]
                                             if h["NodeID"] == r["NodeID"]), None),
        "HardwareCategoryID": lambda c, r: 1 if r["Name"].startswith("PSU") else 2 if r["Name"].startswith("Fan") else 3,
        "HardwareUnitDescription": lambda c, r: r["Unit"], "ParentID": lambda c, r: None,
        "Message": lambda c, r: "", "OriginalStatus": lambda c, r: "ok", "StatusDescription": lambda c, r: "Up"},
    "Orion.HardwareHealth.HardwareInfo": {"ID": lambda c, r: c.i + 1, "PollingMethod": lambda c, r: 1,
                                          "ParentObjectID": lambda c, r: r["NodeID"],
                                          "LastPollStatus": lambda c, r: 1, "LastPollStatusName": lambda c, r: None},
    "Orion.Events": {"NetObjectValue": lambda c, r: (c.node or {}).get("Caption")},
    "Orion.AlertObjects": {"IsActiveAlert": lambda c, r: any(a["AlertObjectID"] == r["AlertObjectID"]
                                                             for a in c.ctx.D["Orion.AlertActive"]),
                           "EntityUri": lambda c, r: None, "RealEntityUri": lambda c, r: None,
                           "RelatedNodeUri": lambda c, r: None},
    "Orion.OLM.LogEntryType": {"Name": lambda c, r: r["Type"], "DetailPageEnabled": lambda c, r: True,
                               "RetentionPeriodInDays": lambda c, r: None},
    "NCM.ConfigArchive": {"ObservationTimestamp": lambda c, r: r["DownloadTime"],
                          "ModifiedTime": lambda c, r: r["DownloadTime"],
                          "AttemptedDownloadTime": lambda c, r: r["DownloadTime"], "IsIndexed": lambda c, r: True,
                          "ObservationSeverity": lambda c, r: None, "ObservationSeverityName": lambda c, r: None,
                          "Hash": lambda c, r: f"{zlib.crc32(r['Config'].encode()):08x}"},
    "Cirrus.PolicyCacheResults": {
        "PolicyID": lambda c, r: _guid("policy", "ISP policy"), "RuleID": lambda c, r: _guid("rule", r["RuleName"]),
        "ReportName": lambda c, r: next((x["Name"] for x in c.ctx.D["Cirrus.PolicyReports"]
                                         if x["PolicyReportID"] == r["ReportID"]), None),
        "CacheID": lambda c, r: 1, "ConfigType": lambda c, r: "Running", "RuleDescription": lambda c, r: ""},
    "IPAM.Subnet": {"TotalCount": lambda c, r: 2 ** (32 - r["CIDR"]), "ReservedCount": lambda c, r: 0,
                    "TransientCount": lambda c, r: 0,
                    "AddressMask": lambda c, r: str(ipaddress.IPv4Network(f"0.0.0.0/{r['CIDR']}").netmask),
                    "StatusName": lambda c, r: None, "StatusShortDescription": lambda c, r: None},
    "IPAM.IPNode": {"ResponseTime": lambda c, r: None, "IPOrdinal": lambda c, r: None},
    "Orion.NodeVlans": {"VlanIfIndex": lambda c, r: None, "LastSyncUtc": lambda c, r: _fmt(c.ctx.now_utc),
                        "DisplayName": lambda c, r: r["VlanName"], "Description": lambda c, r: ""},
    "Orion.NPM.CustomPollerAssignment": {
        "CustomPollerName": lambda c, r: c.extra["_cp"].get(r["CustomPollerID"], {}).get("UniqueName"),
        "CustomPollerOid": lambda c, r: c.extra["_cp"].get(r["CustomPollerID"], {}).get("OID"),
        "CustomPollerMIB": lambda c, r: c.extra["_cp"].get(r["CustomPollerID"], {}).get("MIB"),
        "CustomPollerDescription": lambda c, r: c.extra["_cp"].get(r["CustomPollerID"], {}).get("Description"),
        "CurrentValue": lambda c, r: None, "ID": lambda c, r: c.i + 1},
    "Orion.Engines": {"DisplayName": lambda c, r: r["ServerName"], "EngineVersion": lambda c, r: "2024.2.1",
                      "PackageName": lambda c, r: "Orion Platform", "MinutesSinceKeepAlive": lambda c, r: 1,
                      "NodePollInterval": lambda c, r: 120, "NodeStatPollInterval": lambda c, r: 10,
                      "InterfacePollInterval": lambda c, r: 120, "InterfaceStatPollInterval": lambda c, r: 9,
                      "Evaluation": lambda c, r: "False", "CompanyName": lambda c, r: "ISP",
                      "LicenseKey": lambda c, r: None, "SerialNumber": lambda c, r: None},
}


# ---------------------------------------------------------------------------------------------------------------
# Building the rows
# ---------------------------------------------------------------------------------------------------------------
def synthetic_key(name: str, fields: dict) -> str | None:
    """Same rule as MockSwis._synthetic_key: the field the mock lists as IsKey."""
    short = name.rsplit(".", 1)[-1]
    for k in (f"{short}ID", f"{short.rstrip('s')}ID", "ID"):
        if k in fields:
            return k
    return None


def _key_value(entity: str, t: str, seq: int):
    if t in ("System.Int32", "System.Int64", "System.Int16"):
        return seq + 1
    return _guid(entity, "key", seq) if t == "System.Guid" else f"{entity.rsplit('.', 1)[-1]}:{seq + 1}"


def make_row(entity: str, fields: dict, c: RowCtx, values: dict, key: str | None, seq: int) -> dict:
    row = {}
    for f, t in fields.items():
        if f == "Uri":
            continue
        if f in values:
            v = values[f](c) if callable(values[f]) else values[f]
        else:
            v = guess(entity, f, t, c)
            if v is UNKNOWN:
                v = None
        if f == key and v in (None, ""):          # a key needs a value; generate() renumbers duplicates
            v = _key_value(entity, t, seq)
        if t in ("System.Single", "System.Double", "System.Decimal") and isinstance(v, int) and not isinstance(v, bool):
            v = float(v)
        row[f] = v
    return row


def generate(D: dict, ctx: Ctx) -> dict[str, str]:
    """Rows for every documented entity the curated ISP lacks. Returns {entity: reason} for the empty ones."""
    empty: dict[str, str] = {}
    p = pages()
    names = [e for e in p if e not in D and not e.startswith(("System.", "Metadata."))]
    first = ["NCM.ConfigInterface", "NCM.AccessList", "Cirrus.ConfigSnippets", "Cirrus.PolicyRules",
             "Orion.HardwareHealth.HardwareInfoBase"]
    names.sort(key=lambda e: (e not in first, 0 if e.startswith("NCM.") else 2 if e.startswith("Cirrus.") else 1, e))
    for name in names:
        bases = p[name].get("bases") or []
        if "System.Indication" in bases:
            continue                                   # indications are events of a subscription, not data rows
        fields = doc_fields(name)
        if name in RICH_SERIES:
            continue                                   # metrics: built by the history model
        why = empty_reason(name, fields)
        if why:
            D[name], empty[name] = [], why
            continue
        key = synthetic_key(name, p[name].get("fields") or {})
        if name in MIRRORS and MIRRORS[name] in D:
            D[name] = [mirror_row(name, fields, r, ctx, i, key) for i, r in enumerate(D[MIRRORS[name]])]
            continue
        spec = SPECS.get(name) or default_scope(fields)
        rows = []
        for i, c in enumerate(contexts(spec, ctx, D)):
            c.i = i
            rows.append(make_row(name, fields, c, spec.get("values") or {}, key, i))
        if key and len({r[key] for r in rows}) < len(rows):     # the key the mock lists must be unique
            for i, r in enumerate(rows):
                r[key] = _key_value(name, fields[key], i)
        D[name] = rows
    return empty


def mirror_row(entity: str, fields: dict, src: dict, ctx: Ctx, i: int, key: str | None) -> dict:
    node = ctx.node.get(src.get("NodeID")) if isinstance(src.get("NodeID"), int) else \
        ctx.node.get(ctx.core_of.get(src.get("NodeID")))
    c = RowCtx(ctx, node=node, guid=src.get("NodeID") if isinstance(src.get("NodeID"), str) else None)
    c.i = i
    out = {}
    for f, t in fields.items():
        if f == "Uri":
            continue
        if f in src:
            out[f] = src[f] if fits(src[f], t) else None       # a twin value of another type: no value here
        elif any(a in src for a in ALIASES.get(f, [])):
            v = next(src[a] for a in ALIASES[f] if a in src)
            out[f] = v if fits(v, t) else None
        else:
            v = guess(entity, f, t, c)
            out[f] = None if v is UNKNOWN else v
        if f == key and out[f] is None:
            out[f] = i + 1
    return out


def fill_missing(D: dict, ctx: Ctx, skip: set) -> dict[str, list[str]]:
    """Fill the documented fields that curated rows lack, where a value is known. Never overwrites a value."""
    filled: dict[str, list[str]] = {}
    cps = {cp["CustomPollerID"]: cp for cp in D.get("Orion.NPM.CustomPollers", [])}
    for name in sorted(D):
        if name in skip or name.startswith(("System.", "Metadata.")) or name not in pages():
            continue
        fields = doc_fields(name)
        rows = D[name]
        have = set().union(*[set(r) for r in rows]) if rows else set()
        missing = {f: t for f, t in fields.items() if f not in have and f != "Uri"}
        if not missing or not rows:
            continue
        over = FILL.get(name, {})
        for i, r in enumerate(rows):
            nid = r.get("NodeID")
            node = ctx.node.get(nid) if isinstance(nid, int) else ctx.node.get(ctx.core_of.get(nid)) if nid else None
            node = node or (r if name == "Orion.Nodes" else None) or ctx.node.get(r.get("NetworkNode")) or \
                ctx.node.get(r.get("CoreNodeID")) or ctx.node.get(r.get("RelatedNodeId"))
            iface = r if name == "Orion.NPM.Interfaces" else ctx.iface.get(r.get("InterfaceID"))
            extra = {"_fill": True, "_cp": cps}
            if r.get("IPAddress") or r.get("IpAddress"):
                extra["ip"] = r.get("IPAddress") or r.get("IpAddress")
            c = RowCtx(ctx, node=node, guid=nid if isinstance(nid, str) else None, iface=iface, extra=extra)
            c.i = i
            for f, t in missing.items():
                if f in over:
                    v = over[f](c, r)
                else:
                    c.extra.pop(f.lower(), None)
                    v = guess(name, f, t, c)
                    v = None if v is UNKNOWN else v
                if t in ("System.Single", "System.Double", "System.Decimal") and isinstance(v, int) \
                        and not isinstance(v, bool):
                    v = float(v)
                if v is not None and not fits(v, t):
                    v = None
                r[f] = v
        filled[name] = sorted(missing)
    return filled


def fit_curated(D: dict) -> None:
    """Curated values that do not fit their documented type, made to fit where the meaning is plain: "" in a
    DateTime or Guid field is no value (null); a number written as text in a Single/Double field is that number.
    Other misfits keep their value, and the mock then lists the field with the value's own type."""
    for name, rows in D.items():
        if name not in pages() or not rows:
            continue
        for f, t in doc_fields(name).items():
            for r in rows:
                v = r.get(f)
                if v == "" and t in ("System.DateTime", "System.Guid"):
                    r[f] = None
                elif isinstance(v, str) and t in ("System.Single", "System.Double", "System.Decimal"):
                    try:
                        r[f] = float(v)
                    except ValueError:
                        pass
    for s in D.get("IPAM.Subnet", []):                 # the curated subnets count used addresses only
        total = 2 ** (32 - s["CIDR"])
        s["AvailableCount"] = total - s["UsedCount"]
        s["PercentUsed"] = round(100.0 * s["UsedCount"] / total, 2)


def remap_ids(D: dict) -> None:
    """The curated ISP uses short string IDs where the 2026.2 schema documents a Guid. Rich mode replaces them
    with GUIDs everywhere they appear, so each column gets its documented type."""
    cp = {r["CustomPollerID"]: _guid("cp", r["CustomPollerID"]) for r in D["Orion.NPM.CustomPollers"]}
    asg = {r["CustomPollerAssignmentID"]: _guid("cpa", r["CustomPollerAssignmentID"])
           for r in D["Orion.NPM.CustomPollerAssignment"]}
    rep = {r["PolicyReportID"]: _guid("report", r["PolicyReportID"]) for r in D["Cirrus.PolicyReports"]}
    for r in D["Orion.NPM.CustomPollers"]:
        r["CustomPollerID"] = cp[r["CustomPollerID"]]
    for r in D["Orion.NPM.CustomPollerAssignment"]:
        r["CustomPollerID"] = cp[r["CustomPollerID"]]
        r["CustomPollerAssignmentID"] = asg[r["CustomPollerAssignmentID"]]
    for r in D["Orion.NPM.CustomPollerStatus"]:
        r["CustomPollerAssignmentID"] = asg[r["CustomPollerAssignmentID"]]
    for r in D["Cirrus.PolicyReports"]:
        r["PolicyReportID"] = rep[r["PolicyReportID"]]
    for e in ("Cirrus.PolicyCacheResults", "Cirrus.PolicyReportViolations"):
        for r in D[e]:
            r["ReportID"] = rep.get(r["ReportID"], r["ReportID"])


# ---------------------------------------------------------------------------------------------------------------
# Events, syslog / traps, alert history: 7 days
# ---------------------------------------------------------------------------------------------------------------
def build_events(D: dict, ctx: Ctx, days: int) -> None:
    now = ctx.now
    nodes = [n for n in ctx.nodes if n["Status"] != 9]
    events, eid = [], 0
    t = now - timedelta(days=days)
    planted = [(now - timedelta(minutes=420), ctx.byname["AGG-06"], 1, None),
               (now - timedelta(hours=2), ctx.byname["ACC-11"], 14, None)]
    k = 0
    while t < now - timedelta(minutes=2):
        k += 1
        n = nodes[_ri(0, len(nodes) - 1, "ev-node", k)]
        ifs = [i for i in ctx.if_by_node.get(n["NodeID"], []) if i["Type"] == 6]
        kind = _ri(0, 9, "ev-kind", k)
        if kind <= 5 and ifs:
            i = ifs[_ri(0, len(ifs) - 1, "ev-if", k)]
            for et, dt, word in ((10, 0, "Down"), (11, _ri(1, 9, "ev-up", k), "Up")):
                events.append((t + timedelta(minutes=dt), n, et, i, f"Interface {i['Caption']} is {word}"))
        elif kind <= 7:
            et = 5000 if kind == 6 else 5001
            events.append((t, n, et, None, f"Alert {'triggered' if et == 5000 else 'reset'}: High CPU on {n['Caption']}"))
        else:
            events.append((t, n, 14, None, f"Node {n['Caption']} rebooted"))
        t += timedelta(seconds=_ri(240, 900, "ev-gap", k))
    for when, n, et, _ in planted:
        events.append((when, n, et, None, f"Node {n['Caption']} is Down" if et == 1 else f"Node {n['Caption']} rebooted"))
    events.sort(key=lambda e: e[0])
    out = []
    for when, n, et, i, msg in events:
        if when >= now - timedelta(minutes=1):
            continue
        eid += 1
        out.append({"EventID": eid, "EventTime": _fmt(when), "NetworkNode": n["NodeID"],
                    "NetObjectID": i["InterfaceID"] if i else n["NodeID"], "NetObjectType": "I" if i else "N",
                    "EventType": et, "Message": msg, "Acknowledged": when < now - timedelta(days=1),
                    "_visible_at": _fmt(when)})
    D["Orion.Events"] = out

    log, lid = [], 0
    t = now - timedelta(days=days)
    while t < now - timedelta(minutes=1):
        lid += 1
        n = ctx.snmp[_ri(0, len(ctx.snmp) - 1, "log-node", lid)]
        vk = vendor_key(n) or "cisco"
        ifs = [i for i in ctx.if_by_node.get(n["NodeID"], []) if i["Type"] == 6] or [None]
        i = ifs[_ri(0, len(ifs) - 1, "log-if", lid)]
        peer = ctx.nodes[_ri(0, len(ctx.nodes) - 1, "log-peer", lid)]["IPAddress"]
        trap = _ri(0, 4, "log-trap", lid) == 0
        if trap:
            msg = TRAPS[_ri(0, len(TRAPS) - 1, "trap", lid)].format(idx=(i or {}).get("Index", 1), peer=peer)
            lvl = 4
        elif i and _ri(0, 2, "log-kind", lid) == 0:
            down = _ri(0, 1, "log-dir", lid) == 0
            msg = LINK_MSG[vk][0 if down else 1].format(ifn=i["Name"], idx=i["Index"])
            lvl = 3 if down else 5
        else:
            pool = OTHER_SYSLOG[vk]
            msg = pool[_ri(0, len(pool) - 1, "log-msg", lid)].format(peer=peer)
            lvl = 6 if "ALARM" not in msg and "FAN" not in msg else 4
        log.append({"LogEntryID": lid, "DateTime": _fmt(t), "MessageDateTime": _fmt(t - timedelta(seconds=2)),
                    "NodeID": n["NodeID"], "LogEntryTypeID": 2 if trap else 1, "LogEntryLevelID": lvl,
                    "Level": LEVELS[lvl], "LevelKey": LEVELS[lvl], "MessageSourceID": n["NodeID"], "Message": msg})
        t += timedelta(seconds=_ri(90, 420, "log-gap", lid))
    D["Orion.OLM.LogEntry"] = log

    # Alert history: trigger/reset pairs before the curated active alerts (IDs below theirs, older times).
    objs, hist = D["Orion.AlertObjects"], D["Orion.AlertHistory"]
    busy = [i for i in ctx.ifaces if i["NodeID"] in ctx.node and role(ctx.node[i["NodeID"]]) in ("P-CORE", "PE")
            and i["Type"] == 6][:6]
    new_objs = []
    for k, i in enumerate(busy):
        n = ctx.node[i["NodeID"]]
        new_objs.append({"AlertObjectID": 120 + k, "AlertID": 3, "EntityCaption": i["Caption"],
                         "EntityType": "Orion.NPM.Interfaces", "RelatedNodeId": n["NodeID"],
                         "RelatedNodeCaption": n["Caption"], "LastTriggeredDateTime": None})
    for k, n in enumerate([x for x in ctx.snmp if role(x) in ("PE", "BR")][:4]):
        new_objs.append({"AlertObjectID": 140 + k, "AlertID": 5, "EntityCaption": n["Caption"],
                         "EntityType": "Orion.Nodes", "RelatedNodeId": n["NodeID"], "RelatedNodeCaption": n["Caption"],
                         "LastTriggeredDateTime": None})
    older, hid, aid = [], 1000, 100
    t = now - timedelta(days=days)
    k = 0
    while t < now - timedelta(hours=4):
        k += 1
        o = new_objs[_ri(0, len(new_objs) - 1, "ah", k)]
        what = "utilization above 80%" if o["AlertID"] == 3 else "CPU load above 85%"
        aid += 1
        for et, dt in ((0, 0), (1, _ri(10, 90, "ah-reset", k))):
            hid += 1
            older.append({"AlertHistoryID": hid, "EventType": et, "Message": f"{o['EntityCaption']} {what}"
                          + (" (reset)" if et else ""), "TimeStamp": _fmt(t + timedelta(minutes=dt)), "AccountID": "",
                          "AlertActiveID": aid, "AlertObjectID": o["AlertObjectID"], "ActionID": 0})
        o["LastTriggeredDateTime"] = _fmt(t)
        t += timedelta(minutes=_ri(180, 600, "ah-gap", k))
    objs.extend(new_objs)
    D["Orion.AlertHistory"] = older + hist


# ---------------------------------------------------------------------------------------------------------------
# Metrics history and the sample model
# ---------------------------------------------------------------------------------------------------------------
PROFILE = [0.35, 0.28, 0.22, 0.18, 0.16, 0.18, 0.25, 0.38, 0.50, 0.58, 0.62, 0.64,
           0.66, 0.66, 0.65, 0.66, 0.70, 0.76, 0.84, 0.92, 0.98, 1.00, 0.86, 0.60]


def daily(t: datetime) -> float:
    """ISP traffic shape by server-local hour of day: lowest near 04:00, peak near 21:00."""
    h = t.hour + t.minute / 60
    a, b = PROFILE[int(h) % 24], PROFILE[(int(h) + 1) % 24]
    v = a + (b - a) * (h - int(h))
    return v * (1.05 if t.weekday() in (4, 5) else 1.0)          # a busier weekend (Friday/Saturday in Israel)


class Model:
    """Deterministic samples per (entity, key, time), shared by the history and by the mock's live sampling."""

    def __init__(self, ctx: Ctx, history_days: int, hourly_days: int):
        self.ctx, self.history_days, self.hourly_days = ctx, history_days, hourly_days
        now = ctx.now.replace(second=0)
        self.now = now
        self.down_at = {ctx.byname["AGG-06"]["NodeID"]: now - timedelta(minutes=420)}
        self.unmanaged_at = {ctx.byname["ACC-14"]["NodeID"]: now - timedelta(days=2)}
        self.reboot_at = {ctx.byname["ACC-11"]["NodeID"]: now - timedelta(hours=2)}
        links = set()
        for tc in ctx.D["Orion.TopologyConnections"]:
            for k in ("SrcInterfaceID", "DestInterfaceID"):
                if tc[k] in ctx.iface:
                    links.add(tc[k])
        self.ifaces = [i for i in ctx.ifaces if i["InterfaceID"] in links or i["Type"] == 161]
        hot = next((i for i in ctx.ifaces if i["Caption"] == "HundredGigE0/0/0/1 · P-CORE-02"), None)
        if hot and hot not in self.ifaces:
            self.ifaces.append(hot)
        self.hot = hot["InterfaceID"] if hot else None
        uplinks = [i for i in self.ifaces if role(ctx.node[i["NodeID"]]) == "AGG"]
        self.crc = uplinks[2]["InterfaceID"] if len(uplinks) > 2 else None
        self.nodes = [n for n in ctx.nodes]
        self.leak = ctx.byname["PE-03"]["NodeID"]
        self.drift = None                                  # the custom poller assignment whose Rx power drifts
        self.assign = {}
        for a in ctx.D["Orion.NPM.CustomPollerAssignment"]:
            cp = next((p for p in ctx.D["Orion.NPM.CustomPollers"] if p["CustomPollerID"] == a["CustomPollerID"]), {})
            self.assign[a["CustomPollerAssignmentID"]] = (a, cp)
            if self.drift is None and cp.get("UniqueName", "").startswith("jnxDomCurrentRx") and \
                    ctx.node[a["NodeID"]]["Caption"] == "PE-04":
                self.drift = a["CustomPollerAssignmentID"]

    # ----- which samples exist -----
    def polled(self, nid: int, t: datetime, snmp: bool = True) -> bool:
        n = self.ctx.node.get(nid)
        if not n:
            return False
        if nid in self.unmanaged_at and t >= self.unmanaged_at[nid]:
            return False
        if snmp and n["ObjectSubType"] != "SNMP":
            return False
        if snmp and nid in self.down_at and t >= self.down_at[nid]:
            return False
        rb = self.reboot_at.get(nid)
        if snmp and rb and rb - timedelta(minutes=5) <= t < rb + timedelta(minutes=25):
            return False
        return True

    def alive(self, entity: str, key: tuple, t: datetime) -> bool:
        if entity in ("Orion.NPM.InterfaceTraffic", "Orion.NPM.InterfaceErrors"):
            i = self.ctx.iface.get(key[0])
            return bool(i) and self.polled(i["NodeID"], t)
        if entity == "Orion.CPULoad":
            return self.polled(key[0], t)
        if entity == "Orion.ResponseTime":
            return self.polled(key[0], t, snmp=False)
        if entity == "Orion.NPM.CustomPollerStatistics":
            a = self.assign.get(key[0])
            return bool(a) and self.polled(a[0]["NodeID"], t)
        return True

    # ----- the values -----
    def sample(self, entity: str, key: tuple, t: datetime, archive: int = 0) -> dict:
        ts = _fmt(t)
        mi = int(t.timestamp() // 60)
        if entity == "Orion.CPULoad":
            nid = key[0]
            n = self.ctx.node[nid]
            base = {"P-CORE": 18, "PE": 28, "AGG": 22, "ACC": 12, "BR": 32}.get(role(n), 15)
            avg = base * (0.6 + 0.6 * daily(t)) + 6 * (_u("cpu", nid, mi) - 0.5)
            if nid == self.ctx.byname["PE-01"]["NodeID"] and 13 <= t.hour < 15 and t.weekday() == 2:
                avg = 88 + 6 * _u("spike", mi)                # a weekly maintenance job that pegs the CPU
            avg = int(max(1, min(99, round(avg))))
            mem_pct = 40 + 25 * _u("mem", nid)
            if nid == self.leak:                             # memory creeping up across the week
                age = (self.now - t).total_seconds() / 86400
                mem_pct = 78 - 3 * age
            mem_pct = round(max(5.0, min(97.0, mem_pct + 1.5 * (_u("memn", nid, mi) - 0.5))), 2)
            total = float({"P-CORE": 32, "PE": 64, "AGG": 16, "ACC": 4, "BR": 32}.get(role(n), 8) * 1024 ** 3)
            used = round(total * mem_pct / 100)
            return {"NodeID": nid, "DateTime": ts, "Archive": archive, "AvgLoad": avg,
                    "MinLoad": max(0, avg - _ri(1, 6, "cmin", nid, mi)), "MaxLoad": min(100, avg + _ri(2, 15, "cmax", nid, mi)),
                    "AvgPercentMemoryUsed": mem_pct, "PeakPercentMemoryUsed": round(min(99.0, mem_pct + 2.5), 2),
                    "TotalMemory": total, "AvgMemoryUsed": float(used), "MinMemoryUsed": float(round(used * 0.98)),
                    "MaxMemoryUsed": float(round(used * 1.02))}
        if entity == "Orion.ResponseTime":
            nid = key[0]
            n = self.ctx.node[nid]
            down = (nid in self.down_at and t >= self.down_at[nid]) or \
                   (nid in self.reboot_at and self.reboot_at[nid] <= t < self.reboot_at[nid] + timedelta(minutes=10))
            if down:
                return {"NodeID": nid, "DateTime": ts, "Archive": archive, "AvgResponseTime": None,
                        "MinResponseTime": None, "MaxResponseTime": None, "PercentLoss": 100, "Availability": 0.0,
                        "PercentDown": 100.0}
            base = 2 + (n["EngineID"] - 1) * 6 + 8 * _u("rtt", nid)
            avg = int(round(base * (0.9 + 0.3 * daily(t)) + 3 * _u("rttn", nid, mi)))
            loss = 0
            if _u("loss", nid, mi) > 0.985:
                loss = _ri(1, 12, "lossv", nid, mi)
            return {"NodeID": nid, "DateTime": ts, "Archive": archive, "AvgResponseTime": avg,
                    "MinResponseTime": max(1, avg - _ri(0, 3, "rmin", nid, mi)), "MaxResponseTime": avg + _ri(1, 25, "rmax", nid, mi),
                    "PercentLoss": loss, "Availability": 100.0, "PercentDown": 0.0}
        if entity == "Orion.NPM.InterfaceTraffic":
            iid = key[0]
            i = self.ctx.iface[iid]
            speed = float(i["Speed"])
            r_ = role(self.ctx.node[i["NodeID"]])
            peak = {"P-CORE": 0.55, "PE": 0.45, "AGG": 0.35, "ACC": 0.25, "BR": 0.6}.get(r_, 0.3)
            peak *= 0.6 + 0.8 * _u("peak", iid)
            if i["Type"] == 161:
                peak = 0.5
            if iid == self.hot:
                peak = 0.88
            step = 3600 if archive else 600
            if i["OperStatus"] != 1:
                inb = outb = 0.0
            else:
                u = peak * daily(t) * (0.95 + 0.1 * _u("tn", iid, mi))
                inb = round(speed * min(0.99, u), 1)
                outb = round(inb * (0.55 + 0.35 * _u("asym", iid)), 1)
            pin, pout = round(100 * inb / speed, 2), round(100 * outb / speed, 2)
            pk_in, pk_out = inb / 8 / 800, outb / 8 / 800
            return {"InterfaceID": iid, "NodeID": i["NodeID"], "DateTime": ts, "Archive": archive,
                    "InAveragebps": inb, "OutAveragebps": outb, "InMaxbps": round(inb * 1.12, 1),
                    "OutMaxbps": round(outb * 1.12, 1), "InTotalBytes": round(inb / 8 * step),
                    "OutTotalBytes": round(outb / 8 * step), "InTotalPkts": round(pk_in * step),
                    "OutTotalPkts": round(pk_out * step), "InPercentUtil": pin, "OutPercentUtil": pout}
        if entity == "Orion.NPM.InterfaceErrors":
            iid = key[0]
            i = self.ctx.iface[iid]
            crc = 0.0
            if iid == self.crc and t >= self.now - timedelta(days=2):
                crc = float(int(40 * _u("crc", mi) * (1 + (t - (self.now - timedelta(days=2))).total_seconds() / 86400)))
            ine = crc + (2.0 if _u("ie", iid, mi) > 0.97 else 0.0)
            disc = float(_ri(1, 40, "dsc", iid, mi)) if (iid == self.hot and daily(t) > 0.9) else \
                (5.0 if _u("dc", iid, mi) > 0.98 else 0.0)
            return {"InterfaceID": iid, "NodeID": i["NodeID"], "DateTime": ts, "Archive": archive,
                    "InErrors": ine, "OutErrors": 0.0, "InDiscards": 0.0, "OutDiscards": disc,
                    "CRCAlignErrors": crc, "Errors": ine, "Discards": disc}
        if entity == "Orion.NPM.CustomPollerStatistics":
            aid = key[0]
            a, cp = self.assign[aid]
            rx = cp.get("UniqueName", "").startswith("jnxDomCurrentRx")
            v = (-4.0 - 2 * _u("rx", aid)) if rx else (-1.0 + 2 * _u("tx", aid))
            if aid == self.drift:
                v -= 7 * (1 - (self.now - t).total_seconds() / (86400 * (self.history_days + self.hourly_days)))
            v = round(v + 0.15 * (_u("dom", aid, mi) - 0.5), 2)
            return {"CustomPollerAssignmentID": aid, "RowID": "0", "DateTime": ts, "Status": f"{v:.2f}",
                    "RawStatus": float(int(v * 100)), "Description": cp.get("Description"), "AvgRate": None,
                    "MinRate": None, "MaxRate": None, "Total": None}
        raise KeyError(entity)

    def keys(self, entity: str) -> list[tuple]:
        if entity in ("Orion.NPM.InterfaceTraffic", "Orion.NPM.InterfaceErrors"):
            return [(i["InterfaceID"],) for i in self.ifaces]
        if entity == "Orion.CPULoad":
            return [(n["NodeID"],) for n in self.ctx.snmp]
        if entity == "Orion.ResponseTime":
            return [(n["NodeID"],) for n in self.ctx.nodes]
        if entity == "Orion.NPM.CustomPollerStatistics":
            return [(k,) for k in self.assign]
        return []

    def history(self, entity: str) -> list[dict]:
        end = self.now.replace(minute=self.now.minute - self.now.minute % DETAIL_STEP)
        start = end - timedelta(days=self.history_days)
        detail = []
        t = start + timedelta(minutes=DETAIL_STEP)
        while t <= end:
            detail.append(t)
            t += timedelta(minutes=DETAIL_STEP)
        hourly = []
        t = start - timedelta(days=self.hourly_days)
        if t.minute:                                   # the first whole hour inside the hourly window
            t = t.replace(minute=0) + timedelta(hours=1)
        while t < start:
            hourly.append(t)
            t += timedelta(hours=1)
        rows = []
        for key in self.keys(entity):
            for times, arch in ((hourly, 1), (detail, 0)):
                for t in times:
                    if self.alive(entity, key, t):
                        rows.append(self.sample(entity, key, t, arch))
        rows.sort(key=lambda r: r["DateTime"])
        return rows


# ---------------------------------------------------------------------------------------------------------------
# Entry point (called at the end of fixtures.build when rich=True)
# ---------------------------------------------------------------------------------------------------------------
def extend(D: dict, now_utc: datetime, now: datetime, history_days: int = 7, hourly_days: int = 7) -> dict:
    """Turns the curated ISP into the rich one, in place. Returns what the mock needs: the sample model, the empty
    entities with their reasons, and the fields filled on curated entities."""
    remap_ids(D)
    fit_curated(D)
    ctx = Ctx(D, now_utc, now)
    build_events(D, ctx, history_days)
    curated = set(D)
    filled = fill_missing(D, ctx, skip=set(RICH_SERIES))
    empty = generate(D, ctx)
    model = Model(ctx, history_days, hourly_days)
    for entity in RICH_SERIES:
        D[entity] = model.history(entity)
    return {"model": model, "empty": empty, "filled": filled, "curated": curated}

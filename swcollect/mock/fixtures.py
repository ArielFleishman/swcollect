# Copied from swcheck swcheck/mock/fixtures.py at commit 0958074 (2026-11-18). Local changes: import paths only.
"""Deterministic sample ISP for the mock SWIS.

Planted findings (so every analysis has something to show):
  - duplicate IP 10.20.5.1 on ACC-03 and ACC-07
  - overlapping IPAM subnets 10.30.0.0/24 and 10.30.0.128/25
  - an interface IP outside every IPAM subnet (172.16.9.1 on BR-01)
  - AGG-06 stale (420 min since last sync); ACC-11 rebooted 2 hours ago; ACC-14 unmanaged
  - OOB-SW-01/02 ICMP only, no vendor
  - Huawei border routers: no hardware inventory, not in NCM
  - one BGP neighbour Idle; one LLDP neighbour not in Orion; one one-sided LLDP link
  - one OSPF neighbour (on ACC-02) whose address is on no Orion node
  - AGG-02 config backup 20 days old; config changes between versions on some nodes
  - NTA, UDT, MPLS, multicast, HSRP/VRRP and EoL entities are not installed
  - one topology connection whose destination interface is not in Orion.NPM.Interfaces (stage B: ref rule)
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone

TZ_OFFSET = timedelta(hours=2)  # server local time = UTC+2, like an Israeli Orion server in winter


def _fmt(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S")


ROLES = [
    # prefix, count, vendor, model, version, ifaces, subtype
    ("P-CORE", 4, "Cisco", "Cisco ASR 9006", "IOS XR 7.9.2", 16, "SNMP"),
    ("PE", 8, "Juniper Networks, Inc.", "Juniper MX960", "JUNOS 22.4R3", 12, "SNMP"),
    ("AGG", 10, "Nokia", "Nokia 7750 SR-7", "TiMOS-C-23.10.R2", 10, "SNMP"),
    ("ACC", 14, "Cisco", "Cisco ASR-920-24SZ-M", "IOS XE 17.9.4", 6, "SNMP"),
    ("BR", 2, "Huawei Technologies Co.,Ltd", "Huawei NE40E-X8A", "VRP V800R021C10", 8, "SNMP"),
    ("OOB-SW", 2, "", "", "", 0, "ICMP"),
]


def lo(nid: int) -> str:
    """Loopback address for a node; valid for up to 65k nodes."""
    return f"10.255.{nid // 256}.{nid % 256}"


def build(now_utc: datetime | None = None, scale: int = 1) -> dict:
    """scale multiplies the number of routers (scale=50 gives about 2,000 nodes)."""
    rnd = random.Random(42)
    now_utc = (now_utc or datetime.now(timezone.utc).replace(tzinfo=None)).replace(microsecond=0)
    now = now_utc + TZ_OFFSET  # server-local
    D: dict[str, list[dict]] = {}

    # ---------------- platform ----------------
    D["Orion.InstalledModule"] = [
        {"Name": "Orion Platform", "Version": "2024.2.1", "IsEval": False, "LicenseName": "SL"},
        {"Name": "NPM", "Version": "2024.2.1", "IsEval": False, "LicenseName": "SL"},
        {"Name": "NCM", "Version": "2024.2.1", "IsEval": False, "LicenseName": "SL"},
        {"Name": "IPAM", "Version": "2024.2", "IsEval": False, "LicenseName": "SL"},
    ]
    D["Orion.Engines"] = [
        {"EngineID": 1, "ServerName": "ORION-MPE", "IP": "10.250.0.10", "ServerType": "Primary",
         "KeepAlive": _fmt(now - timedelta(seconds=40)), "Elements": 640, "Nodes": 40, "Interfaces": 392, "PollingCompletion": 99.8},
        {"EngineID": 2, "ServerName": "ORION-APE-01", "IP": "10.250.0.11", "ServerType": "Additional",
         "KeepAlive": _fmt(now - timedelta(seconds=55)), "Elements": 210, "Nodes": 14, "Interfaces": 84, "PollingCompletion": 99.5},
    ]
    D["Orion.Accounts"] = [
        {"AccountID": "admin", "Enabled": "Y", "AllowAdmin": "Y", "AllowNodeManagement": "Y", "AllowUnmanage": "Y",
         "AllowCustomize": "Y", "AllowAlertManagement": "Y", "LimitationID1": 0, "LimitationID2": 0, "LimitationID3": 0,
         "AccountType": 1, "LastLogin": _fmt(now - timedelta(days=1))},
        {"AccountID": "readonly", "Enabled": "Y", "AllowAdmin": "N", "AllowNodeManagement": "N", "AllowUnmanage": "N",
         "AllowCustomize": "N", "AllowAlertManagement": "N", "LimitationID1": 12, "LimitationID2": 0, "LimitationID3": 0,
         "AccountType": 1, "LastLogin": _fmt(now)},
    ]
    D["Orion.Settings"] = [
        {"SettingID": "SWNetPerfMon-Settings-Retain Detail", "Name": "Detailed statistics retention", "CurrentValue": 7, "Units": "days"},
        {"SettingID": "SWNetPerfMon-Settings-Retain Hourly", "Name": "Hourly statistics retention", "CurrentValue": 30, "Units": "days"},
        {"SettingID": "SWNetPerfMon-Settings-Retain Daily", "Name": "Daily statistics retention", "CurrentValue": 365, "Units": "days"},
        {"SettingID": "SWNetPerfMon-Settings-Default Node Poll Interval", "Name": "Node poll interval", "CurrentValue": 120, "Units": "seconds"},
    ]

    # ---------------- nodes ----------------
    nodes, names = [], {}
    nid = 0
    for prefix, count, vendor, model, ver, nif, sub in ROLES:
        if sub == "SNMP":
            count *= scale
        for i in range(1, count + 1):
            nid += 1
            name = f"{prefix}-{i:02d}"
            names[nid] = name
            up_hours = rnd.uniform(400, 9000)
            mins = rnd.choice([0, 1, 1, 2, 2, 3])
            n = {
                "NodeID": nid, "Caption": name, "IPAddress": lo(nid), "DNS": f"{name.lower()}.isp.local",
                "SysName": name if sub == "SNMP" else "", "Vendor": vendor, "MachineType": model,
                "IOSVersion": ver, "IOSImage": "", "SysObjectID": "" if sub == "ICMP" else f"1.3.6.1.4.1.{rnd.randint(9, 50000)}",
                "Description": "", "Location": rnd.choice(["Tel Aviv DC1", "Haifa POP", "Jerusalem POP", "Beer Sheva POP"]),
                "Contact": "noc@isp.local", "Status": 1, "StatusDescription": "Node status is Up.",
                "ObjectSubType": sub, "EngineID": 2 if prefix == "ACC" else 1, "UnManaged": False, "Category": 1,
                "Community": "mock-secret-ro", "RWCommunity": "mock-secret-rw",
                "LastBoot": _fmt(now - timedelta(hours=up_hours)), "SystemUpTime": round(up_hours * 3600),
                "LastSync": _fmt(now - timedelta(minutes=mins, seconds=rnd.randint(0, 59))), "MinutesSinceLastSync": mins,
                "NextPoll": _fmt(now + timedelta(seconds=rnd.randint(10, 120))), "PollInterval": 120, "StatCollection": 10,
                "LastSystemUpTimePollUtc": _fmt(now_utc - timedelta(minutes=mins)),
                "CPULoad": rnd.randint(3, 60), "PercentMemoryUsed": rnd.randint(20, 80),
                "ResponseTime": rnd.randint(1, 30), "PercentLoss": 0,
            }
            if sub == "ICMP":
                n.update(SystemUpTime=0, LastBoot="1899-12-30T00:00:00")
            nodes.append(n)
    byname = {n["Caption"]: n for n in nodes}
    byname["AGG-06"].update(MinutesSinceLastSync=420, LastSync=_fmt(now - timedelta(minutes=420)),
                            Status=2, StatusDescription="Node status is Down.")
    byname["ACC-11"].update(SystemUpTime=7200, LastBoot=_fmt(now - timedelta(hours=2)))
    byname["ACC-14"].update(UnManaged=True, Status=9, StatusDescription="Node is Unmanaged.")
    D["Orion.Nodes"] = nodes
    D["Orion.NodesCustomProperties"] = [
        {"NodeID": n["NodeID"], "Network": "ISP-PILOT", "Site": n["Location"], "Role": n["Caption"].split("-")[0],
         "PlanEId": f"PE-{n['NodeID']:05d}" if n["NodeID"] % 9 else ""} for n in nodes]

    # ---------------- interfaces ----------------
    ifaces, ifid = [], 0
    if_by_node: dict[int, list[dict]] = {}
    for n in nodes:
        prefix = n["Caption"].rsplit("-", 1)[0]
        role = next(r for r in ROLES if r[0] == prefix)
        lst = []
        for k in range(role[5]):
            ifid += 1
            if n["Vendor"].startswith("Cisco"):
                nm = f"HundredGigE0/0/0/{k}" if prefix == "P-CORE" else f"TenGigabitEthernet0/0/{k}"
            elif n["Vendor"].startswith("Juniper"):
                nm = f"et-0/0/{k}"
            elif n["Vendor"] == "Nokia":
                nm = f"1/1/c{k + 1}/1"
            else:
                nm = f"GigabitEthernet0/{k}/0"
            speed = 100e9 if prefix == "P-CORE" else 10e9
            oper = 1 if k < role[5] - 2 else rnd.choice([1, 2])
            row = {"InterfaceID": ifid, "NodeID": n["NodeID"], "Name": nm, "Caption": f"{nm} · {n['Caption']}",
                   "IfName": nm, "Index": k + 1, "Description": nm, "Alias": "" if k == role[5] - 1 else f"to-link-{k}",
                   "Type": 6, "TypeName": "ethernetCsmacd", "TypeDescription": "Ethernet", "Speed": speed, "MTU": 9216,
                   "PhysicalAddress": f"00:1A:{n['NodeID']:02X}:{k:02X}:00:01", "AdminStatus": 1, "OperStatus": oper,
                   "Status": oper, "InBandwidth": speed, "OutBandwidth": speed,
                   "LastChange": _fmt(now - timedelta(days=rnd.randint(1, 90))), "LastSync": n["LastSync"], "UnManaged": False,
                   "FullName": f"{n['Caption']} - {nm}"}
            lst.append(row)
        if prefix in ("P-CORE", "PE"):
            ifid += 1
            lag = "Bundle-Ether1" if prefix == "P-CORE" else "ae0"
            lst.append({"InterfaceID": ifid, "NodeID": n["NodeID"], "Name": lag, "Caption": f"{lag} · {n['Caption']}", "IfName": lag,
                        "Index": 100, "Description": lag, "Alias": "LAG to peer", "Type": 161, "TypeName": "ieee8023adLag", "TypeDescription": "LAG",
                        "Speed": 200e9, "MTU": 9216, "PhysicalAddress": "", "AdminStatus": 1, "OperStatus": 1, "Status": 1,
                        "InBandwidth": 200e9, "OutBandwidth": 200e9, "LastChange": _fmt(now - timedelta(days=30)),
                        "LastSync": n["LastSync"], "UnManaged": False, "FullName": f"{n['Caption']} - {lag}"})
        if_by_node[n["NodeID"]] = lst
        ifaces.extend(lst)
    D["Orion.NPM.Interfaces"] = ifaces

    # ---------------- topology ----------------
    ids = {p: [n["NodeID"] for n in nodes if n["Caption"].startswith(p + "-")] for p, *_ in ROLES}
    # One "region" = 4 P, 8 PE, 10 AGG, 14 ACC, 2 BR; scale > 1 adds regions joined by a core ring.
    links = []
    core, pe_ids, agg_ids = ids["P-CORE"], ids["PE"], ids["AGG"]
    for r in range(0, len(core), 4):
        grp = core[r:r + 4]
        for i, a in enumerate(grp):
            for b in grp[i + 1:]:
                links.append((a, b))
        if r:
            links.append((core[r - 4], core[r]))
    for k, pe in enumerate(pe_ids):
        b = (k // 8) * 4
        links += [(pe, core[b + k % 4]), (pe, core[b + (k + 1) % 4])]
    for k, ag in enumerate(agg_ids):
        b = (k // 10) * 8
        links += [(ag, pe_ids[b + k % 8]), (ag, pe_ids[b + (k + 3) % 8])]
    for k, ac in enumerate(ids["ACC"]):
        b = (k // 14) * 10
        links += [(ac, agg_ids[b + k % 10]), (ac, agg_ids[b + (k + 1) % 10])]
    for j, br in enumerate(ids["BR"]):
        b = ((j // 2) * 4) % len(core)
        links += [(br, core[b]), (br, core[b + 1])]
    port_ptr = {n["NodeID"]: 0 for n in nodes}

    def next_if(node_id):
        lst = [x for x in if_by_node[node_id] if x["Type"] == 6]
        if not lst:
            return None
        x = lst[port_ptr[node_id] % len(lst)]
        port_ptr[node_id] += 1
        return x

    topo, lldp, cdp, nips, p2p = [], [], [], [], 0
    link_ips: dict[tuple, tuple] = {}   # (a, b) -> ((a ifIndex, a IP), (b ifIndex, b IP))
    for a, b in links:
        ia, ib = next_if(a), next_if(b)
        topo.append({"SrcNodeID": a, "SrcInterfaceID": ia["InterfaceID"], "DestNodeID": b, "DestInterfaceID": ib["InterfaceID"],
                     "SrcType": "Orion.Nodes", "DestType": "Orion.Nodes", "LayerType": "L2", "DataSourceNodeID": a,
                     "LastUpdateUtc": _fmt(now_utc - timedelta(minutes=rnd.randint(1, 50)))})
        topo.append({"SrcNodeID": b, "SrcInterfaceID": ib["InterfaceID"], "DestNodeID": a, "DestInterfaceID": ia["InterfaceID"],
                     "SrcType": "Orion.Nodes", "DestType": "Orion.Nodes", "LayerType": "L2", "DataSourceNodeID": b,
                     "LastUpdateUtc": _fmt(now_utc - timedelta(minutes=rnd.randint(1, 50)))})
        for x, ix, y, iy in ((a, ia, b, ib), (b, ib, a, ia)):
            entry = {"NodeID": x, "LocalPortNumber": ix["Index"], "RemoteChassisId": f"00:1A:{y:02X}:00:00:00",
                     "RemotePortId": iy["Name"], "RemotePortDescription": iy["Alias"], "RemoteSystemName": names[y],
                     "RemoteIpAddress": lo(y)}
            lldp.append(entry)
            if byname[names[x]]["Vendor"] == "Cisco" and byname[names[y]]["Vendor"] == "Cisco":
                cdp.append({"NodeID": x, "IfIndex": ix["Index"], "DeviceId": f"{names[y]}.isp.local", "DevicePort": iy["Name"],
                            "IpAddress": lo(y), "Platform": byname[names[y]]["MachineType"]})
        p2p += 1
        base = f"10.1.{p2p // 128}.{(p2p % 128) * 2}"
        a_ip = base
        b_ip = f"10.1.{p2p // 128}.{(p2p % 128) * 2 + 1}"
        nips.append({"NodeID": a, "IPAddress": a_ip, "IPAddressType": "IPv4", "InterfaceIndex": ia["Index"], "SubnetMask": "255.255.255.254"})
        nips.append({"NodeID": b, "IPAddress": b_ip, "IPAddressType": "IPv4", "InterfaceIndex": ib["Index"], "SubnetMask": "255.255.255.254"})
        link_ips[(a, b)] = ((ia["Index"], a_ip), (ib["Index"], b_ip))
    # one-sided: drop AGG-09's view of its first neighbour
    agg9 = byname["AGG-09"]["NodeID"]
    first = next(i for i, e in enumerate(lldp) if e["NodeID"] == agg9)
    lldp.pop(first)
    # neighbour not in Orion
    lldp.append({"NodeID": byname["ACC-05"]["NodeID"], "LocalPortNumber": 5, "RemoteChassisId": "AA:BB:CC:00:11:22",
                 "RemotePortId": "GigabitEthernet0/1", "RemotePortDescription": "uplink", "RemoteSystemName": "CUST-CPE-77",
                 "RemoteIpAddress": "192.168.77.1"})
    # a connection to an interface Orion does not hold (stage B contract rule "ref"); same node pair, so the map is unchanged
    topo.append(dict(topo[0], DestInterfaceID=99999))
    D["Orion.TopologyConnections"] = topo
    D["Orion.NodeLldpEntry"] = lldp
    D["Orion.NodeCdpEntry"] = cdp
    for n in nodes:
        nips.append({"NodeID": n["NodeID"], "IPAddress": n["IPAddress"], "IPAddressType": "IPv4", "InterfaceIndex": 0, "SubnetMask": "255.255.255.255"})
    for name in ("ACC-03", "ACC-07"):
        nips.append({"NodeID": byname[name]["NodeID"], "IPAddress": "10.20.5.1", "IPAddressType": "IPv4", "InterfaceIndex": 3, "SubnetMask": "255.255.255.0"})
    nips.append({"NodeID": byname["BR-01"]["NodeID"], "IPAddress": "172.16.9.1", "IPAddressType": "IPv4", "InterfaceIndex": 4, "SubnetMask": "255.255.255.252"})
    D["Orion.NodeIPAddresses"] = nips
    D["Orion.NodeVlans"] = [{"NodeID": n, "VlanId": v, "VlanName": f"VLAN{v}"} for n in ids["ACC"] for v in (100, 200, 300)]

    # NCM keys devices by its own GUID; CoreNodeID is the Orion NodeID.
    ncm_guid = {n["NodeID"]: str(uuid.UUID(int=rnd.getrandbits(128))) for n in nodes
                if n["ObjectSubType"] == "SNMP" and not n["Vendor"].startswith("Huawei")}

    # ---------------- hardware ----------------
    phys, hw_items, hw_info = [], [], []
    pid = 0
    for n in nodes:
        if n["ObjectSubType"] != "SNMP" or n["Vendor"].startswith("Huawei"):
            continue
        parts = [("chassis", n["MachineType"]), ("module", "Route Processor 0"), ("module", "Line Card 0"),
                 ("powerSupply", "Power Supply 0"), ("powerSupply", "Power Supply 1"), ("fan", "Fan Tray 0")]
        for cls, nm in parts:
            pid += 1
            serial = "" if (n["Vendor"] == "Nokia" and cls == "chassis" and n["NodeID"] % 3 == 0) else f"SN{rnd.randint(10**7, 10**8)}"
            phys.append({"EntityID": pid, "NodeID": ncm_guid[n["NodeID"]], "EntPhysicalIndex": pid, "EntityClass": cls,
                         "EntityName": nm, "EntityDescription": f"{n['MachineType']} {nm}", "Serial": serial,
                         "Model": n["MachineType"] if cls == "chassis" else f"{cls.upper()}-{rnd.randint(100, 999)}",
                         "HardwareRevision": "V01", "FirmwareRevision": "", "SoftwareRevision": n["IOSVersion"],
                         "Manufacturer": n["Vendor"]})
        if n["Vendor"] in ("Cisco", "Juniper Networks, Inc."):
            hw_info.append({"NodeID": n["NodeID"], "Manufacturer": n["Vendor"], "Model": n["MachineType"],
                            "ServiceTag": "", "IsDisabled": False, "StatusDescription": "Up", "LastPollTime": n["LastSync"]})
            for nm, val, unit in (("PSU 0", 1, ""), ("PSU 1", 1, ""), ("Fan 0", 6200, "RPM"), ("Inlet Temp", 31, "C")):
                hw_items.append({"NodeID": n["NodeID"], "Name": nm, "Status": 1, "HardwareCategoryStatusID": 1,
                                 "Value": val, "Unit": unit, "IsDisabled": False, "UniqueName": f"{n['Caption']}-{nm}",
                                 "IsDeleted": False})
    D["NCM.EntityPhysical"] = phys
    D["Orion.NPM.InterfacesRelationship"] = [
        {"NodeID": n["NodeID"], "ParentInterfaceIndex": 100, "ChildInterfaceIndex": c, "RelationshipTypeID": 1,
         "LACPOperState": 61, "LACPAdminState": 61}
        for n in nodes if n["Caption"].startswith(("P-CORE", "PE")) for c in (1, 2)]
    D["Orion.HardwareHealth.HardwareInfo"] = hw_info
    D["Orion.HardwareHealth.HardwareItem"] = hw_items

    # ---------------- routing ----------------
    D["Orion.Routing.RoutingProtocol"] = [{"ProtocolID": 13, "DisplayName": "OSPF"}, {"ProtocolID": 14, "DisplayName": "BGP"},
                                          {"ProtocolID": 9, "DisplayName": "IS-IS"}]
    D["Orion.Routing.RoutingProtocolStateMapping"] = [
        {"ProtocolID": 13, "ProtocolStatus": 8, "DisplayName": "Full"}, {"ProtocolID": 13, "ProtocolStatus": 1, "DisplayName": "Down"},
        {"ProtocolID": 14, "ProtocolStatus": 6, "DisplayName": "Established"}, {"ProtocolID": 14, "ProtocolStatus": 1, "DisplayName": "Idle"},
        {"ProtocolID": 9, "ProtocolStatus": 3, "DisplayName": "Up"}, {"ProtocolID": 9, "ProtocolStatus": 1, "DisplayName": "Down"}]
    nbrs, nbid = [], 0
    for a, b in links:
        pa, pb = names[a].split("-")[0], names[b].split("-")[0]
        if {pa, pb} <= {"P", "PE"} or "P" in (pa, pb):
            proto, st = 9, 3
        elif "AGG" in (pa, pb):
            proto, st = 13, 8
        else:
            continue
        (ia, a_ip), (ib, b_ip) = link_ips[(a, b)]
        for x, y, (lidx, lip), (_, rip) in ((a, b, (ia, a_ip), (ib, b_ip)), (b, a, (ib, b_ip), (ia, a_ip))):
            nbid += 1
            row = {"NeighborID": nbid, "NodeID": x, "NeighborIP": lo(y), "ProtocolID": proto, "ProtocolStatus": st,
                   "AutonomousSystem": 0, "LastChange": _fmt(now - timedelta(days=rnd.randint(2, 120))), "IsDeleted": False,
                   "LocalProtocolInterfaceIndex": 0, "LocalProtocolInterfaceIp": ""}
            if proto == 13:   # OSPF neighbours are seen on the link address, with the local port and IP
                row.update(NeighborIP=rip, LocalProtocolInterfaceIndex=lidx, LocalProtocolInterfaceIp=lip)
            nbrs.append(row)
    # OSPF neighbour whose address is not on any Orion node (a CPE that is not monitored)
    acc2 = byname["ACC-02"]["NodeID"]
    nbid += 1
    nbrs.append({"NeighborID": nbid, "NodeID": acc2, "NeighborIP": "192.168.40.2", "ProtocolID": 13, "ProtocolStatus": 8,
                 "AutonomousSystem": 0, "LastChange": _fmt(now - timedelta(days=9)), "IsDeleted": False,
                 "LocalProtocolInterfaceIndex": 4, "LocalProtocolInterfaceIp": "192.168.40.1"})
    for k, pe in enumerate(ids["PE"]):
        for rr in ids["P-CORE"][:2]:
            nbid += 1
            st = 1 if (k == 5 and rr == ids["P-CORE"][1]) else 6
            nbrs.append({"NeighborID": nbid, "NodeID": pe, "NeighborIP": lo(rr), "ProtocolID": 14, "ProtocolStatus": st,
                         "AutonomousSystem": 65000, "LastChange": _fmt(now - timedelta(hours=3 if st == 1 else rnd.randint(48, 900))),
                         "IsDeleted": False})
    D["Orion.Routing.Neighbors"] = nbrs
    routes = []
    for n in nodes[:30]:
        for r in range(40):
            routes.append({"NodeID": n["NodeID"], "RouteDestination": f"10.{r % 40}.{r}.0", "RouteMaskLen": 24,
                           "RouteNextHop": lo((n['NodeID'] % 4) + 1), "ProtocolID": rnd.choice([9, 13, 14]),
                           "Metric": rnd.randint(1, 50), "InterfaceIndex": 1})
    D["Orion.Routing.RoutingTable"] = routes
    D["Orion.Routing.RoutingTableFlap"] = [
        {"FlapId": i + 1, "NodeId": ids["PE"][5 % len(ids["PE"])], "RouteDestination": f"10.{i}.0.0", "CIDR": 16,
         "DateTime": _fmt(now - timedelta(hours=3, minutes=i)), "RouteNextHop": lo(ids["P-CORE"][1]), "VrfIndex": 0}
        for i in range(6)]
    D["Orion.Routing.VRF"] = [{"NodeID": pe, "Name": vrf, "VrfIndex": j + 1, "RouteDistinguisher": f"65000:{100 + j}", "Status": 1,
                               "Description": f"Customer {vrf}"} for pe in ids["PE"] for j, vrf in enumerate(("INTERNET", "CUST-A", "CUST-B"))]

    # ---------------- IPAM ----------------
    subnets = [
        {"SubnetId": 1, "Address": "10.0.0.0", "CIDR": 8, "FriendlyName": "ISP 10/8", "ParentId": 0, "GroupTypeText": "Supernet"},
        {"SubnetId": 2, "Address": "10.255.0.0", "CIDR": 16, "FriendlyName": "Loopbacks", "ParentId": 1, "GroupTypeText": "Subnet"},
        {"SubnetId": 3, "Address": "10.1.0.0", "CIDR": 16, "FriendlyName": "P2P links", "ParentId": 1, "GroupTypeText": "Subnet"},
        {"SubnetId": 4, "Address": "10.20.0.0", "CIDR": 16, "FriendlyName": "Access mgmt", "ParentId": 1, "GroupTypeText": "Subnet"},
        {"SubnetId": 5, "Address": "10.30.0.0", "CIDR": 24, "FriendlyName": "Services A", "ParentId": 1, "GroupTypeText": "Subnet"},
        {"SubnetId": 6, "Address": "10.30.0.128", "CIDR": 25, "FriendlyName": "Services B (overlaps A)", "ParentId": 1, "GroupTypeText": "Subnet"},
        {"SubnetId": 7, "Address": "10.250.0.0", "CIDR": 24, "FriendlyName": "Orion servers", "ParentId": 1, "GroupTypeText": "Subnet"},
    ]
    for s in subnets:
        s.update({"VLAN": "", "Location": "", "UsedCount": 0, "AvailableCount": 0, "PercentUsed": 0.0,
                  "LastDiscovery": _fmt(now - timedelta(hours=6)), "Status": 1})
    ipn = []
    sid = {"10.255": 2, "10.1": 3, "10.20": 4}
    for k, r in enumerate(nips):
        ip = r["IPAddress"]
        s = next((v for p, v in sid.items() if ip.startswith(p + ".")), None)
        if s is None:
            continue
        ipn.append({"IpNodeId": k + 1, "SubnetId": s, "IPAddress": ip, "Status": 1,
                    "MAC": "", "DnsBackward": names.get(r["NodeID"], "").lower(), "SysName": names.get(r["NodeID"], ""),
                    "Vendor": "", "LastSync": _fmt(now - timedelta(hours=6)), "Comments": ""})
    for s in subnets:
        s["UsedCount"] = sum(1 for x in ipn if x["SubnetId"] == s["SubnetId"])
    D["IPAM.Subnet"] = subnets
    D["IPAM.IPNode"] = ipn
    D["IPAM.Conflict"] = [{"ConflictId": 1, "IPAddress": "10.20.5.1", "AssignedMac": "00:1A:0D:03:00:01",
                           "ConflictingMac": "00:1A:0D:07:00:01", "ConflictTypeText": "IP address conflict",
                             "ConflictTimeUTC": _fmt(now_utc - timedelta(hours=5))}]

    # ---------------- custom pollers (optical DOM) ----------------
    D["Orion.NPM.CustomPollers"] = [
        {"CustomPollerID": "a1", "UniqueName": "jnxDomCurrentRxLaserPower", "OID": "1.3.6.1.4.1.2636.3.60.1.1.1.1.5",
         "MIB": "JUNIPER-DOM-MIB", "Description": "Rx optical power (0.01 dBm)", "SNMPGetType": "GetNext", "Unit": "dBm"},
        {"CustomPollerID": "a2", "UniqueName": "jnxDomCurrentTxLaserOutputPower", "OID": "1.3.6.1.4.1.2636.3.60.1.1.1.1.7",
         "MIB": "JUNIPER-DOM-MIB", "Description": "Tx optical power (0.01 dBm)", "SNMPGetType": "GetNext", "Unit": "dBm"},
        {"CustomPollerID": "b1", "UniqueName": "ifHCInMulticastPkts", "OID": "1.3.6.1.2.1.31.1.1.1.8", "MIB": "IF-MIB",
         "Description": "Multicast in", "SNMPGetType": "GetNext", "Unit": ""},
    ]
    asg, vals, aid = [], [], 0
    for pe in ids["PE"][:6]:
        for cp in ("a1", "a2"):
            aid += 1
            asg.append({"CustomPollerAssignmentID": f"as{aid}", "CustomPollerID": cp, "NodeID": pe, "InterfaceID": None,
                        "AssignmentName": f"{cp} on {names[pe]}"})
            for row in range(4):
                v = rnd.uniform(-9, -2) if cp == "a1" else rnd.uniform(-2, 2)
                vals.append({"CustomPollerAssignmentID": f"as{aid}", "RowID": f"{row}", "Status": f"{v:.2f}",
                             "RawStatus": f"{int(v * 100)}", "DateTime": _fmt(now - timedelta(minutes=rnd.randint(1, 9)))})
    D["Orion.NPM.CustomPollerAssignment"] = asg
    D["Orion.NPM.CustomPollerStatus"] = vals

    # ---------------- alerts / events / syslog / traps ----------------
    D["Orion.AlertConfigurations"] = [
        {"AlertID": 1, "Name": "Node is down", "Severity": 2, "Enabled": True, "ObjectType": "Node"},
        {"AlertID": 2, "Name": "BGP neighbor not established", "Severity": 2, "Enabled": True, "ObjectType": "Routing Neighbor"},
        {"AlertID": 3, "Name": "Interface utilization > 80%", "Severity": 1, "Enabled": True, "ObjectType": "Interface"},
        {"AlertID": 4, "Name": "Node rebooted", "Severity": 1, "Enabled": True, "ObjectType": "Node"},
        {"AlertID": 5, "Name": "High CPU", "Severity": 1, "Enabled": True, "ObjectType": "Node"},
    ]
    D["Orion.AlertObjects"] = [
        {"AlertObjectID": 101, "AlertID": 1, "EntityCaption": "AGG-06", "EntityType": "Orion.Nodes",
         "RelatedNodeId": byname["AGG-06"]["NodeID"], "RelatedNodeCaption": "AGG-06", "LastTriggeredDateTime": _fmt(now - timedelta(minutes=410))},
        {"AlertObjectID": 102, "AlertID": 2, "EntityCaption": lo(ids['P-CORE'][1]), "EntityType": "Orion.Routing.Neighbors",
         "RelatedNodeId": ids["PE"][5], "RelatedNodeCaption": names[ids["PE"][5]], "LastTriggeredDateTime": _fmt(now - timedelta(hours=3))},
        {"AlertObjectID": 103, "AlertID": 4, "EntityCaption": "ACC-11", "EntityType": "Orion.Nodes",
         "RelatedNodeId": byname["ACC-11"]["NodeID"], "RelatedNodeCaption": "ACC-11", "LastTriggeredDateTime": _fmt(now - timedelta(hours=2))},
        {"AlertObjectID": 104, "AlertID": 3, "EntityCaption": "HundredGigE0/0/0/1 · P-CORE-02", "EntityType": "Orion.NPM.Interfaces",
         "RelatedNodeId": ids["P-CORE"][1], "RelatedNodeCaption": "P-CORE-02", "LastTriggeredDateTime": _fmt(now - timedelta(minutes=25))},
    ]
    D["Orion.AlertActive"] = [
        {"AlertActiveID": 1, "AlertObjectID": 101, "TriggeredDateTime": _fmt(now - timedelta(minutes=410)),
         "TriggeredMessage": "AGG-06 is down", "Acknowledged": False, "AcknowledgedBy": ""},
        {"AlertActiveID": 2, "AlertObjectID": 102, "TriggeredDateTime": _fmt(now - timedelta(hours=3)),
         "TriggeredMessage": f"BGP neighbor on {names[ids['PE'][5]]} is Idle", "Acknowledged": True, "AcknowledgedBy": "noc1"},
        {"AlertActiveID": 3, "AlertObjectID": 103, "TriggeredDateTime": _fmt(now - timedelta(hours=2)),
         "TriggeredMessage": "ACC-11 rebooted", "Acknowledged": False, "AcknowledgedBy": ""},
        {"AlertActiveID": 4, "AlertObjectID": 104, "TriggeredDateTime": _fmt(now - timedelta(minutes=25)),
         "TriggeredMessage": "P-CORE-02 Hu0/0/0/1 utilization 86%", "Acknowledged": False, "AcknowledgedBy": ""},
    ]
    hist, hid = [], 9000
    for a_ in D["Orion.AlertActive"]:
        hid += 1
        hist.append({"AlertHistoryID": hid, "EventType": 0, "Message": a_["TriggeredMessage"],
                     "TimeStamp": a_["TriggeredDateTime"], "AccountID": "", "AlertActiveID": a_["AlertActiveID"],
                     "AlertObjectID": a_["AlertObjectID"], "ActionID": 0})
    D["Orion.AlertHistory"] = sorted(hist, key=lambda x: x["AlertHistoryID"])
    D["Orion.AlertSuppression"] = [{"ID": 1, "EntityUri": f"swis://{MOCK_HOST}/Orion/Orion.Nodes/NodeID={byname['ACC-11']['NodeID']}",
                                    "SuppressFrom": _fmt(now_utc - timedelta(hours=1)), "SuppressUntil": _fmt(now_utc + timedelta(hours=3))}]
    D["Orion.Pollers"] = [{"PollerID": i + 1, "PollerType": "N.Status.ICMP.Native" if n["ObjectSubType"] == "ICMP" else "N.Status.SNMP.Native",
                           "NetObject": f"N:{n['NodeID']}", "NetObjectType": "N", "NetObjectID": n["NodeID"], "Enabled": True}
                          for i, n in enumerate(nodes)]
    D["Orion.EventTypes"] = [{"EventType": t, "Name": nm} for t, nm in
                             ((1, "Node Down"), (5, "Node Up"), (10, "Interface Down"), (11, "Interface Up"),
                              (14, "Node Rebooted"), (5000, "Alert Triggered"), (5001, "Alert Reset"))]
    events = []
    t = now - timedelta(hours=24)
    eid = 0
    while t < now - timedelta(minutes=2):
        eid += 1
        nn = rnd.choice(nodes)
        et = rnd.choice([10, 11, 10, 11, 5000, 5001, 14])
        events.append({"EventID": eid, "EventTime": _fmt(t), "NetworkNode": nn["NodeID"], "NetObjectID": nn["NodeID"],
                       "NetObjectType": "N", "EventType": et, "Message": f"{nn['Caption']}: event type {et}",
                       "Acknowledged": False, "_visible_at": _fmt(t)})
        t += timedelta(seconds=rnd.randint(120, 600))
    D["Orion.Events"] = events
    # Syslog and traps live in Orion.OLM.LogEntry (Orion.SysLog / Orion.Traps are gone since 2023.1).
    D["Orion.OLM.LogEntryType"] = [{"LogEntryTypeID": 1, "Type": "Syslog"}, {"LogEntryTypeID": 2, "Type": "Trap"}]
    log = []
    for i in range(250):
        trap = i % 5 == 0
        nd = rnd.choice(nodes)
        ts = now - timedelta(minutes=i * 6)
        log.append({"LogEntryID": i + 1, "DateTime": _fmt(ts), "MessageDateTime": _fmt(ts - timedelta(seconds=2)),
                    "NodeID": nd["NodeID"], "LogEntryTypeID": 2 if trap else 1, "LogEntryLevelID": rnd.choice([3, 4, 5, 6]),
                    "Level": rnd.choice(["error", "warning", "notice", "info"]), "MessageSourceID": nd["NodeID"],
                    "Message": rnd.choice(["IF-MIB::linkDown", "BGP4-MIB::bgpBackwardTransition"]) if trap
                    else "%LINK-3-UPDOWN: Interface changed state"})
    D["Orion.OLM.LogEntry"] = log

    # ---------------- NCM ----------------
    ncm_nodes, archive, cid = [], [], 0
    for n in nodes:
        if n["ObjectSubType"] != "SNMP" or n["Vendor"].startswith("Huawei"):
            continue
        g = ncm_guid[n["NodeID"]]
        eos = EOS.get(n["MachineType"], {})
        ncm_nodes.append({"NodeID": g, "CoreNodeID": n["NodeID"], "NodeCaption": n["Caption"], "AgentIP": n["IPAddress"],
                          "Password": "mock-secret-pw", "EnablePassword": "mock-secret-enable",
                          "MachineType": n["MachineType"],
                          "ConnectionProfile": 1, "LoginStatus": "Login OK", "StatusText": "Up",
                          "EndOfSales": eos.get("sales", ""), "EndOfSupport": eos.get("support", ""),
                          "EndOfSoftware": eos.get("software", ""), "EosType": "Auto" if eos else "",
                          "EosLink": eos.get("link", ""), "EosComments": "", "ReplacementPartNumber": eos.get("repl", "")})
        base_age = 20 if n["Caption"] == "AGG-02" else 0
        hostname_line = f"hostname {n['Caption']}"
        for v in range(3):
            cid += 1
            changed = v == 0 and n["NodeID"] % 2 == 0
            dl = now - timedelta(days=base_age + v, hours=rnd.randint(0, 3))
            body = [f"! Last configuration change at {dl:%H:%M:%S %a %b %d %Y} by netops", hostname_line, "!", "interface Loopback0", f" ipv4 address {lo(n['NodeID'])} 255.255.255.255", "!",
                    "router isis CORE", " net 49.0001.0000.0000.00%02d.00" % n["NodeID"], "!",
                    "snmp-server community ****** RO", "logging host 10.250.0.10"]
            if changed:
                body += ["!", "interface TenGigE0/0/1", " description NEW-CUSTOMER-LINK", " shutdown"]
            archive.append({"ConfigID": str(uuid.UUID(int=rnd.getrandbits(128))), "NodeID": g,
                            "DownloadTime": _fmt(dl),
                            "ConfigType": "Running", "ConfigTitle": f"{n['Caption']} running", "Comments": "", "Baseline": False,
                            "IsBinary": False,
                            "Config": "\n".join(body)})
        cid += 1
        archive.append({"ConfigID": str(uuid.UUID(int=rnd.getrandbits(128))), "NodeID": g,
                        "DownloadTime": _fmt(now - timedelta(days=base_age + 1)), "ConfigType": "Startup",
                        "ConfigTitle": f"{n['Caption']} startup", "Comments": "", "Baseline": False, "IsBinary": False,
                        "Config": hostname_line})
    D["Cirrus.Nodes"] = ncm_nodes
    D["NCM.Nodes"] = [{"NodeID": x["NodeID"], "CoreNodeID": x["CoreNodeID"]} for x in ncm_nodes]
    last_backup = {}
    for a_ in archive:
        last_backup[a_["NodeID"]] = max(last_backup.get(a_["NodeID"], ""), a_["DownloadTime"])
    D["NCM.ConfigBackupStatus"] = [{"NodeID": x["NodeID"], "BackupStatus": "Success",
                                    "LastSuccessfulBackupDate": last_backup.get(x["NodeID"], "")} for x in ncm_nodes]
    D["NCM.ConfigArchive"] = archive
    D["Cirrus.PolicyReports"] = [{"PolicyReportID": rid, "Name": nm, "Comment": "", "LastUpdated": _fmt(now - timedelta(hours=8)),
                                  "LastModified": _fmt(now - timedelta(days=30)), "CacheStatus": 3, "LastError": "",
                                  "ReportStatus": True}
                                 for rid, nm in (("r1", "ISP baseline security"), ("r2", "Logging & SNMP"))]
    transfers = []
    for k, x in enumerate(ncm_nodes[:12]):
        failed = k == 4
        transfers.append({"TransferID": str(uuid.UUID(int=rnd.getrandbits(128))), "NodeID": x["NodeID"],
                          "Action": 1 if k % 3 else 3, "RequestedConfigType": "Running" if k % 3 else "",
                          "RequestedScript": "" if k % 3 else "show version", "RequestedReboot": False,
                          "ConfigID": "", "TransferProtocol": "SSH", "Status": 3 if failed else 2,
                          "ErrorMessage": "Connection refused by remote host" if failed else None,
                          "DeviceOutput": "" if k % 3 else "Cisco IOS XR Software, Version 7.9.2", "UserName": "svc_ncm",
                          "DateTime": _fmt(now - timedelta(hours=k + 1)), "JobId": 0})
    D["NCM.TransferResults"] = transfers
    viol = [{"NodeID": rnd.choice(ncm_nodes)["NodeID"], "ReportID": rnd.choice(["r1", "r2"]), "PolicyName": "ISP policy",
             "RuleName": rnd.choice(["No telnet", "SNMP RO only", "NTP configured"]), "ErrorLevel": rnd.choice([1, 2]),
             "IsViolation": True, "ConfigID": "", "FoundLine": ""} for _ in range(7)]
    D["Cirrus.PolicyCacheResults"] = viol + [dict(v, IsViolation=False, RuleName="Logging host set") for v in viol[:3]]
    D["Cirrus.PolicyReportViolations"] = [
        {"ReportID": r, "Info": 0, "Warning": sum(1 for v in viol if v["ReportID"] == r and v["ErrorLevel"] == 1),
         "Error": sum(1 for v in viol if v["ReportID"] == r and v["ErrorLevel"] == 2), "TimeStamp": _fmt(now - timedelta(hours=8))}
        for r in ("r1", "r2")]
    D["NCM.VulnerabilitiesAnnouncements"] = [
        {"EntryId": f"CVE-2024-{20000 + i}", "Summary": t_, "Score": s, "Severity": "High" if s >= 7 else "Medium",
         "PublishedDate": _fmt(now - timedelta(days=60 + i * 20)), "AnnouncementUrl": f"https://vendor.example/advisory/{i}"}
        for i, (t_, s) in enumerate((("IOS XR SSH DoS", 7.5), ("IOS XE Web UI privilege escalation", 10.0),
                                     ("IOS XR BGP malformed update", 8.6), ("Junos J-Web RCE", 9.8), ("Junos BFD crash", 6.5)))]
    vn = []
    for g in ncm_nodes:
        n = next(x for x in nodes if x["NodeID"] == g["CoreNodeID"])
        if n["Vendor"] == "Cisco":
            for e in ("CVE-2024-20000", "CVE-2024-20002") if n["Caption"].startswith("P-") else ("CVE-2024-20001",):
                vn.append({"CoreNodeID": g["CoreNodeID"], "EntryId": e, "State": 0, "Comment": "", "StateChange": ""})
        elif n["Vendor"].startswith("Juniper"):
            vn.append({"CoreNodeID": g["CoreNodeID"], "EntryId": "CVE-2024-20003", "State": 1, "Comment": "", "StateChange": ""})
    D["NCM.VulnerabilitiesAnnouncementsNodes"] = vn

    # ---------------- layer 2 (core NPM; UDT not installed) ----------------
    D["Orion.STPRecords"] = [{"NodeID": n, "BridgeID": f"8000.001a.{n:04x}.0000", "RootPort": 1, "RootPathCost": 4,
                              "TopologyChangeCount": 3 if n == ids["ACC"][2] else 0,
                              "TopologyLastChange": _fmt(now - timedelta(days=12)), "Mode": "rstp"} for n in ids["ACC"]]
    D["Orion.NodeL3Entries"] = [{"NodeID": n, "IfIndex": 3, "MACAddress": f"00:50:56:{n % 256:02X}:{k:02X}:01",
                                 "IPAddress": f"10.20.{n % 256}.{k + 10}"} for n in ids["ACC"] for k in range(5)]
    D["Orion.NodeL2Connections"] = [{"NodeID": n, "PortID": k + 1, "MACAddress": f"00:50:56:{n % 256:02X}:{k:02X}:01",
                                     "VlanId": 100} for n in ids["ACC"] for k in range(5)]

    # ---------------- history ----------------
    rt, cpu, traf, errs = [], [], [], []
    hist_nodes = [n for n in nodes if n["Status"] == 1][:4]
    hist_ifs = [i for i in ifaces if i["OperStatus"] == 1][:4]

    def series(start_days: int, detail_min: int, hourly_days: int):
        pts = [now - timedelta(minutes=detail_min * k) for k in range(int(start_days * 1440 / detail_min))]
        pts += [now - timedelta(days=start_days, hours=h) for h in range(hourly_days * 24)]
        return pts

    for n in hist_nodes:
        for ts in series(7, 2, 23):
            rt.append({"NodeID": n["NodeID"], "DateTime": _fmt(ts), "AvgResponseTime": rnd.randint(1, 20),
                       "MinResponseTime": 1, "MaxResponseTime": 30, "PercentLoss": 0 if rnd.random() > 0.02 else 10, "Availability": 100})
        for ts in series(7, 10, 23):
            cpu.append({"NodeID": n["NodeID"], "DateTime": _fmt(ts), "AvgLoad": rnd.randint(5, 50), "MaxLoad": 70,
                        "AvgPercentMemoryUsed": rnd.randint(30, 60)})
    for i in hist_ifs:
        for ts in series(7, 10, 23):
            traf.append({"InterfaceID": i["InterfaceID"], "NodeID": i["NodeID"], "DateTime": _fmt(ts),
                         "InAveragebps": rnd.uniform(1e8, 8e9), "OutAveragebps": rnd.uniform(1e8, 8e9)})
            errs.append({"InterfaceID": i["InterfaceID"], "NodeID": i["NodeID"], "DateTime": _fmt(ts),
                         "InErrors": rnd.choice([0, 0, 0, 2]), "OutErrors": 0, "InDiscards": rnd.choice([0, 0, 5]), "OutDiscards": 0})
    D["Orion.ResponseTime"] = rt
    D["Orion.CPULoad"] = cpu
    D["Orion.NPM.InterfaceTraffic"] = traf
    D["Orion.NPM.InterfaceErrors"] = errs

    # ---------------- schema extras ----------------
    D["System.Subscription"] = []
    for ind in INDICATIONS:
        D[ind] = []
    return D


INDICATIONS = {"System.InstanceCreated", "System.InstanceModified", "System.InstanceDeleted",
               "Orion.AlertTriggered", "Orion.AlertReset"}
# EoL/EoS dates NCM fills in per model (blank = NCM found no match for the model).
EOS = {
    "Cisco ASR-920-24SZ-M": {"sales": "2024-07-31T00:00:00", "support": "2029-07-31T00:00:00", "software": "2025-07-31T00:00:00",
                             "link": "https://www.cisco.com/c/en/us/products/eos-eol-listing.html", "repl": "N540X-16Z4G8Q2C-A"},
    "Cisco ASR 9006": {"sales": "2022-11-30T00:00:00", "support": "2025-11-30T00:00:00", "software": "2023-11-30T00:00:00",
                       "link": "https://www.cisco.com/c/en/us/products/eos-eol-listing.html", "repl": "ASR-9906"},
    "Juniper MX960": {"sales": "2025-06-30T00:00:00", "support": "2030-06-30T00:00:00"},
}
CAPS = {
    "Orion.Nodes": {"CanCreate": True, "CanUpdate": True, "CanDelete": True},
    "Orion.NodesCustomProperties": {"CanCreate": False, "CanUpdate": True, "CanDelete": False},
    "Orion.Pollers": {"CanCreate": True, "CanUpdate": True, "CanDelete": True},
    "System.Subscription": {"CanCreate": True, "CanUpdate": True, "CanDelete": True},
}
# Users of the mock: the read-only account and a separate action account (spec §3.1 #1).
ACTION_USER = "actions"
VERBS = [
    ("Orion.Nodes", "Unmanage"), ("Orion.Nodes", "Remanage"), ("Orion.Nodes", "PollNow"),
    ("Orion.Nodes", "ScheduleListResources"),
    ("Orion.AlertSuppression", "SuppressAlerts"), ("Orion.AlertSuppression", "ResumeAlerts"),
    ("Orion.Discovery", "StartDiscovery"), ("Orion.Discovery", "CreateCorePluginConfiguration"),
    ("Cirrus.ConfigArchive", "ExecuteScript"), ("Cirrus.ConfigArchive", "ExecuteScriptOnNodes"),
    ("Cirrus.ConfigArchive", "DownloadConfig"), ("Cirrus.ConfigArchive", "UploadConfig"),
    ("Cirrus.ConfigArchive", "CompareConfigs"),
    ("Orion.NPM.Interfaces", "DiscoverInterfacesOnNode"), ("Orion.NPM.Interfaces", "AddInterfacesOnNode"),
    ("Orion.NodesCustomProperties", "ModifyCustomProperty"), ("Orion.NodesCustomProperties", "CreateCustomProperty"),
    ("NCM.Eos", "BeginRefreshAll"), ("NCM.Eos", "RefreshNow"),
    ("Cirrus.ConfigArchive", "Diff"), ("Cirrus.PolicyReports", "GetComplianceDataTable"),
    ("Cirrus.ConfigArchive", "CancelTransfers"), ("Cirrus.PolicyReports", "StartCaching"),
    ("Cirrus.PolicyReports", "GenerateRemediationScriptForNodes"), ("Cirrus.Nodes", "AddNodeToNCM"),
    ("Orion.AlertSuppression", "GetAlertSuppressionState"),
    ("Cirrus.ConfigArchive", "GetConfigTypes"), ("Cirrus.PolicyReports", "GetPolicyReport"),
    ("Cirrus.Nodes", "GetPageableEosDataTable"), ("Cirrus.Nodes", "GetPageableEosRowCount"),
]
# Mock signatures only, so the UI has something to build a form from. A real server's signature comes from
# its own Metadata.VerbArgument and may differ; the client never assumes these.
VERB_ARGUMENTS = [
    {"EntityName": "Cirrus.ConfigArchive", "VerbName": "Diff", "Position": 0, "Name": "configId1",
     "Type": "System.Guid", "IsOptional": False},
    {"EntityName": "Cirrus.ConfigArchive", "VerbName": "Diff", "Position": 1, "Name": "configId2",
     "Type": "System.Guid", "IsOptional": False},
    {"EntityName": "Cirrus.PolicyReports", "VerbName": "GetComplianceDataTable", "Position": 0,
     "Name": "policyReportId", "Type": "System.String", "IsOptional": False},
    {"EntityName": "Cirrus.PolicyReports", "VerbName": "GetComplianceDataTable", "Position": 1,
     "Name": "includePolicyData", "Type": "System.Boolean", "IsOptional": True},
] + [
    # Read-only verbs, in the argument order of the OrionSDK 2026.2 schema pages (names are the mock's own).
    {"EntityName": e, "VerbName": v, "Position": i, "Name": n, "Type": t, "IsOptional": o}
    for e, v, args in (
        ("Cirrus.ConfigArchive", "GetConfigTypes", []),
        ("Cirrus.PolicyReports", "GetPolicyReport", [("policyReportId", "System.String", False),
                                                     ("includePolicies", "System.Boolean", False)]),
        ("Cirrus.Nodes", "GetPageableEosDataTable", [("nodeIds", "System.Guid[]", False), ("startRow", "System.Int32", False),
                                                     ("pageSize", "System.Int32", False)]),
        ("Cirrus.Nodes", "GetPageableEosRowCount", [("nodeIds", "System.Guid[]", False)]),
    )
    for i, (n, t, o) in enumerate(args)
] + [
    # Action verbs, in the argument order of the OrionSDK 2026.2 schema pages (names are the mock's own).
    {"EntityName": e, "VerbName": v, "Position": i, "Name": n, "Type": t, "IsOptional": o}
    for e, v, args in (
        ("Cirrus.ConfigArchive", "DownloadConfig", [("nodeIds", "System.Guid[]", False), ("configType", "System.String", False)]),
        ("Cirrus.ConfigArchive", "ExecuteScript", [("nodeIds", "System.Guid[]", False), ("script", "System.String", False),
                                                   ("reboot", "System.Boolean", True)]),
        ("Cirrus.ConfigArchive", "UploadConfig", [("nodeIds", "System.Guid[]", False), ("configType", "System.String", False),
                                                  ("config", "System.String", False), ("reboot", "System.Boolean", False)]),
        ("Cirrus.ConfigArchive", "CancelTransfers", [("transferIds", "System.Guid[]", False)]),
        ("Cirrus.PolicyReports", "StartCaching", [("policyReportIds", "System.String[]", True)]),
        ("Cirrus.PolicyReports", "GenerateRemediationScriptForNodes", [
            ("nodeIds", "System.Guid[]", False), ("reportId", "System.String", False), ("policyId", "System.String", False),
            ("ruleId", "System.String", False), ("scriptTemplate", "System.String", True)]),
        ("Cirrus.Nodes", "AddNodeToNCM", [("nodeId", "System.Int32", False)]),
        ("Orion.AlertSuppression", "SuppressAlerts", [("entityUris", "System.String[]", False),
                                                       ("suppressFrom", "System.DateTime", True),
                                                       ("suppressUntil", "System.DateTime", True)]),
        ("Orion.AlertSuppression", "ResumeAlerts", [("entityUris", "System.String[]", False)]),
        ("Orion.AlertSuppression", "GetAlertSuppressionState", [("entityUris", "System.String[]", False)]),
        ("Orion.Nodes", "Unmanage", [("netObjectId", "System.String", False), ("unmanageTime", "System.DateTime", False),
                                     ("remanageTime", "System.DateTime", False), ("isRelative", "System.Boolean", False)]),
        ("Orion.Nodes", "Remanage", [("netObjectId", "System.String", False)]),
    )
    for i, (n, t, o) in enumerate(args)
]
MOCK_HOST = "mock-orion"
# Entities whose rows get a swis:// Uri, so they can be fetched with a CRUD read: entity -> key field.
URI_KEYS = {"Orion.Nodes": "NodeID", "Orion.NPM.Interfaces": "InterfaceID", "Orion.NodesCustomProperties": "NodeID",
            "Cirrus.Nodes": "NodeID", "NCM.ConfigArchive": "ConfigID", "Cirrus.PolicyReports": "PolicyReportID",
            "Orion.Pollers": "PollerID", "NCM.EntityPhysical": "EntityID", "Orion.AlertActive": "AlertActiveID"}
EXTRA_ENTITIES = ["Orion.Discovery", "Cirrus.ConfigArchive", "NCM.Eos"]
# Metadata.Property.IsKey per entity, so the client can order pages stably.
KEYS = {"Orion.Nodes": ["NodeID"], "Orion.NPM.Interfaces": ["InterfaceID"], "NCM.ConfigArchive": ["ConfigID"],
        "NCM.TransferResults": ["TransferID"], "Orion.AlertHistory": ["AlertHistoryID"], "Orion.Pollers": ["PollerID"],
        "Orion.AlertSuppression": ["ID"], "Cirrus.Nodes": ["NodeID"], "Cirrus.PolicyReports": ["PolicyReportID"],
        "Orion.Events": ["EventID"], "Orion.NodeIPAddresses": ["NodeID", "IPAddress"]}
# Entities that exist (module installed) but hold no rows in the mock: name -> {field: sample value}.
EMPTY_SCHEMA = {
    "Orion.NPM.MulticastRouting.PIMNeighbors": {"NodeID": 0, "NeighborAddress": "", "InterfaceIndex": 0, "UpTime": 0.0},
    "Orion.NPM.MulticastRouting.RendezvousPoints": {"NodeID": 0, "RPAddress": "", "GroupAddress": ""},
    "Orion.NPM.MulticastRouting.Groups": {"NodeID": 0, "GroupAddress": "", "SourceAddress": ""},
}
ABSTRACT = ["System.Entity", "System.Indication"]
# Navigation properties and the join that each one stands for. This join is the MOCK's own truth: the mock uses it
# to answer navigation SWQL (s.Interfaces.InterfaceID) and lists the property in Metadata.Property / Metadata.Relationship
# without the join columns, like SWIS. The collector must infer the columns itself and prove them (swcollect phase 1).
# (source entity, navigation property, target entity, [(source column, target column)], source card., target card.)
NAV_RELATIONS = [
    ("Orion.Nodes", "Interfaces", "Orion.NPM.Interfaces", [("NodeID", "NodeID")], "1", "0..*"),
    ("Orion.NPM.Interfaces", "Node", "Orion.Nodes", [("NodeID", "NodeID")], "0..*", "1"),
    ("Orion.Nodes", "CustomProperties", "Orion.NodesCustomProperties", [("NodeID", "NodeID")], "1", "1"),
    ("Orion.Nodes", "IPAddresses", "Orion.NodeIPAddresses", [("NodeID", "NodeID")], "1", "0..*"),
    ("Orion.NPM.Interfaces", "Traffic", "Orion.NPM.InterfaceTraffic", [("InterfaceID", "InterfaceID")], "1", "0..*"),
    ("Orion.AlertActive", "AlertObject", "Orion.AlertObjects", [("AlertObjectID", "AlertObjectID")], "0..*", "1"),
    ("Orion.Routing.Neighbors", "Node", "Orion.Nodes", [("NodeID", "NodeID")], "0..*", "1"),
    ("Orion.NodeLldpEntry", "Node", "Orion.Nodes", [("NodeID", "NodeID")], "0..*", "1"),
    # The NCM node GUID and the Orion NodeID have different names: inference by name cannot find this one.
    ("NCM.Nodes", "OrionNode", "Orion.Nodes", [("CoreNodeID", "NodeID")], "0..1", "1"),
    ("NCM.ConfigArchive", "Node", "NCM.Nodes", [("NodeID", "NodeID")], "0..*", "1"),
]
NAVIGATION = {}
for _s, _p, _t, *_ in NAV_RELATIONS:
    NAVIGATION.setdefault(_s, []).append((_p, _t))

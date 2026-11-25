# Copied from swcheck swcheck/pipeline.py (INTERFACE_PREFIXES, normalize_interface_name) at commit 0958074 (2026-11-18).
"""Transformation rules declared for the curated layer (SPEC-B §6). sw_core.normalize_interface_name mirrors them."""
from __future__ import annotations

import re

# §6: one interface may be named Gi0/0/1 or GigabitEthernet0/0/1. Prefix (lower case) -> one spelling.
# Used for future matching between sources only; ingest is proven on the raw name and InterfaceID.
INTERFACE_PREFIXES = {
    "gi": "GigabitEthernet", "gig": "GigabitEthernet", "gigabitethernet": "GigabitEthernet",
    "te": "TenGigabitEthernet", "ten": "TenGigabitEthernet", "tengige": "TenGigabitEthernet",
    "tengigabitethernet": "TenGigabitEthernet",
    "twe": "TwentyFiveGigE", "twentyfivegige": "TwentyFiveGigE",
    "fo": "FortyGigabitEthernet", "fortygige": "FortyGigabitEthernet", "fortygigabitethernet": "FortyGigabitEthernet",
    "hu": "HundredGigE", "hundredgige": "HundredGigE",
    "fa": "FastEthernet", "fastethernet": "FastEthernet",
    "et": "Ethernet", "eth": "Ethernet", "ethernet": "Ethernet",
    "po": "Port-channel", "port-channel": "Port-channel",
    "be": "Bundle-Ether", "bundle-ether": "Bundle-Ether",
    "lo": "Loopback", "loopback": "Loopback",
    "vl": "Vlan", "vlan": "Vlan",
    "tu": "Tunnel", "tunnel": "Tunnel",
}
_IFNAME = re.compile(r"^([A-Za-z][A-Za-z-]*?)\s*(\d.*)$")


def normalize_interface_name(name):
    if not isinstance(name, str):
        return name
    m = _IFNAME.match(name.strip())
    if not m:
        return name.strip()
    full = INTERFACE_PREFIXES.get(m.group(1).lower())
    return f"{full}{m.group(2)}" if full else name.strip()


TRANSFORMS = {"interface_name": normalize_interface_name}
TRANSFORM_LABELS = {"interface_name": "נרמול שם ממשק (§6)"}

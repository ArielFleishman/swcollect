"""Loads config.yaml (SWIS network profiles, sinks) and .env.

Adapted from swcheck/config.py (commit 0958074). Network profiles have the same format, so a swcheck config.yaml
works here. The collector is read-only, so a profile with actions_enabled is refused (see Settings.network).

Environment variables (real ones win over .env):
  SWIS_URL, SWIS_USERNAME, SWIS_PASSWORD, SWIS_NETWORK, SWIS_VERIFY_TLS, SWIS_CA_BUNDLE, SWIS_TIMEOUT
                       one SWIS profile, as in swcheck
  SWCOLLECT_PG_DSN     postgresql://user:pass@host:5432/swcollect
  SWCOLLECT_LOKI_URL   http://loki:3100
  SWCOLLECT_HOOK_TOKEN shared token the Orion alert action sends in the X-Swcollect-Token header
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"
EXAMPLE_CONFIG = ROOT / "config.example.yaml"
ENV_FILE = ROOT / ".env"
CATALOG_DIR = ROOT / "catalog"
DEFAULT_BASE_PATH = "/SolarWinds/InformationService/v3/Json"

_TRUE = {"1", "true", "yes", "on", "y"}
_from_file: dict[str, str] = {}


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, _, val = line.partition("=")
    key, val = key.strip().removeprefix("export ").strip(), val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
        val = val[1:-1]
    return key, val          # no escape processing: DOMAIN\user stays as written


def read_env_file(path: Path | None = None) -> dict[str, str]:
    path = path or ENV_FILE
    if not Path(path).exists():
        return {}
    pairs = (_parse_line(l) for l in Path(path).read_text(encoding="utf-8-sig").splitlines())
    return dict(p for p in pairs if p)


def load_env_file(path: Path | None = None) -> dict[str, str]:
    """Reads KEY=VALUE lines into os.environ. Real environment variables are never overwritten."""
    values = read_env_file(path)
    for key, val in values.items():
        if key not in os.environ or _from_file.get(key) == os.environ.get(key):
            os.environ[key] = val
            _from_file[key] = val
    return values


def parse_url(url: str) -> tuple[str, str, int, str]:
    """'https://h:17774/SolarWinds/...' -> (scheme, host, port, base_path). Scheme defaults to https, port to 17774."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    u = urlsplit(url)
    if not u.hostname:
        raise ValueError(f"SWIS_URL has no host: {url}")
    path = u.path.rstrip("/")
    if path.lower().endswith("/query"):
        path = path[: -len("/query")]
    return u.scheme or "https", u.hostname, u.port or 17774, path or DEFAULT_BASE_PATH


@dataclass
class Network:
    name: str
    host: str
    port: int = 17774
    scheme: str = "https"
    base_path: str = DEFAULT_BASE_PATH
    username: str = ""
    password: str | None = None
    password_env: str | None = None
    verify_tls: bool = False
    ca_bundle: str | None = None
    timeout: int = 120
    one_way: bool = False
    description: str = ""
    source: str = "config.yaml"
    actions_enabled: bool = False     # accepted for swcheck compatibility; the collector refuses such profiles
    network_id: str = ""              # stable-ID network part (SPEC-B §6). Defaults to the profile name.

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{self.base_path}"

    @property
    def stable_network_id(self) -> str:
        return self.network_id or self.name

    @property
    def instance_key(self) -> str:
        """network_id/host:port – the instance part of every stable ID (local NodeID is not global)."""
        return f"{self.stable_network_id}/{self.host}:{self.port}"

    def resolve_password(self) -> str | None:
        if self.password_env and os.environ.get(self.password_env):
            return os.environ[self.password_env]
        return self.password


@dataclass
class Settings:
    networks: dict[str, Network] = field(default_factory=dict)
    page_size: int = 5000
    pg_dsn: str = "postgresql://swcollect:swcollect@127.0.0.1:15432/swcollect"
    loki_url: str = "http://127.0.0.1:13100"
    hook_host: str = "0.0.0.0"
    hook_port: int = 18090
    hook_token: str | None = None
    max_concurrent_queries: int = 2      # per Orion instance
    config_path: Path | None = None

    def network(self, name: str) -> Network:
        if name not in self.networks:
            raise KeyError(f"No network profile named {name!r}. Known: {', '.join(self.networks) or 'none'}")
        net = self.networks[name]
        if net.actions_enabled:
            raise ValueError(f"Profile {name!r} has actions_enabled. The collector runs only on read-only profiles.")
        return net


def apply_env(networks: dict[str, Network]) -> str | None:
    """Adds or overrides the network profile described by SWIS_* variables. Returns its name."""
    env = os.environ
    url = env.get("SWIS_URL", "").strip()
    if not url:
        return None
    name = env.get("SWIS_NETWORK", "").strip() or "env"
    scheme, host, port, base_path = parse_url(url)
    net = networks.get(name) or Network(name=name, host=host, description="SWIS from .env / environment")
    net.scheme, net.host, net.port, net.base_path = scheme, host, port, base_path
    net.source = ".env" if "SWIS_URL" in _from_file else "environment"
    if env.get("SWIS_USERNAME"):
        net.username = env["SWIS_USERNAME"]
    if env.get("SWIS_PASSWORD"):
        net.password = env["SWIS_PASSWORD"]
    if env.get("SWIS_VERIFY_TLS"):
        net.verify_tls = env["SWIS_VERIFY_TLS"].strip().lower() in _TRUE
    if env.get("SWIS_CA_BUNDLE"):
        net.ca_bundle = env["SWIS_CA_BUNDLE"]
    if env.get("SWIS_TIMEOUT"):
        net.timeout = int(env["SWIS_TIMEOUT"])
    networks[name] = net
    return name


def load(path: str | os.PathLike | None = None) -> Settings:
    load_env_file()
    p = Path(path) if path else (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    s = Settings(config_path=p)
    for n in raw.get("networks", []):
        net = Network(**n)
        s.networks[net.name] = net
    apply_env(s.networks)
    for key in ("page_size", "pg_dsn", "loki_url", "hook_host", "hook_port", "max_concurrent_queries"):
        if key in raw:
            setattr(s, key, raw[key])
    env = os.environ
    s.pg_dsn = env.get("SWCOLLECT_PG_DSN") or s.pg_dsn
    s.loki_url = (env.get("SWCOLLECT_LOKI_URL") or s.loki_url).rstrip("/")
    s.hook_token = env.get("SWCOLLECT_HOOK_TOKEN") or raw.get("hook_token")
    return s

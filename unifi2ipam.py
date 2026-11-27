#!/usr/bin/env python3

# Synchronize network client information from a UniFi Network Controller to a phpIPAM instance.
#
# phpIPAM is meant to feed DNS, so this tool writes only what a DNS consumer can trust:
#   - hostname   the curated DNS name (UniFi "Local DNS Record", else a valid alias), stored as the
#                part before the configured zone ("pet-cam.iot"), or EMPTY when the client has no
#                forward name (no curated name, a randomised MAC, or a duplicate name).
#   - description the human-readable UniFi display name (so the inventory value is kept).
#   - owner      "unifi2ipam" on every row this tool creates. It never updates or deletes a row
#                it did not create.
#   - lastSeen   set to the time of the run for every client that is connected.
# Nothing is ever written if UniFi cannot be read. --dryrun reads everything and writes nothing.

import argparse
import ipaddress
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3
import yaml

__version__ = "1.2.0"
DEFAULT_CONFIG_PATH = Path("/etc/unifi2ipam/config.yaml")


@dataclass
class Config:
    unifi_url: str
    unifi_key: str
    ipam_url: str
    ipam_key: str
    verify_ssl: bool
    include_subnets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(default_factory=list)
    exclude_subnets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(default_factory=list)
    # The legacy (non-integration) UniFi API is the only one that exposes a client's alias and
    # Local DNS Record. Derived from unifi_url when not given.
    unifi_legacy_url: str = ""
    unifi_site_name: str = "default"
    dns_zone: str = ""
    dry_run: bool = False


def setup_logging(args) -> None:
    """Configure logging level and handlers based on CLI flags."""
    if args.quiet:
        level = logging.ERROR
    elif args.verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file))

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=handlers,
    )


def parse_subnets(raw: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse a list of CIDR strings into network objects, exiting on any invalid entry."""
    result = []
    for cidr in raw:
        try:
            result.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError as e:
            logging.error(f"Invalid subnet '{cidr}': {e}")
            raise SystemExit(1)
    return result


def is_in_filter(cfg: Config, ip: str) -> bool:
    """
    Return True if the given IP should be processed based on the configured subnet filter.

    - If include_subnets is set: only process IPs that fall within one of those subnets.
    - If exclude_subnets is set: skip IPs that fall within any of those subnets.
    - If neither is set: always process.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # malformed IP — pass through and let it fail naturally

    if cfg.include_subnets:
        return any(addr in subnet for subnet in cfg.include_subnets)
    if cfg.exclude_subnets:
        return not any(addr in subnet for subnet in cfg.exclude_subnets)
    return True


def load_config(args) -> Config:
    """
    Build a Config object by merging values from (lowest to highest precedence):
      1. YAML config file
      2. Environment variables
      3. CLI flags

    Returns:
        Config: Populated configuration object.

    Raises:
        SystemExit: If any required value is missing after merging all sources.
    """
    values = {
        "unifi_url": None,
        "unifi_key": None,
        "ipam_base_url": None,
        "ipam_app_id": None,
        "ipam_key": None,
        "insecure": False,
        "unifi_legacy_url": None,
        "unifi_site_name": None,
        "dns_zone": None,
        "include_subnets": [],
        "exclude_subnets": [],
    }

    # 1. Config file
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    if config_path.exists():
        logging.debug(f"Loading config file: {config_path}")
        with open(config_path) as f:
            file_cfg = yaml.safe_load(f) or {}

        unifi_cfg = file_cfg.get("unifi", {})
        ipam_cfg = file_cfg.get("ipam", {})

        values["unifi_url"] = unifi_cfg.get("url")
        values["unifi_key"] = unifi_cfg.get("api_key")
        values["ipam_base_url"] = ipam_cfg.get("base_url")
        values["ipam_app_id"] = ipam_cfg.get("app_id")
        values["ipam_key"] = ipam_cfg.get("api_key")
        values["unifi_legacy_url"] = unifi_cfg.get("legacy_url")
        values["unifi_site_name"] = unifi_cfg.get("site_name")
        values["dns_zone"] = (file_cfg.get("dns") or {}).get("zone")
        values["insecure"] = file_cfg.get("insecure", False)
        values["include_subnets"] = file_cfg.get("subnets", {}).get("include", [])
        values["exclude_subnets"] = file_cfg.get("subnets", {}).get("exclude", [])
    elif args.config:
        # Only error if a path was explicitly provided but doesn't exist.
        logging.error(f"Config file not found: {config_path}")
        raise SystemExit(1)

    # 2. Environment variables
    env_map = {
        "UNIFI_URL": "unifi_url",
        "UNIFI_API_KEY": "unifi_key",
        "IPAM_BASE_URL": "ipam_base_url",
        "IPAM_APP_ID": "ipam_app_id",
        "IPAM_API_KEY": "ipam_key",
        "UNIFI_LEGACY_URL": "unifi_legacy_url",
        "UNIFI_SITE_NAME": "unifi_site_name",
        "DNS_ZONE": "dns_zone",
    }
    for env_var, key in env_map.items():
        val = os.environ.get(env_var)
        if val:
            values[key] = val

    # 3. CLI flags
    if args.unifi_url:
        values["unifi_url"] = args.unifi_url
    if args.ipam_base_url:
        values["ipam_base_url"] = args.ipam_base_url
    if args.ipam_app_id:
        values["ipam_app_id"] = args.ipam_app_id
    if args.unifi_site_name:
        values["unifi_site_name"] = args.unifi_site_name
    if args.dns_zone:
        values["dns_zone"] = args.dns_zone
    if args.insecure:
        values["insecure"] = True
    if args.include_subnet:
        values["include_subnets"] = args.include_subnet
    if args.exclude_subnet:
        values["exclude_subnets"] = args.exclude_subnet

    # Derive the full IPAM URL from base URL + app ID.
    ipam_url = None
    if values["ipam_base_url"] and values["ipam_app_id"]:
        base = values["ipam_base_url"].rstrip("/")
        ipam_url = f"{base}/api/{values['ipam_app_id']}/"

    # Subnet mutual-exclusivity check — catch this before anything else.
    if values["include_subnets"] and values["exclude_subnets"]:
        logging.error("--include-subnet and --exclude-subnet (subnets.include / subnets.exclude) are mutually exclusive.")
        raise SystemExit(1)

    # The legacy API lives next to the integration API: .../proxy/network/integration/v1/ -> .../proxy/network/
    legacy_url = values["unifi_legacy_url"]
    if not legacy_url and values["unifi_url"] and "/integration/" in values["unifi_url"]:
        legacy_url = values["unifi_url"].split("/integration/")[0] + "/"

    # Pre-flight validation — all four required fields must be present.
    missing = []
    if not values["unifi_url"]:
        missing.append("UniFi URL  (--unifi-url, UNIFI_URL env var, or config file unifi.url)")
    if not values["unifi_key"]:
        missing.append("UniFi API key  (UNIFI_API_KEY env var or config file unifi.api_key)")
    if not values["ipam_base_url"]:
        missing.append("phpIPAM base URL  (--ipam-base-url, IPAM_BASE_URL env var, or config file ipam.base_url)")
    if not values["ipam_app_id"]:
        missing.append("phpIPAM App ID  (--ipam-app-id, IPAM_APP_ID env var, or config file ipam.app_id)")
    if not values["ipam_key"]:
        missing.append("phpIPAM API key  (IPAM_API_KEY env var or config file ipam.api_key)")

    if not legacy_url:
        missing.append("UniFi legacy URL  (UNIFI_LEGACY_URL, or a unifi.url containing /integration/ to derive it from)")

    if missing:
        logging.error("Missing required configuration:")
        for item in missing:
            logging.error(f"  - {item}")
        raise SystemExit(1)

    include_subnets = parse_subnets(values["include_subnets"])
    exclude_subnets = parse_subnets(values["exclude_subnets"])

    if include_subnets:
        logging.info(f"Subnet filter: include only {[str(s) for s in include_subnets]}")
    elif exclude_subnets:
        logging.info(f"Subnet filter: exclude {[str(s) for s in exclude_subnets]}")

    if values["insecure"]:
        logging.warning("SSL certificate verification is disabled (--insecure).")
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    assert ipam_url is not None  # validated above; both ipam_base_url and ipam_app_id are set
    return Config(
        unifi_url=values["unifi_url"],
        unifi_key=values["unifi_key"],
        ipam_url=ipam_url,
        ipam_key=values["ipam_key"],
        verify_ssl=not values["insecure"],
        include_subnets=include_subnets,
        exclude_subnets=exclude_subnets,
        unifi_legacy_url=legacy_url,
        unifi_site_name=values["unifi_site_name"] or "default",
        dns_zone=(values["dns_zone"] or "").strip().strip(".").lower(),
        dry_run=bool(args.dryrun),
    )


OWNER = "unifi2ipam"
# v1.0.x wrote this note on every row it created and nothing else marked them. Rows carrying it
# are adopted as ours; a row without it (hand-made, Terraform, other tools) is foreign.
LEGACY_NOTE = "Created by UniFi2IPAM"
DESC_MAX = 60  # phpIPAM's description column is short; longer values fail with SQLSTATE 22001

_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class IpamError(Exception):
    """phpIPAM did not give a usable answer. Never read as 'not found'."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def xmit(cfg: Config, service, endpoint, params=None, method="get") -> dict:
    """
    Abstracted helper function to send requests to either the UniFi or phpIPAM API.

    Args:
        cfg (Config): The active configuration object.
        service (str): 'unifi' (integration API), 'unifi_legacy' (classic API) or 'ipam'.
        endpoint (str): The specific API endpoint to target (e.g., 'sites').
        params (dict, optional): Query params for GET, JSON body for POST/PATCH.
        method (str, optional): The HTTP method to use. Defaults to "get".

    Returns:
        dict: The JSON response from the API as a dictionary, or {} on failure.

    UniFi is read-only here, and in dry-run mode nothing that is not a GET leaves this function.
    """
    if service == "unifi":
        headers = {"X-API-Key": cfg.unifi_key, "Content-Type": "application/json"}
        base_url = cfg.unifi_url
    elif service == "unifi_legacy":
        headers = {"X-API-Key": cfg.unifi_key, "Content-Type": "application/json"}
        base_url = cfg.unifi_legacy_url
    elif service == "ipam":
        headers = {"token": cfg.ipam_key, "Content-Type": "application/json"}
        base_url = cfg.ipam_url
    else:
        raise ValueError("Invalid service specified. Use 'unifi', 'unifi_legacy' or 'ipam'")

    if service != "ipam" and method.lower() != "get":
        raise ValueError("UniFi is read-only: refusing a non-GET request")

    if cfg.dry_run and method.lower() != "get":
        logging.info(f"[dry-run] would {method.upper()} {endpoint} {json.dumps(params) if params else ''}".rstrip())
        return {"success": True, "dry_run": True}

    try:
        requests_method = getattr(requests, method.lower())
        kwargs = {"headers": headers, "verify": cfg.verify_ssl, "timeout": 10}

        if method.lower() == "get":
            kwargs["params"] = params
        elif method.lower() in ["post", "patch", "put"]:
            kwargs["json"] = params

        url = f"{base_url.rstrip('/')}/{endpoint}"
        logging.debug(f"{method.upper()} {url}")

        response = requests_method(url, **kwargs)
        response.raise_for_status()
        return response.json()

    except requests.exceptions.HTTPError as errh:
        if errh.response is not None:
            try:
                error_body = errh.response.json()
                if errh.response.status_code >= 500:
                    # Server-side errors are always unexpected — log loudly.
                    logging.error(f"HTTP server error {errh.response.status_code}: {error_body.get('message', errh)}")
                else:
                    # 4xx errors are client-level conditions (not found, conflict, etc.)
                    # — log at debug and return the body so callers can handle them.
                    logging.debug(f"HTTP {errh.response.status_code}: {error_body.get('message', '')} ({errh.response.url})")
                return error_body
            except ValueError:
                logging.error(f"HTTP error: {errh}")
                logging.error(f"Response body: {errh.response.text}")
        else:
            logging.error(f"HTTP error: {errh}")
    except AttributeError:
        logging.error(f"Invalid or unsupported HTTP method '{method}'")
    except (requests.exceptions.RequestException, ValueError) as err:
        logging.error(f"Request failed: {err}")

    return {}


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def normalize_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


def is_randomised_mac(mac: str) -> bool:
    """True for a locally administered MAC (bit 0x02 of the first octet): iOS/Android/Windows privacy addresses."""
    try:
        return bool(int(normalize_mac(mac).split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def dns_name_from_local_record(record: str, zone: str) -> str | None:
    """
    'pet-cam.iot.home.8none1.com' -> 'pet-cam.iot' for zone 'home.8none1.com'.
    None if the record is not in the zone, is the zone apex, has more than two labels left of the
    zone, or any label is not a valid lowercase DNS label.
    """
    if not zone:
        return None
    rec = record.strip().rstrip(".").lower()
    suffix = "." + zone
    if not rec.endswith(suffix):
        return None
    labels = rec[: -len(suffix)].split(".")
    if not 1 <= len(labels) <= 2 or not all(_LABEL_RE.match(label) for label in labels):
        return None
    return ".".join(labels)


def dns_name_from_alias(alias: str) -> str | None:
    """
    An alias is a curated name only if it is already a single valid label ('k3s-server-01',
    'Bambu-P1S'). 'Living Room TV', "Adam's iPhone" or 'SonosZP 16:0c' are display names, not DNS
    names, and are never repaired into one.
    """
    a = alias.strip().lower()
    if a in ("", "unknown") or not _LABEL_RE.match(a):
        return None
    return a


def curated_name(cfg: Config, mac: str, user: dict | None) -> tuple[str | None, str]:
    """Return (dns_name or None, reason). The reason is for the log and the dry-run plan."""
    if is_randomised_mac(mac):
        return None, "randomised MAC"
    if not user:
        return None, "no UniFi client record"
    record = (user.get("local_dns_record") or "").strip()
    if record and user.get("local_dns_record_enabled", True):
        name = dns_name_from_local_record(record, cfg.dns_zone)
        if name:
            return name, "local DNS record"
        return None, "local DNS record not usable (outside zone or not valid labels)"
    alias = (user.get("name") or "").strip()
    if alias:
        name = dns_name_from_alias(alias)
        if name:
            return name, "alias"
        return None, "alias is not a valid DNS name"
    return None, "no curated name"


def build_desired(cfg: Config, client: dict, user: dict | None) -> dict:
    mac = normalize_mac(client["macAddress"])
    name, reason = curated_name(cfg, mac, user)
    display = (client.get("name") or "").strip() or mac
    return {
        "ip": client["ipAddress"],
        "mac": mac,
        "hostname": name or "",
        "reason": reason,
        "description": display[:DESC_MAX],
        "owner": OWNER,
        "note": LEGACY_NOTE,
    }


def enforce_unique_names(plans: list[dict]) -> None:
    """A name two connected clients both claim is not a curated unique name: neither gets it."""
    seen: dict[str, list[dict]] = {}
    for p in plans:
        if p["hostname"]:
            seen.setdefault(p["hostname"], []).append(p)
    for name, group in seen.items():
        if len(group) > 1:
            logging.warning(f"Name '{name}' claimed by {[p['mac'] for p in group]} — none of them gets a forward name")
            for p in group:
                p["hostname"] = ""
                p["reason"] = "duplicate name"


# ---------------------------------------------------------------------------
# phpIPAM
# ---------------------------------------------------------------------------

def is_owned(row: dict) -> bool:
    return row.get("owner") == OWNER or (row.get("note") or "").startswith(LEGACY_NOTE)


def ipam_lookup(cfg: Config, endpoint: str) -> list:
    """
    GET a phpIPAM collection. 404 ('nothing found') is []. Anything else that is not a success
    (no response, 5xx, 401, malformed) raises IpamError: an unknown must not read as 'not found'.
    """
    resp = xmit(cfg, "ipam", endpoint)
    if not resp:
        raise IpamError(f"no response from phpIPAM for {endpoint}")
    if resp.get("success"):
        data = resp.get("data") or []
        return [data] if isinstance(data, dict) else list(data)
    if resp.get("code") == 404:
        return []
    raise IpamError(f"phpIPAM answered {resp.get('code')} {resp.get('message')} for {endpoint}")


def ipam_write(cfg: Config, endpoint: str, params, method: str) -> bool:
    resp = xmit(cfg, "ipam", endpoint, params=params, method=method)
    if resp and resp.get("success"):
        return True
    logging.error(f"phpIPAM {method.upper()} {endpoint} failed: {(resp or {}).get('message', 'no response')}")
    return False


def same_ip(a: str, b: str) -> bool:
    try:
        return ipaddress.ip_address(a) == ipaddress.ip_address(b)
    except ValueError:
        return a == b


def lookup_subnet_id(cfg: Config, ip: str):
    subnets = ipam_lookup(cfg, f"subnets/overlapping/{ip}/32")
    if not subnets:
        return None
    # Most specific containing subnet.
    return max(subnets, key=lambda s: int(s.get("mask") or 0))["id"]


def row_changes(row: dict, desired: dict, last_seen: str) -> dict:
    """Fields to PATCH: whatever differs, plus lastSeen (always: the client is connected)."""
    changes = {}
    for key in ("hostname", "description", "mac", "owner", "note"):
        if (row.get(key) or "") != desired[key]:
            changes[key] = desired[key]
    changes["lastSeen"] = last_seen
    return changes


def delete_owned(cfg: Config, row: dict) -> bool:
    """The only delete in this tool. Refuses any row that is not ours."""
    if not is_owned(row):
        logging.error(f"Refusing to delete row {row.get('id')}: not created by unifi2ipam")
        return False
    return ipam_write(cfg, f"addresses/{row['id']}/", None, "delete")


def create_row(cfg: Config, desired: dict, last_seen: str) -> bool:
    subnet_id = lookup_subnet_id(cfg, desired["ip"])
    if subnet_id is None:
        logging.error(f"No containing subnet found for {desired['ip']}. Cannot create address.")
        return False
    payload = {
        "ip": desired["ip"],
        "subnetId": subnet_id,
        "mac": desired["mac"],
        "description": desired["description"],
        "owner": OWNER,
        "note": LEGACY_NOTE,
        "lastSeen": last_seen,
    }
    if desired["hostname"]:
        payload["hostname"] = desired["hostname"]
    return ipam_write(cfg, "addresses", payload, "post")


def sync_client(cfg: Config, desired: dict, last_seen: str) -> str:
    """
    Reconcile one connected client. Returns the outcome: created, updated, moved, takeover,
    foreign (address held by a row we do not own: left alone), or error.

    Rules: a row we did not create is never updated or deleted, whatever MAC or address it holds.
    """
    ip, mac = desired["ip"], desired["mac"]
    try:
        by_mac = [r for r in ipam_lookup(cfg, f"addresses/search_mac/{mac}/") if is_owned(r)]
        at_ip = [r for r in ipam_lookup(cfg, f"addresses/search/{ip}/") if same_ip(r.get("ip", ""), ip)]
    except IpamError as e:
        logging.error(f"{mac} ({ip}): {e}")
        return "error"

    foreign = [r for r in at_ip if not is_owned(r)]
    if foreign:
        logging.warning(
            f"{mac} ({ip}): address held by row {foreign[0].get('id')} ({foreign[0].get('hostname')!r}) "
            f"which unifi2ipam did not create — left alone"
        )
        return "foreign"

    mine_here = [r for r in at_ip if normalize_mac(r.get("mac") or "") == mac]
    mine_elsewhere = [r for r in by_mac if not same_ip(r.get("ip", ""), ip)]
    stale_here = [r for r in at_ip if r not in mine_here]  # ours, but another MAC's row

    if mine_here:
        row = mine_here[0]
        ok = ipam_write(cfg, f"addresses/{row['id']}/", row_changes(row, desired, last_seen), "patch")
        return "updated" if ok else "error"

    if stale_here:
        # Our own row for a device that left this address: reuse it for the new holder.
        row = stale_here[0]
        if not ipam_write(cfg, f"addresses/{row['id']}/", row_changes(row, desired, last_seen), "patch"):
            return "error"
        for old in mine_elsewhere:
            if not delete_owned(cfg, old):
                return "error"
        return "takeover"

    # Nothing at this address. phpIPAM cannot change a row's IP, so a move is create-then-delete,
    # and the old row is removed only after the new one exists.
    if not create_row(cfg, desired, last_seen):
        return "error"
    for old in mine_elsewhere:
        if not delete_owned(cfg, old):
            return "error"
    return "moved" if mine_elsewhere else "created"


# ---------------------------------------------------------------------------
# UniFi
# ---------------------------------------------------------------------------

def select_site_id(sites) -> str:
    """
    Prompts the user to select a site from the available sites in the UniFi API.
    Exits if no valid choice is made.
    """
    print("===== Available sites =====")
    for i, site in enumerate(sites.get("data", []), start=1):
        print(f"{i} ---> Site Name: {site['name']}, ID: {site['id']}")
    print("===========================\n")

    count = len(sites.get("data", []))
    choice = input(f"Select a site by number (1-{count}) or press Enter for the first: ")

    if choice.isdigit() and 1 <= int(choice) <= count:
        return sites["data"][int(choice) - 1]["id"]

    logging.warning("No valid choice made. Exiting.")
    raise SystemExit(0)


def get_unifi_clients(cfg: Config, limit, site_arg=None) -> list:
    """
    Fetches the connected clients from the UniFi integration API. Exits 1 on an error or an empty list.
    """
    if site_arg:
        logging.info(f"Using specified site ID: {site_arg}")
        site_id = site_arg
    else:
        logging.info("Fetching sites from UniFi API...")
        sites = xmit(cfg, "unifi", "sites")

        if not sites or len(sites.get("data", [])) == 0:
            logging.error("No sites found or unable to retrieve site data.")
            raise SystemExit(1)

        if len(sites.get("data", [])) > 1:
            site_id = select_site_id(sites)
        else:
            site_id = sites["data"][0]["id"]
            logging.info(f"One site found — using site ID: {site_id}")

    logging.info("Fetching clients from UniFi API...")
    client_list = xmit(cfg, "unifi", f"sites/{site_id}/clients", params={"limit": limit})

    if client_list and client_list.get("data"):
        total = client_list.get("totalCount")
        if isinstance(total, int) and total > len(client_list["data"]):
            logging.warning(f"UniFi reports {total} clients but returned {len(client_list['data'])} (raise --limit)")
        return client_list["data"]

    logging.error("No clients found or unable to retrieve client data.")
    raise SystemExit(1)


def get_unifi_user_records(cfg: Config) -> dict:
    """
    The classic API's known-clients list (alias + Local DNS Record), keyed by lower-case MAC.
    The integration API does not expose either field. Exits 1 on any error or an empty list:
    without it every curated name would silently fall back to 'none'.
    """
    resp = xmit(cfg, "unifi_legacy", f"api/s/{cfg.unifi_site_name}/rest/user")
    meta_ok = (resp or {}).get("meta", {}).get("rc") == "ok"
    data = (resp or {}).get("data")
    if not resp or not meta_ok or not isinstance(data, list) or not data:
        logging.error("Unable to read client names (alias / Local DNS Record) from UniFi. Nothing written.")
        raise SystemExit(1)
    return {normalize_mac(u["mac"]): u for u in data if u.get("mac")}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def plan_run(cfg: Config, clients: list, users: dict) -> list:
    """Pure: UniFi data in, one desired-state dict per in-scope client out."""
    plans = []
    seen_ips, seen_macs = set(), set()
    for client in clients:
        if "ipAddress" not in client or "macAddress" not in client:
            logging.debug(f"Client missing IP or MAC — skipping: {json.dumps(client)}")
            continue
        if not is_in_filter(cfg, client["ipAddress"]):
            logging.debug(f"Skipping {client['macAddress']} ({client['ipAddress']}) — filtered by subnet configuration")
            continue
        plan = build_desired(cfg, client, users.get(normalize_mac(client["macAddress"])))
        if plan["ip"] in seen_ips or plan["mac"] in seen_macs:
            logging.warning(f"Duplicate IP or MAC in UniFi client list ({plan['mac']} {plan['ip']}) — skipped")
            continue
        seen_ips.add(plan["ip"])
        seen_macs.add(plan["mac"])
        plans.append(plan)
    enforce_unique_names(plans)
    return plans


def process_client(cfg: Config, plan: dict, last_seen: str) -> str:
    name = plan["hostname"] or "(no forward name)"
    logging.info(f"Syncing {plan['mac']} ({plan['ip']}) -> {name} [{plan['reason']}]")
    return sync_client(cfg, plan, last_seen)


def main(cfg: Config, args) -> int:
    """Main execution flow. Returns the process exit code."""
    if args.dryrun:
        logging.info("Dry run mode — phpIPAM and UniFi are read, nothing is written.")

    # Everything UniFi says is read first: if it fails, phpIPAM is never touched.
    clients = get_unifi_clients(cfg, args.limit, args.site_id)
    users = get_unifi_user_records(cfg)
    plans = plan_run(cfg, clients, users)
    logging.info(f"Found {len(clients)} clients, {len(plans)} in scope.")
    if not plans:
        logging.error("No in-scope clients (check the subnet filter). Nothing written.")
        return 1

    try:
        ipam_lookup(cfg, "subnets/")  # phpIPAM must answer before the first write
    except IpamError as e:
        logging.error(f"phpIPAM is not usable: {e}. Nothing written.")
        return 1

    last_seen = utcnow().strftime("%Y-%m-%d %H:%M:%S")
    outcomes: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_client, cfg, p, last_seen): p for p in plans}
        for future in as_completed(futures):
            try:
                outcome = future.result()
            except Exception as e:
                logging.error(f"Unhandled error processing {futures[future]['mac']}: {e}")
                outcome = "error"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1

    no_name = sum(1 for p in plans if not p["hostname"])
    logging.info(f"Result: {dict(sorted(outcomes.items()))}; {no_name} of {len(plans)} without a forward name.")
    if outcomes.get("error"):
        logging.error(f"{outcomes['error']} client(s) failed.")
        return 1
    logging.info("Dry run complete." if args.dryrun else "Sync complete.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync UniFi clients to phpIPAM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Configuration precedence (lowest to highest):\n"
            f"  config file ({DEFAULT_CONFIG_PATH})\n"
            "  environment variables\n"
            "  CLI flags\n\n"
            "Required env vars:  UNIFI_API_KEY, IPAM_API_KEY\n"
            "Optional env vars:  UNIFI_URL, IPAM_BASE_URL, IPAM_APP_ID, UNIFI_LEGACY_URL,\n"
            "                    UNIFI_SITE_NAME, DNS_ZONE"
        ),
    )

    # Operational flags
    parser.add_argument("--limit", type=int, default=1000,
                        help="Maximum number of clients to fetch from UniFi (default: 1000).")
    parser.add_argument("--site-id", type=str, default=None, metavar="ID",
                        help="UniFi site ID to use. If omitted, available sites are listed for selection.")
    parser.add_argument("--dryrun", action="store_true",
                        help="Read UniFi and phpIPAM and print what would change; write nothing.")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of parallel workers for client processing (default: 4).")
    parser.add_argument("--dns-zone", type=str, default=None, metavar="ZONE",
                        help="Zone of the Default network (e.g. home.8none1.com). A Local DNS Record in this "
                             "zone is stored without it. Overrides DNS_ZONE and config dns.zone. "
                             "Without a zone, Local DNS Records are not used.")
    parser.add_argument("--unifi-site-name", type=str, default=None, metavar="NAME",
                        help="Site name for the classic UniFi API (default: default).")

    # Configuration sources
    parser.add_argument("--config", type=str, default=None, metavar="PATH",
                        help=f"Path to YAML config file (default: {DEFAULT_CONFIG_PATH}).")
    parser.add_argument("--unifi-url", type=str, default=None, metavar="URL",
                        help="UniFi controller base URL. Overrides UNIFI_URL env var and config file.")
    parser.add_argument("--ipam-base-url", type=str, default=None, metavar="URL",
                        help="phpIPAM base URL. Overrides IPAM_BASE_URL env var and config file.")
    parser.add_argument("--ipam-app-id", type=str, default=None, metavar="ID",
                        help="phpIPAM application ID. Overrides IPAM_APP_ID env var and config file.")
    parser.add_argument("--insecure", action="store_true",
                        help="Disable SSL certificate verification. Use for self-signed certificates.")
    parser.add_argument("--include-subnet", action="append", default=[], metavar="CIDR",
                        help="Only process clients whose IP falls within this subnet. "
                             "May be repeated. Mutually exclusive with --exclude-subnet.")
    parser.add_argument("--exclude-subnet", action="append", default=[], metavar="CIDR",
                        help="Skip clients whose IP falls within this subnet. "
                             "May be repeated. Mutually exclusive with --include-subnet.")

    # Logging flags
    parser.add_argument("--verbose", action="store_true", help="Enable debug-level output.")
    parser.add_argument("--quiet", action="store_true", help="Suppress all output except errors.")
    parser.add_argument("--log-file", type=str, default=None, metavar="PATH",
                        help="Write log output to this file in addition to stdout.")
    parser.add_argument("--version", action="version", version=f"unifi2ipam v{__version__}")
    return parser


def cli(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args)
    cfg = load_config(args)
    return main(cfg, args)


if __name__ == "__main__":
    raise SystemExit(cli())

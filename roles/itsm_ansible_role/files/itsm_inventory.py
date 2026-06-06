#!/usr/bin/env python3
"""
Ansible dynamic inventory — sync hosts from ITSM assets marked for external inventory.

Fetches GET /api/v1/assets?external_only=true (HTTP Basic auth). Assets must have
external_inventory=true in the ITSM UI or API (see itsm-app README).

Environment:
  ITSM_API_BASE_URL or ITSM_API_BASE   Base URL without trailing slash
  ITSM_API_USER                        Basic auth username
  ITSM_API_PASSWORD                    Basic auth password
  ITSM_VALIDATE_CERTS                  If "false"/"0"/"no", skip TLS verify (default: true)
  ITSM_INVENTORY_EXTERNAL_ONLY         If "false"/"0"/"no", include all assets (default: true)
  ITSM_INVENTORY_QUERY                 Optional ?q= filter (name/description search)

Usage:
  export ITSM_API_BASE_URL=http://127.0.0.1:8000 ITSM_API_USER=admin ITSM_API_PASSWORD=admin
  ./itsm_inventory.py --list
  ansible-playbook -i ./itsm_inventory.py site.yml
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Custom field keys checked (in order) for ansible_host when not set explicitly.
_ANSIBLE_HOST_CF_KEYS = ("ansible_host", "management_ip", "ip_address", "ip", "hostname")


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _base_url() -> str:
    url = (
        os.environ.get("ITSM_API_BASE_URL", "").strip()
        or os.environ.get("ITSM_API_BASE", "").strip()
    ).rstrip("/")
    if not url:
        print(
            "itsm_inventory.py: set ITSM_API_BASE_URL or ITSM_API_BASE",
            file=sys.stderr,
        )
        sys.exit(1)
    return url


def _credentials() -> tuple[str, str]:
    user = os.environ.get("ITSM_API_USER", "").strip()
    password = os.environ.get("ITSM_API_PASSWORD", "")
    if not user or not password:
        print(
            "itsm_inventory.py: set ITSM_API_USER and ITSM_API_PASSWORD",
            file=sys.stderr,
        )
        sys.exit(1)
    return user, password


def _sanitize_group(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]", "_", (name or "itsm_untyped").strip())
    slug = re.sub(r"_+", "_", slug).strip("_").lower()
    return slug or "itsm_untyped"


def _fetch_assets() -> list[dict[str, Any]]:
    base = _base_url()
    user, password = _credentials()
    params: dict[str, str] = {}
    if _env_bool("ITSM_INVENTORY_EXTERNAL_ONLY", default=True):
        params["external_only"] = "true"
    query = os.environ.get("ITSM_INVENTORY_QUERY", "").strip()
    if query:
        params["q"] = query
    url = f"{base}/api/v1/assets"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    token = urllib.request.base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    req.add_header("Authorization", f"Basic {token}")

    context = None
    if not _env_bool("ITSM_VALIDATE_CERTS", default=True):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=60, context=context) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"itsm_inventory.py: HTTP {exc.code} from {url}: {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"itsm_inventory.py: request failed: {exc.reason}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, list):
        print("itsm_inventory.py: expected JSON array from /api/v1/assets", file=sys.stderr)
        sys.exit(1)
    return data


def _hostvars(asset: dict[str, Any]) -> dict[str, Any]:
    name = asset.get("name", "")
    hostvars = {k: v for k, v in asset.items() if k != "name"}
    hostvars["itsm_asset_name"] = name
    if "id" in asset:
        hostvars["itsm_asset_id"] = asset["id"]

    custom = asset.get("custom_fields") or {}
    if isinstance(custom, dict):
        for key, val in custom.items():
            hostvars.setdefault(key, val)

    if "ansible_host" not in hostvars:
        for key in _ANSIBLE_HOST_CF_KEYS:
            val = custom.get(key) if isinstance(custom, dict) else None
            if val:
                hostvars["ansible_host"] = val
                break

    return hostvars


def build_inventory(assets: list[dict[str, Any]]) -> dict[str, Any]:
    inventory: dict[str, Any] = {
        "_meta": {"hostvars": {}},
        "itsm": {"hosts": []},
    }

    for asset in assets:
        name = (asset.get("name") or "").strip()
        if not name:
            continue

        inventory["itsm"]["hosts"].append(name)
        inventory["_meta"]["hostvars"][name] = _hostvars(asset)

        type_name = asset.get("asset_type_name") or "itsm_untyped"
        group = f"itsm_type_{_sanitize_group(str(type_name))}"
        inventory.setdefault(group, {"hosts": []})
        if name not in inventory[group]["hosts"]:
            inventory[group]["hosts"].append(name)

        env = (asset.get("custom_fields") or {}).get("environment")
        if env:
            env_group = f"itsm_env_{_sanitize_group(str(env))}"
            inventory.setdefault(env_group, {"hosts": []})
            if name not in inventory[env_group]["hosts"]:
                inventory[env_group]["hosts"].append(name)

    return inventory


def main() -> None:
    parser = argparse.ArgumentParser(description="ITSM Ansible dynamic inventory")
    parser.add_argument("--list", action="store_true", help="List inventory (default)")
    parser.add_argument("--host", help="Get variables for a single host")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON (debug)",
    )
    args = parser.parse_args()

    assets = _fetch_assets()
    inventory = build_inventory(assets)

    if args.host:
        payload = inventory["_meta"]["hostvars"].get(args.host, {})
    else:
        payload = inventory

    indent = 2 if args.pretty else None
    print(json.dumps(payload, indent=indent))


if __name__ == "__main__":
    main()

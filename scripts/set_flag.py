#!/usr/bin/env python3
"""Flip feature flags in KV without redeploying.

    python3 scripts/set_flag.py --base URL --admin-token T builds_enabled=false
    python3 scripts/set_flag.py --base URL --admin-token T clinic.bookings_enabled=false clinic.min_notice_minutes=120

Plain names go to `flags/global` (Forge); `clinic.`-prefixed names go to
`flags/clinic`. Values true/false become booleans, digits become integers.
With no assignments it prints the current flags.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Dict


def parse(value: str) -> Any:
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if low.lstrip("-").isdigit():
        return int(low)
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--admin-token", default="")
    ap.add_argument("assignments", nargs="*", help="name=value")
    a = ap.parse_args()
    body: Dict[str, Dict[str, Any]] = {"forge": {}, "clinic": {}}
    for item in a.assignments:
        if "=" not in item:
            ap.error(f"expected name=value, got {item!r}")
        name, _, value = item.partition("=")
        scope = "forge"
        if name.startswith("clinic."):
            scope, name = "clinic", name[len("clinic."):]
        body[scope][name.strip()] = parse(value)
    method = "POST" if a.assignments else "GET"
    data = json.dumps(body).encode() if method == "POST" else None
    headers = {"content-type": "application/json"}
    if a.admin_token:
        headers["authorization"] = f"Bearer {a.admin_token}"
    req = urllib.request.Request(a.base.rstrip("/") + "/ops/flags", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(json.dumps(json.loads(resp.read()), indent=2))
            return 0
    except urllib.error.HTTPError as err:
        print(f"HTTP {err.code}: {err.read().decode('utf-8', 'replace')[:300]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

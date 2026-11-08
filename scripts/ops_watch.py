#!/usr/bin/env python3
"""Watch a running forge-api: health, traffic, error rate, latency per route and
recent errors, every 5 seconds. Prints ALERT lines (with a terminal bell) when:

- /ops/health is unhealthy (any check failing, or > 20% errors over 5 minutes)
- the error rate in the window is above 20%
- p95 latency is above 1200 ms
- build_agent had any error

    python3 scripts/ops_watch.py --base https://forge-api-<org>.telnyxcompute.com --admin-token T
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Tuple


def get(url: str, token: str = "") -> Tuple[int, Any]:
    req = urllib.request.Request(url, headers={"authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"null")
        except ValueError:
            return err.code, None
    except Exception as exc:  # noqa: BLE001 - network errors are an alert, not a crash
        return 0, {"error": str(exc)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--admin-token", default="")
    ap.add_argument("--window", type=int, default=5, help="minutes of metrics to aggregate")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true", help="print one report and exit (exit 1 on alerts)")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    while True:
        alerts = []
        hstatus, health = get(f"{base}/ops/health")
        mstatus, m = get(f"{base}/ops/metrics?window={a.window}", a.admin_token)
        stamp = time.strftime("%H:%M:%S")
        if hstatus != 200 or not (health or {}).get("ok"):
            checks = (health or {}).get("checks") or {}
            bad = [k for k, v in checks.items() if not v.get("ok")] or [str((health or {}).get("error") or hstatus)]
            alerts.append(f"unhealthy ({', '.join(bad)})")
        print(f"\n[{stamp}] health={'ok' if not alerts else 'UNHEALTHY'}", end="")
        if mstatus == 200 and isinstance(m, dict):
            print(f"  requests={m['requests']}  errors={m['errors']}  error_rate={100 * m['error_rate']:.1f}%"
                  f"  p50={m['latency_p50_ms']}ms  p95={m['latency_p95_ms']}ms  (last {m['window_minutes']} min)")
            if m["requests"] >= 5 and m["error_rate"] > 0.2:
                alerts.append(f"error rate {100 * m['error_rate']:.0f}%")
            if m["latency_p95_ms"] > 1200:
                alerts.append(f"p95 latency {m['latency_p95_ms']} ms")
            routes = sorted(m["by_route"].items(), key=lambda kv: -kv[1]["count"])
            for route, r in routes:
                print(f"    {route:<18} count={r['count']:<5} errors={r['errors']:<4} degraded={r['degraded']}")
            if m["by_route"].get("build_agent", {}).get("errors"):
                alerts.append("build_agent errors")
            for e in m["recent_errors"][:5]:
                when = time.strftime("%H:%M:%S", time.localtime(e["at"] / 1000))
                print(f"    ! {when} {e['route']} {e['outcome']} trace={e['trace_id']} {e.get('error') or ''}")
        else:
            print(f"  metrics unavailable (HTTP {mstatus}; pass --admin-token?)")
        for alert in alerts:
            print(f"\aALERT {stamp} {alert}")
        if a.once:
            return 1 if alerts else 0
        time.sleep(a.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)

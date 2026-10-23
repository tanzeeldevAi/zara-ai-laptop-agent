#!/usr/bin/env python3
"""Seller tool: revoke a CodeHubb Assistant license.

With a license server configured (LICENSE_SERVER_URL + LICENSE_ADMIN_TOKEN
in the environment) this calls POST /admin/revoke. Without one, it prints
the exact SQL to run against the server's licenses.db by hand — revocation
is a server-side fact; a key already activated offline keeps working until
its copy of the app next validates, which is the documented grace model.
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import licensing  # noqa: E402


def license_id_of(key, license_id):
    if license_id:
        return license_id
    try:
        payload = json.loads(licensing._b64d(str(key).split(".")[1]))
        return payload["license_id"]
    except Exception:  # noqa: BLE001
        return ""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", default="", help="the full CA1... license key")
    parser.add_argument("--license-id", default="", help="or just the license id")
    args = parser.parse_args(argv)

    license_id = license_id_of(args.key, args.license_id)
    if not license_id:
        print("Give --license-id or a readable --key.", file=sys.stderr)
        return 2

    server = (os.getenv("LICENSE_SERVER_URL") or "").rstrip("/")
    token = os.getenv("LICENSE_ADMIN_TOKEN") or ""
    if server and token:
        body = json.dumps({"license_id": license_id}).encode("utf-8")
        request = urllib.request.Request(
            f"{server}/admin/revoke", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Admin-Token": token},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            print(response.read().decode("utf-8"))
        return 0

    print("No license server configured (LICENSE_SERVER_URL / LICENSE_ADMIN_TOKEN).")
    print("Run this against the server's licenses.db instead:")
    print(f"  UPDATE licenses SET status = 'revoked' WHERE license_id = '{license_id}';")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

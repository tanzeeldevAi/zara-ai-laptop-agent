#!/usr/bin/env python3
"""Seller tool: mint CodeHubb Assistant license keys by hand.

This is the manual sales route (JazzCash / bank transfer / cash): the
buyer pays, the seller runs this, and sends the printed key to the buyer.

    # one-time: create the seller keypair (private key OUTSIDE the repo)
    python tools/issue_license.py --init --private-key-out ~/seller-keys/license_private.pem
    # -> prints the PUBLIC key: put it in the release build's .env as
    #    LICENSE_PUBLIC_KEY and in the license server's environment.

    # issue keys (private key from env LICENSE_PRIVATE_KEY or a file)
    LICENSE_PRIVATE_KEY_PATH=~/seller-keys/license_private.pem \
        python tools/issue_license.py --email buyer@example.com --plan pro \
            --days 365 --machines 2
    # --lifetime instead of --days for a never-expiring key.

The private key is read from, in order: --private-key PATH,
$LICENSE_PRIVATE_KEY_PATH, $LICENSE_PRIVATE_KEY (PEM or base64 text).
It is NEVER written into the repo: --init refuses any output path inside
the repository tree, and .gitignore covers *.pem / license_private*.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import licensing  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_private_key(args):
    if args.private_key:
        text = Path(args.private_key).read_text(encoding="utf-8")
    elif os.getenv("LICENSE_PRIVATE_KEY_PATH"):
        text = Path(os.environ["LICENSE_PRIVATE_KEY_PATH"]).read_text(encoding="utf-8")
    elif os.getenv("LICENSE_PRIVATE_KEY"):
        text = os.environ["LICENSE_PRIVATE_KEY"]
    else:
        return None
    return licensing.load_private_key(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true",
                        help="generate a new seller keypair and exit")
    parser.add_argument("--private-key-out",
                        help="where --init writes the private key (must be OUTSIDE the repo)")
    parser.add_argument("--private-key", help="private key PEM file (issue mode)")
    parser.add_argument("--email", default="", help="buyer email")
    parser.add_argument("--plan", choices=licensing.PLANS, default="personal")
    parser.add_argument("--days", type=int, default=None,
                        help="license length in days (omit with --lifetime)")
    parser.add_argument("--lifetime", action="store_true",
                        help="the key never expires")
    parser.add_argument("--machines", type=int, default=None,
                        help="machine limit (default: 1 personal / 2 pro)")
    parser.add_argument("--features", default="",
                        help="comma-separated feature flags")
    args = parser.parse_args(argv)

    if args.init:
        if not args.private_key_out:
            parser.error("--init needs --private-key-out PATH")
        out = Path(args.private_key_out).expanduser().resolve()
        if out.is_relative_to(REPO_ROOT):
            print(
                "REFUSED: the private key must live OUTSIDE the repository "
                f"tree ({REPO_ROOT}). Pick e.g. ~/seller-keys/license_private.pem",
                file=sys.stderr,
            )
            return 2
        if out.exists():
            print(f"REFUSED: {out} already exists — refusing to overwrite a key.", file=sys.stderr)
            return 2
        private_pem, public_b64 = licensing.generate_keypair()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(private_pem, encoding="utf-8")
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
        print(f"Private key written to: {out}  (keep it secret, back it up)")
        print("Public key — set as LICENSE_PUBLIC_KEY in the release build")
        print("and in the license server's environment:")
        print(public_b64)
        return 0

    private = _load_private_key(args)
    if private is None:
        print(
            "No usable private key. Pass --private-key PATH or set "
            "LICENSE_PRIVATE_KEY_PATH / LICENSE_PRIVATE_KEY.",
            file=sys.stderr,
        )
        return 2
    machines = args.machines
    if machines is None:
        machines = 2 if args.plan == "pro" else 1
    days = None if args.lifetime else args.days
    payload = licensing.make_payload(
        args.email,
        plan=args.plan,
        machines_limit=machines,
        days=days,
        features=[f.strip() for f in args.features.split(",") if f.strip()],
    )
    key = licensing.sign_license(private, payload)
    print(f"License for {args.email or '(no email)'} — plan {args.plan}, "
          f"machines {machines}, "
          f"{'lifetime' if days is None else str(days) + ' days'}:", file=sys.stderr)
    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

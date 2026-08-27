#!/usr/bin/env python3
"""Upload the FEC draft newsletters to Ghost as DRAFTS.

Deliberately does NOT reuse lobby-landscapes' upload_all_docs_to_ghost: that
function scans final_export, uploads matplotlib figures, generates ledes via
Azure, and attaches the LIVE channel nav tags. Attaching those would place these
validation drafts on the public /tag/ pages.

Safety choices baked in:
  * status is always "draft" -- never published, so Ghost sends no email.
  * Only INTERNAL tags (Ghost treats a leading "#" as internal/private), so the
    posts cannot surface on any public tag page.
  * Titles are prefixed so nobody mistakes them for a real edition.

Credentials come from the lobby-landscapes-us .env, which already holds them.
"""
from __future__ import annotations
import argparse, datetime, json, os, pathlib, sys

import jwt, requests
from dotenv import load_dotenv

LOBBY = pathlib.Path.home() / "Projects" / "lobby-landscapes-us"
SRC = pathlib.Path(__file__).resolve().parents[1] / "out" / "ghost"

POSTS = [
    ("Finance_and_Insurance_SEND.html",
     "[DRAFT] FEC pipeline — Finance & Insurance, money SENT (2026 Q2)"),
    ("Finance_and_Insurance_RECEIVE.html",
     "[DRAFT] FEC pipeline — Finance & Insurance, money RECEIVED (2026 Q2)"),
]
# Leading "#" makes a Ghost tag internal: usable for filtering in admin, never
# rendered publicly and never given a public tag page.
TAGS = [{"name": "#fec-pipeline-draft"}, {"name": "#2026_Q2"}, {"name": "#no-hero"}]


def token(admin_key: str) -> str:
    key_id, secret = admin_key.split(":")
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    return jwt.encode(
        {"iat": now, "exp": now + 5 * 60, "aud": "/v5/admin/"},
        bytes.fromhex(secret), algorithm="HS256",
        headers={"kid": key_id, "alg": "HS256", "typ": "JWT"},
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="show exactly what would be posted; make no request")
    args = ap.parse_args()

    load_dotenv(LOBBY / ".env")
    domain = (os.getenv("GHOST_ADMIN_DOMAIN") or "").rstrip("/")
    admin_key = os.getenv("GHOST_ADMIN_API_KEY") or ""
    if not domain or not admin_key:
        print("Missing GHOST_ADMIN_DOMAIN / GHOST_ADMIN_API_KEY", file=sys.stderr)
        return 1

    print(f"Ghost site : {domain}")
    print(f"Status     : draft (never published — Ghost sends no email)")
    print(f"Tags       : {', '.join(t['name'] for t in TAGS)}  (all internal)\n")

    for fname, title in POSTS:
        path = SRC / fname
        if not path.exists():
            print(f"  MISSING {path}", file=sys.stderr)
            return 1
        body = path.read_text(encoding="utf-8")
        print(f"  {title}\n     from {path.name} ({len(body):,} bytes)")
        if args.dry_run:
            continue
        resp = requests.post(
            f"{domain}/ghost/api/admin/posts/?source=html",
            json={"posts": [{"title": title, "html": body,
                             "tags": TAGS, "status": "draft"}]},
            headers={"Authorization": f"Ghost {token(admin_key)}",
                     "Accept": "application/json",
                     "Content-Type": "application/json"},
            timeout=60,
        )
        if resp.status_code >= 300:
            print(f"     FAILED {resp.status_code}: {resp.text[:400]}", file=sys.stderr)
            return 1
        post = resp.json()["posts"][0]
        print(f"     -> draft created  id={post['id']}")
        print(f"        edit: {domain}/ghost/#/editor/post/{post['id']}")

    if args.dry_run:
        print("\nDry run — nothing was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

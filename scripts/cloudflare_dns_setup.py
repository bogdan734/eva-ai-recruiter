"""Configure Cloudflare DNS for kozyrtrans-ai.com — call after Hetzner VPS up.

Usage:
    python3 scripts/cloudflare_dns_setup.py <VPS_IP>

Creates A records:
    api.kozyrtrans-ai.com       → VPS_IP  (proxied)
    webhooks.kozyrtrans-ai.com  → VPS_IP  (proxied)
    bot.kozyrtrans-ai.com       → VPS_IP  (proxied)

Idempotent — re-running updates existing records instead of duplicating.
"""
from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import certifi


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in (Path(__file__).resolve().parent.parent / ".env").read_text().splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if "#" in v:
            v = v.split("#", 1)[0]
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
TOKEN = ENV["CLOUDFLARE_API_TOKEN"]
ZONE_ID = ENV["CLOUDFLARE_ZONE_ID"]
DOMAIN = ENV.get("CLOUDFLARE_DOMAIN", "kozyrtrans-ai.com")
CTX = ssl.create_default_context(cafile=certifi.where())

SUBDOMAINS = ["api", "webhooks", "bot"]


def api(method: str, path: str, body: dict | None = None) -> dict:
    url = f"https://api.cloudflare.com/client/v4{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body else None,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        r = urllib.request.urlopen(req, timeout=15, context=CTX)
        return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"errors": [{"http": e.code, "body": e.read().decode()[:400]}]}


def upsert_a_record(subdomain: str, ip: str) -> None:
    fqdn = f"{subdomain}.{DOMAIN}"
    existing = api("GET", f"/zones/{ZONE_ID}/dns_records?type=A&name={fqdn}")
    items = existing.get("result") or []
    body = {
        "type": "A",
        "name": fqdn,
        "content": ip,
        "proxied": True,
        "ttl": 1,  # auto when proxied
        "comment": "managed by ai-recruiter bootstrap",
    }
    if items:
        rec_id = items[0]["id"]
        res = api("PUT", f"/zones/{ZONE_ID}/dns_records/{rec_id}", body)
        if res.get("success"):
            print(f"  ✅ updated {fqdn} → {ip}")
        else:
            print(f"  ❌ update failed {fqdn}: {res}")
    else:
        res = api("POST", f"/zones/{ZONE_ID}/dns_records", body)
        if res.get("success"):
            print(f"  ✅ created {fqdn} → {ip}")
        else:
            print(f"  ❌ create failed {fqdn}: {res}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/cloudflare_dns_setup.py <VPS_IP>", file=sys.stderr)
        sys.exit(1)
    ip = sys.argv[1]
    print(f"Configuring DNS for {DOMAIN} → {ip}")
    for sub in SUBDOMAINS:
        upsert_a_record(sub, ip)
    print("\nDone. SSL via Cloudflare proxy is automatic (full strict).")


if __name__ == "__main__":
    main()

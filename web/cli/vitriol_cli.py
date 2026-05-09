"""Command-line client for the Vitriol HTTP API.

Usage:
  python -m web.cli.vitriol_cli login --url https://app.example --key vit_xxx_yyy
  python -m web.cli.vitriol_cli convert input.pdf --to .png --stone --password ...
  python -m web.cli.vitriol_cli jobs
  python -m web.cli.vitriol_cli cancel <job_id>
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path(os.path.expanduser("~/.vitriol/config.json"))


def load_config() -> dict:
    if not CONFIG.exists():
        return {}
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _request(method: str, url: str, *, headers=None, data=None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def cmd_login(args):
    cfg = load_config()
    if not args.url or not args.key:
        sys.exit("--url and --key are required")
    cfg["url"] = args.url.rstrip("/")
    cfg["key"] = args.key
    save_config(cfg)
    # Verify by hitting /me.
    code, body = _request("GET", f"{cfg['url']}/api/v1/me", headers={"Authorization": f"Bearer {cfg['key']}"})
    if code != 200:
        sys.exit(f"Login failed: {code} {body.decode(errors='replace')}")
    print(f"Logged in as {json.loads(body)['username']}")


def _auth(cfg):
    return {"Authorization": f"Bearer {cfg['key']}"}


def cmd_convert(args):
    cfg = load_config()
    if not cfg.get("url") or not cfg.get("key"):
        sys.exit("Run `login` first.")
    src = Path(args.file)
    if not src.exists():
        sys.exit(f"No such file: {src}")

    boundary = "----vitriolcli" + os.urandom(8).hex()
    parts: list[bytes] = []

    def add(name: str, value: str):
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode())
        parts.append(b"\r\n")

    add("dst_ext", args.to)
    add("stone", "true" if args.stone else "false")
    add("verify_round_trip", "true" if args.verify else "false")
    if args.password:
        add("password", args.password)
    if args.self_compile:
        add("self_compile_target", args.self_compile)
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="file"; filename="{src.name}"\r\n'.encode())
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    parts.append(src.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    code, resp = _request(
        "POST", f"{cfg['url']}/api/v1/convert",
        headers={**_auth(cfg), "Content-Type": f"multipart/form-data; boundary={boundary}"},
        data=body,
    )
    if code >= 400:
        sys.exit(f"Submit failed: {code} {resp.decode(errors='replace')}")
    job = json.loads(resp)
    job_id = job["id"]
    print(f"Submitted job {job_id}; polling…")

    while True:
        time.sleep(1)
        code, resp = _request("GET", f"{cfg['url']}/api/v1/jobs/{job_id}", headers=_auth(cfg))
        if code != 200:
            sys.exit(f"Poll failed: {code}")
        j = json.loads(resp)
        sys.stdout.write(f"\r[{j['progress']}%] {j['status']}    ")
        sys.stdout.flush()
        if j["status"] in ("done", "failed", "cancelled"):
            print()
            break

    if j["status"] != "done":
        sys.exit(f"Job ended: {j['status']} — {j.get('error') or ''}")

    out = Path(args.out or j["dst_filename"])
    code, content = _request("GET", f"{cfg['url']}/api/v1/jobs/{job_id}/result", headers=_auth(cfg))
    if code != 200:
        sys.exit(f"Download failed: {code}")
    out.write_bytes(content)
    print(f"Saved -> {out}")


def cmd_jobs(args):
    cfg = load_config()
    code, body = _request("GET", f"{cfg['url']}/api/v1/jobs", headers=_auth(cfg))
    if code != 200:
        sys.exit(f"List failed: {code} {body.decode(errors='replace')}")
    for j in json.loads(body):
        print(f"{j['id']:>5}  {j['status']:<10}  {j['src_filename']} -> {j['dst_filename']}  ({j['progress']}%)")


def cmd_cancel(args):
    cfg = load_config()
    code, body = _request("DELETE", f"{cfg['url']}/api/v1/jobs/{args.job_id}", headers=_auth(cfg))
    if code != 200:
        sys.exit(f"Cancel failed: {code} {body.decode(errors='replace')}")
    print(json.loads(body).get("message", "ok"))


def main() -> None:
    p = argparse.ArgumentParser("vitriol")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("login")
    pl.add_argument("--url", required=True)
    pl.add_argument("--key", required=True)
    pl.set_defaults(func=cmd_login)

    pc = sub.add_parser("convert")
    pc.add_argument("file")
    pc.add_argument("--to", required=True, help="Destination extension (e.g. .png)")
    pc.add_argument("--out", default=None)
    pc.add_argument("--stone", action="store_true")
    pc.add_argument("--verify", action="store_true")
    pc.add_argument("--self-compile", choices=["py", "exe"], default=None)
    pc.add_argument("--password", default=None)
    pc.set_defaults(func=cmd_convert)

    pj = sub.add_parser("jobs")
    pj.set_defaults(func=cmd_jobs)

    px = sub.add_parser("cancel")
    px.add_argument("job_id", type=int)
    px.set_defaults(func=cmd_cancel)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

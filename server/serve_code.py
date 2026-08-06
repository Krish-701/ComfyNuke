#!/usr/bin/env python3
"""
ComfyNuke code distribution server (read-only HTTP).

Serves the ComfyNuke repo tree so artist machines can load scripts without
SSH/Samba access to the Ubuntu host. ComfyUI stays on :8188; this is :6000.

Usage on Ubuntu (repo root):
  cd /home/radhakrishnan/Comfyui-Setup/ComfyNuke
  python3 server/serve_code.py --host 0.0.0.0 --port 6000

Artist Nuke (one line):
  exec(__import__('urllib.request').request.urlopen('http://192.168.91.13:6000/nuke/remote_bootstrap.py', timeout=60).read().decode('utf-8'))
"""

from __future__ import annotations

import argparse
import os
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


# Only serve these path prefixes (relative to repo root). Blocks random FS access.
ALLOWED_PREFIXES = (
    "nuke/",
    "client/",
    "docs/",
    "Edit_Image_v05.json",
    "Image_generation_v01.json",
    "video_minimax_h3_i2v.json",
    "studio_config.json",
    "studio_config.example.json",
    "README.md",
    "PLAYBOOK.md",
    ".gitignore",
)

# Never serve
BLOCKED_PARTS = (
    ".git",
    "__pycache__",
    ".env",
    "client/out",
    ".ssh",
)


class ComfyNukeHandler(SimpleHTTPRequestHandler):
    server_version = "ComfyNukeCode/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[code-server] %s - %s\n" % (self.address_string(), fmt % args))

    def translate_path(self, path: str) -> str:
        # Map URL path under repo root (self.directory set by partial)
        root = Path(self.directory).resolve()  # type: ignore[attr-defined]
        # strip query
        path = path.split("?", 1)[0].split("#", 1)[0]
        # url path like /nuke/launch.py
        rel = path.lstrip("/")
        if rel == "":
            # index: tiny help
            return str(root / ".code_server_index.html")

        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return str(root / ".forbidden")

        rel_posix = candidate.relative_to(root).as_posix()
        for bad in BLOCKED_PARTS:
            if bad in rel_posix.split("/") or rel_posix.startswith(bad):
                return str(root / ".forbidden")

        allowed = False
        for pref in ALLOWED_PREFIXES:
            if pref.endswith("/"):
                if rel_posix.startswith(pref) or rel_posix + "/" == pref:
                    allowed = True
                    break
            elif rel_posix == pref:
                allowed = True
                break
        if not allowed:
            return str(root / ".forbidden")

        return str(candidate)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html", "/health"):
            body = (
                b"ComfyNuke code server OK\n"
                b"ComfyUI API: use port 8188\n"
                b"Bootstrap: GET /nuke/remote_bootstrap.py\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        # 404 for blocked paths
        translated = self.translate_path(self.path)
        if translated.endswith(".forbidden") or not os.path.isfile(translated):
            if translated.endswith(".forbidden") or self.path not in ("/",):
                self.send_error(403 if translated.endswith(".forbidden") else 404)
                return
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self) -> None:
        self.send_error(405, "POST not allowed — code server is read-only")

    def do_PUT(self) -> None:
        self.send_error(405, "PUT not allowed — code server is read-only")

    def do_DELETE(self) -> None:
        self.send_error(405, "DELETE not allowed — code server is read-only")


def main() -> int:
    parser = argparse.ArgumentParser(description="ComfyNuke read-only code HTTP server")
    parser.add_argument(
        "--root",
        default="",
        help="ComfyNuke repo root (default: parent of server/)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=6000, help="Port (default 6000)")
    args = parser.parse_args()

    if args.root:
        root = Path(args.root).resolve()
    else:
        root = Path(__file__).resolve().parent.parent

    if not (root / "nuke" / "ComfyEdit.py").is_file():
        print("ERROR: not a ComfyNuke root (missing nuke/ComfyEdit.py): %s" % root, file=sys.stderr)
        return 1

    # placeholder for blocked translate
    (root / ".forbidden").write_text("forbidden\n", encoding="utf-8")

    handler = partial(ComfyNukeHandler, directory=str(root))
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print("=" * 60)
    print("ComfyNuke code server")
    print("  root:  %s" % root)
    print("  bind:  http://%s:%s/" % (args.host, args.port))
    print("  health: http://%s:%s/health" % (args.host if args.host != "0.0.0.0" else "127.0.0.1", args.port))
    print("  artist bootstrap:")
    print(
        "    exec(__import__('urllib.request').request.urlopen("
        "'http://<SERVER_IP>:%s/nuke/remote_bootstrap.py', timeout=60)"
        ".read().decode('utf-8'))" % args.port
    )
    print("  ComfyUI API remains on port 8188")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

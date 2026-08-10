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

Version endpoints (for Nuke bootstrap update checks):
  GET /health
  GET /version          → plain text version id (content hash)
  GET /manifest.json    → files + sha256 so clients replace stale cache
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List


# Only serve these path prefixes (relative to repo root). Blocks random FS access.
ALLOWED_PREFIXES = (
    "nuke/",
    "client/",
    "docs/",
    "Edit_Image_v06.json",
    "Edit_Image_v05.json",  # legacy allow if still on disk
    "Image_generation_v01.json",
    "video_minimax_h3_i2v.json",
    "studio_config.json",
    "studio_config.example.json",
    "VERSION",
    "README.md",
    "PLAYBOOK.md",
    ".gitignore",
)

# Files artists must stay in sync with (workflows + Nuke client code).
# Keep in lockstep with nuke/remote_bootstrap.py _SYNC_FILES.
SYNC_FILES = (
    "nuke/ComfyEdit.py",
    "nuke/launch.py",
    "client/comfy_client.py",
    "Edit_Image_v06.json",
    "Image_generation_v01.json",
    "video_minimax_h3_i2v.json",
    "studio_config.json",
    "studio_config.example.json",
)

# Never serve
BLOCKED_PARTS = (
    ".git",
    "__pycache__",
    ".env",
    "client/out",
    ".ssh",
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def build_manifest(root: Path) -> Dict[str, Any]:
    """
    Build a content-addressed manifest of code + workflows.
    version = short hash of sorted (path, sha256) pairs so any file change bumps it.
    """
    files: List[Dict[str, Any]] = []
    fingerprint_parts: List[str] = []
    for rel in SYNC_FILES:
        p = root / rel
        entry: Dict[str, Any] = {"path": rel, "optional": rel == "studio_config.json"}
        if p.is_file():
            digest = _sha256_file(p)
            st = p.stat()
            entry.update(
                {
                    "sha256": digest,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "present": True,
                }
            )
            fingerprint_parts.append("%s:%s" % (rel, digest))
        else:
            entry.update(
                {
                    "sha256": None,
                    "size": 0,
                    "mtime": 0,
                    "present": False,
                }
            )
            fingerprint_parts.append("%s:missing" % rel)
        files.append(entry)

    raw = "\n".join(fingerprint_parts).encode("utf-8")
    full = hashlib.sha256(raw).hexdigest()
    version = full[:12]
    # Optional human label from VERSION file (first non-empty line)
    label = ""
    ver_file = root / "VERSION"
    if ver_file.is_file():
        try:
            for line in ver_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    label = line
                    break
        except Exception:
            label = ""

    return {
        "name": "ComfyNuke",
        "version": version,
        "label": label or version,
        "generated_at": int(time.time()),
        "files": files,
    }


class ComfyNukeHandler(SimpleHTTPRequestHandler):
    server_version = "ComfyNukeCode/1.1"
    # set by main() via partial / attribute on class
    comfynuke_root: Path = Path(".")

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[code-server] %s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self) -> None:
        # Never let artist machines / proxies cache scripts or workflows.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        SimpleHTTPRequestHandler.end_headers(self)

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        path_only = self.path.split("?", 1)[0].split("#", 1)[0]

        if path_only in ("/", "/index.html", "/health"):
            try:
                manifest = build_manifest(Path(self.directory).resolve())  # type: ignore[attr-defined]
                ver = manifest.get("version", "?")
                label = manifest.get("label", ver)
            except Exception:
                ver, label = "?", "?"
            body = (
                "ComfyNuke code server OK\n"
                "version: %s\n"
                "label: %s\n"
                "ComfyUI API: use port 8188\n"
                "Bootstrap: GET /nuke/remote_bootstrap.py\n"
                "Manifest: GET /manifest.json\n"
                "Version:  GET /version\n"
            ) % (ver, label)
            self._send_bytes(body.encode("utf-8"), "text/plain; charset=utf-8")
            return

        if path_only in ("/version", "/version.txt"):
            try:
                manifest = build_manifest(Path(self.directory).resolve())  # type: ignore[attr-defined]
                body = ("%s\n" % manifest["version"]).encode("utf-8")
            except Exception as e:
                body = ("error: %s\n" % e).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._send_bytes(body, "text/plain; charset=utf-8")
            return

        if path_only in ("/manifest.json", "/manifest"):
            try:
                manifest = build_manifest(Path(self.directory).resolve())  # type: ignore[attr-defined]
                body = json.dumps(manifest, indent=2).encode("utf-8")
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._send_bytes(body, "application/json; charset=utf-8")
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

    try:
        manifest = build_manifest(root)
        pkg_ver = "%s (%s)" % (manifest["version"], manifest.get("label"))
    except Exception as e:
        pkg_ver = "(could not build: %s)" % e

    handler = partial(ComfyNukeHandler, directory=str(root))
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print("=" * 60)
    print("ComfyNuke code server")
    print("  root:  %s" % root)
    print("  package: %s" % pkg_ver)
    print("  bind:  http://%s:%s/" % (args.host, args.port))
    print("  health: http://%s:%s/health" % (args.host if args.host != "0.0.0.0" else "127.0.0.1", args.port))
    print("  version: http://%s:%s/version" % (args.host if args.host != "0.0.0.0" else "127.0.0.1", args.port))
    print("  manifest: http://%s:%s/manifest.json" % (args.host if args.host != "0.0.0.0" else "127.0.0.1", args.port))
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

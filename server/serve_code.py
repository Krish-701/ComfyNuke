#!/usr/bin/env python3
"""
ComfyNuke code distribution server (HTTP :8600).

- Serves scripts + workflows for Nuke artists
- /admin  — browser UI (login) to enable/disable LAN IPs
- /comfyui/* — optional reverse proxy to ComfyUI :8188 with same IP gate

Usage on Ubuntu (repo root):
  cd /home/radhakrishnan/Comfyui-Setup/ComfyNuke
  python3 server/serve_code.py --host 0.0.0.0 --port 8600

Artist Nuke:
  exec(__import__('urllib.request').request.urlopen(
    'http://192.168.91.13:8600/nuke/remote_bootstrap.py', timeout=60
  ).read().decode('utf-8'))

Admin:
  http://192.168.91.13:8600/admin
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

# Allow `python3 server/serve_code.py` from repo root
_SERVER_DIR = Path(__file__).resolve().parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from access_control import AccessControl  # noqa: E402
from usage_log import (  # noqa: E402
    UsageLog,
    classify_comfy_path,
    parse_history_completion,
    parse_prompt_id_from_queue_response,
    EVENT_ACCESS_DENIED,
    EVENT_API,
    EVENT_BOOTSTRAP,
    EVENT_DOWNLOAD,
    EVENT_UPLOAD,
)


# Only serve these path prefixes (relative to repo root). Blocks random FS access.
ALLOWED_PREFIXES = (
    "nuke/",
    "client/",
    "docs/",
    "Edit_Image_v08.json",
    "Edit_Image_v07.json",
    "Edit_Image_v06.json",
    "Edit_Image_v05.json",
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
    "Edit_Image_v08.json",
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
    "server/access_control.json",
    "access_control.json",
)

SESSION_COOKIE = "comfynuke_admin"


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
    server_version = "ComfyNukeCode/1.3"
    # Injected in main()
    access: AccessControl
    usage: UsageLog
    comfy_upstream: str = "http://127.0.0.1:8188"
    admin_html_path: Path = Path("server/admin_ui.html")

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[code-server] %s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self) -> None:
        self.send_header(
            "Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"
        )
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        SimpleHTTPRequestHandler.end_headers(self)

    def _client_ip(self) -> str:
        # Always use TCP peer address. Do NOT trust X-Forwarded-For (artists
        # could spoof it and bypass the allowlist).
        return (self.client_address[0] if self.client_address else "") or ""

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: int = 200,
        extra_headers: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: Any, status: int = 200, extra_headers=None) -> None:
        body = json.dumps(obj).encode("utf-8")
        self._send_bytes(
            body, "application/json; charset=utf-8", status=status, extra_headers=extra_headers
        )

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _session_token(self) -> Optional[str]:
        raw = self.headers.get("Cookie") or ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        if not morsel:
            return None
        return morsel.value

    def _is_admin(self) -> bool:
        """Authenticated session (any role)."""
        return self.access.session_ok(self._session_token())

    def _session_user(self) -> Optional[Dict[str, Any]]:
        return self.access.session_info(self._session_token())

    def _require_perm(self, perm: str) -> bool:
        """True if session has permission; else sends 401/403 JSON and returns False."""
        info = self._session_user()
        if not info:
            self._send_json({"error": "Not authenticated"}, status=401)
            return False
        if not self.access.session_has(self._session_token(), perm):
            self._send_json(
                {
                    "error": "Permission denied (role=%s needs %s)"
                    % (info.get("role"), perm),
                    "role": info.get("role"),
                    "permission": perm,
                },
                status=403,
            )
            return False
        return True

    def _identity(self) -> Dict[str, str]:
        ip = self._client_ip()
        try:
            self.access.reload()
        except Exception:
            pass
        m = self.access.lookup_machine(ip)
        m["ip"] = m.get("ip") or ip
        return m

    def _require_artist_ip(self) -> bool:
        """Return True if request may continue; else already sent 403.

        Reloads ACL from disk each check so enable/disable in the admin UI
        applies immediately to the next upload/queue (no service restart).
        """
        try:
            self.access.reload()
        except Exception:
            pass
        ip = self._client_ip()
        ok, reason = self.access.client_allowed(ip)
        if ok:
            return True
        ident = self.access.lookup_machine(ip)
        try:
            self.usage.log(
                event=EVENT_ACCESS_DENIED,
                ip=ip,
                machine_id=ident.get("id") or "",
                label=ident.get("label") or "",
                group=ident.get("group") or "",
                method=self.command,
                path=urlparse(self.path).path,
                status=403,
                detail=reason,
            )
        except Exception:
            pass
        body = (
            "ACCESS DENIED for IP %s (%s).\n"
            "Your machine is disabled or not on the allowlist.\n"
            "Admin: http://SERVER:8600/admin → enable this IP "
            "(Access Control master switch must be ON).\n"
            "ComfyUI jobs must use http://SERVER:8600/comfyui (not :8188).\n"
        ) % (ip, reason)
        self._send_bytes(body.encode("utf-8"), "text/plain; charset=utf-8", status=403)
        return False

    def translate_path(self, path: str) -> str:
        root = Path(self.directory).resolve()  # type: ignore[attr-defined]
        path = path.split("?", 1)[0].split("#", 1)[0]
        rel = path.lstrip("/")
        if rel == "":
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

    # ------------------------------------------------------------------ admin / operator portal
    def _handle_admin_get(self, path_only: str) -> bool:
        # Same login UI for /admin, /operator, /login (role decides features after sign-in)
        if path_only in (
            "/admin",
            "/admin/",
            "/operator",
            "/operator/",
            "/login",
            "/login/",
        ):
            html_path = self.admin_html_path
            if not html_path.is_file():
                self._send_bytes(b"admin UI missing\n", "text/plain", status=500)
                return True
            body = html_path.read_bytes()
            self._send_bytes(body, "text/html; charset=utf-8")
            return True

        if path_only == "/admin/api/state":
            st = self.access.snapshot()
            sess = self._session_user()
            st["authenticated"] = bool(sess)
            st["client_ip"] = self._client_ip()
            st["comfy_proxy"] = "http://%s/comfyui" % (
                self.headers.get("Host") or "SERVER:8600"
            )
            st["acl_enabled"] = st.get("enabled")
            if sess:
                st["user"] = {
                    "username": sess.get("username"),
                    "display_name": sess.get("display_name"),
                    "role": sess.get("role"),
                    "permissions": sess.get("permissions") or [],
                    "user_id": sess.get("user_id"),
                }
            else:
                st["user"] = None
            # hide list details if not logged in
            if not st["authenticated"]:
                st["ips"] = []
                st["groups"] = []
            elif not self.access.session_has(self._session_token(), "machines.view"):
                st["ips"] = []
            self._send_json(st)
            return True

        if path_only == "/admin/api/users":
            if not self._require_perm("users.manage"):
                return True
            self._send_json({"ok": True, "users": self.access.list_users()})
            return True

        # ---- usage logs (auth required) ----
        if path_only in (
            "/admin/api/logs",
            "/admin/api/logs/summary",
            "/admin/api/logs/export.csv",
            "/admin/api/logs/export_summary.csv",
        ):
            need = "logs.export" if "export" in path_only else "logs.view"
            if not self._require_perm(need):
                return True
            qs = parse_qs(urlparse(self.path).query)
            def _one(key: str, default: str = "") -> str:
                v = qs.get(key) or [default]
                return str(v[0] if v else default)

            def _ts(key: str) -> Optional[float]:
                raw = _one(key, "")
                if not raw:
                    return None
                try:
                    return float(raw)
                except Exception:
                    # ISO date YYYY-MM-DD
                    try:
                        return time.mktime(time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S"))
                    except Exception:
                        try:
                            return time.mktime(time.strptime(raw[:10], "%Y-%m-%d"))
                        except Exception:
                            return None

            since = _ts("since") or _ts("from")
            until = _ts("until") or _ts("to")
            # default until end of day if date only
            ip = _one("ip")
            label = _one("label")
            group = _one("group")
            event = _one("event")
            q = _one("q")
            try:
                limit = int(_one("limit", "500"))
            except Exception:
                limit = 500

            if path_only == "/admin/api/logs/summary":
                self._send_json(self.usage.summary(since=since, until=until))
                return True

            if path_only == "/admin/api/logs":
                rows = self.usage.iter_records(
                    since=since,
                    until=until,
                    ip=ip,
                    label=label,
                    group=group,
                    event=event,
                    q=q,
                    limit=limit,
                )
                self._send_json({"ok": True, "count": len(rows), "logs": rows})
                return True

            if path_only == "/admin/api/logs/export.csv":
                rows = self.usage.iter_records(
                    since=since,
                    until=until,
                    ip=ip,
                    label=label,
                    group=group,
                    event=event,
                    q=q,
                    limit=min(limit, 50_000),
                    reverse=False,
                )
                csv_text = self.usage.to_csv(rows, kind="events")
                body = csv_text.encode("utf-8")
                self._send_bytes(
                    body,
                    "text/csv; charset=utf-8",
                    extra_headers=[
                        (
                            "Content-Disposition",
                            'attachment; filename="pixedit_usage_events.csv"',
                        )
                    ],
                )
                return True

            if path_only == "/admin/api/logs/export_summary.csv":
                summ = self.usage.summary(since=since, until=until)
                csv_text = self.usage.to_csv(summ.get("users") or [], kind="summary")
                body = csv_text.encode("utf-8")
                self._send_bytes(
                    body,
                    "text/csv; charset=utf-8",
                    extra_headers=[
                        (
                            "Content-Disposition",
                            'attachment; filename="pixedit_usage_summary.csv"',
                        )
                    ],
                )
                return True

        return False

    def _handle_admin_post(self, path_only: str) -> bool:
        if not path_only.startswith("/admin/api/"):
            return False

        data = self._read_json_body()

        if path_only == "/admin/api/login":
            user = str(data.get("username") or "")
            pw = str(data.get("password") or "")
            pub = self.access.authenticate(user, pw)
            if not pub:
                try:
                    self.usage.log(
                        event="admin_login_fail",
                        ip=self._client_ip(),
                        detail="bad_credentials",
                        status=401,
                    )
                except Exception:
                    pass
                self._send_json({"error": "Invalid username or password"}, status=401)
                return True
            token = self.access.create_session(
                username=str(pub.get("username") or user),
                role=str(pub.get("role") or "viewer"),
            )
            cookie = "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (
                SESSION_COOKIE,
                token,
                12 * 3600,
            )
            try:
                self.usage.log(
                    event="admin_login",
                    ip=self._client_ip(),
                    label=str(pub.get("username") or user),
                    detail="role=%s" % pub.get("role"),
                    status=200,
                )
            except Exception:
                pass
            self._send_json(
                {"ok": True, "user": pub},
                extra_headers=[("Set-Cookie", cookie)],
            )
            return True

        if path_only == "/admin/api/logout":
            self.access.destroy_session(self._session_token())
            cookie = "%s=; Path=/; HttpOnly; Max-Age=0" % SESSION_COOKIE
            self._send_json({"ok": True}, extra_headers=[("Set-Cookie", cookie)])
            return True

        # All other admin APIs need session + role permission
        if not self._is_admin():
            self._send_json({"error": "Not authenticated"}, status=401)
            return True

        # map path -> required permission
        perm_map = {
            "/admin/api/set_enabled": "acl.toggle",
            "/admin/api/upsert_ip": "machines.edit",
            "/admin/api/update_entry": "machines.edit",
            "/admin/api/set_ip_enabled": "machines.edit",
            "/admin/api/set_group_enabled": "machines.edit",
            "/admin/api/rename_group": "machines.edit",
            "/admin/api/add_group": "machines.edit",
            "/admin/api/remove_group": "machines.edit",
            "/admin/api/remove_ip": "machines.edit",
            "/admin/api/change_password": "dashboard.view",  # self password
            "/admin/api/users/create": "users.manage",
            "/admin/api/users/update": "users.manage",
            "/admin/api/users/delete": "users.manage",
        }

        try:
            need = perm_map.get(path_only)
            if need and not self._require_perm(need):
                return True

            if path_only == "/admin/api/set_enabled":
                self.access.set_enabled(bool(data.get("enabled")))
                self._send_json({"ok": True, "enabled": bool(data.get("enabled"))})
                return True
            if path_only == "/admin/api/upsert_ip":
                entry = self.access.upsert_ip(
                    str(data.get("ip") or ""),
                    label=str(data.get("label") or ""),
                    enabled=bool(data.get("enabled", True)),
                    group=str(data.get("group") or ""),
                    entry_id=str(data.get("id") or "") or None,
                )
                self._send_json({"ok": True, "entry": entry})
                return True
            if path_only == "/admin/api/update_entry":
                entry = self.access.update_entry(
                    ip=str(data.get("ip") or "") or None,
                    entry_id=str(data.get("id") or "") or None,
                    new_ip=data.get("new_ip"),
                    label=data.get("label"),
                    group=data.get("group"),
                    enabled=data.get("enabled") if "enabled" in data else None,
                )
                self._send_json({"ok": True, "entry": entry})
                return True
            if path_only == "/admin/api/set_ip_enabled":
                ok = self.access.set_ip_enabled(
                    str(data.get("ip") or ""), bool(data.get("enabled"))
                )
                if not ok:
                    self._send_json({"error": "IP not found"}, status=404)
                    return True
                self._send_json({"ok": True})
                return True
            if path_only == "/admin/api/set_group_enabled":
                n = self.access.set_group_enabled(
                    str(data.get("group") or ""), bool(data.get("enabled"))
                )
                self._send_json({"ok": True, "updated": n})
                return True
            if path_only == "/admin/api/rename_group":
                n = self.access.rename_group(
                    str(data.get("old_name") or data.get("old") or ""),
                    str(data.get("new_name") or data.get("new") or ""),
                )
                self._send_json({"ok": True, "updated": n})
                return True
            if path_only == "/admin/api/add_group":
                g = self.access.add_group(str(data.get("name") or data.get("group") or ""))
                self._send_json({"ok": True, "group": g})
                return True
            if path_only == "/admin/api/remove_group":
                n = self.access.remove_group(
                    str(data.get("name") or data.get("group") or ""),
                    reassign_to=str(data.get("reassign_to") or "Ungrouped"),
                )
                self._send_json({"ok": True, "moved": n})
                return True
            if path_only == "/admin/api/remove_ip":
                ok = self.access.remove_ip(
                    ip=str(data.get("ip") or ""),
                    entry_id=str(data.get("id") or ""),
                )
                if not ok:
                    self._send_json({"error": "IP not found"}, status=404)
                    return True
                self._send_json({"ok": True})
                return True
            if path_only == "/admin/api/change_password":
                # users may only change their own password unless admin
                sess = self._session_user() or {}
                target = str(data.get("username") or sess.get("username") or "")
                if (
                    target != sess.get("username")
                    and not self.access.session_has(self._session_token(), "users.manage")
                ):
                    self._send_json({"error": "can only change your own password"}, status=403)
                    return True
                ok = self.access.change_password(
                    target,
                    str(data.get("old_password") or ""),
                    str(data.get("new_password") or ""),
                )
                if not ok:
                    self._send_json({"error": "current password wrong"}, status=400)
                    return True
                self._send_json({"ok": True})
                return True
            if path_only == "/admin/api/users/create":
                u = self.access.create_user(
                    username=str(data.get("username") or ""),
                    password=str(data.get("password") or ""),
                    role=str(data.get("role") or "viewer"),
                    display_name=str(data.get("display_name") or ""),
                    enabled=bool(data.get("enabled", True)),
                )
                self._send_json({"ok": True, "user": u})
                return True
            if path_only == "/admin/api/users/update":
                u = self.access.update_user(
                    username=str(data.get("username") or ""),
                    user_id=str(data.get("id") or ""),
                    display_name=data.get("display_name"),
                    role=data.get("role"),
                    enabled=data.get("enabled") if "enabled" in data else None,
                    new_password=data.get("new_password") or data.get("password"),
                )
                self._send_json({"ok": True, "user": u})
                return True
            if path_only == "/admin/api/users/delete":
                ok = self.access.delete_user(
                    username=str(data.get("username") or ""),
                    user_id=str(data.get("id") or ""),
                )
                if not ok:
                    self._send_json({"error": "user not found"}, status=404)
                    return True
                self._send_json({"ok": True})
                return True
        except ValueError as e:
            self._send_json({"error": str(e)}, status=400)
            return True
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)
            return True

        self._send_json({"error": "unknown admin API"}, status=404)
        return True

    # ------------------------------------------------------------------ comfy proxy
    def _log_comfy_traffic(
        self,
        *,
        method: str,
        path: str,
        status: int,
        t0: float,
        body_in: Optional[bytes],
        body_out: Optional[bytes],
        ident: Dict[str, str],
    ) -> None:
        try:
            kind = classify_comfy_path(method, path)
            duration_ms = int(max(0.0, (time.time() - t0) * 1000))
            bytes_in = len(body_in) if body_in else 0
            bytes_out = len(body_out) if body_out else 0
            base = {
                "ip": ident.get("ip") or self._client_ip(),
                "machine_id": ident.get("id") or "",
                "label": ident.get("label") or "",
                "group": ident.get("group") or "",
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": duration_ms,
                "bytes_in": bytes_in,
                "bytes_out": bytes_out,
            }
            if kind == "job_queue" and body_out:
                pid = parse_prompt_id_from_queue_response(body_out)
                client_id = ""
                detail = "queued"
                if body_in:
                    try:
                        payload = json.loads(body_in.decode("utf-8"))
                        client_id = str(payload.get("client_id") or "")
                        # light hint from filename_prefix if present in prompt graph
                        prompt = payload.get("prompt") or {}
                        for node in prompt.values() if isinstance(prompt, dict) else []:
                            if not isinstance(node, dict):
                                continue
                            pref = (node.get("inputs") or {}).get("filename_prefix")
                            if isinstance(pref, str) and pref:
                                detail = "prefix=" + pref[:80]
                                break
                    except Exception:
                        pass
                if pid:
                    self.usage.track_job_start(
                        pid,
                        ip=base["ip"],
                        machine_id=base["machine_id"],
                        label=base["label"],
                        group=base["group"],
                        client_id=client_id,
                        detail=detail,
                    )
                else:
                    self.usage.log(event=EVENT_API, detail="prompt_no_id", **base)
                return

            if kind == "history" and body_out and status == 200:
                done = parse_history_completion(path, body_out)
                if done:
                    for pid, st in done:
                        self.usage.track_job_done(
                            pid,
                            status=st,
                            ip=base["ip"],
                            detail="history_" + st,
                        )
                    return
                # no completion yet — skip noisy history polls
                return

            if kind == EVENT_UPLOAD:
                self.usage.log(event=EVENT_UPLOAD, detail="image_upload", **base)
                return
            if kind == EVENT_DOWNLOAD:
                self.usage.log(event=EVENT_DOWNLOAD, detail="view_download", **base)
                return
            # skip high-frequency noise: /queue, /system_stats, /object_info
            noisy = (
                path.startswith("/queue")
                or path.startswith("/system_stats")
                or path.startswith("/object_info")
                or path.startswith("/prompt")  # GET prompt
                or path == "/"
            )
            if noisy and method == "GET":
                return
            self.usage.log(event=EVENT_API, detail=kind, **base)
        except Exception:
            pass

    def _proxy_comfy(self, method: str) -> None:
        if not self._require_artist_ip():
            return
        ident = self._identity()
        # Map /comfyui/foo -> upstream/foo
        parsed = urlparse(self.path)
        prefix = "/comfyui"
        path = parsed.path
        if path == prefix:
            path = "/"
        elif path.startswith(prefix + "/"):
            path = path[len(prefix) :]
        else:
            self.send_error(404)
            return
        if not path.startswith("/"):
            path = "/" + path
        qs = ("?" + parsed.query) if parsed.query else ""
        target = self.comfy_upstream.rstrip("/") + path + qs

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else None

        headers = {}
        for k in ("Content-Type", "Accept", "User-Agent"):
            if self.headers.get(k):
                headers[k] = self.headers.get(k)
        t0 = time.time()
        req = urllib.request.Request(target, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = resp.read()
                status = getattr(resp, "status", 200) or 200
                ctype = resp.headers.get("Content-Type") or "application/octet-stream"
                self._log_comfy_traffic(
                    method=method,
                    path=path,
                    status=status,
                    t0=t0,
                    body_in=body,
                    body_out=data,
                    ident=ident,
                )
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self._log_comfy_traffic(
                method=method,
                path=path,
                status=e.code,
                t0=t0,
                body_in=body,
                body_out=data,
                ident=ident,
            )
            self.send_response(e.code)
            self.send_header(
                "Content-Type", e.headers.get("Content-Type") or "text/plain"
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            msg = ("ComfyUI proxy error: %s\nupstream=%s\n" % (e, target)).encode(
                "utf-8"
            )
            self._log_comfy_traffic(
                method=method,
                path=path,
                status=502,
                t0=t0,
                body_in=body,
                body_out=msg,
                ident=ident,
            )
            self._send_bytes(msg, "text/plain; charset=utf-8", status=502)

    def do_GET(self) -> None:
        path_only = urlparse(self.path).path

        # Admin / operator portal + APIs (no IP gate — login + role protect)
        if (
            path_only.startswith("/admin")
            or path_only.startswith("/operator")
            or path_only in ("/login", "/login/")
        ):
            if self._handle_admin_get(path_only):
                return
            self.send_error(404)
            return

        # ComfyUI reverse proxy (IP gated)
        if path_only == "/comfyui" or path_only.startswith("/comfyui/"):
            self._proxy_comfy("GET")
            return

        # Public health always allowed (no secrets)
        if path_only in ("/", "/index.html", "/health"):
            try:
                self.access.reload()
            except Exception:
                pass
            try:
                manifest = build_manifest(Path(self.directory).resolve())  # type: ignore[attr-defined]
                ver = manifest.get("version", "?")
                label = manifest.get("label", ver)
            except Exception:
                ver, label = "?", "?"
            acl_on = self.access.is_acl_enabled()
            body = (
                "ComfyNuke code server OK\n"
                "version: %s\n"
                "label: %s\n"
                "access_control: %s\n"
                "admin: GET /admin  |  operator: GET /operator\n"
                "ComfyUI (gated): /comfyui  → 127.0.0.1:8188\n"
                "Bootstrap: GET /nuke/remote_bootstrap.py\n"
                "Access check: GET /access/check\n"
                "Manifest: GET /manifest.json\n"
                "Version:  GET /version\n"
            ) % (ver, label, "ON" if acl_on else "OFF")
            self._send_bytes(body.encode("utf-8"), "text/plain; charset=utf-8")
            return

        # Live ACL probe (always answers — used by Nuke / diagnostics)
        if path_only in ("/access/check", "/access/status"):
            try:
                self.access.reload()
            except Exception:
                pass
            ip = self._client_ip()
            ok, reason = self.access.client_allowed(ip)
            self._send_json(
                {
                    "allowed": ok,
                    "ip": ip,
                    "reason": reason,
                    "acl_enabled": self.access.is_acl_enabled(),
                    "comfy_proxy": "/comfyui",
                },
                status=200 if ok else 403,
            )
            return

        # Artist code/workflow endpoints — IP gate when ACL enabled
        if not self._require_artist_ip():
            return

        if path_only in ("/version", "/version.txt"):
            try:
                manifest = build_manifest(Path(self.directory).resolve())  # type: ignore[attr-defined]
                body = ("%s\n" % manifest["version"]).encode("utf-8")
            except Exception as e:
                body = ("error: %s\n" % e).encode("utf-8")
                self._send_bytes(body, "text/plain; charset=utf-8", status=500)
                return
            self._send_bytes(body, "text/plain; charset=utf-8")
            return

        if path_only in ("/manifest.json", "/manifest"):
            try:
                manifest = build_manifest(Path(self.directory).resolve())  # type: ignore[attr-defined]
                body = json.dumps(manifest, indent=2).encode("utf-8")
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode("utf-8")
                self._send_bytes(body, "application/json; charset=utf-8", status=500)
                return
            self._send_bytes(body, "application/json; charset=utf-8")
            return

        translated = self.translate_path(self.path)
        if translated.endswith(".forbidden") or not os.path.isfile(translated):
            self.send_error(403 if translated.endswith(".forbidden") else 404)
            return
        # Usage: bootstrap / script pulls
        try:
            rel = path_only.lstrip("/")
            if rel.endswith("remote_bootstrap.py") or rel in (
                "nuke/ComfyEdit.py",
                "client/comfy_client.py",
            ) or rel.endswith(".json") and "Edit_Image" in rel:
                ident = self._identity()
                self.usage.log(
                    event=EVENT_BOOTSTRAP if rel.endswith("remote_bootstrap.py") else EVENT_API,
                    ip=ident.get("ip") or self._client_ip(),
                    machine_id=ident.get("id") or "",
                    label=ident.get("label") or "",
                    group=ident.get("group") or "",
                    method="GET",
                    path=path_only,
                    status=200,
                    detail=rel,
                )
        except Exception:
            pass
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self) -> None:
        path_only = urlparse(self.path).path
        if path_only.startswith("/admin/api/"):
            if self._handle_admin_post(path_only):
                return
            self.send_error(404)
            return
        if path_only == "/comfyui" or path_only.startswith("/comfyui/"):
            self._proxy_comfy("POST")
            return
        self.send_error(405, "POST not allowed on code paths")

    def do_PUT(self) -> None:
        path_only = urlparse(self.path).path
        if path_only == "/comfyui" or path_only.startswith("/comfyui/"):
            self._proxy_comfy("PUT")
            return
        self.send_error(405, "PUT not allowed")

    def do_DELETE(self) -> None:
        path_only = urlparse(self.path).path
        if path_only == "/comfyui" or path_only.startswith("/comfyui/"):
            self._proxy_comfy("DELETE")
            return
        self.send_error(405, "DELETE not allowed")


def _parse_ports(primary: int, extra: str) -> List[int]:
    """Primary port + optional comma list from --extra-ports or env (empty = primary only)."""
    ports: List[int] = [int(primary)]
    raw = (extra or "").strip()
    if not raw:
        raw = (os.environ.get("COMFYNUKE_CODE_EXTRA_PORTS") or "").strip()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            p = int(part)
        except ValueError:
            continue
        if p not in ports and 1 <= p <= 65535:
            ports.append(p)
    return ports


def main() -> int:
    parser = argparse.ArgumentParser(description="ComfyNuke code HTTP server + access control")
    parser.add_argument("--root", default="", help="ComfyNuke repo root")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument(
        "--port",
        type=int,
        default=8600,
        help="Listen port (default 8600 only)",
    )
    parser.add_argument(
        "--extra-ports",
        default="",
        help="Optional extra ports (comma-separated). Default: none — only --port.",
    )
    parser.add_argument(
        "--comfy-upstream",
        default=os.environ.get("COMFYNUKE_COMFY_UPSTREAM") or "http://127.0.0.1:8188",
        help="ComfyUI base URL for /comfyui proxy (default http://127.0.0.1:8188)",
    )
    args = parser.parse_args()

    if args.root:
        root = Path(args.root).resolve()
    else:
        root = Path(__file__).resolve().parent.parent

    if not (root / "nuke" / "ComfyEdit.py").is_file():
        print(
            "ERROR: not a ComfyNuke root (missing nuke/ComfyEdit.py): %s" % root,
            file=sys.stderr,
        )
        return 1

    (root / ".forbidden").write_text("forbidden\n", encoding="utf-8")

    acl_path = root / "server" / "access_control.json"
    # First-run only: set COMFYNUKE_ADMIN_PASSWORD before first start (hashed on disk).
    # Live secrets live in access_control.json (gitignored).
    bootstrap_pw = os.environ.get("COMFYNUKE_ADMIN_PASSWORD")
    access = AccessControl(
        acl_path,
        bootstrap_user=os.environ.get("COMFYNUKE_ADMIN_USER") or "Krish",
        bootstrap_password=bootstrap_pw,  # None → keep existing file / generate random once
    )
    admin_html = root / "server" / "admin_ui.html"

    try:
        manifest = build_manifest(root)
        pkg_ver = "%s (%s)" % (manifest["version"], manifest.get("label"))
    except Exception as e:
        pkg_ver = "(could not build: %s)" % e

    usage_path = root / "server" / "usage_logs.jsonl"
    usage = UsageLog(usage_path)

    # Bind access + comfy onto handler class (shared by threads)
    ComfyNukeHandler.access = access
    ComfyNukeHandler.usage = usage
    ComfyNukeHandler.comfy_upstream = args.comfy_upstream.rstrip("/")
    ComfyNukeHandler.admin_html_path = admin_html

    handler = partial(ComfyNukeHandler, directory=str(root))
    ports = _parse_ports(args.port, args.extra_ports)
    servers: List[ThreadingHTTPServer] = []
    threads: List[threading.Thread] = []

    for port in ports:
        try:
            httpd = ThreadingHTTPServer((args.host, port), handler)
        except OSError as e:
            print(
                "ERROR: cannot bind %s:%s — %s" % (args.host, port, e),
                file=sys.stderr,
            )
            if port == args.port:
                return 1
            print("  (skipping extra port %s)" % port, file=sys.stderr)
            continue
        servers.append(httpd)
        t = threading.Thread(
            target=httpd.serve_forever,
            name="code-http-%s" % port,
            daemon=True,
        )
        t.start()
        threads.append(t)

    if not servers:
        print("ERROR: no ports bound", file=sys.stderr)
        return 1

    host_show = args.host if args.host != "0.0.0.0" else "127.0.0.1"
    primary = ports[0]
    print("=" * 60)
    print("ComfyNuke code server")
    print("  root:     %s" % root)
    print("  package:  %s" % pkg_ver)
    print("  bind:     %s" % ", ".join("http://%s:%s/" % (args.host, p) for p in ports))
    print("  health:   http://%s:%s/health" % (host_show, primary))
    print("  admin UI:    http://%s:%s/admin" % (host_show, primary))
    print("  operator UI: http://%s:%s/operator" % (host_show, primary))
    print("  ACL file: %s" % acl_path)
    print("  usage log:%s" % usage_path)
    print("  ACL on:   %s" % access.is_acl_enabled())
    print("  comfy proxy → %s  (path /comfyui/...)" % args.comfy_upstream)
    print("  artist bootstrap (port %s only):" % primary)
    print(
        "    exec(__import__('urllib.request').request.urlopen("
        "'http://<SERVER_IP>:%s/nuke/remote_bootstrap.py', timeout=60)"
        ".read().decode('utf-8'))" % primary
    )
    print("=" * 60)
    try:
        # Keep main thread alive; workers run serve_forever
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopped")
        for s in servers:
            try:
                s.shutdown()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""Per-workflow ComfyUI server routing for ComfyNuke (:8600)."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


_LOCK = threading.RLock()
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,47}$")

KNOWN_WORKFLOWS = (
    "Edit_Image_v08.json",
    "Image_generation_v01.json",
    "video_minimax_h3_i2v.json",
)


def default_config() -> Dict[str, Any]:
    return {
        "default_server_id": "main",
        "servers": [
            {
                "id": "main",
                "name": "Main ComfyUI :8188",
                "url": "http://127.0.0.1:8188",
            },
            {
                "id": "8166",
                "name": "ComfyUI-8166",
                "url": "http://127.0.0.1:8166",
            },
        ],
        "workflows": [
            {
                "file": "Edit_Image_v08.json",
                "server_id": "8166",
                "notes": "Edit image jobs on 8166",
            },
            {
                "file": "Image_generation_v01.json",
                "server_id": "main",
                "notes": "",
            },
            {
                "file": "video_minimax_h3_i2v.json",
                "server_id": "8166",
                "notes": "MiniMax H3 I2V",
            },
        ],
    }


def routes_path(root: Path) -> Path:
    return Path(root) / "workflow_routes.json"


def load_routes(root: Path) -> Dict[str, Any]:
    path = routes_path(root)
    with _LOCK:
        if not path.is_file():
            cfg = default_config()
            _write_unlocked(path, cfg)
            return cfg
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = default_config()
        if not isinstance(data, dict):
            data = default_config()
        data.setdefault("default_server_id", "main")
        data.setdefault("servers", [])
        data.setdefault("workflows", [])
        return data


def _write_unlocked(path: Path, cfg: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def save_routes(root: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = validate_config(cfg)
    path = routes_path(root)
    with _LOCK:
        _write_unlocked(path, cleaned)
        _sync_studio_config(root, cleaned)
    return cleaned


def _sync_studio_config(root: Path, cfg: Dict[str, Any]) -> None:
    """Keep studio_config.json in sync so Nuke bootstrap picks up routes."""
    studio = Path(root) / "studio_config.json"
    try:
        data = json.loads(studio.read_text(encoding="utf-8")) if studio.is_file() else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["comfy_servers"] = cfg.get("servers") or []
    data["default_server_id"] = cfg.get("default_server_id") or "main"
    data["workflow_routes"] = {
        str(w.get("file")): str(w.get("server_id") or "")
        for w in (cfg.get("workflows") or [])
        if w.get("file")
    }
    tmp = studio.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(studio)


def validate_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(cfg, dict):
        raise ValueError("config must be an object")
    servers_in = cfg.get("servers") or []
    if not isinstance(servers_in, list) or not servers_in:
        raise ValueError("at least one ComfyUI server is required")
    servers: List[Dict[str, str]] = []
    seen = set()
    for s in servers_in:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "").strip()
        name = str(s.get("name") or sid).strip()
        url = _normalize_url(str(s.get("url") or "").strip())
        if not sid or not _ID_RE.match(sid):
            raise ValueError("invalid server id: %r (use letters, numbers, ._- )" % sid)
        if sid in seen:
            raise ValueError("duplicate server id: %s" % sid)
        if not url:
            raise ValueError("server %s needs a URL" % sid)
        seen.add(sid)
        servers.append({"id": sid, "name": name or sid, "url": url})
    if not servers:
        raise ValueError("at least one valid server is required")

    default_id = str(cfg.get("default_server_id") or servers[0]["id"]).strip()
    if default_id not in seen:
        default_id = servers[0]["id"]

    wfs_in = cfg.get("workflows") or []
    if not isinstance(wfs_in, list):
        wfs_in = []
    workflows: List[Dict[str, str]] = []
    wf_seen = set()
    for w in wfs_in:
        if not isinstance(w, dict):
            continue
        fname = str(w.get("file") or "").strip()
        if not fname or fname in wf_seen:
            continue
        if "/" in fname or "\\" in fname:
            fname = Path(fname).name
        if not fname.endswith(".json"):
            raise ValueError("workflow file must be a .json name: %s" % fname)
        sid = str(w.get("server_id") or default_id).strip()
        if sid not in seen:
            sid = default_id
        notes = str(w.get("notes") or "")
        wf_seen.add(fname)
        workflows.append({"file": fname, "server_id": sid, "notes": notes})

    return {
        "default_server_id": default_id,
        "servers": servers,
        "workflows": workflows,
    }


def _normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("invalid server URL: %s" % url)
    return url


def discover_workflow_files(root: Path) -> List[str]:
    names = []
    for name in KNOWN_WORKFLOWS:
        if (Path(root) / name).is_file():
            names.append(name)
    try:
        for p in sorted(Path(root).glob("*.json")):
            if p.name in ("studio_config.json", "studio_config.example.json", "workflow_routes.json"):
                continue
            if p.name not in names:
                # skip tiny non-workflow json
                if p.stat().st_size < 80:
                    continue
                names.append(p.name)
    except Exception:
        pass
    return names


def snapshot(root: Path) -> Dict[str, Any]:
    cfg = load_routes(root)
    known = discover_workflow_files(root)
    have = {w["file"] for w in cfg.get("workflows") or []}
    extra = []
    for name in known:
        if name not in have:
            extra.append(
                {
                    "file": name,
                    "server_id": cfg.get("default_server_id") or "main",
                    "notes": "",
                    "unassigned": True,
                }
            )
    return {
        "ok": True,
        "config": cfg,
        "discovered_workflows": known,
        "unassigned": extra,
        "hub_ips": _local_ipv4s(),
    }


def server_by_id(cfg: Dict[str, Any], sid: str) -> Optional[Dict[str, str]]:
    for s in cfg.get("servers") or []:
        if str(s.get("id")) == str(sid):
            return s
    return None


def default_upstream(cfg: Dict[str, Any], fallback: str) -> str:
    sid = str(cfg.get("default_server_id") or "")
    s = server_by_id(cfg, sid)
    if s and s.get("url"):
        return str(s["url"]).rstrip("/")
    if cfg.get("servers"):
        return str(cfg["servers"][0].get("url") or fallback).rstrip("/")
    return fallback.rstrip("/")


def route_for_workflow(cfg: Dict[str, Any], filename: str) -> Optional[Dict[str, str]]:
    base = Path(str(filename or "")).name
    for w in cfg.get("workflows") or []:
        if str(w.get("file")) == base:
            sid = str(w.get("server_id") or "")
            srv = server_by_id(cfg, sid)
            if srv:
                return {
                    "file": base,
                    "server_id": sid,
                    "server_name": srv.get("name") or sid,
                    "upstream": srv.get("url") or "",
                }
    return None


def _local_ipv4s() -> List[str]:
    ips = ["127.0.0.1"]
    try:
        import socket as _s
        host = _s.gethostname()
        for info in _s.getaddrinfo(host, None, _s.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def _port_open(host: str, port: int, timeout: float) -> bool:
    import socket as _s
    try:
        with _s.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def ping_upstream(url: str, timeout: float = 5.0) -> Dict[str, Any]:
    """HTTP ping from the :8600 hub (not from the admin browser)."""
    import socket as _s

    url = (url or "").rstrip("/")
    t0 = time.time()
    parsed = urlparse(url if "://" in url else "http://" + url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    info: Dict[str, Any] = {
        "ok": False,
        "url": url,
        "host": host,
        "port": port,
        "tcp_ok": False,
        "from": "hub",
    }

    # Fast TCP check (IPv4)
    try:
        with _s.create_connection((host, int(port)), timeout=min(timeout, 3.0)):
            info["tcp_ok"] = True
    except Exception as e:
        info["tcp_error"] = str(e)
        local_alt = None
        if _port_open("127.0.0.1", int(port), 0.3):
            local_alt = "http://127.0.0.1:%s" % port
        info["local_alt"] = local_alt
        info["ms"] = int((time.time() - t0) * 1000)
        info["error"] = (
            "Hub %s cannot open TCP %s:%s (%s). "
            "Your browser can still open the page if you are on that machine "
            "or allowed by its firewall. Open port %s from this hub "
            "(192.168.91.0/24) on %s, or change the URL to this hub "
            "(%s) if ComfyUI actually runs here."
            % (
                ",".join(_local_ipv4s()),
                host,
                port,
                e,
                port,
                host,
                local_alt or "http://127.0.0.1:%s" % port,
            )
        )
        info["hint"] = (
            "On %s run: sudo ufw allow from 192.168.91.0/24 to any port %s proto tcp"
            % (host, port)
        )
        return info

    try:
        req = urllib.request.Request(url + "/system_stats", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ms = int((time.time() - t0) * 1000)
            info.update({"ok": True, "status": getattr(resp, "status", 200), "ms": ms})
            try:
                data = json.loads(raw.decode("utf-8"))
                info["comfyui_version"] = (data.get("system") or {}).get("comfyui_version")
                devs = data.get("devices") or []
                if devs:
                    info["device"] = devs[0].get("name")
            except Exception:
                pass
            return info
    except urllib.error.HTTPError as e:
        info.update(
            {"status": e.code, "error": str(e), "ms": int((time.time() - t0) * 1000)}
        )
        return info
    except Exception as e:
        info.update({"error": str(e), "ms": int((time.time() - t0) * 1000)})
        return info

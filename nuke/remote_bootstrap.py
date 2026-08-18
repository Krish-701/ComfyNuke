# -*- coding: utf-8 -*-
"""
ComfyNuke remote bootstrap — served from Ubuntu :8600, run inside Nuke.

Artists do NOT need share/SSH access. They only need:
  - HTTP :8600  (this script + code + /comfyui proxy — IP gated)
  - ComfyUI jobs use http://SERVER:8600/comfyui (not raw :8188)

ONE LINE in Nuke Script Editor:

  exec(__import__('urllib.request').request.urlopen('http://192.168.91.13:8600/nuke/remote_bootstrap.py', timeout=60).read().decode('utf-8'))

On every launch this script:
  1) Asks the code server for /manifest.json (latest package version)
  2) Compares with the artist's local cache (~/.comfynuke/cache)
  3) If the local copy is missing or older, replaces code + workflows
     with whatever the server currently has
  4) Registers the Pix-Edit menu (Edit Image / Image Gen / Image to Video)
"""

from __future__ import print_function

import hashlib
import importlib.util
import json
import os
import sys

try:
    from urllib.request import Request, urlopen
except ImportError:
    from urllib2 import Request, urlopen  # type: ignore

# ---------------------------------------------------------------------------
# Defaults (override by editing on server, or set env before exec)
# ---------------------------------------------------------------------------
CODE_BASE = (os.environ.get("COMFYNUKE_CODE_BASE") or "http://192.168.91.13:8600").rstrip(
    "/"
)
# ComfyUI jobs MUST go through the :8600 /comfyui proxy so Access Control
# enable/disable applies on every upload/queue (not direct :8188).
_DEFAULT_COMFY_PROXY = CODE_BASE + "/comfyui"
COMFY_SERVER = (os.environ.get("COMFYNUKE_SERVER") or _DEFAULT_COMFY_PROXY).rstrip("/")
# If someone still points at raw :8188, rewrite to the gated proxy on same host.
if COMFY_SERVER.rstrip("/").endswith(":8188") or COMFY_SERVER.rstrip("/").endswith(
    ":8188/"
):
    COMFY_SERVER = _DEFAULT_COMFY_PROXY

# Files to mirror from code server → local cache (paths relative to repo root).
# Keep in lockstep with server/serve_code.py SYNC_FILES.
_SYNC_FILES = (
    "nuke/ComfyEdit.py",
    "nuke/launch.py",
    "client/comfy_client.py",
    "Edit_Image_v08.json",
    "Image_generation_v01.json",
    "video_minimax_h3_i2v.json",
    "studio_config.json",  # optional
    "studio_config.example.json",
    "workflow_routes.json",
)

_OPTIONAL = frozenset(["studio_config.json", "workflow_routes.json"])
# Always re-download these from the hub on every Nuke launch (never keep stale
# artist-cache copies after the server graph/scripts are edited).
_ALWAYS_REFRESH = frozenset(
    [
        "nuke/ComfyEdit.py",
        "client/comfy_client.py",
        "Edit_Image_v08.json",
        "Image_generation_v01.json",
        "video_minimax_h3_i2v.json",
        "workflow_routes.json",
        "studio_config.json",
    ]
)
_LOCAL_MANIFEST = ".comfynuke_manifest.json"
_TIMEOUT = 90


def _log(msg):
    try:
        import nuke  # type: ignore

        nuke.tprint("[ComfyNuke] %s" % msg)
    except Exception:
        print("[ComfyNuke] %s" % msg)


def _http_get(url):
    req = Request(url, headers={"User-Agent": "ComfyNuke-Nuke-Bootstrap/1.1"})
    resp = urlopen(req, timeout=_TIMEOUT)
    try:
        data = resp.read()
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return data


def _cache_root():
    custom = (os.environ.get("COMFYNUKE_CACHE") or "").strip()
    if custom:
        return custom
    home = os.path.expanduser("~")
    return os.path.join(home, ".comfynuke", "cache")


def _write_file(path, data):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "wb") as f:
        f.write(data)


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _local_manifest_path(cache):
    return os.path.join(cache, _LOCAL_MANIFEST)


def _load_local_manifest(cache):
    path = _local_manifest_path(cache)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("version"):
            return data
    except Exception:
        pass
    return None


def _save_local_manifest(cache, manifest):
    path = _local_manifest_path(cache)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def _fetch_server_manifest(base_url):
    """
    Prefer /manifest.json from code server. Fall back to building a thin
    stub so older servers still force a full sync.
    """
    url = "%s/manifest.json" % base_url
    try:
        raw = _http_get(url)
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict) and data.get("version") and data.get("files"):
            return data
        raise RuntimeError("invalid manifest shape")
    except Exception as e:
        _log("manifest unavailable (%s) — will full-sync" % e)
        return {
            "version": "unknown",
            "label": "unknown",
            "files": [{"path": rel, "sha256": None, "present": True} for rel in _SYNC_FILES],
            "_fallback": True,
        }


def _file_needs_update(cache, rel, server_entry):
    """True if local file is missing or hash does not match server."""
    dest = os.path.join(cache, *rel.split("/"))
    if not os.path.isfile(dest):
        return True
    server_hash = (server_entry or {}).get("sha256")
    if not server_hash:
        # No hash from server (fallback) → always refresh required files
        return True
    try:
        return _sha256_file(dest) != server_hash
    except Exception:
        return True


def _sync_from_server(base_url, cache, server_manifest, force_all=False):
    """
    Download only files that are missing or stale vs server manifest.
    Always overwrites local with server bytes when an update is needed
    so artists get the workflows the hub currently ships.
    """
    files = server_manifest.get("files") or []
    by_path = {}
    for entry in files:
        if isinstance(entry, dict) and entry.get("path"):
            by_path[entry["path"]] = entry

    # Also walk the known list so we never skip a required path if the
    # server manifest is older than this bootstrap script.
    paths = list(_SYNC_FILES)
    for p in by_path:
        if p not in paths:
            paths.append(p)

    updated = []
    skipped = []
    missing_optional = []

    for rel in paths:
        entry = by_path.get(rel) or {}
        optional = rel in _OPTIONAL or entry.get("optional") is True
        dest = os.path.join(cache, *rel.split("/"))

        if entry.get("present") is False:
            if optional:
                missing_optional.append(rel)
                continue
            # Required but missing on server — error only if we don't already have it
            if not os.path.isfile(dest):
                raise RuntimeError(
                    "Server is missing required file %s and no local copy exists." % rel
                )
            skipped.append(rel)
            continue

        # Workflows: always pull from server (hash skip can leave Nuke on an
        # old graph if the artist skips re-bootstrap or cache was partial).
        always = rel in _ALWAYS_REFRESH or rel.lower().endswith(".json") and (
            "Edit_Image" in rel
            or "Image_generation" in rel
            or "video_" in rel
            or rel.endswith("_i2v.json")
        )
        need = force_all or always or _file_needs_update(cache, rel, entry)
        if not need:
            skipped.append(rel)
            _log("up-to-date %s" % rel)
            continue

        url = "%s/%s" % (base_url, rel.replace("\\", "/"))
        try:
            data = _http_get(url)
            if not data:
                raise RuntimeError("empty response")
            # Verify hash when server provided one (skip if always-refresh and
            # server hash is momentarily out of date vs disk — still write).
            expect = entry.get("sha256")
            if expect and not always:
                got = _sha256_bytes(data)
                if got != expect:
                    raise RuntimeError(
                        "hash mismatch for %s (server %s… local download %s…)"
                        % (rel, expect[:12], got[:12])
                    )
            elif expect and always:
                got = _sha256_bytes(data)
                if got != expect:
                    _log(
                        "warn hash drift %s (manifest %s… download %s…) — using download"
                        % (rel, expect[:12], got[:12])
                    )
            _write_file(dest, data)
            updated.append(rel)
            why = "forced workflow refresh" if always else "stale/missing"
            _log("updated %s (%s bytes) ← server [%s]" % (rel, len(data), why))
        except Exception as e:
            if optional:
                missing_optional.append(rel)
                _log("optional skip %s: %s" % (rel, e))
            else:
                raise RuntimeError(
                    "Failed to download %s\n  url: %s\n  error: %s" % (rel, url, e)
                )

    return updated, skipped, missing_optional


def _ensure_studio_config(cache, comfy_server, code_base):
    path = os.path.join(cache, "studio_config.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    else:
        cfg = {}
    # Always pin to gated proxy (ACL live). Never leave artists on raw :8188.
    srv = (comfy_server or "").rstrip("/")
    if not srv or srv.endswith(":8188") or "/comfyui" not in srv:
        srv = code_base.rstrip("/") + "/comfyui"
    cfg["server"] = srv
    cfg["code_base_url"] = code_base
    cfg["studio_name"] = cfg.get("studio_name") or "ComfyNuke Studio"
    # empty output_dir → ComfyEdit uses ~/ComfyNuke_out/<host>
    if "output_dir" not in cfg:
        cfg["output_dir"] = ""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return path


def bootstrap():
    cache = _cache_root()
    if not os.path.isdir(cache):
        os.makedirs(cache)

    _log("code base:  %s" % CODE_BASE)
    _log("ComfyUI:    %s" % COMFY_SERVER)
    _log("cache:      %s" % cache)

    # Quick health check
    try:
        health = _http_get("%s/health" % CODE_BASE)
        _log("code server: %s" % health.decode("utf-8", "replace").strip().split("\n")[0])
    except Exception as e:
        raise RuntimeError(
            "Cannot reach code server at %s (port 8600).\n"
            "Is serve_code.py running on Ubuntu?\n%s" % (CODE_BASE, e)
        )

    server_manifest = _fetch_server_manifest(CODE_BASE)
    server_ver = server_manifest.get("version") or "unknown"
    server_label = server_manifest.get("label") or server_ver
    local_manifest = _load_local_manifest(cache)
    local_ver = (local_manifest or {}).get("version") if local_manifest else None

    force_all = bool(server_manifest.get("_fallback"))
    if local_ver:
        _log("local package:  %s" % local_ver)
    else:
        _log("local package:  (none — first install or cache cleared)")
    _log("server package: %s (%s)" % (server_ver, server_label))

    if not force_all and local_ver and local_ver == server_ver:
        # Version id matches — still verify each file hash (disk may have been edited)
        _log("version matches server — verifying files…")
    elif local_ver and local_ver != server_ver:
        _log(
            "OUTDATED cache %s → replacing with server %s (code + workflows)"
            % (local_ver, server_ver)
        )
    else:
        _log("installing / refreshing cache from server %s" % server_ver)

    updated, skipped, missing_optional = _sync_from_server(
        CODE_BASE, cache, server_manifest, force_all=force_all
    )

    if updated:
        _log("replaced %d file(s) from server: %s" % (len(updated), ", ".join(updated)))
    else:
        _log("all synced files already match server")
    if skipped:
        _log("unchanged: %d file(s)" % len(skipped))
    if missing_optional:
        _log("optional missing on server: %s" % ", ".join(missing_optional))

    # Persist server manifest locally after successful sync
    if not server_manifest.get("_fallback"):
        _save_local_manifest(cache, server_manifest)
        _log("cache version pinned to %s" % server_ver)

    cfg_path = _ensure_studio_config(cache, COMFY_SERVER, CODE_BASE)
    _log("studio config: %s" % cfg_path)

    os.environ["COMFYNUKE_ROOT"] = cache
    os.environ["COMFYNUKE_SERVER"] = COMFY_SERVER
    os.environ["COMFYNUKE_CODE_BASE"] = CODE_BASE
    os.environ["COMFYNUKE_PACKAGE_VERSION"] = str(server_ver)

    nuke_dir = os.path.join(cache, "nuke")
    client_dir = os.path.join(cache, "client")
    edit_py = os.path.join(nuke_dir, "ComfyEdit.py")
    if not os.path.isfile(edit_py):
        raise RuntimeError("ComfyEdit.py missing after sync: %s" % edit_py)

    for p in (nuke_dir, client_dir, cache):
        if p not in sys.path:
            sys.path.insert(0, p)

    for mod in ("ComfyEdit", "comfy_client"):
        if mod in sys.modules:
            del sys.modules[mod]

    spec = importlib.util.spec_from_file_location("ComfyEdit", edit_py)
    ComfyEdit = importlib.util.module_from_spec(spec)
    sys.modules["ComfyEdit"] = ComfyEdit
    spec.loader.exec_module(ComfyEdit)
    ComfyEdit.register_menu()

    _log("=" * 56)
    _log("READY — multi-user remote load OK")
    _log("  package: %s" % server_ver)
    _log("  server:  %s" % getattr(ComfyEdit, "DEFAULT_SERVER", COMFY_SERVER))
    _log("  root:    %s" % getattr(ComfyEdit, "REPO_ROOT", cache))
    _log("  out:     %s" % getattr(ComfyEdit, "DEFAULT_OUT", ""))
    _log("  workflows (from server when outdated):")
    _log("    edit:  %s" % getattr(ComfyEdit, "DEFAULT_WORKFLOW", ""))
    _log("    gen:   %s" % getattr(ComfyEdit, "IMAGE_GEN_WORKFLOW", ""))
    _log("    i2v:   %s" % getattr(ComfyEdit, "I2V_WORKFLOW", ""))
    _log("  Menu: Nuke > Pix-Edit")
    _log("    Edit Image | Image Gen | Image to Video | Ping")
    _log("  Jobs share one ComfyUI queue on the Ubuntu GPU.")
    _log("=" * 56)
    return ComfyEdit


# Run immediately when exec'd from urlopen
try:
    _comfy_edit = bootstrap()
except Exception as _boot_err:
    try:
        import nuke  # type: ignore

        nuke.message("ComfyNuke bootstrap failed:\n%s" % _boot_err)
        nuke.tprint("[ComfyNuke] BOOTSTRAP ERROR: %s" % _boot_err)
    except Exception:
        print("[ComfyNuke] BOOTSTRAP ERROR: %s" % _boot_err)
        raise

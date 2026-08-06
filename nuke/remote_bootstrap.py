# -*- coding: utf-8 -*-
"""
ComfyNuke remote bootstrap — served from Ubuntu :6000, run inside Nuke.

Artists do NOT need share/SSH access. They only need:
  - HTTP :6000  (this script + code)
  - HTTP :8188  (ComfyUI API)

ONE LINE in Nuke Script Editor:

  exec(__import__('urllib.request').request.urlopen('http://192.168.91.13:6000/nuke/remote_bootstrap.py', timeout=60).read().decode('utf-8'))

This file downloads the latest code + workflows into a local cache, then
registers the ComfyUI menu (Edit Image / Image Gen / Image to Video).
"""

from __future__ import print_function

import importlib.util
import json
import os
import sys
import tempfile

try:
    from urllib.request import Request, urlopen
except ImportError:
    from urllib2 import Request, urlopen  # type: ignore

# ---------------------------------------------------------------------------
# Defaults (override by editing on server, or set env before exec)
# ---------------------------------------------------------------------------
CODE_BASE = (os.environ.get("COMFYNUKE_CODE_BASE") or "http://192.168.91.13:6000").rstrip(
    "/"
)
COMFY_SERVER = (
    os.environ.get("COMFYNUKE_SERVER") or "http://192.168.91.13:8188"
).rstrip("/")

# Files to mirror from code server → local cache (paths relative to repo root)
_SYNC_FILES = (
    "nuke/ComfyEdit.py",
    "client/comfy_client.py",
    "Edit_Image_v05.json",
    "Image_generation_v01.json",
    "video_minimax_h3_i2v.json",
    "studio_config.json",  # optional
    "studio_config.example.json",
)

_TIMEOUT = 90


def _log(msg):
    try:
        import nuke  # type: ignore

        nuke.tprint("[ComfyNuke] %s" % msg)
    except Exception:
        print("[ComfyNuke] %s" % msg)


def _http_get(url):
    req = Request(url, headers={"User-Agent": "ComfyNuke-Nuke-Bootstrap/1.0"})
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
    # binary-safe
    mode = "wb"
    with open(path, mode) as f:
        f.write(data)


def _sync_all(base_url, cache):
    ok = []
    missing_optional = []
    for rel in _SYNC_FILES:
        url = "%s/%s" % (base_url, rel.replace("\\", "/"))
        dest = os.path.join(cache, *rel.split("/"))
        try:
            data = _http_get(url)
            if not data:
                raise RuntimeError("empty response")
            _write_file(dest, data)
            ok.append(rel)
            _log("synced %s (%s bytes)" % (rel, len(data)))
        except Exception as e:
            if rel in ("studio_config.json",):
                missing_optional.append(rel)
                _log("optional skip %s: %s" % (rel, e))
            else:
                raise RuntimeError("Failed to download %s\n  url: %s\n  error: %s" % (rel, url, e))
    return ok, missing_optional


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
    cfg["server"] = comfy_server
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
            "Cannot reach code server at %s (port 6000).\n"
            "Is serve_code.py running on Ubuntu?\n%s" % (CODE_BASE, e)
        )

    _sync_all(CODE_BASE, cache)
    cfg_path = _ensure_studio_config(cache, COMFY_SERVER, CODE_BASE)
    _log("studio config: %s" % cfg_path)

    os.environ["COMFYNUKE_ROOT"] = cache
    os.environ["COMFYNUKE_SERVER"] = COMFY_SERVER
    os.environ["COMFYNUKE_CODE_BASE"] = CODE_BASE

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
    _log("  server:  %s" % getattr(ComfyEdit, "DEFAULT_SERVER", COMFY_SERVER))
    _log("  root:    %s" % getattr(ComfyEdit, "REPO_ROOT", cache))
    _log("  out:     %s" % getattr(ComfyEdit, "DEFAULT_OUT", ""))
    _log("  Menu: Nuke > ComfyUI")
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

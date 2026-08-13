# -*- coding: utf-8 -*-
"""
ComfyNuke multi-user launch (artists run ONLY this from Nuke Script Editor).

HUB MODEL
  - Ubuntu main server: ComfyUI (:8188) + shared ComfyNuke folder (NFS/Samba/git)
  - Artist PCs: Nuke only — no local copy required if share is mounted

ONE-LINER (edit share path once for your studio):

  exec(open(r"//192.168.91.13/ComfyNuke/nuke/launch.py", encoding="utf-8").read())

Linux Nuke / mounted path:

  exec(open("/mnt/comfynuke/nuke/launch.py", encoding="utf-8").read())

Local dev:

  exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/launch.py", encoding="utf-8").read())

Optional before launch:
  import os
  os.environ["COMFYNUKE_SERVER"] = "http://192.168.91.13:8188"
  os.environ["COMFYNUKE_ROOT"] = r"//192.168.91.13/ComfyNuke"
"""

from __future__ import print_function

import importlib.util
import os
import sys

# ---------------------------------------------------------------------------
# Resolve install root (this file lives in <root>/nuke/launch.py)
# ---------------------------------------------------------------------------
def _launch_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        # exec(open(...).read()) often has no __file__
        # Try common studio locations, then fail with instructions
        candidates = [
            os.environ.get("COMFYNUKE_ROOT", ""),
            r"//192.168.91.13/ComfyNuke",
            r"\\192.168.91.13\ComfyNuke",
            "/mnt/comfynuke",
            "/opt/ComfyNuke",
            r"D:/AI-Dev/Krish-ComfyNuke",
        ]
        for root in candidates:
            if not root:
                continue
            p = os.path.join(root, "nuke", "ComfyEdit.py")
            if os.path.isfile(p):
                return os.path.join(root, "nuke")
        raise RuntimeError(
            "ComfyNuke launch: cannot find install root.\n"
            "Set os.environ['COMFYNUKE_ROOT'] to the share path, then re-run launch.\n"
            "Example:\n"
            "  import os\n"
            "  os.environ['COMFYNUKE_ROOT'] = r'\\\\192.168.91.13\\ComfyNuke'\n"
            "  exec(open(r'\\\\192.168.91.13\\ComfyNuke\\nuke\\launch.py', encoding='utf-8').read())"
        )


_NUKE_DIR = _launch_dir()
_REPO_ROOT = os.path.normpath(os.path.join(_NUKE_DIR, ".."))
_EDIT_PY = os.path.join(_NUKE_DIR, "ComfyEdit.py")
_CLIENT = os.path.join(_REPO_ROOT, "client")

if not os.path.isfile(_EDIT_PY):
    raise RuntimeError("ComfyEdit.py not found: %s" % _EDIT_PY)

# Pin root for ComfyEdit path/config resolution
os.environ["COMFYNUKE_ROOT"] = _REPO_ROOT

for p in (_NUKE_DIR, _CLIENT, _REPO_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

# Drop cached modules so share updates are picked up on re-launch
for _mod in ("ComfyEdit", "comfy_client"):
    if _mod in sys.modules:
        del sys.modules[_mod]

_spec = importlib.util.spec_from_file_location("ComfyEdit", _EDIT_PY)
ComfyEdit = importlib.util.module_from_spec(_spec)
sys.modules["ComfyEdit"] = ComfyEdit
_spec.loader.exec_module(ComfyEdit)
ComfyEdit.register_menu()

_server = getattr(ComfyEdit, "DEFAULT_SERVER", "?")
_out = getattr(ComfyEdit, "DEFAULT_OUT", "?")
_cfg = getattr(ComfyEdit, "STUDIO_CONFIG_PATH", "") or "(defaults / env)"
_studio = getattr(ComfyEdit, "STUDIO_NAME", "ComfyNuke")

print("=" * 60)
print("%s — multi-user launch OK" % _studio)
print("  root:     %s" % _REPO_ROOT)
print("  config:   %s" % _cfg)
print("  ComfyUI:  %s" % _server)
print("  results:  %s" % _out)
print("  workflows:")
print("    edit:   %s" % getattr(ComfyEdit, "DEFAULT_WORKFLOW", ""))
print("    gen:    %s" % getattr(ComfyEdit, "IMAGE_GEN_WORKFLOW", ""))
print("    i2v:    %s" % getattr(ComfyEdit, "I2V_WORKFLOW", ""))
print()
print("  Menu: Nuke > Pix-Edit")
print("    Edit Image... | Image Gen... | Image to Video... | Ping Server")
print()
print("  All artists share ONE ComfyUI queue on the Ubuntu server.")
print("  Wait if the server is busy (jobs run one after another).")
print("=" * 60)

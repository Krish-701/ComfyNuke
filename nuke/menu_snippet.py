# Permanent Nuke menu — load ComfyNuke from studio share or local tree.
#
# In C:/Users/<you>/.nuke/menu.py add:
#
#   import os
#   os.environ["COMFYNUKE_ROOT"] = r"\\192.168.91.13\ComfyNuke"
#   os.environ["COMFYNUKE_SERVER"] = "http://192.168.91.13:8188"
#   exec(open(os.path.join(os.environ["COMFYNUKE_ROOT"], "nuke", "menu_snippet.py"), encoding="utf-8").read())
#
# Or local:
#   exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/menu_snippet.py", encoding="utf-8").read())

from __future__ import print_function

import os
import sys

_ROOT = (os.environ.get("COMFYNUKE_ROOT") or "").strip()
if not _ROOT:
    try:
        _ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    except NameError:
        _ROOT = r"D:/AI-Dev/Krish-ComfyNuke"

os.environ["COMFYNUKE_ROOT"] = _ROOT
_LAUNCH = os.path.join(_ROOT, "nuke", "launch.py")
if not os.path.isfile(_LAUNCH):
    raise RuntimeError("ComfyNuke launch not found: %s" % _LAUNCH)

with open(_LAUNCH, "r", encoding="utf-8") as _f:
    _code = _f.read()
exec(compile(_code, _LAUNCH, "exec"), {"__file__": _LAUNCH, "__name__": "comfynuke_menu"})

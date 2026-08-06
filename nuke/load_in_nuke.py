"""
Nuke Script Editor — local/dev reload.

Multi-user studio (preferred):
  See nuke/artist_launch.txt — launch from Ubuntu share.

Local dev:
  exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/load_in_nuke.py", encoding="utf-8").read())
"""

from __future__ import print_function

import os

try:
    _HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _HERE = r"D:/AI-Dev/Krish-ComfyNuke/nuke"

_LAUNCH = os.path.join(_HERE, "launch.py")
if not os.path.isfile(_LAUNCH):
    _HERE = r"D:/AI-Dev/Krish-ComfyNuke/nuke"
    _LAUNCH = os.path.join(_HERE, "launch.py")

if not os.path.isfile(_LAUNCH):
    raise RuntimeError("launch.py not found next to load_in_nuke.py")

_root = os.path.normpath(os.path.join(_HERE, ".."))
os.environ.setdefault("COMFYNUKE_ROOT", _root)

with open(_LAUNCH, "r", encoding="utf-8") as _f:
    _code = _f.read()
exec(compile(_code, _LAUNCH, "exec"), {"__file__": _LAUNCH, "__name__": "comfynuke_launch"})

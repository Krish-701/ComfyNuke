# Add this to your Nuke menu.py  (usually:  C:/Users/<you>/.nuke/menu.py)
#
#   exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/menu_snippet.py", encoding="utf-8").read())
#
# Or copy the block below into menu.py.

from __future__ import print_function

import importlib.util
import os
import sys

_COMFY_NUKE = r"D:/AI-Dev/Krish-ComfyNuke/nuke"
_COMFY_EDIT = os.path.join(_COMFY_NUKE, "ComfyEdit.py")
_CLIENT = os.path.normpath(os.path.join(_COMFY_NUKE, "..", "client"))

if _COMFY_NUKE not in sys.path:
    sys.path.insert(0, _COMFY_NUKE)
if _CLIENT not in sys.path:
    sys.path.insert(0, _CLIENT)

if "ComfyEdit" in sys.modules:
    del sys.modules["ComfyEdit"]

_spec = importlib.util.spec_from_file_location("ComfyEdit", _COMFY_EDIT)
ComfyEdit = importlib.util.module_from_spec(_spec)
sys.modules["ComfyEdit"] = ComfyEdit
_spec.loader.exec_module(ComfyEdit)
ComfyEdit.register_menu()

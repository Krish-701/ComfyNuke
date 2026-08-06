"""
Nuke Script Editor:
  exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/load_in_nuke.py", encoding="utf-8").read())
"""

from __future__ import print_function

import importlib.util
import os
import sys

COMFY_NUKE_DIR = r"D:/AI-Dev/Krish-ComfyNuke/nuke"
COMFY_EDIT_PY = os.path.join(COMFY_NUKE_DIR, "ComfyEdit.py").replace("\\", "/")
CLIENT_DIR = os.path.normpath(os.path.join(COMFY_NUKE_DIR, "..", "client"))

if not os.path.isfile(COMFY_EDIT_PY):
    raise RuntimeError("ComfyEdit.py not found: %s" % COMFY_EDIT_PY)

if COMFY_NUKE_DIR not in sys.path:
    sys.path.insert(0, COMFY_NUKE_DIR)
if CLIENT_DIR not in sys.path:
    sys.path.insert(0, CLIENT_DIR)

# Drop cached modules so client + Nuke UI both pick up latest code
for _mod in ("ComfyEdit", "comfy_client"):
    if _mod in sys.modules:
        del sys.modules[_mod]

spec = importlib.util.spec_from_file_location("ComfyEdit", COMFY_EDIT_PY)
ComfyEdit = importlib.util.module_from_spec(spec)
sys.modules["ComfyEdit"] = ComfyEdit
spec.loader.exec_module(ComfyEdit)
ComfyEdit.register_menu()

print("=" * 56)
print("ComfyEdit loaded — Edit Image + Image Gen")
print("  server:   ", ComfyEdit.DEFAULT_SERVER)
print("  edit:     ", ComfyEdit.DEFAULT_WORKFLOW)
print("  image gen:", ComfyEdit.IMAGE_GEN_WORKFLOW)
print()
print("  Menu: Nuke > ComfyUI > Edit Image...")
print("        Nuke > ComfyUI > Image Gen...")
print()
print("  Edit:  Roto1 (mask) or Read1 (full frame)")
print("  Gen:   any prompt → node 73 → Comfy → new Read")
print()
print('  ComfyEdit.schedule_image_gen(prompt="mountain landscape")')
print("=" * 56)

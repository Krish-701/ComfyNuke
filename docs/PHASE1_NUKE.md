# Phase 1 — One Nuke → ComfyUI (sequential)

## Setup

| Item | Value |
|------|--------|
| Comfy server | `http://192.168.91.13:8188` |
| Workflow | `Edit_Image_v08.json` (plate `80`, mask `123`, prompt `109.value`) |
| Client | `client/comfy_client.py` |
| Nuke UI | `nuke/ComfyEdit.py` |

## Behaviour

- **One job at a time per Nuke session** — Execute blocks until the image is back.
- **Shared server queue** — If another artist already queued, you wait in Comfy’s queue, then run.
- **Unique filenames** — Multi-Nuke safe (no overwriting inputs/outputs).

## CLI test (Windows, no Nuke)

```bat
cd D:\AI-Dev\Krish-ComfyNuke
python client\comfy_client.py --ping-only --image dummy --prompt x
```

Full edit test:

```bat
python client\comfy_client.py ^
  --server http://192.168.91.13:8188 ^
  --workflow Edit_Image_v08.json ^
  --image "Smart-Image-Crop-and-Stitch\workflows\polar bear 5K.jpg" ^
  --prompt "remove bear and shadow, dont change the color" ^
  --seed 42 ^
  --output-dir client\out
```

## Nuke install

### Why the old line failed

```python
# WRONG — causes: NameError: name 'ComfyEdit' is not defined
exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/ComfyEdit.py").read())
```

`exec(...)` runs the file body but **does not create a module named `ComfyEdit`**.
Menu callbacks and `ComfyEdit.show_panel()` then fail. Also `__file__` is missing under bare `exec`.

### Correct load (Script Editor)

**Option A — easiest:** paste / run the loader:

```python
exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/load_in_nuke.py", encoding="utf-8").read())
```

**Option B — full import block:**

```python
import importlib.util, sys
_path = r"D:/AI-Dev/Krish-ComfyNuke/nuke/ComfyEdit.py"
_spec = importlib.util.spec_from_file_location("ComfyEdit", _path)
ComfyEdit = importlib.util.module_from_spec(_spec)
sys.modules["ComfyEdit"] = ComfyEdit
_spec.loader.exec_module(ComfyEdit)
ComfyEdit.register_menu()
ComfyEdit.show_panel()
```

You should see `ComfyEdit loaded OK` and a panel. Menu: **Nuke → ComfyUI**.

### Permanent menu

Add to `~/.nuke/menu.py` (Windows often `C:/Users/<you>/.nuke/menu.py`):

```python
exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/menu_snippet.py", encoding="utf-8").read())
```

## Nuke graph tip

Your graph (correct for Phase 1):

```
Read1 (plate) → Roto1 (output: alpha, bg = plate) → SELECT Roto1 → Execute
```

- **RGB** comes through Roto bg from the plate  
- **Alpha** = roto shape (sent to Comfy as mask via PNG alpha)  
- Select **Roto1** (not only Viewer) before Execute  

### Error: "I'm already executing something else"

This is a **Nuke UI lock**, not ComfyUI.

- Cause: `nuke.execute(Write)` was called from the panel button callback  
- Fix: panel now uses `nuke.executeDeferred` so the write runs after the UI unlocks  
- Reload the tool (`load_in_nuke.py`) and try Execute again  

Comfy can be idle (0 jobs) while Nuke still shows this message — they are unrelated.

## Sequential multi-user (later)

Same gizmo on every Nuke PC, same server URL. Each artist hits Execute; Comfy queues jobs. GPU runs one, returns image, next starts. No parallel GPU jobs on one A6000.

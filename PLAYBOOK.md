# PLAYBOOK — Krish-ComfyNuke (Nuke ↔ ComfyUI)

Handoff for an autonomous agent that will run commands and edit files.
Labels: **[VERIFIED]** observed in this workspace/code/runtime · **[INFERRED]** follows from code/evidence, not end-to-end retested here · **[ASSUMED]** no proof — verify before acting · **GAP** unknown, do not invent.

---

## 0. Read First

1. **Do not change the default Comfy server URL, force-push, delete `client/out` results the user may still need, or queue jobs on `http://192.168.91.13:8188` without human approval** if the human is not present — that host is a shared Linux ComfyUI box. [VERIFIED] server default is `http://192.168.91.13:8188` in `nuke/ComfyEdit.py` and `client/comfy_client.py`.
2. **Before any edit**, from a shell on this machine:
   ```bat
   cd /d D:\AI-Dev\Krish-ComfyNuke
   python --version
   python -c "from client.comfy_client import ComfyClient; c=ComfyClient(workflow_path=r'D:\AI-Dev\Krish-ComfyNuke\video_minimax_h3_i2v.json'); c.load_workflow(); print(c.id_load, c.id_prompt, c.id_prompt_key)"
   ```
   Expected inject for i2v: `114 141 value`. [VERIFIED] 2026-08-05 on this machine with Python 3.12.4.
3. **Never reintroduce Python worker threads + `nuke.executeInMainThread` for Comfy polling.** Polling is main-thread `QTimer` only (`_poll_comfy_tick` in `nuke/ComfyEdit.py`). Threads caused Nuke crashes (see §5). [INFERRED] from code comments + conversation history; crash not re-reproduced in this handoff session.
4. **Never inject artist prompt into LLM system PrimitiveStringMultiline** (edit: system is separate from user node `289`; i2v: system is node `140`, user is `141`). Auto-discovery uses `user_prompt_input` → linked primitive. [VERIFIED] discovery returns `141`/`value` for i2v and `289`/`value` for edit.
5. **If reality contradicts this doc** (different server, missing workflow nodes, different prompt IDs): stop, re-run discovery with `ComfyClient.load_workflow()` on the actual JSON on disk, and ask a human before rewriting inject IDs or changing production server.
6. **Stop and ask a human** before: changing Comfy models/paths on the Linux server; deleting workflows; amending git history; force-push; spending GPU time with long MiniMax i2v runs if the queue is shared.
7. **Nuke UI work must be validated inside Nuke.** Shell tests only cover `comfy_client` inject/HTTP, not Write/Roto/Read.

---

## 1. Purpose and Current State

### Purpose
Multi-artist **Nuke (Windows)** talks to **ComfyUI (Linux, default `http://192.168.91.13:8188`)** for three modes:

| Mode | Menu | Workflow file | Inject summary |
|------|------|---------------|----------------|
| Edit Image | Pix-Edit → Edit Image... | `D:\AI-Dev\Krish-ComfyNuke\Edit_Image_v08.json` | plate LoadImage `80`, mask LoadImage `123`, prompt `109.value`, SaveImage `121` |
| Image Gen (txt2img) | ComfyUI → Image Gen... | `D:\AI-Dev\Krish-ComfyNuke\Image_generation_v01.json` | no LoadImage, prompt `73.value`, SaveImage `29` |
| Image to Video | ComfyUI → Image to Video... | `D:\AI-Dev\Krish-ComfyNuke\video_minimax_h3_i2v.json` | LoadImage `114`, user prompt `141.value`, RandomNoise seed `132.noise_seed`, SaveVideo `92` |

[VERIFIED] node IDs via `ComfyClient.load_workflow()` on this disk 2026-08-05.

Jobs are **sequential per Nuke session** (`_BG_JOB_ACTIVE` + client job lock). Export PNG → upload → `/prompt` → history poll → download → new Nuke `Read` named `ComfyEdit_Result_###`. Progress in Script Editor log; popup when done.

### Done (code present; confidence mixed)
- **Edit Image path** (Roto mask or full-frame Read): implemented in `nuke/ComfyEdit.py`. [ASSUMED] still works end-to-end unless workflows/server models moved; not re-run against live Comfy in this handoff session.
- **Image Gen path**: implemented; [ASSUMED] live OK if server has that workflow’s models.
- **Image to Video path**: implemented; user reported **video return working**. [VERIFIED] local out files exist e.g. `D:\AI-Dev\Krish-ComfyNuke\client\out\comfy_i2v_20260805_172952_90fec0ad.mp4` and `last_comfy_result.mp4`.
- **SaveVideo support in `build_prompt`**: without this, i2v failed with `Workflow has no SaveImage / stitch / preview image source`. [VERIFIED] fixed; post-fix build sets `id_save=92`, no fake SaveImage.
- **Video Read frame range + format**: `_probe_video_media` + `_apply_video_read_range` so Read is not 1-frame hold. [VERIFIED] probe on sample mp4 returned `width=1376 height=768 fps=24.0 frames=124` via OpenCV (ffprobe not on PATH). [INFERRED] Nuke Read knobs set correctly when that path runs inside Nuke; full Nuke retest not done in this handoff write.
- **Menu trimmed**: only Edit Image / Image Gen / Image to Video / Ping Server (no “Run … on selected” shortcuts). [VERIFIED] `register_menu` in current `ComfyEdit.py`.

### Half-done / stale
- `nuke/load_in_nuke.py` banner still prints only “Edit Image + Image Gen” and omits i2v menu lines. [VERIFIED] file content. Loader still works; message is outdated.
- `docs/PHASE1_NUKE.md` documents Phase 1 edit focus; may lag i2v/menu. Prefer this PLAYBOOK + source.

### Untouched (do not invent status)
- GAP: multi-artist queue fairness beyond “one job per Nuke session + Comfy server queue”.
- GAP: permanent install in every artist’s `~/.nuke/menu.py` (snippet exists; who installed what unknown).
- GAP: ComfyUI custom node / model versions on `192.168.91.13`.
- Nukomfy Suite under `ComfyUI-Nukomfy-Suite/` is a separate package in-repo; **not** the Nuke menu path used by `ComfyEdit.py`. [VERIFIED] separate tree; integration status not claimed.

### Resume point
If continuing development: **stabilize video Read in Nuke** (confirm `first`/`last`/`format` after live i2v), then refresh `load_in_nuke.py` print strings. No open code compile failures known for the SaveVideo inject path.

---

## 2. Setup and Orientation

### Environment
| Item | Value | Label |
|------|--------|--------|
| Workspace | `D:\AI-Dev\Krish-ComfyNuke` | [VERIFIED] |
| OS (dev machine) | Windows (PowerShell/cmd) | [VERIFIED] user_info |
| Python (shell) | 3.12.4 | [VERIFIED] `python --version` |
| Nuke Python | GAP: exact Nuke version not recorded in this session | |
| Comfy server default | `http://192.168.91.13:8188` | [VERIFIED] code |
| Credentials | None in repo for Comfy HTTP API (open LAN assumed) | [ASSUMED] no auth headers in client |
| Secrets | N/A in code for Comfy | — |

### Key paths
| Role | Full path |
|------|-----------|
| Nuke UI + export | `D:\AI-Dev\Krish-ComfyNuke\nuke\ComfyEdit.py` |
| Reload entry | `D:\AI-Dev\Krish-ComfyNuke\nuke\load_in_nuke.py` |
| Permanent menu snippet | `D:\AI-Dev\Krish-ComfyNuke\nuke\menu_snippet.py` |
| HTTP client | `D:\AI-Dev\Krish-ComfyNuke\client\comfy_client.py` |
| Config example | `D:\AI-Dev\Krish-ComfyNuke\client\config.example.json` |
| Download dir | `D:\AI-Dev\Krish-ComfyNuke\client\out\` |
| Edit workflow | `D:\AI-Dev\Krish-ComfyNuke\Edit_Image_v08.json` |
| Gen workflow | `D:\AI-Dev\Krish-ComfyNuke\Image_generation_v01.json` |
| I2V workflow | `D:\AI-Dev\Krish-ComfyNuke\video_minimax_h3_i2v.json` |
| Nuke temp exports | `%TEMP%\comfy_nuke\` e.g. `C:\Users\[REPLACE: windows user]\AppData\Local\Temp\comfy_nuke\` — files: `plate_srgb.png`, `input_rgba.png`, `mask_luma.png`, `i2v_frame.png`, … |

### Practical lookup
- Change default server: `DEFAULT_SERVER` in `nuke/ComfyEdit.py` **and** `ComfyClient.__init__` default / CLI in `client/comfy_client.py`.
- Change default workflows: `DEFAULT_WORKFLOW`, `IMAGE_GEN_WORKFLOW`, `I2V_WORKFLOW` at top of `ComfyEdit.py`.
- Grep inject discovery: `def _find_user_prompt_inject` and `def build_prompt` in `client/comfy_client.py`.
- Grep poll: `def _poll_comfy_tick` in `nuke/ComfyEdit.py`.
- Grep video Read: `def _probe_video_media`, `def _apply_video_read_range`, `def _create_result_read` in `nuke/ComfyEdit.py`.
- Logs: Nuke Script Editor (`nuke.tprint` / `_log`); no dedicated log file. Client CLI prints to stdout.
- Docs: `D:\AI-Dev\Krish-ComfyNuke\docs\PHASE1_NUKE.md`, `docs\NUKOMFY_REFERENCE.md`.

### Dependencies (runtime)
- Nuke with PySide2 or PySide6 for `QTimer`. [VERIFIED] `_qt_timer` tries both.
- Network to Comfy `/prompt`, `/history/{id}`, `/view`, `/upload/image`.
- Pillow for alpha invert on edit path. [INFERRED] used in `_invert_png_alpha_pil`.
- Optional: OpenCV (`cv2`) for video probe when `ffprobe` missing. [VERIFIED] probe used OpenCV successfully; ffprobe raised `WinError 2` (not found).
- Comfy custom nodes for workflows (LLM, MiniMaxH3ImageToVideo, Smart crop, etc.) — **must exist on server**. Versions GAP.

### Load into Nuke (exact)
```python
exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/load_in_nuke.py", encoding="utf-8").read())
```
[VERIFIED] path in `load_in_nuke.py` docstring and `ComfyEdit.py` header.

**Wrong** (documented failure):
```python
exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/ComfyEdit.py").read())
```
Causes `NameError: name 'ComfyEdit' is not defined` for module-style use. [VERIFIED] `docs/PHASE1_NUKE.md`.

---

## 3. Problem and Root Cause

Multiple historical issues. Most important for **current** i2v:

### A. Image-to-video queue failed without SaveImage
- **Symptom:** I2V export/upload OK, then error when building prompt.
- **Exact error:** `Workflow has no SaveImage / stitch / preview image source` (`ComfyError` from `build_prompt`).
- **Reproduce (clean shell, no Nuke):**
  ```bat
  cd /d D:\AI-Dev\Krish-ComfyNuke
  python -c "from client.comfy_client import ComfyClient; c=ComfyClient(workflow_path='video_minimax_h3_i2v.json'); c.load_workflow(); c.build_prompt(image_name='t.png', prompt='x', seed=1)"
  ```
  **Before fix:** that error. **After fix:** succeeds; SaveVideo `92` `filename_prefix` like `video/i2v_t`. [VERIFIED] after-fix run 2026-08-05.
- **Root cause:** `build_prompt` required `SaveImage` or stitch/preview image source. `video_minimax_h3_i2v.json` has only `SaveVideo` (`92`) + `CreateVideo` (`133`), no `SaveImage`.
- **Proof:** workflow JSON node `92` `class_type` `SaveVideo`; discovery listed savers `[('92', 'SaveVideo'), ('133', 'CreateVideo')]`.

### B. Returned video Read stuck on 1 frame / wrong format
- **Symptom (user):** video downloads but Read holds one frame (`first`/`last` effectively single-frame hold); dimensions not matching video.
- **Root cause (code-level):** stills path pins `first=last=current_frame`. Early video path only logged “let Nuke detect” without forcing multi-frame range/format after reload. [VERIFIED] prior code structure; user confirmation that return worked but range wrong.
- **Mitigation added:** `_probe_video_media` + `_apply_video_read_range` sets `first`/`last`/`orig*` and `nuke.addFormat` + `read['format']`. [VERIFIED] code + OpenCV probe numbers on sample mp4.

### C. Edit-path historical issues (context for traps)
| Symptom | Cause (as encoded in code/docs) | Label |
|---------|----------------------------------|--------|
| EXR full-frame alpha treated as mask | Prefer EXR outputs; full-frame A=255 treated carefully; mask via mask_luma | [INFERRED] comments/history |
| Nuke crash | Qt invert / Python threads + main-thread callbacks | [INFERRED] comments in `ComfyEdit.py` |
| Wrong load `exec(ComfyEdit.py)` | Module not registered as `ComfyEdit` | [VERIFIED] PHASE1 doc |
| Image gen missing LoadImage 278 | txt2img has no LoadImage; client must allow `id_load=None` | [VERIFIED] discovery `load None` for gen |

### D. Prompt inject for i2v
- User requirement: node **141** `PrimitiveStringMultiline` `value` for artist prompt; current frame to **114**.
- [VERIFIED] auto-discovery hits `141`/`value` via LLM `user_prompt_input`; system prompt stays on `140`.

---

## 4. The Working Solution

### 4.1 Shell: verify i2v inject (safe, no GPU)
```bat
cd /d D:\AI-Dev\Krish-ComfyNuke
python -c "from client.comfy_client import ComfyClient; c=ComfyClient(workflow_path=r'D:\AI-Dev\Krish-ComfyNuke\video_minimax_h3_i2v.json'); c.load_workflow(); print('load',c.id_load); print('prompt',c.id_prompt,c.id_prompt_key); print('seed',c.id_seed,c.id_seed_key); wf=c.build_prompt(image_name='test_frame.png',prompt='gentle camera move',seed=42,filename_prefix='nuke/host/i2v_test'); print(wf['114']['inputs']['image'], wf['141']['inputs']['value'], wf['92']['inputs']['filename_prefix'], c.id_save)"
```
**Success output shape (exact keys):**
```
load 114
prompt 141 value
seed 132 noise_seed
test_frame.png gentle camera move video/i2v_test 92
```
[VERIFIED] equivalent run succeeded.

**If fails with SaveImage error:** `build_prompt` regression — restore SaveVideo branch (see §8).

**If fails with prompt node error:** workflow JSON changed — re-open `video_minimax_h3_i2v.json` and re-discover.

### 4.2 Shell: probe a downloaded video (safe)
```bat
cd /d D:\AI-Dev\Krish-ComfyNuke
python -c "import sys; sys.path.insert(0,'nuke'); import ComfyEdit as ce; print(ce._probe_video_media(r'D:\AI-Dev\Krish-ComfyNuke\client\out\comfy_i2v_20260805_172452_36e79ce0.mp4'))"
```
**Observed success:** `{'width': 1376, 'height': 768, 'fps': 24.0, 'frames': 124}` (OpenCV). [VERIFIED]  
May log: `[ComfyEdit] ffprobe skip: [WinError 2] The system cannot find the file specified` — harmless if OpenCV works.

### 4.3 Shell: ping Comfy (network; human approval if shared)
```bat
cd /d D:\AI-Dev\Krish-ComfyNuke
python client\comfy_client.py --ping-only --image dummy --prompt x --server http://192.168.91.13:8188
```
[VERIFIED] documented in `docs/PHASE1_NUKE.md`. Live success depends on server up.

### 4.4 Nuke: reload UI code (safe)
In Nuke Script Editor:
```python
exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/load_in_nuke.py", encoding="utf-8").read())
```
**Success:** menu `Nuke → ComfyUI` with Edit Image / Image Gen / Image to Video / Ping Server. Banner may still omit i2v in print text (stale). [VERIFIED] menu registration code; [INFERRED] banner stale only.

### 4.5 Nuke: Image to Video end-to-end (costs GPU time — not silent-rerun)
1. Select a **Read**, **Merge**, or **Roto** (any node that can yield an image via export).
2. Set timeline frame to the frame to send.
3. **ComfyUI → Image to Video...**
4. Enter motion prompt (goes to node **141**).
5. Optional: uncheck Random seed / set seed.
6. Confirm. Keep working; watch Script Editor for `Inject: load=114 prompt=141.value` and later download.
7. Popup: `ComfyUI done (video).` + new `ComfyEdit_Result_###` Read.
8. On that Read: **`first`/`last` should span video duration** (e.g. 1–124), format ≈ media resolution — not 1–1 hold.

**Timeout:** poll timeout for i2v is **1800 s** (30 min) in `schedule_image_to_video`. [VERIFIED] code. Generation can take many minutes; log may show `Comfy running (Ns)…` every ~10s.

**Do not re-queue** while `_BG_JOB_ACTIVE` — second job shows “A Comfy job is already running…”.

### 4.6 Nuke: Edit Image (summary)
1. Prefer **Roto** with matte for masked edit, or **Read** full frame.
2. **ComfyUI → Edit Image...** → prompt → node **289**.
3. Export builds RGBA under `%TEMP%\comfy_nuke\input_rgba.png` (overwrite).
4. Poll timeout **600 s** default for edit path. [VERIFIED] `_POLL_STATE` timeout 600 for edit.

### 4.7 Nuke: Image Gen
1. **ComfyUI → Image Gen...** — no selection required for generation itself.
2. Prompt → node **73**. No upload. [VERIFIED] `id_load is None`.

### 4.8 File edit that fixed SaveVideo (do not reverse)
In `client/comfy_client.py` `build_prompt`, after computing `prefix`:
- If `SaveImage` exists → set its `filename_prefix`.
- **Else if** `SaveVideo` / `VHS_VideoCombine` / `CreateVideo` present → set `id_save` to that node; **do not** require stitch/SaveImage.
- Else inject SaveImage from stitch/preview as before.
- Loop still sets SaveVideo `filename_prefix` to `video/{base}` where `base = Path(prefix).name`.

---

## 5. What FAILED — and Why This Way, Not Another

### Dead ends / failures
1. **`build_prompt` hard-required SaveImage for all workflows**  
   - Looked reasonable for edit/gen (both have SaveImage).  
   - Failed on MiniMax i2v with exact `ComfyError: Workflow has no SaveImage / stitch / preview image source`.  
   - Alternative rejected: always inject a dummy SaveImage wired to CreateVideo frames — would fight video pipeline and duplicate outputs. Prefer native SaveVideo history download.

2. **Bare `exec(ComfyEdit.py)` in Nuke**  
   - Looked like quick load.  
   - Failed: module name / callbacks break (`NameError: name 'ComfyEdit' is not defined` per PHASE1).  
   - Solution: `load_in_nuke.py` + `sys.modules['ComfyEdit']`.

3. **Python threads for Comfy wait**  
   - Looked good for non-blocking UI.  
   - Failed: Nuke crashes when touching Nuke API off main thread / via unsafe callbacks.  
   - Solution: **only** `QTimer.singleShot` → `_poll_comfy_tick` on main thread. Do not “improve” with threads.

4. **Qt-based alpha invert**  
   - Looked fine for mask flip.  
   - Crashed Nuke (bits hacks).  
   - Solution: **PIL-only** invert for alpha/mask_luma.

5. **Assuming LoadImage always exists**  
   - Edit uses 278; gen has none.  
   - Error path if forced (historical: Image Gen + node 278).  
   - Solution: `id_load` optional; only set image if present.

6. **Injecting prompt into wrong PrimitiveStringMultiline**  
   - i2v has two: `140` system (huge compiler system text), `141` user.  
   - If agent picks “first multiline”, may hit system or wrong node depending on order.  
   - Solution: `_find_user_prompt_inject` follows LLM `user_prompt_input` link first.

7. **Video Read: trust Nuke auto range only**  
   - Sometimes left 1-frame hold.  
   - Solution: probe frames + set knobs explicitly; set format from media size.

8. **Prefer EXR over video in multi-output history** (if ever both present)  
   - `prefer_output_file` scores video extensions first, then EXR, then PNG. [VERIFIED] code. Changing score order can download wrong media type.

### Design choices to preserve
| Choice | Why | Do not replace with |
|--------|-----|---------------------|
| Sequential jobs per Nuke session | Shared GPU; simpler client | Parallel multi-prompt from one Nuke |
| Fixed temp filenames under `%TEMP%\comfy_nuke\` | Avoid temp spam | UUID per write every time (disk clutter) |
| Unique Comfy `filename_prefix` per job (`nuke/<host>/<stamp>`) | Multi-artist no clobber | Fixed `ComfyUI_00001` style |
| Auto-discover inject nodes | Workflow renumbers | Hardcode only (still have fallbacks NODE_*) |
| SaveVideo `video/{stamp}` prefix | Isolates video outputs | Same prefix as SaveImage without `video/` |
| Random seed default ON in panels | Artist variety | Always seed=42 only |
| Background poll + log, popup on complete | UX while waiting long i2v | Modal blocking for 30 min |
| No “Run on selected” menu shortcuts | User asked remove; less accidental fire | Re-adding without ask |

---

## 6. Traps

### DO NOT TOUCH (deliberate)
- **Main-thread-only poll** (`_poll_comfy_tick`, `_schedule_ms`) — not redundant.
- **PIL invert, not Qt** — crash avoidance.
- **System prompt node 140 (i2v) / edit system path** — do not overwrite with artist text.
- **`prefer_output_file` video-first ordering** — i2v depends on it if multiple files appear.
- **SaveVideo branch without injecting SaveImage** — reverse breaks i2v.
- **Write `inpanel=False`** where used — panel popups / locks historically painful.

### Order / silent no-ops
- If `load_in_nuke.py` path wrong → RuntimeError `ComfyEdit.py not found`.
- If job already active → early return message; second click appears to “do nothing” beyond message.
- If prompt empty → “Prompt is empty” / no queue.
- If no node selected for i2v/edit → message to select Read/Merge/Roto.
- `build_prompt` with `image_name` but gen workflow: image ignored (no LoadImage) — not an error.
- `ffprobe` missing: probe falls back to OpenCV; log noise only.

### Expected noise vs real failure
| Output | Meaning |
|--------|---------|
| `ffprobe skip: [WinError 2] The system cannot find the file specified` | Harmless if frames/size still probed | [VERIFIED] |
| `Comfy waiting (Ns)…` / `Comfy running (Ns)…` | Normal poll | |
| `Workflow has no SaveImage / stitch / preview image source` | **Real failure** — SaveVideo path broken | |
| `Job finished but no output media (image/video)` | History empty or wrong output type | |
| `A Comfy job is already running in this Nuke session` | Real lock, not noise | |
| Popup `ComfyUI done (video)` with Read **first=last=1** and still image | **False success for duration** — probe/range failed | [INFERRED] |

### Slow operations
| Step | Rough duration |
|------|----------------|
| Nuke PNG export | seconds |
| Upload frame | seconds (size-dependent) |
| Edit / image gen | often minutes (queue + model) |
| MiniMax i2v | many minutes; timeout 1800s | [VERIFIED] timeout; duration GAP per run |
| Download mp4 | seconds–tens of seconds |

### Dangerous
- Queueing on shared `192.168.91.13:8188` consumes GPU and blocks other artists.
- Overwriting `%TEMP%\comfy_nuke\*.png` every run — expected; do not point other tools at those as permanent.
- Deleting `client/out/*` loses artist deliverables (e.g. `comfy_i2v_*.mp4`).
- Long i2v is cost/time on production box.

---

## 7. Verification and Rollback

### A. Inject unit checks (no server required for discovery; build_prompt local only)
Commands in §4.1–4.2.  
**False positive:** `load_workflow` succeeds but you never call `build_prompt` — SaveVideo bug stays hidden until queue time.

### B. Live Comfy ping
```bat
python client\comfy_client.py --ping-only --image dummy --prompt x --server http://192.168.91.13:8188
```
**False positive:** ping OK does not prove models for MiniMax/LLM are loaded.

### C. Full i2v (Nuke)
1. Reload via `load_in_nuke.py`.
2. Run Image to Video on a known plate.
3. Confirm log: `Inject: load=114 prompt=141.value`.
4. Confirm file under `D:\AI-Dev\Krish-ComfyNuke\client\out\comfy_i2v_*.mp4`.
5. Confirm Read: multi-frame range, format matches probe.

**False positive:** file downloads and Read appears, but `first=last=1` (still broken for duration). Check knobs and Script Editor lines:
- `Video Read range X–Y`
- `Video Read format → WxH`

### D. Probe-only false positive
OpenCV may report frame count wrong on some codecs; if Nuke disagrees after reload, prefer Nuke `origlast > origfirst` when valid (code already prefers Nuke multi-frame orig range when `ol > of`).

### Rollback
| Change | Undo |
|--------|------|
| Code edits in `comfy_client.py` / `ComfyEdit.py` | `git checkout --` those paths if under git; else restore from `D:\AI-Dev\Krish-ComfyNuke\backup\V01\nuke\ComfyEdit.py` **only if** that backup is intentionally older (may lack i2v). [VERIFIED] backup tree exists; **not** bit-identical to current — treat carefully. |
| Queued Comfy job | Cancel from ComfyUI UI on server if available; client has no cancel API documented here. GAP exact cancel. |
| Downloaded files | Delete specific `client\out\comfy_*` — irreversible for that file. |
| Nuke Read nodes | Delete `ComfyEdit_Result_*` in script — does not delete media on disk. |

Nothing in client automatically deletes remote Comfy output folders beyond unique prefixes.

---

## 8. Changes Made and Open Questions

### Files (primary integration surface)
| Path | Role / one-line summary |
|------|-------------------------|
| `D:\AI-Dev\Krish-ComfyNuke\nuke\ComfyEdit.py` | Nuke menus, export (edit/i2v), QTimer poll, result Read, video range/format helpers |
| `D:\AI-Dev\Krish-ComfyNuke\nuke\load_in_nuke.py` | Safe reload of ComfyEdit + client (stale banner text) |
| `D:\AI-Dev\Krish-ComfyNuke\nuke\menu_snippet.py` | For `~/.nuke/menu.py` permanent load |
| `D:\AI-Dev\Krish-ComfyNuke\client\comfy_client.py` | HTTP API, inject discovery, build_prompt, SaveVideo support, prefer video/EXR/PNG |
| `D:\AI-Dev\Krish-ComfyNuke\client\config.example.json` | Example server/workflow/timeout |
| `D:\AI-Dev\Krish-ComfyNuke\Edit_Image_v08.json` | Edit workflow API format |
| `D:\AI-Dev\Krish-ComfyNuke\Image_generation_v01.json` | Txt2img workflow |
| `D:\AI-Dev\Krish-ComfyNuke\video_minimax_h3_i2v.json` | MiniMax H3 i2v (114 / 141 / 92) |
| `D:\AI-Dev\Krish-ComfyNuke\client\out\` | Downloaded results (png/mp4) |
| `D:\AI-Dev\Krish-ComfyNuke\docs\PHASE1_NUKE.md` | Phase 1 docs (edit-focused) |
| `D:\AI-Dev\Krish-ComfyNuke\docs\NUKOMFY_REFERENCE.md` | Nukomfy reference |
| `D:\AI-Dev\Krish-ComfyNuke\backup\V01\nuke\` | Older snapshot of nuke files |
| `D:\AI-Dev\Krish-ComfyNuke\ComfyUI-Nukomfy-Suite\` | Separate Comfy custom nodes package in-repo |
| `D:\AI-Dev\Krish-ComfyNuke\Smart-Image-Crop-and-Stitch\` | Crop/stitch custom nodes + sample assets |
| `D:\AI-Dev\Krish-ComfyNuke\PLAYBOOK.md` | This handoff |

Deleted files in recent work: **none known** for the i2v fix path. Menu *commands* removed (not files): “Run Edit on selected…”, “Run Image Gen…”, “Run Image to Video on selected…”.

### Open questions / gaps
1. Exact Nuke version and whether all artists use the same `load_in_nuke` path.
2. Whether `ffprobe` should be required on artist PCs or OpenCV-only is policy.
3. MiniMax duration/resolution defaults (workflow node `136` PrimitiveFloat duration value `5`) — intentional length?
4. LLM base URL in workflow `137` is `http://127.0.0.0:11434/` (Ollama on **Comfy host**) — must be healthy for prompt compile; if Ollama down, i2v may fail mid-graph. [VERIFIED] string in JSON; runtime health GAP.
5. Should root project frame range / fps auto-expand to match video? Currently only Read node is adjusted. [ASSUMED] user wanted Read duration only.
6. `load_in_nuke.py` print still outdated — should be fixed next cosmetic pass.
7. Full retest of Edit Image + EXR prefer after i2v changes — not done in this handoff session.
8. Auth / HTTPS / non-default ports for other facilities — not supported beyond editable server string.

### Questions to ask a human if available
- Is `192.168.91.13:8188` still the only production endpoint?
- Confirm latest live i2v Read shows correct frame range after the `_apply_video_read_range` change (user had return working *before* range fix; range fix not re-confirmed by user in a later message).
- Any need to re-add quick-run menu items?

---

## Appendix A — Workflow inject cheatsheet [VERIFIED discovery 2026-08-05]

```
Edit_Image_v08.json
  LoadImage 278 | prompt 289 value | seed 185 seed | SaveImage 299

Image_generation_v01.json
  LoadImage None | prompt 73 value | seed 53 seed | SaveImage 29

video_minimax_h3_i2v.json
  LoadImage 114 | prompt 141 value | seed 132 noise_seed | SaveVideo 92
  (CreateVideo 133 also present; do not require SaveImage)
```

## Appendix B — Nuke reload one-liner [VERIFIED]

```python
exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/load_in_nuke.py", encoding="utf-8").read())
```

---

*End of PLAYBOOK.md*

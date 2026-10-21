# ComfyNuke playbook

Handoff for the next person or agent. Written **2026-10-20** against `main` commit `eb0ca15` on https://github.com/Krish-701/ComfyNuke.

If this file and the code disagree, trust the code and the workflow JSON on disk. Then update this file.

Older docs (`docs/STUDIO_PLAYBOOK.md`, `README.md` history, anything that says `192.168.91.13` or `D:\AI-Dev\Krish-ComfyNuke`) are stale. This hub is **`192.168.91.11`**.

---

## 0. What this tool is

ComfyNuke lets Nuke artists run ComfyUI graphs without Samba or SSH.

Artists paste one line into the Nuke Script Editor. That downloads `nuke/remote_bootstrap.py` from the hub, syncs scripts and workflow JSON into `~/.comfynuke/cache` on the artist machine, and registers the **Nuke → Pix-Edit** menu.

Jobs do not talk to ComfyUI directly. They go through the hub on port **8600**, which checks the artist IP, optionally requires a project code, proxies `/prompt` to the right GPU box, and writes a usage log.

```
Nuke (Windows)
  GET  http://192.168.91.11:8600/nuke/remote_bootstrap.py
  GET  scripts + Workflows/*.json + workflow_routes.json
  POST http://192.168.91.11:8600/comfyui-r/<server_id>/prompt
        → 127.0.0.1:8166          Edit Image
        → 127.0.0.1:8177          Image Gen, Image Description
        → 192.168.91.12:8166      Hi-res, Ref Hi-res, Outpaint, Comp
        → 127.0.0.1:8188          default /comfyui only
```

Repo on the hub: `/home/radhakrishnan/ComfyUI-Setup/ComfyNuke`.

---

## 1. Rules before you change anything

1. Artists only use HTTP. Do not add Samba, SSH, or a file share as the way they get code.
2. Do not invent node IDs. Open the workflow JSON and confirm `class_type` and the input key (`image`, `video`, `value`).
3. Do not send Edit Image to `:8188`. `Edit_Image_v08.json` needs WAS Node Suite, which is on this hub’s `:8166`. A missing node fails the whole graph.
4. Do not point Nuke at raw `:8166`, `:8177`, or `:8188`. Jobs stay on `:8600/comfyui-r/<id>` so the IP gate and project-code check run.
5. Poll inside Nuke on the main thread with `QTimer` (`_poll_comfy_tick`). Do not add Python worker threads plus `nuke.executeInMainThread`. That crashed Nuke.
6. `upload_image()` in `client/comfy_client.py` forces unknown extensions to `.png`. Movies must use `upload_video()`, which keeps `.mp4` / `.mov` / `.mkv` / `.webm` / `.avi`.
7. Do not commit `studio_config.json`, `server/access_control.json`, `server/project_codes.json`, `server/usage_logs.jsonl`, `.grok/`, `.forbidden`, or `temp-old/`.
8. Do not store a GitHub token in the repo, the git remote, or a commit. If someone pastes a PAT, use it once for that push and tell them to revoke it.
9. Do not tick a workflow as “requires a project code” unless a human asks. `require_workflows` starts empty so artists are not blocked.
10. Do not `kill -9` CUDA or ComfyUI processes. SIGTERM only. Do not restart ComfyUI just because the code server changed.
11. Do not move live graphs out of `Workflows/`. Nested Comp graphs stay in `Workflows/Comp/`.
12. Nuke UI changes are not proven until an artist reloads the script and runs the menu. Shell checks do not open Nuke.

---

## 2. Where the code lives

| Path | Role |
|------|------|
| `nuke/ComfyEdit.py` | Pix-Edit menu, export, project-code memory, queue, poll, result Read |
| `nuke/remote_bootstrap.py` | What artists execute. Sync list must match `SYNC_FILES` |
| `nuke/artist_one_liner.txt` | Paste into Nuke |
| `client/comfy_client.py` | Upload, build prompt, queue, download |
| `server/serve_code.py` | Hub HTTP `:8600`: files, IP gate, proxy, admin, project-code enforcement |
| `server/admin_ui.html` | Admin page. Read from disk each request (`Cache-Control: no-store`) |
| `server/project_codes.py` | Project codes, groups, which workflows require a code |
| `server/access_control.py` | Machines, roles, `skip_project_code` (R&D) |
| `server/usage_log.py` | Who ran which Nuke tool, project, iteration |
| `server/workflow_routes.py` | Load/save routes; discover workflow JSON for the admin tick list |
| `workflow_routes.json` | Live route table. Nuke reads it through `/api/comfy-routes` |
| `Workflows/` | Live graphs artists sync. Flat files plus `Workflows/Comp/` |
| `studio_config.json` | Gitignored local fallback. Prefer the live routes file |

Artist cache after bootstrap: `~/.comfynuke/cache/` on that Nuke machine. Remembered project code: `~/.comfynuke/project_code.json` (`code`, `left`). That file never goes to the hub.

---

## 3. Hub process

Admin: http://192.168.91.11:8600/admin

Health: `curl -sS http://127.0.0.1:8600/health` → `ComfyNuke code server OK`.

The systemd unit `comfynuke-code.service` is **inactive**. The process that is actually serving `:8600` is a manual `nohup`:

```bash
/usr/bin/python3 server/serve_code.py \
  --root /home/radhakrishnan/ComfyUI-Setup/ComfyNuke \
  --host 0.0.0.0 --port 8600
```

Log: `/tmp/comfynuke-code-8600.log`.

Restart only this process, and only after a Python change on the hub (`serve_code.py`, `project_codes.py`, `usage_log.py`, `workflow_routes.py`, `access_control.py`). Find the pid with `ps -ef | grep serve_code.py`. SIGTERM that pid. Start the same command with `nohup` again. Confirm `/health` and that an in-flight artist request still returns 200.

These are read from disk and do **not** need a restart:

- `server/admin_ui.html`
- `workflow_routes.json`
- workflow JSON under `Workflows/`
- `nuke/ComfyEdit.py` and `nuke/remote_bootstrap.py` (artists download them on the next Nuke launch)

`SYNC_FILES` inside `serve_code.py` is in memory. A new sync path is invisible to `/manifest.json` until the code server restarts. The nested file is still downloadable before that restart if the URL is under `Workflows/` (`ALLOWED_PREFIXES`).

Do not start `:8188` with `start-comfyui-production.sh`. That script can SIGTERM the existing `:8177` process. Port details and GPU warnings are in `docs/STUDIO_PLAYBOOK.md` §2. Treat that file as ops notes, not as the menu or node-ID list.

| Port | What |
|------|------|
| **8600** | This repo’s code server and proxy |
| **8166** on this hub | Edit ComfyUI. Edit Image v08 |
| **8177** on this hub | Production. Image Gen and Image Description |
| **8188** on this hub | Extra production listener. Default `/comfyui` proxy |
| **8166** on `192.168.91.12` | Route id `8166-12`. Hi-res, Ref Hi-res, Outpaint, Comp |

---

## 4. Artist launch

Every Nuke session, Script Editor:

```python
exec(__import__('urllib.request').request.urlopen('http://192.168.91.11:8600/nuke/remote_bootstrap.py', timeout=60).read().decode('utf-8'))
```

Source file: `nuke/artist_one_liner.txt`.

Bootstrap always re-downloads `nuke/ComfyEdit.py`, `client/comfy_client.py`, and the workflows listed in `_ALWAYS_REFRESH`. Anything under `Workflows/Comp/` is also refreshed because the path contains `/Comp/`.

After a menu or script change, tell artists to run that line again. A git push alone does not update an open Nuke.

---

## 5. Pix-Edit menu

`register_menu()` in `nuke/ComfyEdit.py`. One job at a time per Nuke (`_BG_JOB_ACTIVE`). ComfyUI still has its own queue.

| Menu | Workflow | Server id | What Nuke sends | Result |
|------|----------|-----------|-----------------|--------|
| Edit Image | `Workflows/Edit_Image_v08.json` | `8166` | plate LoadImage **80**, mask LoadImage **123**, prompt **109** `.value` | Read of the still |
| Edit Image Hi-res | `Workflows/Edit_Image_Hi_res_v04.json` (alias `Edit_Image_Hi_res.json`) | `8166-12` | plate **500**, mask **499**, prompt **494** `.value` | Read. If `91.12` is down, tell the artist to use Edit Image. Do not fail over silently |
| Edit Image Ref Hi-res | `Workflows/Edit_Image_Ref_Hi_res_v02.json` | `8166-12` | plate **500**, mask **499**, reference **510**, prompt **509** | Read |
| Image Gen | `Workflows/Image_generation_v01.json` | `8177` | prompt **73** `.value`, no plate | Read. SaveImage **29** |
| Image Outpaint | `Workflows/Image_outpaint_v01.json` | `8166-12` | image **13**, default prompt **73** “outpaint the image in red marked area”, SaveImageAdvanced **110** | Read |
| Image Description | `Workflows/Image_Description_v01.json` | `8177` | text **4**, LoadImage **5**, PreviewAny **12**. A selected Crop sends only the cropped pixels | StickyNote, not a Read |
| Image to Video | `Workflows/video_minimax_h3_i2v.json` | not assigned in `workflow_routes.json` (falls through to `main` / `:8188`) | current frame PNG → LoadImage **114**, prompt **141** `.value` | Video Read. The MiniMax node may be missing on a box; do not invent a route |
| Comp → DepthCrafter Depth | `Workflows/Comp/DepthCrafter_Depth_V02.json` | `8166-12` | movie file → VHS_LoadVideo **3** input `video`. No prompt | SaveVideo **30** |
| Comp → Video Alpha Mate | `Workflows/Comp/LTX_2.5_Alpha_Mate_V02.json` | `8166-12` | movie file → VHS_LoadVideo **9133** input `video` (template value `Comp-98.mp4`). No prompt. VAE Decode **9107** is full decode, not tiled | SaveVideo **9112** |
| Ping Server | — | — | checks the proxy | — |

Hi-res v03 (plate 164 / mask 167 / prompt 161) is legacy. The live menu prefers v04.

Routing is by **workflow basename**. `resolve_server_for_workflow()` calls `GET /api/comfy-routes` and opens `http://192.168.91.11:8600/comfyui-r/<server_id>`. Change a box in the admin **Workflow servers** tab, or edit `workflow_routes.json`. No code-server restart for a route-only edit.

Usage-log tool names (`server/usage_log.py` and `_nuke_tool_from_filename` in the client) must match the menu, and the specific name must be checked **before** the generic `video` / `i2v` rule:

- DepthCrafter → `Comp Depth`
- Alpha Mate → `Video Alpha Mate`

`client_id` prefixes: `nuke-comp-` for Depth, `nuke-alpha-` for Alpha Mate. `infer_nuke_tool` checks `nuke-alpha` before `nuke-comp`.

---

## 6. How a Comp video job runs

`schedule_comp_depth()` is the shared video-to-video path. DepthCrafter calls it with the defaults. Video Alpha Mate passes `load_id=9133`, `save_id=9112`, `label="Video Alpha Mate"`, `client_prefix="nuke-alpha"`.

1. Selected node must be a Read of a real movie, or a node fed by that Read. Sequences (`%` or `#` in the path) are rejected.
2. `_apply_project_gate` runs (see §7).
3. The workflow JSON is refreshed from `:8600`. Comp files must be requested as `Workflows/Comp/<name>.json`, not only `Workflows/<name>.json`. Names are listed in `_COMP_WORKFLOW_NAMES`.
4. `upload_video()` posts to Comfy `/upload/image` with `type=input` and keeps the movie extension.
5. The graph is copied. Node `load_id` input `video` is set to the uploaded filename. Node `save_id` `filename_prefix` becomes `video/<stamp>`. `client.id_save` is the save node so the download prefers that video.
6. `queue_prompt` sends `extra_data.workflow_file`, `nuke_tool`, and `project_code`. The hub enforces the code on `POST /prompt`.
7. `_poll_comfy_tick` waits (timeout 3600s for Comp) and downloads the file.
8. `_create_result_read` makes `ComfyEdit_Result_###`. For a movie, `_apply_video_read_range` sets the Read to frame **1** through the real last frame (`origfirst`/`origlast`/`first`/`last`, frame mode “start at”). It also sets the script frame range to **1–last** when the clip is longer than one frame, so Play covers the clip. The range is written **after** `reload`, because reload puts Nuke’s project range back. Length comes from ffprobe, then the mp4/mov sample table (`_mp4_video_frame_count`), then OpenCV.

Image jobs still export a PNG of the current frame (or the Crop). They do not upload the whole sequence.

---

## 7. Project codes

Live data: `server/project_codes.json` (gitignored). Logic: `server/project_codes.py`. Admin tab: **Project maintenance**.

- Match is case-insensitive. The stored spelling is canonical (`oig_symu` validates as `OIG_SYMU`).
- Groups: Roto, MM, CH-Paint, HYD-Paint, Comp, CG, DMP, All-Group. Groups are for admins. Artists type only the code.
- Archive and restore. Do not delete codes that already appear in logs.
- Bulk import moves an existing active code into the chosen group and restores an archived code into that group.
- Groups in the admin UI start collapsed.
- A workflow requires a code only when its **basename** is in `require_workflows`. Empty list means every workflow runs with no code.
- A machine with **Skip project code** (R&D) bypasses the prompt and is logged with an empty project.
- Enforcement is on the hub at queue time (`_enforce_project_code`), not only in the Nuke dialog. Public endpoints: `GET /projects/policy?workflow=` and `POST /projects/validate`.
- On a required workflow, Nuke remembers the validated code for **6** jobs across any Pix-Edit tool (`PROJECT_CODE_STAY = 6`), then asks again. The next code gets another 6. State is only on that artist’s machine.
- When a code is used, the output folder becomes `output_dir/<CODE>`.

---

## 8. Add another workflow

Do all of these or artists will not see it.

1. Put the API-format JSON in `Workflows/` or, for Comp, `Workflows/Comp/<Name>.json`. Do not leave it only at the repo root.
2. Find the input node and the save node in that JSON. Record the id and the input key (`image` or `video` or `value`).
3. Add a menu command in `register_menu()`. Comp items go on the existing `comp = m.addMenu("Comp")` submenu.
4. Reuse `schedule_comp_depth()` for movie-in / video-out. Reuse the still-image schedulers for plate + prompt. Set `client.id_save` to the save node.
5. Add the basename to `workflow_routes.json` with a real `server_id`. Comp and other custom graphs currently use `8166-12` (`http://192.168.91.12:8166`). Say which box you chose. If that box lacks the custom nodes, the job fails at queue time; move the route in Workflow servers. Do not guess a new IP silently.
6. Add the basename to `KNOWN_WORKFLOWS` in `server/workflow_routes.py` so Project maintenance can list it. Discovery already scans `Workflows/*/*.json` one level down.
7. Add the **repo-relative** path to all three lists: `SYNC_FILES` in `server/serve_code.py`, `_SYNC_FILES` and `_ALWAYS_REFRESH` in `nuke/remote_bootstrap.py`.
8. Add the basename to `_COMP_WORKFLOW_NAMES` in `ComfyEdit.py` if the file lives under `Workflows/Comp/`, so refresh requests `Workflows/Comp/<name>.json`.
9. Map the filename to a stable tool name in **both** `nuke_tool_from_filename` functions, and add that name to `CORE_NUKE_TOOLS`. Check the new name before the generic `video` rule.
10. Restart `:8600` if you edited hub Python. Tell artists to re-run the one-liner.
11. Do not add the workflow to `require_workflows` unless asked.

`workflow_routes.json` stores basenames only. The path on disk can be nested.

---

## 9. Git

Remote: https://github.com/Krish-701/ComfyNuke, branch `main`.

Latest pushed commit when this playbook was written: `eb0ca15` — “Add Pix-Edit Comp video tools and remember a project code for six jobs.”

Commit only the files the task touched. Leave `.forbidden`, `.grok/`, and `temp-old/` untracked. Do not commit the live project-code list.

Push only when the user asks, and only with a token they just gave you. Do not write the token into a file in the repo.

---

## 10. Checks that do not need Nuke

```bash
curl -sS http://127.0.0.1:8600/health
curl -sS -o /dev/null -w "%{http_code}\n" \
  http://127.0.0.1:8600/Workflows/Comp/LTX_2.5_Alpha_Mate_V02.json
curl -sS http://127.0.0.1:8600/api/comfy-routes
python3 -m py_compile nuke/ComfyEdit.py client/comfy_client.py server/serve_code.py
```

A 200 on the workflow URL proves the hub can serve it. It does not prove the GPU box has the custom nodes.

---

## 11. What is already done

- Live graphs are served from `Workflows/`. Old root URLs still alias to the same file when the root copy is missing.
- Pix-Edit menus in the table in §5, including Comp → DepthCrafter Depth and Comp → Video Alpha Mate.
- Returned movies use Read frame range 1 through the last frame, and the script range is set to that span.
- Project codes, groups, bulk import, search, sort, multi-archive, collapsible groups, usage logs, R&D skip.
- A validated project code stays for 6 jobs on that Nuke, then is asked again.

## 12. Do not assume

- Which ComfyUI box has DepthCrafter, LTX Alpha Mate, or MiniMax installed. The route says where we send the job. A missing custom node is a ComfyUI install problem, not a Nuke inject problem.
- That systemd is running the code server. Check the process.
- That an artist Nuke has the new menu before they run the one-liner.
- Node IDs from `docs/STUDIO_PLAYBOOK.md` or the old Windows `PLAYBOOK`. Those files lag this one.

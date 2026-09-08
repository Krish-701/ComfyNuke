# ComfyNuke studio playbook — hub `192.168.91.11`

Handoff for a later session (human or agent). Hub facts were first verified **2026-08-20**; this file was aligned again **2026-09-04** (Hi-res v01, Image Description crop, usage-log dates, `:8600` workflow allow-list). If reality disagrees, re-check the live commands in §8 before changing routing, node IDs, or start scripts.

Nuke artists use **HTTP only**. No Samba, no SSH, no share for code.

---

## 0. Read first

1. **This hub is `192.168.91.11`.** Older docs still say `192.168.91.13` — ignore that IP on this machine.
2. **Do not send Edit Image to `:8188`.** `Edit_Image_v08.json` needs WAS Node Suite (`Text Multiline`, `Mask Invert`), which exists only on **this hub’s `:8166`**. Routing Edit to `:8188` fails with `missing_node_type` on node **113** even when the artist prompt injected correctly into node **109**.
2b. **Hi-res v03 runs on `192.168.91.12:8166`.** Route id **`8166-12`**. Inject: plate **164**, mask **167**, prompt **161.value**. If that box is down, Nuke pops: *Edit Image Hi-res is down — use Edit Image instead.* Do not fail over silently.
3. **Do not run `start-comfyui-production.sh --port 8188`.** That script `pgrep`s `Comfyui-production/main.py` and will **SIGTERM the existing `:8177` instance**. Start `:8188` with the manual command in §2.3.
4. **Do not `kill -9` CUDA processes** on this vGPU/MIG guest. Prefer SIGTERM. Hard-kill can leave the GPU `busy or unavailable` until a full VM power cycle.
5. **Do not invent workflow node IDs.** Re-discover with `ComfyClient.load_workflow()` on the JSON on disk. Current inject (this repo, 2026-09-04): Edit v08 plate **80**, mask **123**, prompt **109.value**; Hi-res v02 plate **164**, mask **167**, prompt **161.value**; Image Description LoadImage **5**, user text **4**, PreviewAny **12**.
6. **Do not commit** `studio_config.json` or `server/access_control.json`.

---

## 1. What this machine is

| Item | Value |
|------|--------|
| Hostname | `WD13825220043rtx6000gpupmq2Y6WeSG` |
| LAN IP | `192.168.91.11` |
| GPU | NVIDIA RTX Pro 6000 Blackwell, **MIG 4g.96gb** (`CUDA_VISIBLE_DEVICES=MIG-e4001397-b8e2-5388-bdce-38dbefcd7f63`) |
| Repo (real path) | `/home/radhakrishnan/ComfyUI-Setup/ComfyNuke` |
| Repo (docs / systemd path) | `/home/radhakrishnan/Comfyui-Setup/ComfyNuke` — **symlink** → `ComfyUI-Setup` |
| Git | https://github.com/Krish-701/ComfyNuke · branch `main` |
| Code server | systemd `comfynuke-code.service` · **`:8600`** |
| Admin UI | http://192.168.91.11:8600/admin |
| Health | http://192.168.91.11:8600/health |

Both path spellings work. Prefer the symlink in commands so they match systemd.

### Ports

| Port | Process | Tree / conda | Who uses it |
|------|---------|--------------|-------------|
| **8600** | `serve_code.py` (read-only GET + ACL + Comfy proxy) | repo `server/` · system `python3` | Nuke bootstrap, admin, `/comfyui` proxy |
| **8166** | ComfyUI **Edit** (this hub) | `Comfyui-Image-edit` · env `Comfyui-edit` | `Edit_Image_v08.json` |
| **91.12:8166** | ComfyUI **Hi-res** | other box | `Edit_Image_Hi_res_v03.json` |
| **8177** | ComfyUI **Production** | `Comfyui-production` · env `Comfyui-production` | `Image_generation_v01.json`, `Image_Description_v01.json` |
| **8188** | ComfyUI extra listener (same production tree, **own SQLite DB**) | `Comfyui-production` · env `Comfyui-production` | default `/comfyui` proxy, Ping, browser |

Artists should **not** point Nuke at raw `:8188` / `:8166` / `:8177`. Jobs go through `:8600` so ACL and per-workflow routing apply:

```
Nuke  ──GET :8600──►  scripts + workflows + ACL
      ──:8600/comfyui            ──►  127.0.0.1:8188          (default)
      ──:8600/comfyui-r/8166     ──►  127.0.0.1:8166          (Edit Image v08)
      ──:8600/comfyui-r/8166-12  ──►  192.168.91.12:8166      (Edit Image Hi-res)
      ──:8600/comfyui-r/8177     ──►  127.0.0.1:8177          (Image Gen + Description)

Browser  ──:8166 / :8177 / :8188──►  ComfyUI UI (not IP-gated)
Admin    ──:8600/admin──►  login
```

---

## 2. Start / stop

### 2.1 Code server `:8600` (systemd — survives reboot)

```bash
sudo systemctl status comfynuke-code.service
sudo systemctl start comfynuke-code.service
sudo systemctl stop comfynuke-code.service
sudo systemctl restart comfynuke-code.service
```

Unit: `/etc/systemd/system/comfynuke-code.service`

```ini
[Unit]
Description=ComfyNuke code HTTP :8600
After=network.target

[Service]
User=radhakrishnan
WorkingDirectory=/home/radhakrishnan/Comfyui-Setup/ComfyNuke
ExecStart=/usr/bin/python3 server/serve_code.py --root /home/radhakrishnan/Comfyui-Setup/ComfyNuke --host 0.0.0.0 --port 8600
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

After editing `serve_code.py` / `access_control.py`: restart `:8600` (systemd below, or the live `python3 server/serve_code.py` process if the unit is inactive).  
**New workflow JSON filenames** are 403 until `:8600` is restarted *or* the name matches `Edit_Image*`, `Image_*`, `video_*` in `_rel_is_allowed` (added 2026-09-04).  
`workflow_routes.json`, `studio_config.json`, and `server/admin_ui.html` are read live — no restart.

**Check:** `curl -sS http://127.0.0.1:8600/health` → `ComfyNuke code server OK`.

### 2.2 Edit ComfyUI `:8166`

Uses the existing launcher (default port 8166). `--keep` leaves it alone if already up.

```bash
# start (or leave running)
/home/radhakrishnan/ComfyUI-Setup/start-comfyui-Image-edit.sh --keep
# or detached:
/home/radhakrishnan/ComfyUI-Setup/start-comfyui-Image-edit.sh -d

# check
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8166/system_stats
```

Browser: http://192.168.91.11:8166/

Log: `/home/radhakrishnan/ComfyUI-Setup/logs/` (image-edit console / dated logs).

### 2.3 Production ComfyUI `:8177`

```bash
/home/radhakrishnan/ComfyUI-Setup/start-comfyui-production.sh --keep
# or: /home/radhakrishnan/ComfyUI-Setup/start_comfyui  (wrapper → production :8177)

curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8177/system_stats
```

Browser: http://192.168.91.11:8177/

### 2.4 Extra ComfyUI `:8188` (not systemd — does **not** survive reboot)

Same production tree as `:8177`, **separate process** and **separate DB**. Required flags on this MIG/vGPU: `--disable-dynamic-vram --disable-cuda-malloc`.

```bash
# start (as user radhakrishnan — do NOT use start-comfyui-production.sh)
export CUDA_VISIBLE_DEVICES=MIG-e4001397-b8e2-5388-bdce-38dbefcd7f63
export CUDA_DEVICE_ORDER=PCI_BUS_ID
cd /home/radhakrishnan/ComfyUI-Setup/Comfyui-production
nohup /home/radhakrishnan/miniconda3/envs/Comfyui-production/bin/python main.py \
  --listen 0.0.0.0 --port 8188 \
  --disable-dynamic-vram --disable-cuda-malloc \
  --database-url sqlite:////home/radhakrishnan/ComfyUI-Setup/logs/comfyui-8188.db \
  >> /home/radhakrishnan/ComfyUI-Setup/logs/comfyui-8188.log 2>&1 &
echo $! > /home/radhakrishnan/ComfyUI-Setup/logs/comfyui-8188.pid

# check
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8188/system_stats

# stop (SIGTERM only)
kill "$(cat /home/radhakrishnan/ComfyUI-Setup/logs/comfyui-8188.pid)"
```

Must use `--database-url` pointing at `logs/comfyui-8188.db`. Sharing `user/comfyui.db` with `:8177` fails with a SQLite lock.

### 2.5 Studio up after reboot

`:8600` comes back via systemd. ComfyUI processes do **not**. Order:

```bash
# 1) GPU instances first
/home/radhakrishnan/ComfyUI-Setup/start-comfyui-Image-edit.sh --keep
/home/radhakrishnan/ComfyUI-Setup/start-comfyui-production.sh --keep
# 2) then :8188 with the nohup command in §2.4
# 3) code server
sudo systemctl start comfynuke-code.service

curl -sS http://127.0.0.1:8600/health
curl -sS -o /dev/null -w "8166=%{http_code} 8177=%{http_code} 8188=%{http_code} 8600-ok\n" \
  http://127.0.0.1:8166/system_stats
```

### 2.6 Free VRAM

Queue empty, then restart **only** the instance holding models. Never SIGKILL.

```bash
nvidia-smi
# restart 8166 via its start script (it stops the port first, gracefully)
/home/radhakrishnan/ComfyUI-Setup/start-comfyui-Image-edit.sh -d
```

---

## 3. Workflow routing (must stay this way unless nodes are installed)

Live table: `workflow_routes.json` (mirrored into gitignored `studio_config.json`). Nuke fetches `/api/comfy-routes` at job time.

| Workflow | Server id | Upstream | Why |
|----------|-----------|----------|-----|
| `Edit_Image_v08.json` | `8166` | `http://127.0.0.1:8166` | WAS Node Suite (`was-ns`) on this hub’s **edit** tree |
| `Edit_Image_Hi_res_v03.json` | `8166-12` | `http://192.168.91.12:8166` | Hi-res v03 (plate 164 / mask 167 / prompt 161) |
| `Edit_Image_Hi_res_v02.json` | `8166-12` | same | legacy v02 |
| `Edit_Image_Hi_res.json` | `8166-12` | same | alias of v03 |
| `Edit_Image_Hi_res_v01.json` | `8166-12` | same | legacy hi-res |
| `Image_generation_v01.json` | `8177` | `http://127.0.0.1:8177` | production gen |
| `Image_Description_v01.json` | `8177` | `http://127.0.0.1:8177` | Ollama describe (nodes 4 / 5 / 12) |
| *(unassigned / default)* | `main` | `http://127.0.0.1:8188` | `/comfyui` proxy |

**Edit Image log you want:**

```text
[ComfyEdit] Server: http://192.168.91.11:8600/comfyui-r/8166
[ComfyEdit] Inject: plate=80 mask=123 prompt=109.value seed=11.value
```

**Broken log** (job queued at `:8188`):

```text
[ComfyEdit] Server: http://192.168.91.11:8600/comfyui
ERROR: HTTP 400 /prompt: missing_node_type  Node 'Text Multiline' not found  Node ID '#113'
```

Node **109** is still the artist prompt. Node **113** is a WAS passthrough (LLM **107** → CLIP encode **25**). ComfyUI validates the **whole** graph, so a missing **113** fails the job even after a correct 109 inject.

`:8188` / `:8177` object_info (2026-08-20): missing `Text Multiline` and `Mask Invert` (both `custom_nodes.was-ns`). `:8166` has both.

`video_minimax_h3_i2v.json` is missing `MiniMaxH3ImageToVideo` on **all three** local ComfyUIs — i2v will fail until that custom node exists on some backend. Do not invent a server id for it.

Change routing in **http://192.168.91.11:8600/admin → Workflow servers**, or edit `workflow_routes.json` (no `:8600` restart).

---

## 4. Artist — Nuke

Every session, Script Editor → Run:

```python
exec(__import__('urllib.request').request.urlopen('http://192.168.91.11:8600/nuke/remote_bootstrap.py', timeout=60).read().decode('utf-8'))
```

Copy from `nuke/artist_one_liner.txt`. Re-run after a server `git pull`.

Menu **Nuke → Pix-Edit**: Edit Image… | Edit Image Hi-res… | Image Gen… | Image Description… | Image to Video… | Ping Server.

Cache on the artist PC: `~/.comfynuke/cache/` (Windows: `%USERPROFILE%\.comfynuke\cache\`).  
Outputs: `~/ComfyNuke_out/<hostname>/` on the **artist** machine, not the hub.

Each job re-downloads the named workflow JSON from `:8600` (`_refresh_workflow_from_server`) so artists do not keep a stale graph after a hub update. Bootstrap `_ALWAYS_REFRESH` also overwrites those JSONs (v02 + alias). `Edit_Image_Hi_res_v01.json` is **optional** in bootstrap.

### 4.0 Plates, MOV/EXR, Crop (Image Description)

- **MOV / EXR sequences:** Nuke writes the **current timeline frame** to a still PNG, then uploads that PNG. Do not send the `.mov` or the sequence to Comfy `LoadImage`.
- **Image Description + Crop:** select the **Crop** node. Only the cropped pixels are written/uploaded (LoadImage **5**). The StickyNote is the description of that crop only. Without Crop, the selected node’s full visible frame is used.

### 4.1 Access control

ACL is **ON**. Unknown IPs get 403 on bootstrap and on `/comfyui`.

1. Open http://192.168.91.11:8600/admin (user **Krish**).
2. **Access control** → Enable → **Add machine** (artist IP + name + group) → toggle **ON**.

Hub IP `192.168.91.11` is allow-listed as `hub-91.11`. Localhost is allowed.

Do not put passwords in git. Live file: `server/access_control.json` (mode 600).

### 4.2 Inject cheatsheet (this repo, 2026-09-04)

```
Edit_Image_v08.json          → :8166 (this hub)
  LoadImage plate 80 | LoadImage mask 123 | prompt 109 value
  WAS-only: 113 Text Multiline, 132 Mask Invert

Edit_Image_Hi_res_v03.json   → 192.168.91.12:8166  (id 8166-12)
  LoadImage plate 164 | LoadImage mask 167 | prompt 161 value
  alias file: Edit_Image_Hi_res.json
  (v01 leftover: 278 / 297 / 290)

Image_generation_v01.json    → :8177
  no LoadImage | prompt 73 value | SaveImage 29

Image_Description_v01.json   → :8177
  user text 4 value | LoadImage 5 | PreviewAny 12 → StickyNote in Nuke
```

### 4.3 Admin — usage logs date range

http://192.168.91.11:8600/admin → **Usage logs** (hard-refresh after HTML changes).

- **From date** / **To date** are calendar pickers. Tables and CSV use that window only.
- Presets fill the pickers: Today, Yesterday, Last 7 days, Last 30 days, This month, Last 24 hours.
- **Apply dates** (or changing either picker) reloads summary + events.
- The chip under the bar shows the selected range (e.g. `Today  4 Sep 2026 → 4 Sep 2026`).

---

## 5. Firewall (UFW, default deny)

Studio LAN rules added for TCP **8600** and **8188**:

- `192.168.91.0/24`
- `192.168.11.0/24`
- `172.31.0.0/16`

`:8166` and `:8177` were already open on `192.168.91.0/24` (and some 11.x hosts).

```bash
sudo ufw status numbered | grep -E '8600|8188|8166|8177'
```

---

## 6. Config files (on the hub)

| Path | Git | Role |
|------|-----|------|
| `studio_config.json` | **ignored** | Nuke: `server` = `http://192.168.91.11:8600/comfyui`, `code_base_url` = `:8600` |
| `workflow_routes.json` | tracked | Backends + per-workflow server id |
| `server/access_control.json` | **ignored** | ACL + hashed users |
| `server/usage_logs.jsonl` | **ignored** | usage log |
| `nuke/remote_bootstrap.py` | tracked | Artist one-liner default `CODE_BASE` = `http://192.168.91.11:8600` |

`:8600` is GET-only for code. Allow-list is `server/serve_code.py` (`nuke/`, `client/`, `SYNC_FILES`, plus root JSON whose name starts with `Edit_Image`, `Image_generation`, `Image_Description`, or `video_`). `access_control.json` is blocked. A filename that is **not** allowed is served as HTTP **403** (do not confuse with ACL 403).

---

## 7. Traps

| Do this | Not this |
|---------|----------|
| Route Edit → `:8166` | Point Edit at `:8188` / `:8600/comfyui` |
| Treat **109** as the artist prompt | “Fix” the 400 by changing prompt node to **113** |
| Start `:8188` with §2.4 | `start-comfyui-production.sh --port 8188` (kills `:8177`) |
| SIGTERM | `kill -9` on Comfy/CUDA |
| Jobs via `:8600/comfyui` or `/comfyui-r/<id>` | Raw `:8188` in Nuke if you want ACL |
| `--database-url` for `:8188` | Share `user/comfyui.db` with `:8177` |
| Re-discover node IDs from JSON | Guess IDs. Hi-res v02 is **164 / 167 / 161**; v01 leftover is 278 / 297 / 290 |

Other:

- `start-comfyui-production.sh` default port is **8177**, not 8188.
- Two ComfyUI processes can share the production **tree**, but not the SQLite DB.
- ACL ON + missing artist IP = bootstrap 403. Health (`/health`) stays public.
- `favicon.ico` 403 on `:8600` is expected (not allow-listed).
- Bootstrap `HTTP Error 403: Forbidden` on a new `Edit_Image_Hi_res_v0N.json` means the **code server process** still has an old allow-list. Restart `:8600`. v01 is optional so a leftover 403 must not abort bootstrap.
- Empty `allowed_workflows: []` on an IP **denies every workflow JSON**. Use `null` for “all”.
- Nuke Script Editor may garble UTF-8 arrows; bootstrap logs use ASCII `->`.
- i2v MiniMax node is not installed on 8166/8177/8188 as of 2026-08-20.

---

## 8. Verify (safe, no GPU job)

```bash
cd /home/radhakrishnan/Comfyui-Setup/ComfyNuke

curl -sS http://127.0.0.1:8600/health
curl -sS http://192.168.91.11:8600/health
curl -sS -o /dev/null -w "bootstrap=%{http_code}\n" http://192.168.91.11:8600/nuke/remote_bootstrap.py
curl -sS -o /dev/null -w "8188=%{http_code}\n" http://192.168.91.11:8188/
curl -sS -o /dev/null -w "8166=%{http_code}\n" http://127.0.0.1:8166/system_stats
curl -sS -o /dev/null -w "8177=%{http_code}\n" http://127.0.0.1:8177/system_stats
curl -sS -o /dev/null -w "proxy8166=%{http_code}\n" http://127.0.0.1:8600/comfyui-r/8166/system_stats
curl -sS -o /tmp/hires_v03.json -w "hires_v03=%{http_code} bytes=%{size_download}\n" \
  http://127.0.0.1:8600/Edit_Image_Hi_res_v03.json

python3 -c "
from client.comfy_client import ComfyClient
c=ComfyClient(workflow_path='Edit_Image_v08.json'); c.load_workflow()
print('edit', c.id_load, c.id_load_mask, c.id_prompt, c.id_prompt_key)
h=ComfyClient(workflow_path='Edit_Image_Hi_res_v03.json'); h.load_workflow()
print('hires', h.id_load, h.id_load_mask, h.id_prompt, h.id_prompt_key)
"
```

Expect: health `ComfyNuke code server OK`, bootstrap **200**, Comfy **200**, `hires_v03=200` and size **> 1000** (10 bytes = `.forbidden` leak), edit inject `80 123 109 value`, hi-res `164 167 161 value`.

Nuke: re-run the one-liner, **Pix-Edit → Ping Server**, then Edit Image. Script Editor should show `comfyui-r/8166` and `prompt=109.value`, then a new `ComfyEdit_Result_###` Read (GPU time).

---

## 9. Git

```bash
cd /home/radhakrishnan/Comfyui-Setup/ComfyNuke
git pull origin main
sudo systemctl restart comfynuke-code.service   # only if server/*.py changed
```

Artists re-run the Nuke one-liner after pull.

Do not force-push. Do not commit ACL or `studio_config.json`.

---

## 10. If you continue this work

- Keep Edit Image **v08** on this hub’s `:8166` until `was-ns` is installed on whichever ComfyUI you retarget (then re-check `/object_info` for `Text Multiline` and `Mask Invert` before changing `workflow_routes.json`).
- Keep Hi-res v02 on **91.12:8166** (`8166-12`). If ping fails, Nuke only pops “Hi-res is down — use Edit Image”; it does not auto-switch graphs.
- After adding a new root workflow JSON: put it in `SYNC_FILES` + `_SYNC_FILES` / `_ALWAYS_REFRESH`, `workflow_routes.json`, then **restart `:8600`** and confirm `GET /ThatFile.json` is 200 with real JSON size.
- Do not rewrite inject IDs unless `ComfyClient.load_workflow()` on the **current** JSON disagrees.
- `:8188` has no systemd unit yet; adding one must **not** call `start-comfyui-production.sh`.
- i2v needs `MiniMaxH3ImageToVideo` on some backend before assigning `video_minimax_h3_i2v.json`.
- Nuke UI changes (Crop describe, MOV/EXR stills) are only proven inside Nuke; shell checks cover inject + HTTP only.

# Multi-user hub — secure Ubuntu (no share access)

## Goal

| Port | Service | Who uses it |
|------|---------|-------------|
| **8188** | ComfyUI API (GPU jobs) | Nuke via `comfy_client` |
| **8600** | ComfyNuke **code** HTTP + **admin ACL** + optional Comfy proxy | Nuke bootstrap; browser ` /admin ` IP allowlist; optional `/comfyui` → :8188 |

Artists **cannot** SSH or open server folders. They only need LAN access to:

- `http://192.168.91.13:8188`
- `http://192.168.91.13:8600`

```
  Nuke PC  --GET :8600-->  code (ComfyEdit, workflows)
  Nuke PC  --API :8188-->  ComfyUI queue / upload / view
```

Repo on server:

`/home/radhakrishnan/Comfyui-Setup/ComfyNuke`

---

## 1. Ubuntu — once

### 1.1 Clone / update

```bash
cd /home/radhakrishnan/Comfyui-Setup
git clone https://github.com/Krish-701/ComfyNuke.git
# or
cd /home/radhakrishnan/Comfyui-Setup/ComfyNuke && git pull
```

### 1.2 Site config

```bash
cd /home/radhakrishnan/Comfyui-Setup/ComfyNuke
cp studio_config.example.json studio_config.json
# ensure:
#   "server": "http://192.168.91.13:8188"
#   "code_base_url": "http://192.168.91.13:8600"
```

### 1.3 ComfyUI on 8188

Run ComfyUI as you already do, listening on LAN, e.g.:

```bash
# example — use your real Comfy start command
python main.py --listen 0.0.0.0 --port 8188
```

### 1.4 Code server on 6000

```bash
cd /home/radhakrishnan/Comfyui-Setup/ComfyNuke
chmod +x server/start_code_server.sh
./server/start_code_server.sh
# or:
python3 server/serve_code.py --root /home/radhakrishnan/Comfyui-Setup/ComfyNuke --host 0.0.0.0 --port 8600
```

Check from any PC:

```text
http://192.168.91.13:8600/health
```

Should say `ComfyNuke code server OK`.

### 1.5 Firewall

Allow LAN **TCP 8188** and **TCP 8600** only (not SSH to artists if you do not want).

Optional systemd unit sketch:

```ini
# /etc/systemd/system/comfynuke-code.service
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

---

## 1b. Access control (browser UI on :8600)

Open on the hub (or any admin machine):

```text
http://192.168.91.13:8600/admin
```

Default admin (first install — change after login if needed):

- Username: `Krish`
- Password: set via `COMFYNUKE_ADMIN_PASSWORD` on first start (stored **hashed** in `server/access_control.json`, not in git)

**Workflow**

1. Sign in at `/admin`
2. **Add IP** of each artist machine (Windows: `ipconfig`, Linux: `ip a`)
3. Toggle **Enabled** per IP
4. Click **Enable access control**

When ACL is **ON**:

- Only enabled IPs can `GET /nuke/remote_bootstrap.py`, workflows, `/manifest.json`, etc.
- Only enabled IPs can use the ComfyUI reverse proxy at `http://SERVER:8600/comfyui/...`
- Direct ComfyUI on **:8188** is **not** blocked by this UI — either firewall 8188 to LAN deny, or set Nuke `server` to  
  `http://192.168.91.13:8600/comfyui` so all jobs go through the gate

When ACL is **OFF**: open LAN (previous behaviour).

---

## 2. Artist PC — every Nuke session

**Paste only this** in Script Editor → Run:

```python
exec(__import__('urllib.request').request.urlopen('http://192.168.91.13:8600/nuke/remote_bootstrap.py', timeout=60).read().decode('utf-8'))
```

Also in `nuke/artist_one_liner.txt`.

Then menu:

**Nuke → Pix-Edit →** Edit Image… | Image Gen… | Image to Video… | Ping Server

Re-run the one-liner each Nuke session (or after a server `git pull`). Bootstrap
compares `/manifest.json` on the code server with `~/.comfynuke/cache/.comfynuke_manifest.json`
and **replaces** any outdated scripts or workflows with the server copies.

---

## 3. What gets downloaded

Bootstrap pulls into `~/.comfynuke/cache/` on the **artist machine**:

- `nuke/ComfyEdit.py`
- `client/comfy_client.py`
- `Edit_Image_v08.json`
- `Image_generation_v01.json`
- `video_minimax_h3_i2v.json`
- `studio_config.json` (if present on server)

Results go to `~/ComfyNuke_out/<hostname>/` (not on the server).

Temps: `%TEMP%/comfy_nuke/<host>_<pid>/`

---

## 4. Multi-user behaviour

- All artists share **one ComfyUI queue** on the GPU (jobs wait in line).
- One active job **per Nuke session**.
- Unique upload names / output prefixes per host so files do not clobber.

---

## 5. Checklist

- [ ] `http://192.168.91.13:8188` opens ComfyUI
- [ ] `http://192.168.91.13:8600/health` OK
- [ ] Nuke one-liner prints `READY — multi-user remote load OK`
- [ ] Ping Server succeeds
- [ ] Edit / Gen / I2V each produce a new Read

## 6. Troubleshooting

| Symptom | Fix |
|---------|-----|
| Bootstrap timeout :8600 | Start `serve_code.py`; open firewall 8600 |
| Ping fails :8188 | ComfyUI listen `0.0.0.0`; firewall 8188 |
| 403 on download | Path not in allow-list; use paths under `nuke/`, `client/`, workflows |
| Old code | `git pull` on server; re-run Nuke one-liner |
| Workflow missing | Ensure JSON files exist in repo root on server |

## Security

- Port **8600** is **read-only** (GET only); only allow-listed relative paths under the ComfyNuke repo.
- Do not put secrets in the served tree.
- Keep ComfyUI and code ports on studio LAN only.

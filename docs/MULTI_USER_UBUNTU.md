# Multi-user hub — one Ubuntu server, many Nuke artists

## Goal

| Role | Machine | Runs |
|------|---------|------|
| **Hub** | Ubuntu server | ComfyUI + shared ComfyNuke code + GPU |
| **Artists** | Windows (or Linux) Nuke | Only a **launch** snippet in Script Editor |

Artists do **not** need a full local clone. They load code from the share and send jobs to the single ComfyUI queue.

```
  [Nuke PC A] --launch-->  \\server\ComfyNuke\nuke\launch.py
  [Nuke PC B] --launch-->  same share
  [Nuke PC C] --launch-->  same share
         \         |         /
          \        |        /
           v       v       v
        Ubuntu: ComfyUI :8188  (one GPU queue)
```

## 1. Ubuntu server setup

### 1.1 ComfyUI

- Install ComfyUI as you already do (models, custom nodes for edit / Krea / MiniMax / LLM).
- Listen on all interfaces, e.g. `--listen 0.0.0.0 --port 8188`.
- Firewall: allow LAN TCP `8188`.

### 1.2 Deploy ComfyNuke code

```bash
# example
sudo mkdir -p /opt/ComfyNuke
sudo chown "$USER:$USER" /opt/ComfyNuke
cd /opt/ComfyNuke
git clone https://github.com/Krish-701/ComfyNuke.git .

# site config (not committed)
cp studio_config.example.json studio_config.json
nano studio_config.json   # set "server": "http://<this-host-ip>:8188"
```

### 1.3 Share the folder (Samba example)

```bash
sudo apt install samba
# export /opt/ComfyNuke as share name ComfyNuke (read-only for artists is OK)
# Artists map: \\192.168.91.13\ComfyNuke
```

Or NFS / existing studio NAS — any path Nuke can `open()`.

### 1.4 Optional: git pull for updates

On the server, when you push new code to GitHub:

```bash
cd /opt/ComfyNuke && git pull
```

Artists re-run **launch** in Nuke to reload modules (launch clears `ComfyEdit` / `comfy_client` cache).

## 2. Artist PC (Windows)

### 2.1 Map share (once)

```
\\192.168.91.13\ComfyNuke
```

### 2.2 Script Editor (every session, or put in menu.py)

Use `nuke/artist_launch.txt` — short form:

```python
import os
os.environ["COMFYNUKE_ROOT"] = r"\\192.168.91.13\ComfyNuke"
os.environ["COMFYNUKE_SERVER"] = "http://192.168.91.13:8188"
exec(open(os.path.join(os.environ["COMFYNUKE_ROOT"], "nuke", "launch.py"), encoding="utf-8").read())
```

### 2.3 Permanent menu (optional)

In `C:/Users/<artist>/.nuke/menu.py`:

```python
import os
os.environ["COMFYNUKE_ROOT"] = r"\\192.168.91.13\ComfyNuke"
os.environ["COMFYNUKE_SERVER"] = "http://192.168.91.13:8188"
exec(open(os.path.join(os.environ["COMFYNUKE_ROOT"], "nuke", "menu_snippet.py"), encoding="utf-8").read())
```

Update `menu_snippet.py` paths if needed — or call `launch.py` instead.

## 3. What is shared vs local

| Asset | Where |
|-------|--------|
| Python code + workflows JSON | Ubuntu share (read) |
| `studio_config.json` | Ubuntu share (server URL) |
| Temp exports (plates/masks) | Artist PC `%TEMP%/comfy_nuke/<host>_<pid>/` |
| Downloaded results | Artist PC `~/ComfyNuke_out/<hostname>/` (override with `COMFYNUKE_OUT`) |
| Comfy models / GPU | Ubuntu only |
| Job queue | ComfyUI global queue (one job at a time on GPU) |

## 4. Multi-user behaviour

- **Per Nuke session:** only one local job at a time (panel lock).
- **Across artists:** ComfyUI queues jobs. Later artists wait; Script Editor shows `Comfy queued` / `Comfy running`.
- **Filenames:** uploads use `nuke_<hostname>_...` and unique `filename_prefix` so outputs do not clobber.
- **Do not** point all artists’ `output_dir` at the same server folder without unique subdirs.

## 5. Checklist

- [ ] ComfyUI reachable: browser `http://<server-ip>:8188`
- [ ] From artist PC: open `\\server\ComfyNuke\nuke\launch.py`
- [ ] Launch prints `ComfyUI: http://...` and menus appear
- [ ] **Ping Server** works
- [ ] Second artist can queue while first is running (waits, then runs)

## 6. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ComfyEdit.py not found` | Fix `COMFYNUKE_ROOT` / share mount |
| Timeout / connection refused | Comfy listen `0.0.0.0`, firewall 8188, correct IP |
| Wrong workflows | `git pull` on server; re-run launch |
| Two Nukes on same PC fight temps | Temps are per-pid; still one job per session |
| Results “overlap” old Reads | Each download is a unique file under `ComfyNuke_out` |

## 7. Security notes

- ComfyUI HTTP API is usually **open LAN** (no auth). Restrict to studio network.
- Share can be **read-only** for artists; only server admin writes code.

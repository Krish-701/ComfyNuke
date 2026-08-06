# ComfyNuke

**Hub model:** one Ubuntu server runs ComfyUI + shared code; many Nuke artists only run a launch script.

| Mode | Workflow |
|------|----------|
| Edit Image | `Edit_Image_v05.json` |
| Image Gen | `Image_generation_v01.json` |
| Image to Video | `video_minimax_h3_i2v.json` |

## Architecture

```
 Nuke PC (artist)          Ubuntu main server
 -----------------         -----------------------
 Script Editor launch  -->  shared ComfyNuke/ (code + workflows)
       |                    ComfyUI :8188 (GPU queue)
       +---- HTTP --------> upload / prompt / download
 results on artist PC       models stay on server
```

Full guide: **[docs/MULTI_USER_UBUNTU.md](docs/MULTI_USER_UBUNTU.md)**

## Artist — only this in Script Editor

```python
import os
os.environ["COMFYNUKE_ROOT"] = r"\\192.168.91.13\ComfyNuke"
os.environ["COMFYNUKE_SERVER"] = "http://192.168.91.13:8188"
exec(open(os.path.join(os.environ["COMFYNUKE_ROOT"], "nuke", "launch.py"), encoding="utf-8").read())
```

See `nuke/artist_launch.txt`. Menu: **Nuke → ComfyUI**.

## Ubuntu server (admin)

```bash
cd /opt/ComfyNuke   # or your share root
git clone https://github.com/Krish-701/ComfyNuke.git .
cp studio_config.example.json studio_config.json
# edit studio_config.json → "server": "http://<server-ip>:8188"
# Samba/NFS export this folder as \\server\ComfyNuke
# Run ComfyUI with --listen 0.0.0.0 --port 8188
```

## Layout

| Path | Role |
|------|------|
| `nuke/launch.py` | **Multi-user bootstrap** (artists) |
| `nuke/artist_launch.txt` | Copy-paste snippet |
| `nuke/ComfyEdit.py` | Menus, export, QTimer poll, Reads |
| `client/comfy_client.py` | HTTP + workflow inject |
| `studio_config.example.json` | Copy → `studio_config.json` on server |
| `docs/MULTI_USER_UBUNTU.md` | Install & multi-user ops |
| `PLAYBOOK.md` | Deep handoff / traps |

## Multi-user safety

- ComfyUI **one GPU queue** for all artists (later jobs wait).
- One job at a time **per Nuke session**.
- Temps: `%TEMP%/comfy_nuke/<host>_<pid>/`
- Results: `~/ComfyNuke_out/<hostname>/` (not on the share)
- Unique upload names + `filename_prefix` per job/host

## Local dev (no share)

```python
exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/load_in_nuke.py", encoding="utf-8").read())
```

## CLI ping

```bat
python client\comfy_client.py --ping-only --image dummy --prompt x --server http://192.168.91.13:8188
```

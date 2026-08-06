# ComfyNuke

Nuke artists talk to a **secure Ubuntu hub**:

| Port | Role |
|------|------|
| **8188** | ComfyUI (generate / edit / i2v) |
| **6000** | Read-only **code** server (scripts + workflows) |

No Samba/SSH needed for artists — only those two HTTP ports.

## Artist — single paste (Nuke Script Editor)

```python
exec(__import__('urllib.request').request.urlopen('http://192.168.91.13:6000/nuke/remote_bootstrap.py', timeout=60).read().decode('utf-8'))
```

Menu: **Nuke → ComfyUI** → Edit Image | Image Gen | Image to Video | Ping Server

## Ubuntu hub

Repo path example:

`/home/radhakrishnan/Comfyui-Setup/ComfyNuke`

```bash
cd /home/radhakrishnan/Comfyui-Setup/ComfyNuke
git pull
cp -n studio_config.example.json studio_config.json

# Terminal 1 — ComfyUI (your usual command), port 8188
# Terminal 2 — code distribution:
python3 server/serve_code.py --root /home/radhakrishnan/Comfyui-Setup/ComfyNuke --host 0.0.0.0 --port 6000
# or: ./server/start_code_server.sh
```

Firewall: allow LAN **8188** + **6000**.

Full guide: [docs/MULTI_USER_UBUNTU.md](docs/MULTI_USER_UBUNTU.md)

## Modes

| Menu | Workflow file |
|------|----------------|
| Edit Image | `Edit_Image_v05.json` |
| Image Gen | `Image_generation_v01.json` |
| Image to Video | `video_minimax_h3_i2v.json` |

## Layout

| Path | Role |
|------|------|
| `server/serve_code.py` | HTTP :6000 code server |
| `server/start_code_server.sh` | Ubuntu start helper |
| `nuke/remote_bootstrap.py` | Downloaded + run by artists |
| `nuke/artist_one_liner.txt` | Copy-paste for Nuke |
| `nuke/ComfyEdit.py` | Menus, export, poll, Reads |
| `client/comfy_client.py` | ComfyUI HTTP client |
| `studio_config.example.json` | Server URLs template |

## Local dev (optional)

```python
exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/load_in_nuke.py", encoding="utf-8").read())
```

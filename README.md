# ComfyNuke

Nuke artists talk to a **secure Ubuntu hub** at `192.168.91.11`. They use HTTP only (no Samba, no SSH).

**Agent handoff (menus, node IDs, routes, project codes, how to add a workflow):** [PLAYBOOK.md](PLAYBOOK.md).

| Port | Role |
|------|------|
| **8600** | Code server, admin, and the ComfyUI proxy artists actually call |
| **8166 / 8177 / 8188** | ComfyUI on this hub. Artists do not point Nuke at these directly |
| **192.168.91.12:8166** | Hi-res, Outpaint, and Comp (route id `8166-12`) |

## Artist — single paste (Nuke Script Editor)

```python
exec(__import__('urllib.request').request.urlopen('http://192.168.91.11:8600/nuke/remote_bootstrap.py', timeout=60).read().decode('utf-8'))
```

Menu: **Nuke → Pix-Edit** — Edit Image, Hi-res, Ref Hi-res, Image Gen, Image Outpaint, Image Description, Image to Video, **Comp** (DepthCrafter Depth, Video Alpha Mate), Ping Server.

## Ubuntu hub

Repo path example:

`/home/radhakrishnan/ComfyUI-Setup/ComfyNuke`

```bash
cd /home/radhakrishnan/ComfyUI-Setup/ComfyNuke
git pull

# Code server (the live process is this command, not systemd):
python3 server/serve_code.py --root /home/radhakrishnan/ComfyUI-Setup/ComfyNuke --host 0.0.0.0 --port 8600
```

Firewall: allow LAN **8188** + **8600**.

**How the tool works and how to extend it:** [PLAYBOOK.md](PLAYBOOK.md).

Start/stop and GPU notes (some menu rows there are older than the playbook): [docs/STUDIO_PLAYBOOK.md](docs/STUDIO_PLAYBOOK.md)

Also: [docs/MULTI_USER_UBUNTU.md](docs/MULTI_USER_UBUNTU.md) · [docs/STUDIO_OPERATIONS.md](docs/STUDIO_OPERATIONS.md)

## Modes

| Menu | Workflow file |
|------|----------------|
| Edit Image | `Workflows/Edit_Image_v08.json` (plate `80`, mask `123`, prompt `109.value`) |
| Image Gen | `Workflows/Image_generation_v01.json` |
| Comp | `Workflows/Comp/` — DepthCrafter and Video Alpha Mate. Full table in [PLAYBOOK.md](PLAYBOOK.md) |

## Layout

| Path | Role |
|------|------|
| `server/serve_code.py` | HTTP :8600 code server |
| `server/start_code_server.sh` | Ubuntu start helper |
| `nuke/remote_bootstrap.py` | Downloaded + run by artists |
| `nuke/artist_one_liner.txt` | Copy-paste for Nuke |
| `nuke/ComfyEdit.py` | Menus, export, poll, Reads |
| `client/comfy_client.py` | ComfyUI HTTP client |
| `studio_config.example.json` | Server URLs template |

## Local dev

Artists do not load the repo from a Windows path. They run the one-liner above. Agents working on this hub edit `/home/radhakrishnan/ComfyUI-Setup/ComfyNuke` and follow [PLAYBOOK.md](PLAYBOOK.md).

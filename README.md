# ComfyNuke

Nuke (Windows) ↔ ComfyUI bridge for sequential artist jobs:

- **Edit Image** — Roto/Read plate → `Edit_Image_v05.json`
- **Image Gen** — text-to-image → `Image_generation_v01.json`
- **Image to Video** — current frame → MiniMax i2v → `video_minimax_h3_i2v.json`

## Layout

| Path | Role |
|------|------|
| `nuke/ComfyEdit.py` | Nuke menu, export, QTimer poll, result Read |
| `nuke/load_in_nuke.py` | Safe reload into Script Editor |
| `nuke/menu_snippet.py` | Optional permanent menu hook |
| `client/comfy_client.py` | HTTP client, workflow inject, download |
| `client/config.example.json` | Example server settings |
| `Edit_Image_v05.json` | Edit workflow (API format) |
| `Image_generation_v01.json` | Txt2img workflow |
| `video_minimax_h3_i2v.json` | Image-to-video workflow |
| `PLAYBOOK.md` | Agent/operator handoff notes |
| `docs/` | Phase notes / references |

## Nuke load

```python
exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/load_in_nuke.py", encoding="utf-8").read())
```

Menu: **Nuke → ComfyUI** → Edit Image / Image Gen / Image to Video / Ping Server.

Default server in code: `http://192.168.91.13:8188` (edit for your site).

## CLI smoke (no Nuke)

```bat
cd /d D:\AI-Dev\Krish-ComfyNuke
python client\comfy_client.py --ping-only --image dummy --prompt x --server http://192.168.91.13:8188
```

## Notes

- One Comfy job at a time per Nuke session.
- Outputs download under `client/out/` (gitignored).
- See `PLAYBOOK.md` for inject node IDs, traps, and verification.

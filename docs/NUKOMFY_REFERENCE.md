# Nukomfy Suite vs our Phase 1 client

## What you already have

| Piece | Status |
|--------|--------|
| `ComfyUI-Nukomfy-Suite` (this folder / on server) | **Installed on** `http://192.168.91.13:8188` — `/nukomfy/manager/ping` OK, `NukomfyRead` present |
| **Nukomfy Nuke plugin** (separate repo) | Optional production path — [francescolorussi/Nukomfy](https://github.com/francescolorussi/Nukomfy) |
| Our Phase 1 | `client/comfy_client.py` + `nuke/ComfyEdit.py` — lightweight, uses `/upload/image` |

## Architecture comparison

### Official Nukomfy (Suite + Nuke plugin)

```
Nuke gizmo
  → Write template renders frames to SHARED DISK cache
  → Comfy NukomfyRead(file_path=cache/...)
  → workflow
  → NukomfyWrite → shared output folder
  → Nuke Read Output(s)
```

- Needs **shared folders** (Windows Nuke + Linux Comfy must see same files; use Nuke path substitutions).
- Workflows need **NukomfyRead** + **NukomfyWrite** (UI-format export).
- Multi-artist: Render Manager, queue, job history, admin password (already configured on your server).
- One job at a time per GPU via Comfy queue (same as we planned).

### Our Phase 1 (Krish-ComfyNuke)

```
Nuke
  → get plate file (from Read) and/or Write PNG
  → HTTP POST /upload/image  (no shared disk required)
  → Edit_Image_API.json (LoadImage)
  → GET /view download
  → Nuke Read node
```

- Works across OS without a NAS if upload works.
- Hit Nuke’s **“I’m already executing something else”** when using `nuke.execute(Write)` from a panel.
- **Fix:** prefer plate path from upstream **Read** (no execute). Roto mask via Write is optional/retry.

## When to use which

| Goal | Use |
|------|-----|
| Ship multi-artist studio pipeline fast | Install **Nukomfy** plugin + keep Suite on server + shared storage |
| Quick single-frame edit, no NAS yet | Our **ComfyEdit** + upload |
| EXR / OCIO / multilayer | Suite nodes (`NukomfyWrite` EXR, OCIO) via full Nukomfy |
| Your Flux edit graph today | Phase 1 `Edit_Image_API.json` OR re-export with NukomfyRead/Write for Nukomfy |

## Suite nodes (reference)

| Class | Role |
|-------|------|
| `NukomfyRead` | Disk → IMAGE + MASK (alpha) + multilayer |
| `NukomfyWrite` | IMAGE → EXR/PNG/… on disk |
| `NukomfyOCIOColorSpace` | OCIO transform |
| `NukomfyMultiLayerPack/Unpack` | Extra EXR layers |

## Multi-Nuke on one server

Same model either way:

1. One Comfy host (`192.168.91.13:8188`).
2. Many Nuke clients submit jobs.
3. Comfy **queues** — GPU runs one, then next.
4. Unique paths / filenames per user (Nukomfy does this in input cache; we use UUID uploads).

Suite already has availability + job history on your host.

## Related links

- Suite (you have a copy): `ComfyUI-Nukomfy-Suite/`
- Nuke plugin: https://github.com/francescolorussi/Nukomfy  
- User guide: https://github.com/francescolorussi/Nukomfy/blob/master/USER_GUIDE.md  
- Alternative: https://github.com/vinavfx/ComfyUI-for-Nuke  

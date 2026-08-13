# Pix-Edit / ComfyNuke — Studio operations guide

How the hub works, how to **start / stop** services, and how to **add or remove workflows**.

Hub machine: Ubuntu GPU box  
Repo root: `/home/radhakrishnan/Comfyui-Setup/ComfyNuke`  
Studio IP example: `192.168.91.13`

---

## 1. What runs on the hub

| Port | What | Who uses it |
|------|------|-------------|
| **8188** | ComfyUI (GPU app + browser UI) | Direct browser: `http://192.168.91.13:8188/` |
| **8600** | Code server + Access Control + logs + Comfy **proxy** | Nuke bootstrap, admin/operator web UI |

```
Artist Nuke  ──:8600──►  code server (scripts + workflows + ACL)
             ──:8600/comfyui──►  proxy  ──localhost:8188──►  ComfyUI

Browser      ──:8188──►  ComfyUI UI (open on studio LAN)
Admin/Ops    ──:8600/admin or /operator──►  login web UI
```

**Important**

- Nuke should use Comfy via **`http://192.168.91.13:8600/comfyui`** so IP Access Control applies.
- Direct ComfyUI in a browser is still **`:8188`** (not role-gated).
- Port **6000 is not used** anymore. Always **8600**.

---

## 2. Start and stop

### 2.1 Code server (port 8600) — systemd

```bash
# Status
sudo systemctl status comfynuke-code.service

# Start
sudo systemctl start comfynuke-code.service

# Stop
sudo systemctl stop comfynuke-code.service

# Restart (after code or workflow list changes)
sudo systemctl restart comfynuke-code.service

# Enable on boot
sudo systemctl enable comfynuke-code.service
```

Unit file: `/etc/systemd/system/comfynuke-code.service`  
Working directory: `/home/radhakrishnan/Comfyui-Setup/ComfyNuke`  
Command: `python3 server/serve_code.py --root ... --host 0.0.0.0 --port 8600`

**Health check**

```bash
curl -sS http://127.0.0.1:8600/health
# or browser: http://192.168.91.13:8600/health
```

**Manual start (if systemd is not used)**

```bash
cd /home/radhakrishnan/Comfyui-Setup/ComfyNuke
bash server/start_code_server.sh
# or:
python3 server/serve_code.py --root /home/radhakrishnan/Comfyui-Setup/ComfyNuke --host 0.0.0.0 --port 8600
```

### 2.2 ComfyUI (port 8188)

ComfyUI is a **separate process** (not started by `comfynuke-code.service`).

Start / stop it the way you normally run ComfyUI on this machine (your ComfyUI venv, launcher, or systemd unit if you have one).

**Check**

```bash
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8188/system_stats
# browser: http://192.168.91.13:8188/
```

Both **8600** and **8188** must be running for Nuke artists to work.

### 2.3 Full “studio down / up”

**Stop**

```bash
sudo systemctl stop comfynuke-code.service
# stop ComfyUI with your usual method
```

**Start**

```bash
# 1) Start ComfyUI first (port 8188)
# 2) Then:
sudo systemctl start comfynuke-code.service
curl -sS http://127.0.0.1:8600/health
```

---

## 3. Web portals (port 8600)

| URL | Purpose |
|-----|---------|
| http://192.168.91.13:8600/admin | Full admin UI |
| http://192.168.91.13:8600/operator | Same app; operators log in here |
| http://192.168.91.13:8600/login | Login alias |
| http://192.168.91.13:8600/health | Health / version (no login) |

### Roles

| Role | Can do |
|------|--------|
| **admin** | Users, ACL master switch, machines, logs, CSV |
| **operator** | Machines (add/edit IP), groups, ACL toggle, logs/CSV — **not** user admin |
| **viewer** | Read machines + logs + CSV only |

Default admin was migrated as **Krish** (password set at first install).  
Create operators under **Users & roles** while logged in as admin.

### Access control (artist IPs)

1. Login as **admin** or **operator**.
2. Tab **Access control**.
3. **Enable access control** (master ON).
4. **Add machine**: IP + short name + group → **Add**.
5. Toggle **ON** for that IP.

When master is **ON**:

- Only enabled IPs can bootstrap Nuke scripts from `:8600`.
- Only enabled IPs can run jobs through `:8600/comfyui`.
- Toggle **OFF** blocks that IP on the **next** request (live).

Local files (on hub, not in git):

- `server/access_control.json` — ACL + users (hashed passwords)
- `server/usage_logs.jsonl` — usage log

---

## 4. Artist Nuke — every session

### One-liner (Script Editor)

```python
exec(__import__('urllib.request').request.urlopen('http://192.168.91.13:8600/nuke/remote_bootstrap.py', timeout=60).read().decode('utf-8'))
```

This:

1. Checks the code server (`/health`, `/manifest.json`).
2. Syncs scripts + workflows into `~/.comfynuke/cache/` (Windows: under user profile).
3. Pins Comfy URL to **`http://192.168.91.13:8600/comfyui`**.
4. Registers menu **Nuke → Pix-Edit**.

### Pix-Edit menu

1. Edit Image…  
2. Image Gen…  
3. Image to Video…  
4. Ping Server  
5. Prompt Examples… (local prompt library — last item)

**Prompt Examples** is saved only on that artist PC:

- Linux/mac: `~/.comfynuke/prompt_examples.json`  
- Windows: `%USERPROFILE%\.comfynuke\prompt_examples.json`  

Copy prompt text → paste into **Edit Image**.

### If bootstrap fails

| Error | Likely cause |
|-------|----------------|
| Connection refused | Code server stopped, or wrong port (must be **8600**) |
| ACCESS DENIED / 403 | ACL on and IP disabled / not listed |
| Workflow not found | Workflow not on hub or not listed in sync (see §5) |

---

## 5. Workflows — current set

Files live in the **repo root**:

| File | Used for |
|------|----------|
| `Edit_Image_v08.json` | Pix-Edit → Edit Image |
| `Image_generation_v01.json` | Image Gen |
| `video_minimax_h3_i2v.json` | Image to Video |

Nuke defaults (in `nuke/ComfyEdit.py`):

- Edit → `Edit_Image_v08.json`  
- Gen → `Image_generation_v01.json`  
- I2V → `video_minimax_h3_i2v.json`  

Edit workflow inject points (v08):

- LoadImage **80** = plate (`plate_srgb.png`)  
- LoadImage **123** = mask (`mask_luma.png`)  
- Prompt **109.value** = artist text  

---

## 6. How to **add** a new workflow

Example: new edit graph `Edit_Image_v09.json`.

### Step A — Export from ComfyUI

1. Build/test the graph in ComfyUI.
2. Export **API format** JSON (not only the UI workflow format).
3. Copy the file to the hub:

```bash
cp /path/to/Edit_Image_v09.json /home/radhakrishnan/Comfyui-Setup/ComfyNuke/Edit_Image_v09.json
```

### Step B — Allow the code server to serve it

Edit `server/serve_code.py`:

1. Add to **`ALLOWED_PREFIXES`** (so GET is allowed):

```python
"Edit_Image_v09.json",
```

2. Add to **`SYNC_FILES`** (so artists download it / manifest includes it):

```python
"Edit_Image_v09.json",
```

### Step C — Bootstrap always pulls it

Edit `nuke/remote_bootstrap.py`:

1. Add to **`_SYNC_FILES`**.
2. Add to **`_ALWAYS_REFRESH`** (so artists always get the latest copy).

### Step D — Point Nuke at the new default (if it replaces the old one)

Edit `nuke/ComfyEdit.py`:

- `DEFAULT_WORKFLOW` → `.../Edit_Image_v09.json`
- Update any “migrate from older version” list if you still support v08.
- If node IDs changed (prompt / load image / mask), update `client/comfy_client.py` fallbacks  
  (`NODE_LOAD_IMAGE`, `NODE_LOAD_MASK`, `NODE_PROMPT`) or rely on auto-discovery.

For Image Gen / I2V, change:

- `IMAGE_GEN_WORKFLOW` or `I2V_WORKFLOW`  
  and the matching JSON names in bootstrap + serve lists.

### Step E — Restart code server + artists re-bootstrap

```bash
sudo systemctl restart comfynuke-code.service
curl -sS http://127.0.0.1:8600/manifest.json | head
```

On each Nuke machine, run the **8600** one-liner again.

### Checklist (add workflow)

- [ ] JSON in API format on hub repo root  
- [ ] `ALLOWED_PREFIXES`  
- [ ] `SYNC_FILES`  
- [ ] `_SYNC_FILES` + `_ALWAYS_REFRESH` in `remote_bootstrap.py`  
- [ ] Defaults in `ComfyEdit.py` (if new default)  
- [ ] `client/comfy_client.py` inject IDs if graph changed  
- [ ] `systemctl restart comfynuke-code`  
- [ ] Artists re-run bootstrap  

---

## 7. How to **remove** or retire a workflow

### Soft retire (keep file, stop using)

1. Point Nuke defaults to the newer file (`ComfyEdit.py`).
2. Remove the old name from `_ALWAYS_REFRESH` / `_SYNC_FILES` if you no longer want it downloaded.
3. Optionally leave it in `ALLOWED_PREFIXES` for a while (legacy).
4. Restart code server; artists re-bootstrap.

### Hard remove

1. Remove the JSON file from the repo root (or stop serving it).
2. Remove from **`ALLOWED_PREFIXES`**, **`SYNC_FILES`**, bootstrap **`_SYNC_FILES`** / **`_ALWAYS_REFRESH`**.
3. Ensure no defaults in `ComfyEdit.py` still point at it.
4. Restart:

```bash
sudo systemctl restart comfynuke-code.service
```

5. Artists re-bootstrap (clears stale expectations).

Do **not** delete a workflow while artists are mid-job.

---

## 8. Updating an **existing** workflow (same filename)

Example: you edited `Edit_Image_v08.json` on the hub.

1. Overwrite the file in the repo root.
2. Restart is **not** required for file content (server reads from disk), but restart is fine.
3. Artists must **re-run bootstrap** (or open Edit Image — plate/mask jobs also re-pull the JSON from `:8600` when configured).

Because `_ALWAYS_REFRESH` includes the main workflow JSONs, each bootstrap overwrites the local cache with the hub file.

---

## 9. Usage logs

- Admin/operator: **http://192.168.91.13:8600/admin** or **/operator** → **Usage logs**
- Export: **Export events CSV** / **Export summary CSV**
- Log file on hub: `server/usage_logs.jsonl`

Logged when traffic goes through **`:8600`** (bootstrap, `/comfyui` jobs, denials).  
Direct browser use of **:8188** is not fully tracked by this log.

---

## 10. Firewall (UFW) — quick reference

Typical open ports on the hub:

- **8600/tcp** — code + admin + Nuke proxy (studio LANs)
- **8188/tcp** — ComfyUI browser (studio LANs)
- **22/tcp** — SSH (as needed)

```bash
sudo ufw status numbered | grep -E '8600|8188'
```

---

## 11. Important paths

| Path | Role |
|------|------|
| `/home/radhakrishnan/Comfyui-Setup/ComfyNuke/` | Repo root |
| `server/serve_code.py` | Code server + ACL + proxy + APIs |
| `server/access_control.py` | ACL + roles |
| `server/access_control.json` | Live secrets (not git) |
| `server/usage_logs.jsonl` | Usage log (not git) |
| `server/admin_ui.html` | Web UI |
| `nuke/remote_bootstrap.py` | Artist download entry |
| `nuke/ComfyEdit.py` | Nuke menus / panels |
| `client/comfy_client.py` | Comfy API client |
| `studio_config.json` | Hub URLs (`server` + `code_base_url`) |
| `Edit_Image_v08.json` etc. | Workflow graphs |

`studio_config.json` should look like:

```json
{
  "studio_name": "ComfyNuke Studio",
  "server": "http://192.168.91.13:8600/comfyui",
  "code_base_url": "http://192.168.91.13:8600",
  "output_dir": ""
}
```

---

## 12. Quick troubleshooting

| Symptom | Fix |
|---------|-----|
| Nuke connection refused on **6000** | Use **8600** only |
| Nuke connection refused on **8600** | `sudo systemctl start comfynuke-code` |
| ACCESS DENIED | Add/enable IP under Access control; master switch ON |
| Browser Comfy blank on **8188** | Start ComfyUI; check UFW 8188 |
| Jobs work in browser but not Nuke | Nuke must use `:8600/comfyui` (re-bootstrap) |
| Old workflow still used | Re-bootstrap; confirm hub file name + SYNC lists |
| Operator URL 403 | Use `/operator` after server update (not a random path) |

---

## 13. Daily operator cheat sheet

1. Open **http://192.168.91.13:8600/operator**  
2. Login as **operator**  
3. Add new artist IP → enable  
4. If someone misbehaves → disable their IP (jobs stop on next request)  
5. **Usage logs** → export CSV for the day  

Admin only: create/remove users under **Users & roles**.

---

*Last aligned with hub layout: code on **:8600**, ComfyUI on **:8188**, edit workflow **Edit_Image_v08.json**.*

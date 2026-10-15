# -*- coding: utf-8 -*-
"""
Usage / activity log for ComfyNuke code server + ComfyUI proxy.

Append-only JSONL on disk. Supports query, per-user summary, CSV export.
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# Events we treat as “usage”
EVENT_JOB_QUEUE = "job_queue"
EVENT_JOB_DONE = "job_done"
EVENT_JOB_ERROR = "job_error"
EVENT_UPLOAD = "upload"
EVENT_DOWNLOAD = "download"
EVENT_ACCESS_DENIED = "access_denied"
EVENT_BOOTSTRAP = "bootstrap"
EVENT_API = "api"
EVENT_LOGIN = "admin_login"


_WF_NAME_PREFIXES = ("Edit_Image", "Image_", "video_")
_WF_SKIP = {
    "studio_config.json",
    "studio_config.example.json",
    "workflow_routes.json",
    "package.json",
}


def _workflow_json_paths(root: Path) -> List[Path]:
    """Working graphs live in Workflows/. Root copies are older leftovers."""
    found: List[Path] = []
    wf_dir = root / "Workflows"
    if wf_dir.is_dir():
        found.extend(p for p in wf_dir.glob("*.json") if p.is_file())
    found.extend(p for p in root.glob("*.json") if p.is_file())
    return found


def _resolve_workflow_path(root: Path, name: str) -> Path:
    base = Path(str(name or "")).name
    for p in (root / "Workflows" / base, root / base):
        if p.is_file():
            return p
    return root / "Workflows" / base


def list_workflow_catalog(repo_root: Optional[Path] = None) -> List[str]:
    """Every workflow JSON we know: disk + workflow_routes.json. New files appear automatically."""
    names = set()
    root = Path(repo_root) if repo_root else None
    if root and root.is_dir():
        for p in _workflow_json_paths(root):
            n = p.name
            if n in _WF_SKIP:
                continue
            if n.startswith(_WF_NAME_PREFIXES):
                names.add(n)
        routes = root / "workflow_routes.json"
        if routes.is_file():
            try:
                wr = json.loads(routes.read_text(encoding="utf-8"))
                for w in wr.get("workflows") or []:
                    f = str((w or {}).get("file") or "").strip()
                    if f.endswith(".json"):
                        names.add(f)
            except Exception:
                pass
    return sorted(names)


def _graph_node_ids(prompt: Any) -> set:
    if not isinstance(prompt, dict):
        return set()
    out = set()
    for k, v in prompt.items():
        if isinstance(v, dict) and v.get("class_type"):
            out.add(str(k))
    return out


_GRAPH_CACHE: Dict[str, Tuple[float, List[Tuple[str, set]]]] = {}


def _catalog_graphs(repo_root: Optional[Path]) -> List[Tuple[str, set]]:
    if not repo_root:
        return []
    root = Path(repo_root)
    key = str(root)
    try:
        mtime = max(
            (p.stat().st_mtime for p in _workflow_json_paths(root)),
            default=0.0,
        )
    except Exception:
        mtime = 0.0
    cached = _GRAPH_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    graphs: List[Tuple[str, set]] = []
    for name in list_workflow_catalog(root):
        p = _resolve_workflow_path(root, name)
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        ids = _graph_node_ids(data)
        if len(ids) >= 3:
            graphs.append((name, ids))
    _GRAPH_CACHE[key] = (mtime, graphs)
    return graphs


def _match_prompt_to_catalog(prompt: Any, repo_root: Optional[Path]) -> str:
    p_ids = _graph_node_ids(prompt)
    if len(p_ids) < 3:
        return ""
    graphs = _catalog_graphs(repo_root)
    if not graphs:
        return ""
    scored = []
    for name, ids in graphs:
        overlap = len(p_ids & ids)
        if overlap < 3:
            continue
        others = set()
        for n2, ids2 in graphs:
            if n2 != name:
                others |= ids2
        unique = ids - others
        unique_hit = len(unique & p_ids)
        score = unique_hit * 1000 + overlap
        scored.append((score, overlap, unique_hit, name))
    if not scored:
        return ""
    scored.sort(reverse=True)
    top = scored[0]
    if top[2] > 0 or top[1] >= 8:
        return top[3]
    return ""


def _safe_workflow_filename(name: str) -> str:
    n = str(name or "").strip().replace("\\", "/").split("/")[-1]
    if not n.endswith(".json"):
        return ""
    if ".." in n or "/" in n:
        return ""
    return n


def infer_workflow(
    prompt: Any = None,
    client_id: str = "",
    detail: str = "",
    extra: Any = None,
    repo_root: Optional[Path] = None,
) -> str:
    """Workflow filename from extra_data, graph match against all JSON on disk, then hints."""
    if isinstance(extra, dict):
        for key in ("workflow_file", "workflow", "file"):
            n = _safe_workflow_filename(str(extra.get(key) or ""))
            if n:
                return n
        png = extra.get("extra_pnginfo")
        if isinstance(png, dict):
            n = _safe_workflow_filename(str(png.get("workflow_file") or png.get("file") or ""))
            if n:
                return n
    cid = str(client_id or "").lower()
    det = str(detail or "").lower()
    blob = cid + " " + det
    if "nuke-desc" in blob or ("describe" in blob and "nuke" in blob):
        return "Image_Description_v01.json"
    if "nuke-t2i" in blob or "/t2i_" in blob or " t2i_" in blob:
        return "Image_generation_v01.json"
    if "nuke-i2v" in blob:
        return "video_minimax_h3_i2v.json"
    matched = _match_prompt_to_catalog(prompt, repo_root)
    if matched:
        return matched
    if "desc" in blob:
        return "Image_Description_v01.json"
    if "t2i" in blob:
        return "Image_generation_v01.json"
    if "i2v" in blob:
        return "video_minimax_h3_i2v.json"
    return ""


CORE_NUKE_TOOLS = (
    "Edit Image",
    "Edit Image Hi-res",
    "Edit Image Ref Hi-res",
    "Image Gen",
    "Image Outpaint",
    "Image Description",
    "Image to Video",
)


def nuke_tool_from_filename(name: str) -> str:
    """
    Map any JSON version to the Nuke menu tool.
    Edit_Image_Hi_res_v04.json and v05.json both count as "Edit Image Hi-res".
    """
    raw = str(name or "").strip()
    if not raw or raw == "(unknown)":
        return ""
    b = raw.replace(" ", "_").replace("-", "_").lower()
    if b.endswith(".json"):
        b = b[:-5]
    if "low_res" in b or "lowres" in b:
        return ""
    if "ref" in b and ("hi_res" in b or "hires" in b):
        return "Edit Image Ref Hi-res"
    if "hi_res" in b or "hires" in b:
        return "Edit Image Hi-res"
    if b.startswith("edit_image"):
        return "Edit Image"
    if "description" in b or "describe" in b:
        return "Image Description"
    if "generation" in b or b.startswith("image_gen"):
        return "Image Gen"
    if "outpaint" in b:
        return "Image Outpaint"
    if "upscale" in b:
        return "Image Upscale"
    if "video" in b or "i2v" in b or "minimax" in b:
        return "Image to Video"
    parts = [
        p
        for p in b.split("_")
        if p and not (len(p) >= 2 and p[0] == "v" and p[1:].isdigit())
    ]
    pretty = " ".join(p.capitalize() if p.isalpha() else p for p in parts)
    return pretty


def infer_nuke_tool(
    *,
    workflow: str = "",
    extra: Any = None,
    client_id: str = "",
    detail: str = "",
) -> str:
    if isinstance(extra, dict):
        t = str(extra.get("nuke_tool") or extra.get("tool") or "").strip()
        if t:
            return t
    t = nuke_tool_from_filename(workflow)
    if t:
        return t
    blob = (str(client_id or "") + " " + str(detail or "")).lower()
    if "nuke-desc" in blob or "describe" in blob:
        return "Image Description"
    if "nuke-t2i" in blob or "t2i" in blob:
        return "Image Gen"
    if "nuke-outpaint" in blob or "outpaint" in blob:
        return "Image Outpaint"
    if "nuke-i2v" in blob or "i2v" in blob:
        return "Image to Video"
    return ""


class UsageLog:
    def __init__(
        self,
        path: Path,
        max_lines: int = 200_000,
        repo_root: Optional[Path] = None,
    ):
        self.path = Path(path)
        self.max_lines = max_lines
        self.repo_root = Path(repo_root) if repo_root else self.path.parent.parent
        self._lock = threading.RLock()
        # In-memory open jobs: prompt_id -> start record
        self._open_jobs: Dict[str, Dict[str, Any]] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.is_file():
            self.path.write_text("", encoding="utf-8")
            try:
                os.chmod(self.path, 0o600)
            except Exception:
                pass

    def _new_id(self) -> str:
        return uuid.uuid4().hex[:16]

    def log(self, **fields: Any) -> Dict[str, Any]:
        now = time.time()
        rec: Dict[str, Any] = {
            "id": fields.pop("id", None) or self._new_id(),
            "ts": fields.pop("ts", None) or now,
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        }
        for k, v in fields.items():
            if v is not None:
                rec[k] = v
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            # light rotate if huge
            try:
                if self.path.stat().st_size > 80 * 1024 * 1024:
                    self._trim_unlocked()
            except Exception:
                pass
        return rec

    def _trim_unlocked(self) -> None:
        """Keep last max_lines lines if file grows too large."""
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) <= self.max_lines:
                return
            keep = lines[-self.max_lines :]
            tmp = self.path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            os.replace(str(tmp), str(self.path))
        except Exception:
            pass

    def track_job_start(
        self,
        prompt_id: str,
        *,
        ip: str = "",
        machine_id: str = "",
        label: str = "",
        group: str = "",
        client_id: str = "",
        detail: str = "",
        workflow: str = "",
        tool: str = "",
        project_code: str = "",
        project_bypass: bool = False,
    ) -> Dict[str, Any]:
        tool = (tool or infer_nuke_tool(workflow=workflow, client_id=client_id, detail=detail) or "")
        rec = self.log(
            event=EVENT_JOB_QUEUE,
            ip=ip,
            machine_id=machine_id,
            label=label,
            group=group,
            method="POST",
            path="/prompt",
            status=200,
            prompt_id=prompt_id,
            client_id=client_id,
            detail=detail or "queued",
            workflow=workflow or "",
            tool=tool,
            project_code=project_code or "",
            project_bypass=bool(project_bypass),
        )
        with self._lock:
            self._open_jobs[str(prompt_id)] = {
                "start_ts": rec["ts"],
                "ip": ip,
                "machine_id": machine_id,
                "label": label,
                "group": group,
                "client_id": client_id,
                "workflow": workflow or "",
                "tool": tool,
                "project_code": project_code or "",
                "project_bypass": bool(project_bypass),
            }
        return rec

    def track_job_done(
        self,
        prompt_id: str,
        *,
        status: str = "success",
        ip: str = "",
        detail: str = "",
    ) -> Optional[Dict[str, Any]]:
        pid = str(prompt_id or "")
        if not pid:
            return None
        with self._lock:
            start = self._open_jobs.pop(pid, None)
        if not start:
            # still log completion if we never saw queue (restart)
            start = {
                "start_ts": time.time(),
                "ip": ip,
                "machine_id": "",
                "label": "",
                "group": "",
                "client_id": "",
                "workflow": "",
                "project_code": "",
                "project_bypass": False,
            }
        now = time.time()
        runtime = max(0.0, float(now - float(start.get("start_ts") or now)))
        return self.log(
            event=EVENT_JOB_DONE if status == "success" else EVENT_JOB_ERROR,
            ip=ip or start.get("ip") or "",
            machine_id=start.get("machine_id") or "",
            label=start.get("label") or "",
            group=start.get("group") or "",
            method="GET",
            path="/history",
            status=200 if status == "success" else 500,
            prompt_id=pid,
            client_id=start.get("client_id") or "",
            runtime_sec=round(runtime, 2),
            duration_ms=int(runtime * 1000),
            detail=detail or status,
            workflow=start.get("workflow") or "",
            tool=start.get("tool") or infer_nuke_tool(
                workflow=str(start.get("workflow") or ""),
                client_id=str(start.get("client_id") or ""),
            ),
            project_code=start.get("project_code") or "",
            project_bypass=bool(start.get("project_bypass")),
        )

    def iter_records(
        self,
        *,
        since: Optional[float] = None,
        until: Optional[float] = None,
        ip: str = "",
        label: str = "",
        group: str = "",
        event: str = "",
        workflow: str = "",
        project: str = "",
        q: str = "",
        limit: int = 500,
        reverse: bool = True,
    ) -> List[Dict[str, Any]]:
        ip = (ip or "").strip()
        label = (label or "").strip().lower()
        group = (group or "").strip().lower()
        event = (event or "").strip()
        workflow = (workflow or "").strip()
        project = (project or "").strip().casefold()
        q = (q or "").strip().lower()
        limit = max(1, min(int(limit or 500), 50_000))

        out: List[Dict[str, Any]] = []
        with self._lock:
            try:
                raw_lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                raw_lines = []

        if reverse:
            raw_lines = list(reversed(raw_lines))

        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            ts = float(rec.get("ts") or 0)
            if since is not None and ts < since:
                continue
            if until is not None and ts > until:
                continue
            if ip and str(rec.get("ip") or "") != ip:
                continue
            if label and label not in str(rec.get("label") or "").lower():
                continue
            if group and group not in str(rec.get("group") or "").lower():
                continue
            if event and str(rec.get("event") or "") != event:
                continue
            if workflow and str(rec.get("workflow") or "") != workflow:
                continue
            if project and str(rec.get("project_code") or "").casefold() != project:
                continue
            if q:
                blob = " ".join(
                    str(rec.get(k) or "")
                    for k in (
                        "id",
                        "ip",
                        "label",
                        "group",
                        "event",
                        "path",
                        "prompt_id",
                        "client_id",
                        "detail",
                        "machine_id",
                        "workflow",
                        "tool",
                        "project_code",
                    )
                ).lower()
                if q not in blob:
                    continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    def find_by_prompt_id(self, prompt_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Match ComfyUI prompt_id / job id (full or unique prefix)."""
        needle = str(prompt_id or "").strip()
        if not needle:
            return []
        needle_l = needle.lower()
        rows = self.iter_records(limit=50_000, reverse=True)
        out: List[Dict[str, Any]] = []
        for r in rows:
            pid = str(r.get("prompt_id") or "")
            if not pid:
                continue
            pl = pid.lower()
            if pl == needle_l or pl.startswith(needle_l) or needle_l in pl:
                if not r.get("workflow"):
                    guessed = infer_workflow(
                        None,
                        client_id=str(r.get("client_id") or ""),
                        detail=str(r.get("detail") or ""),
                        extra=r.get("extra"),
                        repo_root=self.repo_root,
                    )
                    if guessed:
                        r = dict(r)
                        r["workflow"] = guessed
                out.append(r)
                if len(out) >= max(1, min(int(limit or 50), 200)):
                    break
        return out

    def summary(
        self,
        *,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> Dict[str, Any]:
        # Read more for summary
        rows = self.iter_records(since=since, until=until, limit=50_000, reverse=False)
        by_user: Dict[str, Dict[str, Any]] = {}
        totals = {
            "events": 0,
            "jobs_queued": 0,
            "jobs_done": 0,
            "jobs_error": 0,
            "uploads": 0,
            "downloads": 0,
            "access_denied": 0,
            "total_runtime_sec": 0.0,
        }

        def key_for(r: Dict[str, Any]) -> str:
            return (
                str(r.get("machine_id") or "")
                or str(r.get("ip") or "")
                or "unknown"
            )

        for r in rows:
            if not r.get("workflow"):
                guessed = infer_workflow(
                    None,
                    client_id=str(r.get("client_id") or ""),
                    detail=str(r.get("detail") or ""),
                    extra=r.get("extra"),
                    repo_root=self.repo_root,
                )
                if guessed:
                    r["workflow"] = guessed
            if not r.get("tool"):
                t = infer_nuke_tool(
                    workflow=str(r.get("workflow") or ""),
                    extra=r.get("extra"),
                    client_id=str(r.get("client_id") or ""),
                    detail=str(r.get("detail") or ""),
                )
                if t:
                    r["tool"] = t
            totals["events"] += 1
            ev = str(r.get("event") or "")
            k = key_for(r)
            u = by_user.setdefault(
                k,
                {
                    "machine_id": r.get("machine_id") or "",
                    "ip": r.get("ip") or "",
                    "label": r.get("label") or "",
                    "group": r.get("group") or "",
                    "jobs_queued": 0,
                    "jobs_done": 0,
                    "jobs_error": 0,
                    "uploads": 0,
                    "downloads": 0,
                    "access_denied": 0,
                    "api_calls": 0,
                    "total_runtime_sec": 0.0,
                    "avg_runtime_sec": 0.0,
                    "bytes_in": 0,
                    "bytes_out": 0,
                    "first_ts": r.get("ts"),
                    "last_ts": r.get("ts"),
                    "first_iso": r.get("ts_iso"),
                    "last_iso": r.get("ts_iso"),
                    "workflow_counts": {},
                    "project_counts": {},
                },
            )
            # refresh identity
            if r.get("label"):
                u["label"] = r.get("label")
            if r.get("group"):
                u["group"] = r.get("group")
            if r.get("ip"):
                u["ip"] = r.get("ip")
            ts = r.get("ts")
            if ts is not None:
                if u["first_ts"] is None or ts < u["first_ts"]:
                    u["first_ts"] = ts
                    u["first_iso"] = r.get("ts_iso")
                if u["last_ts"] is None or ts > u["last_ts"]:
                    u["last_ts"] = ts
                    u["last_iso"] = r.get("ts_iso")

            u["bytes_in"] += int(r.get("bytes_in") or 0)
            u["bytes_out"] += int(r.get("bytes_out") or 0)

            wf_name = str(r.get("tool") or "").strip() or nuke_tool_from_filename(
                str(r.get("workflow") or "")
            )
            if ev in (EVENT_JOB_QUEUE, EVENT_JOB_DONE, EVENT_JOB_ERROR) and wf_name:
                wc = u.setdefault("workflow_counts", {})
                slot = wc.setdefault(
                    wf_name, {"jobs_queued": 0, "jobs_done": 0, "jobs_error": 0}
                )
                if ev == EVENT_JOB_QUEUE:
                    slot["jobs_queued"] += 1
                elif ev == EVENT_JOB_DONE:
                    slot["jobs_done"] += 1
                else:
                    slot["jobs_error"] += 1

            pcode = str(r.get("project_code") or "").strip()
            if ev in (EVENT_JOB_QUEUE, EVENT_JOB_DONE, EVENT_JOB_ERROR) and pcode:
                pc = u.setdefault("project_counts", {})
                pslot = pc.setdefault(
                    pcode, {"jobs_queued": 0, "jobs_done": 0, "jobs_error": 0}
                )
                if ev == EVENT_JOB_QUEUE:
                    pslot["jobs_queued"] += 1
                elif ev == EVENT_JOB_DONE:
                    pslot["jobs_done"] += 1
                else:
                    pslot["jobs_error"] += 1

            if ev == EVENT_JOB_QUEUE:
                u["jobs_queued"] += 1
                totals["jobs_queued"] += 1
            elif ev == EVENT_JOB_DONE:
                u["jobs_done"] += 1
                totals["jobs_done"] += 1
                rt = float(r.get("runtime_sec") or 0)
                u["total_runtime_sec"] += rt
                totals["total_runtime_sec"] += rt
            elif ev == EVENT_JOB_ERROR:
                u["jobs_error"] += 1
                totals["jobs_error"] += 1
            elif ev == EVENT_UPLOAD:
                u["uploads"] += 1
                totals["uploads"] += 1
            elif ev == EVENT_DOWNLOAD:
                u["downloads"] += 1
                totals["downloads"] += 1
            elif ev == EVENT_ACCESS_DENIED:
                u["access_denied"] += 1
                totals["access_denied"] += 1
            else:
                u["api_calls"] += 1

        users = []
        for u in by_user.values():
            done = int(u["jobs_done"] or 0)
            u["total_runtime_sec"] = round(float(u["total_runtime_sec"]), 2)
            u["avg_runtime_sec"] = (
                round(float(u["total_runtime_sec"]) / done, 2) if done else 0.0
            )
            counts = u.get("workflow_counts") or {}
            parts = []
            ranked = sorted(
                counts.items(),
                key=lambda kv: (
                    -(int((kv[1] or {}).get("jobs_done") or 0)
                      + int((kv[1] or {}).get("jobs_queued") or 0)),
                    kv[0],
                ),
            )
            names = []
            for wname, info in ranked:
                info = info or {}
                n = wname[:-5] if str(wname).endswith(".json") else wname
                jd = int(info.get("jobs_done") or 0)
                jq = int(info.get("jobs_queued") or 0)
                show = jd or jq
                if not show:
                    continue
                parts.append("%s (%s)" % (n, jd if jd else jq))
                names.append(n)
            u["workflows"] = names
            u["workflows_label"] = ", ".join(parts) if parts else ""
            pc_parts = []
            for pname, info in sorted(
                (u.get("project_counts") or {}).items(),
                key=lambda kv: (
                    -(int((kv[1] or {}).get("jobs_done") or 0)
                      + int((kv[1] or {}).get("jobs_queued") or 0)),
                    kv[0],
                ),
            ):
                info = info or {}
                jd = int(info.get("jobs_done") or 0)
                jq = int(info.get("jobs_queued") or 0)
                show = jd or jq
                if not show:
                    continue
                pc_parts.append("%s (%s)" % (pname, jd if jd else jq))
            u["projects_label"] = ", ".join(pc_parts) if pc_parts else ""
            users.append(u)
        users.sort(
            key=lambda x: (-float(x.get("total_runtime_sec") or 0), -int(x.get("jobs_done") or 0))
        )
        totals["total_runtime_sec"] = round(float(totals["total_runtime_sec"]), 2)

        by_wf: Dict[str, Dict[str, Any]] = {}

        def _empty_wf(name: str) -> Dict[str, Any]:
            return {
                "workflow": name,
                "jobs_queued": 0,
                "jobs_done": 0,
                "jobs_error": 0,
                "total_runtime_sec": 0.0,
                "avg_runtime_sec": 0.0,
                "people": 0,
            }

        for name in CORE_NUKE_TOOLS:
            by_wf[name] = _empty_wf(name)
        for fname in list_workflow_catalog(self.repo_root):
            t = nuke_tool_from_filename(fname)
            if t:
                by_wf.setdefault(t, _empty_wf(t))
        for r in rows:
            ev = str(r.get("event") or "")
            wf = (
                str(r.get("tool") or "").strip()
                or nuke_tool_from_filename(str(r.get("workflow") or ""))
                or "(unknown)"
            )
            w = by_wf.setdefault(wf, _empty_wf(wf))
            if ev == EVENT_JOB_QUEUE:
                w["jobs_queued"] += 1
            elif ev == EVENT_JOB_DONE:
                w["jobs_done"] += 1
                w["total_runtime_sec"] += float(r.get("runtime_sec") or 0)
            elif ev == EVENT_JOB_ERROR:
                w["jobs_error"] += 1
        people_by_wf: Dict[str, set] = defaultdict(set)
        for r in rows:
            ev = str(r.get("event") or "")
            if ev not in (EVENT_JOB_QUEUE, EVENT_JOB_DONE, EVENT_JOB_ERROR):
                continue
            wf = (
                str(r.get("tool") or "").strip()
                or nuke_tool_from_filename(str(r.get("workflow") or ""))
                or "(unknown)"
            )
            who = (
                str(r.get("machine_id") or "")
                or str(r.get("ip") or "")
                or str(r.get("label") or "")
            )
            if who:
                people_by_wf[wf].add(who)
        workflows = []
        for wf, w in by_wf.items():
            done = int(w["jobs_done"] or 0)
            w["total_runtime_sec"] = round(float(w["total_runtime_sec"]), 2)
            w["avg_runtime_sec"] = (
                round(float(w["total_runtime_sec"]) / done, 2) if done else 0.0
            )
            w["people"] = len(people_by_wf.get(wf) or [])
            workflows.append(w)
        workflows.sort(
            key=lambda x: (
                str(x.get("workflow") or "") == "(unknown)",
                -int(x.get("jobs_done") or 0),
                -int(x.get("jobs_queued") or 0),
                -float(x.get("total_runtime_sec") or 0),
                str(x.get("workflow") or ""),
            )
        )
        totals["workflows"] = len(
            [w for w in workflows if w.get("workflow") and w.get("workflow") != "(unknown)"]
        )

        by_proj: Dict[str, Dict[str, Any]] = {}
        people_by_proj: Dict[str, set] = defaultdict(set)
        for r in rows:
            ev = str(r.get("event") or "")
            if ev not in (EVENT_JOB_QUEUE, EVENT_JOB_DONE, EVENT_JOB_ERROR):
                continue
            pcode = str(r.get("project_code") or "").strip() or "(no code)"
            slot = by_proj.setdefault(
                pcode,
                {
                    "project": pcode,
                    "jobs_queued": 0,
                    "jobs_done": 0,
                    "jobs_error": 0,
                    "total_runtime_sec": 0.0,
                    "people": 0,
                    "users_label": "",
                },
            )
            who = str(r.get("label") or "").strip() or str(r.get("ip") or "")
            if who:
                people_by_proj[pcode].add(who)
            if ev == EVENT_JOB_QUEUE:
                slot["jobs_queued"] += 1
            elif ev == EVENT_JOB_DONE:
                slot["jobs_done"] += 1
                slot["total_runtime_sec"] += float(r.get("runtime_sec") or 0)
            elif ev == EVENT_JOB_ERROR:
                slot["jobs_error"] += 1
        user_counts: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"jobs_queued": 0, "jobs_done": 0})
        )
        for r in rows:
            ev = str(r.get("event") or "")
            if ev not in (EVENT_JOB_QUEUE, EVENT_JOB_DONE):
                continue
            pcode = str(r.get("project_code") or "").strip() or "(no code)"
            who = str(r.get("label") or "").strip() or str(r.get("ip") or "") or "?"
            bucket = user_counts[pcode][who]
            if ev == EVENT_JOB_QUEUE:
                bucket["jobs_queued"] += 1
            else:
                bucket["jobs_done"] += 1
        projects = []
        for pcode, slot in by_proj.items():
            slot["total_runtime_sec"] = round(float(slot["total_runtime_sec"]), 2)
            slot["people"] = len(people_by_proj.get(pcode) or [])
            bits = []
            ranked_users = sorted(
                user_counts.get(pcode, {}).items(),
                key=lambda kv: (
                    -(kv[1].get("jobs_done") or 0) - (kv[1].get("jobs_queued") or 0),
                    kv[0],
                ),
            )
            for who, info in ranked_users:
                n = int(info.get("jobs_done") or 0) or int(info.get("jobs_queued") or 0)
                if n:
                    bits.append("%s (%s)" % (who, n))
            slot["users_label"] = ", ".join(bits)
            projects.append(slot)
        projects.sort(
            key=lambda x: (
                str(x.get("project") or "") == "(no code)",
                -int(x.get("jobs_done") or 0),
                -int(x.get("jobs_queued") or 0),
                str(x.get("project") or ""),
            )
        )
        return {
            "totals": totals,
            "users": users,
            "workflows": workflows,
            "projects": projects,
            "count_users": len(users),
            "count_workflows": len(workflows),
            "count_projects": len(projects),
        }

    def to_csv(
        self,
        records: Iterable[Dict[str, Any]],
        *,
        kind: str = "events",
    ) -> str:
        buf = io.StringIO()
        if kind == "summary":
            # expect already summarized user dicts
            fieldnames = [
                "machine_id",
                "label",
                "group",
                "ip",
                "jobs_queued",
                "jobs_done",
                "jobs_error",
                "uploads",
                "downloads",
                "access_denied",
                "total_runtime_sec",
                "avg_runtime_sec",
                "workflows_label",
                "projects_label",
                "bytes_in",
                "bytes_out",
                "first_iso",
                "last_iso",
            ]
            w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for r in records:
                w.writerow({k: r.get(k, "") for k in fieldnames})
            return buf.getvalue()
        if kind == "workflows":
            fieldnames = [
                "workflow",
                "jobs_queued",
                "jobs_done",
                "jobs_error",
                "total_runtime_sec",
                "avg_runtime_sec",
                "people",
            ]
            w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for r in records:
                w.writerow({k: r.get(k, "") for k in fieldnames})
            return buf.getvalue()
        if kind == "projects":
            fieldnames = [
                "project",
                "jobs_queued",
                "jobs_done",
                "jobs_error",
                "total_runtime_sec",
                "people",
                "users_label",
            ]
            w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for r in records:
                w.writerow({k: r.get(k, "") for k in fieldnames})
            return buf.getvalue()
        else:
            fieldnames = [
                "id",
                "ts_iso",
                "ts",
                "event",
                "machine_id",
                "label",
                "group",
                "ip",
                "method",
                "path",
                "status",
                "prompt_id",
                "client_id",
                "workflow",
                "tool",
                "project_code",
                "project_bypass",
                "runtime_sec",
                "duration_ms",
                "bytes_in",
                "bytes_out",
                "detail",
            ]
            w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for r in records:
                w.writerow({k: r.get(k, "") for k in fieldnames})
        return buf.getvalue()


def classify_comfy_path(method: str, path: str) -> str:
    m = (method or "GET").upper()
    p = path or "/"
    if m == "POST" and p.rstrip("/") == "/prompt":
        return EVENT_JOB_QUEUE
    if m == "POST" and "/upload" in p:
        return EVENT_UPLOAD
    if m == "GET" and p.startswith("/view"):
        return EVENT_DOWNLOAD
    if m == "GET" and p.startswith("/history"):
        return "history"
    return EVENT_API


def parse_prompt_id_from_queue_response(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8"))
        pid = data.get("prompt_id") or data.get("promptId") or ""
        return str(pid)
    except Exception:
        return ""


def parse_history_completion(path: str, body: bytes) -> List[Tuple[str, str]]:
    """
    Return list of (prompt_id, status) for completed jobs found in history payload.
    status: success | error
    """
    out: List[Tuple[str, str]] = []
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return out
    if not isinstance(data, dict):
        return out

    # /history/{prompt_id} → single entry or map
    # Comfy usually returns {prompt_id: {status, outputs, ...}}
    def status_of(entry: Any) -> str:
        if not isinstance(entry, dict):
            return ""
        st = entry.get("status") or {}
        if isinstance(st, dict):
            if st.get("status_str") == "success" or st.get("completed") is True:
                # check error messages
                msgs = st.get("messages") or []
                for m in msgs:
                    if isinstance(m, (list, tuple)) and m and m[0] == "execution_error":
                        return "error"
                return "success"
            if st.get("status_str") in ("error", "failed"):
                return "error"
        if entry.get("outputs"):
            return "success"
        return ""

    # path may include prompt id
    parts = [x for x in path.split("/") if x]
    single_id = ""
    if len(parts) >= 2 and parts[0] == "history":
        single_id = parts[1]

    if single_id and single_id in data:
        st = status_of(data[single_id])
        if st:
            out.append((single_id, st))
        return out

    for pid, entry in data.items():
        st = status_of(entry)
        if st:
            out.append((str(pid), st))
    return out

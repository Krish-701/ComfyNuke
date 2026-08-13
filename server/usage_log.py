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


class UsageLog:
    def __init__(self, path: Path, max_lines: int = 200_000):
        self.path = Path(path)
        self.max_lines = max_lines
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
    ) -> Dict[str, Any]:
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
        )
        with self._lock:
            self._open_jobs[str(prompt_id)] = {
                "start_ts": rec["ts"],
                "ip": ip,
                "machine_id": machine_id,
                "label": label,
                "group": group,
                "client_id": client_id,
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
        q: str = "",
        limit: int = 500,
        reverse: bool = True,
    ) -> List[Dict[str, Any]]:
        ip = (ip or "").strip()
        label = (label or "").strip().lower()
        group = (group or "").strip().lower()
        event = (event or "").strip()
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
                    )
                ).lower()
                if q not in blob:
                    continue
            out.append(rec)
            if len(out) >= limit:
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
            users.append(u)
        users.sort(
            key=lambda x: (-float(x.get("total_runtime_sec") or 0), -int(x.get("jobs_done") or 0))
        )
        totals["total_runtime_sec"] = round(float(totals["total_runtime_sec"]), 2)
        return {"totals": totals, "users": users, "count_users": len(users)}

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
                "bytes_in",
                "bytes_out",
                "first_iso",
                "last_iso",
            ]
            w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for r in records:
                w.writerow({k: r.get(k, "") for k in fieldnames})
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

# -*- coding: utf-8 -*-
"""Project codes for Nuke jobs.

Active codes are matched case-insensitively. The stored spelling is what
the admin typed. Archived codes stay in the file for restore and do not match.
A workflow in require_workflows must send a matching code unless the machine
has skip_project_code.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_LOCK = threading.RLock()
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,39}$")

DEFAULT_GROUPS = (
    "Roto",
    "MM",
    "CH-Paint",
    "HYD-Paint",
    "Comp",
    "CG",
    "DMP",
    "All-Group",
)

SEED_CODES = (
    "OIG_SYMU",
    "LBP_EXS",
    "INI_XMP",
    "OFM_TLMP",
    "SUB_BC",
    "img_prc",
    "RFX_HKR",
    "RFX_SHV",
    "nba_mrcl",
    "cxz_sra",
    "DNG_GPW",
    "FPS_LLA",
    "FXM_CPS",
    "NEN_NMX",
    "NEN_TCR",
    "NLH_MRC",
    "QUI_FZO",
    "RWT_CKS",
)


def _now() -> int:
    return int(time.time())


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def normalize_code(raw: str) -> str:
    return str(raw or "").strip()


def code_key(raw: str) -> str:
    return normalize_code(raw).casefold()


def workflow_name(raw: str) -> str:
    name = str(raw or "").replace("\\", "/").split("/")[-1].strip()
    return name


def normalize_group(raw: str) -> str:
    text = " ".join(str(raw or "").split())
    return text[:40]


def parse_code_list(raw: str) -> List[str]:
    text = str(raw or "").replace(";", ",").replace("\n", ",").replace("\t", ",")
    out = []
    seen = set()
    for part in text.split(","):
        code = normalize_code(part)
        if not code:
            continue
        key = code_key(code)
        if key in seen:
            continue
        seen.add(key)
        out.append(code)
    return out


class ProjectCodes:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._cfg: Dict[str, Any] = {"codes": [], "require_workflows": []}
        self.load()

    def load(self) -> None:
        with _LOCK:
            if not self.path.is_file():
                self._cfg = self._seed()
                self._save_unlocked()
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            codes = []
            for row in data.get("codes") or []:
                if not isinstance(row, dict):
                    continue
                code = normalize_code(row.get("code") or "")
                if not code:
                    continue
                codes.append(
                    {
                        "id": str(row.get("id") or _new_id()),
                        "code": code,
                        "group": normalize_group(row.get("group") or ""),
                        "note": str(row.get("note") or "").strip(),
                        "archived": bool(row.get("archived")),
                        "created_at": int(row.get("created_at") or _now()),
                        "updated_at": int(row.get("updated_at") or _now()),
                    }
                )
            req = []
            for name in data.get("require_workflows") or []:
                base = workflow_name(name)
                if base and base not in req:
                    req.append(base)
            groups = []
            for name in list(DEFAULT_GROUPS) + list(data.get("groups") or []):
                g = normalize_group(name)
                if g and g not in groups:
                    groups.append(g)
            for row in codes:
                g = row.get("group") or ""
                if g and g not in groups:
                    groups.append(g)
            self._cfg = {"codes": codes, "require_workflows": req, "groups": groups}
            if not codes:
                self._cfg = self._seed()
                self._save_unlocked()

    def _seed(self) -> Dict[str, Any]:
        now = _now()
        return {
            "codes": [
                {
                    "id": _new_id(),
                    "code": code,
                    "group": "",
                    "note": "",
                    "archived": False,
                    "created_at": now,
                    "updated_at": now,
                }
                for code in SEED_CODES
            ],
            "require_workflows": [],
            "groups": list(DEFAULT_GROUPS),
        }

    def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(str(tmp), str(self.path))

    def reload(self) -> None:
        self.load()

    def _active(self) -> List[Dict[str, Any]]:
        return [c for c in self._cfg.get("codes") or [] if not c.get("archived")]

    def match(self, raw: str) -> Optional[str]:
        key = code_key(raw)
        if not key:
            return None
        with _LOCK:
            for row in self._active():
                if code_key(row.get("code") or "") == key:
                    return str(row.get("code") or "")
        return None

    def requires(self, filename: str) -> bool:
        name = workflow_name(filename)
        if not name:
            return False
        with _LOCK:
            return name in (self._cfg.get("require_workflows") or [])

    def check(self, filename: str, raw: str, skip: bool) -> Tuple[bool, str, str]:
        """Return (ok, canonical_code, error). Exempt machines ok with empty code."""
        if skip or not self.requires(filename):
            return True, "", ""
        found = self.match(raw)
        if not found:
            if not normalize_code(raw):
                return False, "", "Project code is required for this workflow"
            return False, "", "Project code not recognized"
        return True, found, ""

    def snapshot(self, discovered: Optional[List[str]] = None) -> Dict[str, Any]:
        with _LOCK:
            codes = [dict(c) for c in self._cfg.get("codes") or []]
            required = list(self._cfg.get("require_workflows") or [])
            groups = list(self._cfg.get("groups") or list(DEFAULT_GROUPS))
        active = [c for c in codes if not c.get("archived")]
        archived = [c for c in codes if c.get("archived")]
        active.sort(key=lambda c: (code_key(c.get("group") or ""), code_key(c.get("code") or "")))
        archived.sort(key=lambda c: (code_key(c.get("group") or ""), code_key(c.get("code") or "")))
        names = []
        for name in discovered or []:
            base = workflow_name(name)
            if base and base not in names and base.endswith(".json"):
                names.append(base)
        for name in required:
            if name not in names:
                names.append(name)
        return {
            "ok": True,
            "codes": active,
            "archived": archived,
            "groups": groups,
            "require_workflows": required,
            "workflows": names,
        }

    def _remember_group(self, group: str) -> None:
        g = normalize_group(group)
        if not g:
            return
        groups = list(self._cfg.get("groups") or [])
        if g not in groups:
            groups.append(g)
            self._cfg["groups"] = groups

    def save_code(
        self,
        code: str,
        note: str = "",
        entry_id: str = "",
        group: str = "",
    ) -> Dict[str, Any]:
        text = normalize_code(code)
        if not _CODE_RE.match(text):
            raise ValueError(
                "Project code must be 2–40 letters, numbers, _ or -"
            )
        note_s = str(note or "").strip()[:200]
        group_s = normalize_group(group)
        with _LOCK:
            rows = list(self._cfg.get("codes") or [])
            key = code_key(text)
            target = None
            if entry_id:
                for row in rows:
                    if str(row.get("id")) == str(entry_id):
                        target = row
                        break
                if target is None:
                    raise ValueError("project code not found")
            for row in rows:
                if target is not None and row is target:
                    continue
                if row.get("archived"):
                    continue
                if code_key(row.get("code") or "") == key:
                    raise ValueError("That project code already exists")
            now = _now()
            if target is None:
                target = {
                    "id": _new_id(),
                    "code": text,
                    "group": group_s,
                    "note": note_s,
                    "archived": False,
                    "created_at": now,
                    "updated_at": now,
                }
                rows.append(target)
            else:
                target["code"] = text
                target["group"] = group_s
                target["note"] = note_s
                target["updated_at"] = now
            self._remember_group(group_s)
            self._cfg["codes"] = rows
            self._save_unlocked()
            return dict(target)

    def set_archived(self, entry_id: str, archived: bool) -> Dict[str, Any]:
        with _LOCK:
            rows = list(self._cfg.get("codes") or [])
            target = None
            for row in rows:
                if str(row.get("id")) == str(entry_id):
                    target = row
                    break
            if target is None:
                raise ValueError("project code not found")
            if not archived:
                key = code_key(target.get("code") or "")
                for row in rows:
                    if row is target or row.get("archived"):
                        continue
                    if code_key(row.get("code") or "") == key:
                        raise ValueError("An active code with that spelling already exists")
            target["archived"] = bool(archived)
            target["updated_at"] = _now()
            self._cfg["codes"] = rows
            self._save_unlocked()
            return dict(target)

    def set_archived_many(self, ids: List[str], archived: bool) -> Dict[str, Any]:
        updated = []
        missing = []
        errors = []
        for raw_id in ids or []:
            entry_id = str(raw_id or "").strip()
            if not entry_id:
                continue
            try:
                updated.append(self.set_archived(entry_id, archived))
            except ValueError as e:
                msg = str(e)
                if "not found" in msg:
                    missing.append(entry_id)
                else:
                    errors.append({"id": entry_id, "error": msg})
        return {"updated": len(updated), "missing": missing, "errors": errors}

    def import_codes(self, raw: str, group: str) -> Dict[str, Any]:
        group_s = normalize_group(group)
        if not group_s:
            raise ValueError("Pick a group before importing")
        added = []
        moved = []
        same = []
        restored = []
        invalid = []
        for code in parse_code_list(raw):
            if not _CODE_RE.match(code):
                invalid.append(code)
                continue
            key = code_key(code)
            with _LOCK:
                rows = list(self._cfg.get("codes") or [])
                hit = None
                for row in rows:
                    if code_key(row.get("code") or "") == key:
                        hit = row
                        break
            if hit is None:
                added.append(self.save_code(code, group=group_s)["code"])
                continue
            if hit.get("archived"):
                self.set_archived(str(hit.get("id")), False)
                self.save_code(
                    code,
                    note=str(hit.get("note") or ""),
                    entry_id=str(hit.get("id")),
                    group=group_s,
                )
                restored.append(str(hit.get("code") or code))
                continue
            if normalize_group(hit.get("group") or "") == group_s:
                same.append(str(hit.get("code") or code))
                continue
            self.save_code(
                str(hit.get("code") or code),
                note=str(hit.get("note") or ""),
                entry_id=str(hit.get("id")),
                group=group_s,
            )
            moved.append(str(hit.get("code") or code))
        return {
            "group": group_s,
            "added": added,
            "moved": moved,
            "already": same,
            "restored": restored,
            "invalid": invalid,
        }

    def set_required(self, names: List[str]) -> List[str]:
        clean = []
        for name in names or []:
            base = workflow_name(name)
            if not base.endswith(".json"):
                continue
            if base not in clean:
                clean.append(base)
        with _LOCK:
            self._cfg["require_workflows"] = clean
            self._save_unlocked()
            return list(clean)

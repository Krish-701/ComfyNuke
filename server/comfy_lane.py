"""One waiting line per ComfyUI server.

Nuke asks the hub for a ticket, then polls until that server has no running
and no pending job. Only the head of the line may POST /prompt. A ticket-less
prompt is still allowed when the line is empty, so an old script keeps working
until the artist reloads.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

READY_LEASE_SEC = 60.0
IDLE_DROP_SEC = 90.0
TICKET_MAX_AGE_SEC = 6 * 3600.0
PROBE_CACHE_SEC = 1.5
SUBMIT_HOLD_SEC = 25.0

ProbeFn = Callable[[str], bool]


class ComfyLane:
    def __init__(self, probe_busy: ProbeFn) -> None:
        self._probe_busy = probe_busy
        self._lock = threading.RLock()
        self._lanes: Dict[str, Dict[str, Any]] = {}
        self._busy_cache: Dict[str, Tuple[float, bool]] = {}
        self._hold_until: Dict[str, float] = {}

    def join(self, server_id: str, artist: str = "", workflow: str = "") -> Dict[str, Any]:
        sid = (server_id or "main").strip() or "main"
        now = time.time()
        ticket = uuid.uuid4().hex
        with self._lock:
            self._gc_locked(now)
            lane = self._lanes.setdefault(sid, {"order": [], "tickets": {}})
            lane["tickets"][ticket] = {
                "id": ticket,
                "server_id": sid,
                "artist": str(artist or "")[:80],
                "workflow": str(workflow or "")[:180],
                "created": now,
                "seen": now,
                "state": "waiting",
                "ready_until": 0.0,
            }
            lane["order"].append(ticket)
        return self.status(ticket)

    def status(self, ticket: str) -> Dict[str, Any]:
        ticket = str(ticket or "").strip()
        with self._lock:
            rec = self._find_locked(ticket)
            sid = str(rec["server_id"]) if rec else ""
        busy = self._is_busy(sid) if sid else True
        now = time.time()
        with self._lock:
            self._gc_locked(now)
            if sid and not busy and now >= float(self._hold_until.get(sid) or 0):
                self._promote_locked(sid, now)
            rec = self._find_locked(ticket)
            if rec is None:
                return {
                    "ok": True,
                    "ticket": ticket,
                    "state": "gone",
                    "position": 0,
                    "queue_size": 0,
                    "server_id": sid,
                }
            rec["seen"] = now
            lane = self._lanes.get(sid) or {"order": []}
            order = lane.get("order") or []
            try:
                pos = order.index(ticket) + 1
            except ValueError:
                pos = 0
            return {
                "ok": True,
                "ticket": ticket,
                "state": rec.get("state") or "waiting",
                "position": pos,
                "queue_size": len(order),
                "server_id": sid,
            }

    def leave(self, ticket: str) -> Dict[str, Any]:
        ticket = str(ticket or "").strip()
        with self._lock:
            rec = self._find_locked(ticket)
            if rec is None:
                return {"ok": True, "ticket": ticket, "state": "gone"}
            self._drop_locked(str(rec["server_id"]), ticket)
        return {"ok": True, "ticket": ticket, "state": "left"}

    def claim(self, server_id: str, ticket: str) -> Tuple[bool, str]:
        """Let this prompt through. Consumes a ready ticket, or passes when the line is empty."""
        sid = (server_id or "main").strip() or "main"
        ticket = str(ticket or "").strip()
        now = time.time()
        with self._lock:
            self._gc_locked(now)
            lane = self._lanes.get(sid)
            order = list((lane or {}).get("order") or [])
            hold = now < float(self._hold_until.get(sid) or 0)
            if not ticket:
                if order or hold:
                    return False, "ComfyUI is busy. Wait in the queue and send again."
                return True, ""
            rec = (lane or {}).get("tickets", {}).get(ticket) if lane else None
            if not rec or rec.get("server_id") != sid:
                return False, "Queue ticket is not valid for this ComfyUI."
            if not order or order[0] != ticket:
                return False, "It is not your turn yet."
            if rec.get("state") != "ready" or now > float(rec.get("ready_until") or 0):
                return False, "ComfyUI is still busy. Wait for your turn."
            self._drop_locked(sid, ticket)
            self._hold_until[sid] = now + SUBMIT_HOLD_SEC
            return True, ""

    def _find_locked(self, ticket: str) -> Optional[Dict[str, Any]]:
        if not ticket:
            return None
        for lane in self._lanes.values():
            rec = (lane.get("tickets") or {}).get(ticket)
            if rec:
                return rec
        return None

    def _drop_locked(self, sid: str, ticket: str) -> None:
        lane = self._lanes.get(sid)
        if not lane:
            return
        lane["tickets"].pop(ticket, None)
        lane["order"] = [t for t in lane.get("order") or [] if t != ticket]

    def _gc_locked(self, now: float) -> None:
        for sid, lane in list(self._lanes.items()):
            kept = []
            tickets = lane.get("tickets") or {}
            order = list(lane.get("order") or [])
            for index, tid in enumerate(order):
                rec = tickets.get(tid)
                if not rec:
                    continue
                age = now - float(rec.get("created") or now)
                idle = now - float(rec.get("seen") or now)
                if age > TICKET_MAX_AGE_SEC or idle > IDLE_DROP_SEC:
                    tickets.pop(tid, None)
                    continue
                if rec.get("state") == "ready" and now > float(rec.get("ready_until") or 0):
                    rec["state"] = "waiting"
                if index == 0 or tid in tickets:
                    kept.append(tid)
            lane["order"] = kept

    def _promote_locked(self, sid: str, now: float) -> None:
        lane = self._lanes.get(sid)
        if not lane:
            return
        order = lane.get("order") or []
        if not order:
            return
        rec = (lane.get("tickets") or {}).get(order[0])
        if not rec:
            return
        if rec.get("state") == "ready" and now <= float(rec.get("ready_until") or 0):
            return
        rec["state"] = "ready"
        rec["ready_until"] = now + READY_LEASE_SEC

    def _is_busy(self, server_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hit = self._busy_cache.get(server_id)
            if hit is not None and (now - hit[0]) < PROBE_CACHE_SEC:
                return hit[1]
            # Hold the previous answer so a burst of status polls shares one probe.
            self._busy_cache[server_id] = (now, True if hit is None else hit[1])
        try:
            busy = bool(self._probe_busy(server_id))
        except Exception:
            busy = True
        with self._lock:
            self._busy_cache[server_id] = (time.monotonic(), busy)
        return busy

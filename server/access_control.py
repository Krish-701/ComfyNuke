# -*- coding: utf-8 -*-
"""
LAN access control for ComfyNuke code server (:8600).

Stores allowlist + admin credentials in access_control.json (not committed).
Entries support short name (label), group, enable flag; admin UI can edit/rename.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_ADMIN_USER = "Krish"
# First-run password: set COMFYNUKE_ADMIN_PASSWORD env.
# Never commit the live access_control.json (hashed secret lives there).

PBKDF2_ROUNDS = 120_000
SESSION_TTL_SEC = 12 * 3600
UNGROUPED = "Ungrouped"

# Role-based access control for /admin UI + APIs
ROLES = ("admin", "operator", "viewer")
ROLE_PERMS: Dict[str, List[str]] = {
    # full control
    "admin": ["*"],
    # manage studio machines + ACL + logs; no user admin
    "operator": [
        "dashboard.view",
        "acl.view",
        "acl.toggle",
        "machines.view",
        "machines.edit",
        "logs.view",
        "logs.export",
    ],
    # read-only
    "viewer": [
        "dashboard.view",
        "acl.view",
        "machines.view",
        "logs.view",
        "logs.export",
    ],
}
ROLE_LABELS = {
    "admin": "Admin — full control (users, ACL, logs)",
    "operator": "Operator — manage IPs/groups + view logs",
    "viewer": "Viewer — read-only machines + logs",
}


def role_permissions(role: str) -> List[str]:
    r = (role or "viewer").strip().lower()
    if r not in ROLE_PERMS:
        r = "viewer"
    return list(ROLE_PERMS[r])


def has_permission(role: str, perm: str) -> bool:
    perms = role_permissions(role)
    if "*" in perms:
        return True
    if perm in perms:
        return True
    # prefix: machines.edit implies machines.view? optional — keep explicit
    return False


def _hash_password(password: str, salt: Optional[bytes] = None) -> Dict[str, str]:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS
    )
    return {
        "algo": "pbkdf2_sha256",
        "rounds": str(PBKDF2_ROUNDS),
        "salt": salt.hex(),
        "hash": digest.hex(),
    }


def _verify_password(password: str, stored: Dict[str, Any]) -> bool:
    try:
        salt = bytes.fromhex(str(stored.get("salt") or ""))
        rounds = int(stored.get("rounds") or PBKDF2_ROUNDS)
        expect = str(stored.get("hash") or "")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, rounds
        )
        return hmac.compare_digest(digest.hex(), expect)
    except Exception:
        return False


def _normalize_ip(ip: str) -> str:
    ip = (ip or "").strip()
    if not ip:
        raise ValueError("empty IP")
    if ip.startswith("[") and "]" in ip:
        ip = ip[1 : ip.index("]")]
    if "%" in ip:
        ip = ip.split("%", 1)[0]
    obj = ipaddress.ip_address(ip)
    if isinstance(obj, ipaddress.IPv6Address) and obj.ipv4_mapped:
        return str(obj.ipv4_mapped)
    return str(obj)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _normalize_group(group: Optional[str]) -> str:
    g = (group or "").strip()
    return g if g else UNGROUPED


def _normalize_entry(e: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure entry has id, label, group, enabled, ip."""
    out = dict(e) if isinstance(e, dict) else {}
    if not out.get("id"):
        out["id"] = _new_id()
    try:
        out["ip"] = _normalize_ip(str(out.get("ip") or ""))
    except Exception:
        out["ip"] = str(out.get("ip") or "").strip()
    out["label"] = str(out.get("label") or "").strip()
    out["group"] = _normalize_group(str(out.get("group") or ""))
    out["enabled"] = bool(out.get("enabled", True))
    return out


def _make_user(
    username: str,
    password: str,
    role: str = "admin",
    display_name: str = "",
    enabled: bool = True,
    password_hash: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    role = (role or "viewer").strip().lower()
    if role not in ROLES:
        role = "viewer"
    return {
        "id": _new_id(),
        "username": username.strip(),
        "display_name": (display_name or username).strip(),
        "role": role,
        "enabled": bool(enabled),
        "password": password_hash if password_hash is not None else _hash_password(password),
        "created_at": int(time.time()),
    }


def _public_user(u: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": u.get("id") or "",
        "username": u.get("username") or "",
        "display_name": u.get("display_name") or u.get("username") or "",
        "role": u.get("role") or "viewer",
        "enabled": bool(u.get("enabled", True)),
        "permissions": role_permissions(str(u.get("role") or "viewer")),
        "role_label": ROLE_LABELS.get(str(u.get("role") or "viewer"), ""),
    }


def _default_config(admin_user: str, admin_password: str) -> Dict[str, Any]:
    admin = _make_user(admin_user, admin_password, role="admin")
    return {
        "enabled": False,
        # legacy fields kept in sync with primary admin for older tools
        "admin_user": admin_user,
        "admin_password": admin["password"],
        "users": [admin],
        "session_secret": secrets.token_hex(32),
        "allow_localhost": True,
        "ips": [],
        "groups": [],  # optional ordered group names
        "notes": "When enabled=true, only listed+enabled IPs may download scripts/workflows "
        "or use the ComfyUI proxy on this port. Admin UI uses role-based login.",
        "updated_at": int(time.time()),
    }


class AccessControl:
    def __init__(
        self,
        path: Path,
        bootstrap_user: str = DEFAULT_ADMIN_USER,
        bootstrap_password: Optional[str] = None,
    ):
        self.path = Path(path)
        self._lock = threading.RLock()
        # token -> {username, role, exp}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._cfg: Dict[str, Any] = {}
        self._bootstrap_user = bootstrap_user
        self._bootstrap_password = bootstrap_password or os.environ.get(
            "COMFYNUKE_ADMIN_PASSWORD"
        ) or secrets.token_urlsafe(12)
        self._generated_bootstrap = bootstrap_password is None and not os.environ.get(
            "COMFYNUKE_ADMIN_PASSWORD"
        )
        self.load_or_create()

    def _migrate_users_unlocked(self) -> bool:
        """Ensure users[] exists; migrate legacy admin_user/admin_password."""
        dirty = False
        users = self._cfg.get("users")
        if not isinstance(users, list) or not users:
            # migrate single admin
            username = str(self._cfg.get("admin_user") or self._bootstrap_user or "admin")
            pw_hash = self._cfg.get("admin_password")
            if isinstance(pw_hash, dict) and pw_hash.get("hash"):
                u = _make_user(
                    username,
                    password="unused",
                    role="admin",
                    password_hash=pw_hash,
                )
            else:
                u = _make_user(
                    username,
                    self._bootstrap_password,
                    role="admin",
                )
            self._cfg["users"] = [u]
            self._cfg["admin_user"] = u["username"]
            self._cfg["admin_password"] = u["password"]
            dirty = True
        else:
            fixed = []
            for u in users:
                if not isinstance(u, dict):
                    continue
                nu = dict(u)
                if not nu.get("id"):
                    nu["id"] = _new_id()
                    dirty = True
                nu["username"] = str(nu.get("username") or "").strip()
                nu["display_name"] = str(
                    nu.get("display_name") or nu["username"]
                ).strip()
                role = str(nu.get("role") or "viewer").lower()
                if role not in ROLES:
                    role = "viewer"
                    dirty = True
                nu["role"] = role
                nu["enabled"] = bool(nu.get("enabled", True))
                if not isinstance(nu.get("password"), dict):
                    dirty = True
                    continue  # skip broken
                fixed.append(nu)
            if not fixed:
                fixed = [
                    _make_user(
                        self._bootstrap_user,
                        self._bootstrap_password,
                        role="admin",
                    )
                ]
                dirty = True
            self._cfg["users"] = fixed
            # keep legacy primary admin pointer = first admin
            for u in fixed:
                if u.get("role") == "admin" and u.get("enabled", True):
                    self._cfg["admin_user"] = u["username"]
                    self._cfg["admin_password"] = u["password"]
                    break
        return dirty

    def load_or_create(self) -> None:
        with self._lock:
            if not self.path.is_file():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._cfg = _default_config(
                    self._bootstrap_user, self._bootstrap_password
                )
                self._save_unlocked()
            else:
                self._cfg = json.loads(self.path.read_text(encoding="utf-8"))
                dirty = False
                if "session_secret" not in self._cfg:
                    self._cfg["session_secret"] = secrets.token_hex(32)
                    dirty = True
                if "admin_password" not in self._cfg or not self._cfg.get("admin_user"):
                    self._cfg["admin_user"] = self._bootstrap_user
                    self._cfg["admin_password"] = _hash_password(
                        self._bootstrap_password
                    )
                    dirty = True
                if self._migrate_users_unlocked():
                    dirty = True
                if "groups" not in self._cfg:
                    self._cfg["groups"] = []
                    dirty = True
                # migrate entries
                ips = []
                for e in self._cfg.get("ips") or []:
                    if isinstance(e, dict):
                        ne = _normalize_entry(e)
                        ips.append(ne)
                        dirty = True
                self._cfg["ips"] = ips
                # collect groups from entries
                known = list(self._cfg.get("groups") or [])
                for e in ips:
                    g = e.get("group") or UNGROUPED
                    if g not in known and g != UNGROUPED:
                        known.append(g)
                        dirty = True
                self._cfg["groups"] = known
                if dirty:
                    self._save_unlocked()

    def _save_unlocked(self) -> None:
        self._cfg["updated_at"] = int(time.time())
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._cfg, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(self.path))
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    def reload(self) -> None:
        with self._lock:
            if self.path.is_file():
                self._cfg = json.loads(self.path.read_text(encoding="utf-8"))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            ips = [_normalize_entry(e) for e in (self._cfg.get("ips") or []) if isinstance(e, dict)]
            groups = list(self._cfg.get("groups") or [])
            for e in ips:
                g = e.get("group") or UNGROUPED
                if g not in groups and g != UNGROUPED:
                    groups.append(g)
            return {
                "enabled": bool(self._cfg.get("enabled")),
                "allow_localhost": bool(self._cfg.get("allow_localhost", True)),
                "admin_user": str(self._cfg.get("admin_user") or ""),
                "ips": ips,
                "groups": groups,
                "ungrouped_name": UNGROUPED,
                "updated_at": self._cfg.get("updated_at"),
                "notes": self._cfg.get("notes") or "",
                "roles": [
                    {"id": r, "label": ROLE_LABELS.get(r, r), "permissions": ROLE_PERMS[r]}
                    for r in ROLES
                ],
            }

    def is_acl_enabled(self) -> bool:
        with self._lock:
            return bool(self._cfg.get("enabled"))

    def lookup_machine(self, client_ip: str) -> Dict[str, str]:
        """Return {id, label, group, ip, enabled} for an IP if known."""
        try:
            nip = _normalize_ip(client_ip)
        except Exception:
            return {
                "id": "",
                "label": "",
                "group": "",
                "ip": (client_ip or "").strip(),
                "enabled": "",
            }
        with self._lock:
            for entry in self._cfg.get("ips") or []:
                if not isinstance(entry, dict):
                    continue
                try:
                    if _normalize_ip(str(entry.get("ip") or "")) == nip:
                        e = _normalize_entry(entry)
                        return {
                            "id": str(e.get("id") or ""),
                            "label": str(e.get("label") or ""),
                            "group": str(e.get("group") or ""),
                            "ip": str(e.get("ip") or nip),
                            "enabled": "1" if e.get("enabled") else "0",
                        }
                except Exception:
                    continue
        return {
            "id": "",
            "label": "",
            "group": "",
            "ip": nip,
            "enabled": "",
        }

    def client_allowed(self, client_ip: str) -> Tuple[bool, str]:
        with self._lock:
            if not self._cfg.get("enabled"):
                return True, "acl_disabled"
            try:
                nip = _normalize_ip(client_ip)
            except Exception:
                return False, "invalid_client_ip"

            if self._cfg.get("allow_localhost", True):
                try:
                    obj = ipaddress.ip_address(nip)
                    if obj.is_loopback:
                        return True, "localhost"
                except Exception:
                    pass
                if nip in ("127.0.0.1", "::1"):
                    return True, "localhost"

            for entry in self._cfg.get("ips") or []:
                if not isinstance(entry, dict):
                    continue
                if not entry.get("enabled", True):
                    continue
                try:
                    if _normalize_ip(str(entry.get("ip") or "")) == nip:
                        return True, "allowlist"
                except Exception:
                    continue
            return False, "ip_not_allowed"

    def _find_user_unlocked(self, username: str) -> Optional[Dict[str, Any]]:
        uname = (username or "").strip()
        for u in self._cfg.get("users") or []:
            if not isinstance(u, dict):
                continue
            if str(u.get("username") or "") == uname:
                return u
        # legacy single admin
        if uname and uname == str(self._cfg.get("admin_user") or ""):
            return {
                "id": "legacy",
                "username": uname,
                "display_name": uname,
                "role": "admin",
                "enabled": True,
                "password": self._cfg.get("admin_password") or {},
            }
        return None

    def verify_admin(self, username: str, password: str) -> bool:
        """Back-compat: any enabled user with valid password."""
        return self.authenticate(username, password) is not None

    def authenticate(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """Return public user dict on success, else None."""
        with self._lock:
            u = self._find_user_unlocked(username)
            if not u:
                return None
            if not u.get("enabled", True):
                return None
            stored = u.get("password") or {}
            if not isinstance(stored, dict):
                return None
            if not _verify_password(password, stored):
                return None
            return _public_user(u)

    def create_session(self, username: str = "", role: str = "admin") -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            # resolve role from user if needed
            r = (role or "viewer").lower()
            uname = (username or "").strip()
            if uname:
                u = self._find_user_unlocked(uname)
                if u:
                    r = str(u.get("role") or r).lower()
            if r not in ROLES:
                r = "viewer"
            self._sessions[token] = {
                "username": uname,
                "role": r,
                "exp": time.time() + SESSION_TTL_SEC,
            }
            now = time.time()
            dead = [k for k, s in self._sessions.items() if float(s.get("exp") or 0) < now]
            for k in dead:
                del self._sessions[k]
        return token

    def session_ok(self, token: Optional[str]) -> bool:
        return self.session_info(token) is not None

    def session_info(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        with self._lock:
            s = self._sessions.get(token)
            if not s:
                return None
            exp = float(s.get("exp") or 0)
            if exp < time.time():
                del self._sessions[token]
                return None
            s["exp"] = time.time() + SESSION_TTL_SEC
            username = str(s.get("username") or "")
            role = str(s.get("role") or "viewer")
            # refresh role from live user record
            u = self._find_user_unlocked(username) if username else None
            if u:
                if not u.get("enabled", True):
                    del self._sessions[token]
                    return None
                role = str(u.get("role") or role)
                s["role"] = role
            return {
                "username": username,
                "role": role,
                "permissions": role_permissions(role),
                "display_name": (u or {}).get("display_name") or username,
                "user_id": (u or {}).get("id") or "",
            }

    def session_has(self, token: Optional[str], perm: str) -> bool:
        info = self.session_info(token)
        if not info:
            return False
        return has_permission(str(info.get("role") or ""), perm)

    def destroy_session(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def list_users(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                _public_user(u)
                for u in (self._cfg.get("users") or [])
                if isinstance(u, dict)
            ]

    def create_user(
        self,
        username: str,
        password: str,
        role: str = "viewer",
        display_name: str = "",
        enabled: bool = True,
    ) -> Dict[str, Any]:
        uname = (username or "").strip()
        if not uname or len(uname) < 2:
            raise ValueError("username too short")
        if len(password or "") < 8:
            raise ValueError("password min 8 characters")
        role = (role or "viewer").lower()
        if role not in ROLES:
            raise ValueError("invalid role (admin|operator|viewer)")
        with self._lock:
            if self._find_user_unlocked(uname):
                raise ValueError("username already exists")
            u = _make_user(uname, password, role=role, display_name=display_name, enabled=enabled)
            users = list(self._cfg.get("users") or [])
            users.append(u)
            self._cfg["users"] = users
            self._save_unlocked()
            return _public_user(u)

    def update_user(
        self,
        username: str = "",
        user_id: str = "",
        *,
        display_name: Optional[str] = None,
        role: Optional[str] = None,
        enabled: Optional[bool] = None,
        new_password: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            idx = -1
            users = list(self._cfg.get("users") or [])
            for i, u in enumerate(users):
                if not isinstance(u, dict):
                    continue
                if user_id and str(u.get("id") or "") == user_id:
                    idx = i
                    break
                if username and str(u.get("username") or "") == username.strip():
                    idx = i
                    break
            if idx < 0:
                raise ValueError("user not found")
            u = dict(users[idx])
            if display_name is not None:
                u["display_name"] = str(display_name).strip() or u.get("username")
            if role is not None:
                r = str(role).lower()
                if r not in ROLES:
                    raise ValueError("invalid role")
                # prevent removing last admin
                if u.get("role") == "admin" and r != "admin":
                    admins = [
                        x
                        for x in users
                        if isinstance(x, dict)
                        and x.get("role") == "admin"
                        and x.get("enabled", True)
                        and str(x.get("id")) != str(u.get("id"))
                    ]
                    if not admins:
                        raise ValueError("cannot demote the last admin")
                u["role"] = r
            if enabled is not None:
                if u.get("role") == "admin" and not enabled:
                    admins = [
                        x
                        for x in users
                        if isinstance(x, dict)
                        and x.get("role") == "admin"
                        and x.get("enabled", True)
                        and str(x.get("id")) != str(u.get("id"))
                    ]
                    if not admins:
                        raise ValueError("cannot disable the last admin")
                u["enabled"] = bool(enabled)
            if new_password is not None and str(new_password):
                if len(str(new_password)) < 8:
                    raise ValueError("password min 8 characters")
                u["password"] = _hash_password(str(new_password))
            users[idx] = u
            self._cfg["users"] = users
            if u.get("role") == "admin" and u.get("enabled", True):
                self._cfg["admin_user"] = u["username"]
                self._cfg["admin_password"] = u["password"]
            self._save_unlocked()
            return _public_user(u)

    def delete_user(self, username: str = "", user_id: str = "") -> bool:
        with self._lock:
            users = list(self._cfg.get("users") or [])
            keep = []
            removed = None
            for u in users:
                if not isinstance(u, dict):
                    continue
                match = (user_id and str(u.get("id") or "") == user_id) or (
                    username and str(u.get("username") or "") == username.strip()
                )
                if match:
                    removed = u
                    continue
                keep.append(u)
            if not removed:
                return False
            if removed.get("role") == "admin":
                admins = [x for x in keep if x.get("role") == "admin" and x.get("enabled", True)]
                if not admins:
                    raise ValueError("cannot delete the last admin")
            self._cfg["users"] = keep
            self._save_unlocked()
            return True

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._cfg["enabled"] = bool(enabled)
            self._save_unlocked()

    def set_allow_localhost(self, allow: bool) -> None:
        with self._lock:
            self._cfg["allow_localhost"] = bool(allow)
            self._save_unlocked()

    def _find_index(self, ip: Optional[str] = None, entry_id: Optional[str] = None) -> int:
        ips = self._cfg.get("ips") or []
        if entry_id:
            for i, e in enumerate(ips):
                if isinstance(e, dict) and str(e.get("id") or "") == entry_id:
                    return i
        if ip:
            try:
                nip = _normalize_ip(ip)
            except Exception:
                nip = (ip or "").strip()
            for i, e in enumerate(ips):
                if not isinstance(e, dict):
                    continue
                try:
                    if _normalize_ip(str(e.get("ip") or "")) == nip:
                        return i
                except Exception:
                    if str(e.get("ip") or "") == nip:
                        return i
        return -1

    def _remember_group(self, group: str) -> None:
        g = _normalize_group(group)
        if g == UNGROUPED:
            return
        groups = list(self._cfg.get("groups") or [])
        if g not in groups:
            groups.append(g)
            self._cfg["groups"] = groups

    def upsert_ip(
        self,
        ip: str,
        label: str = "",
        enabled: bool = True,
        group: str = "",
        entry_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        nip = _normalize_ip(ip)
        g = _normalize_group(group)
        with self._lock:
            ips: List[Dict[str, Any]] = list(self._cfg.get("ips") or [])
            idx = self._find_index(ip=nip, entry_id=entry_id)
            if idx >= 0:
                e = _normalize_entry(ips[idx])
                e["ip"] = nip
                if label is not None:
                    e["label"] = str(label).strip()
                e["enabled"] = bool(enabled)
                e["group"] = g
                ips[idx] = e
            else:
                # refuse duplicate IP under another id
                if self._find_index(ip=nip) >= 0:
                    raise ValueError("IP already exists: %s" % nip)
                e = _normalize_entry(
                    {
                        "id": entry_id or _new_id(),
                        "ip": nip,
                        "label": (label or "").strip(),
                        "group": g,
                        "enabled": bool(enabled),
                    }
                )
                ips.append(e)
            self._remember_group(g)
            self._cfg["ips"] = ips
            self._save_unlocked()
            return e

    def update_entry(
        self,
        ip: Optional[str] = None,
        entry_id: Optional[str] = None,
        new_ip: Optional[str] = None,
        label: Optional[str] = None,
        group: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Edit an existing entry: change IP, rename label, move group, toggle."""
        with self._lock:
            idx = self._find_index(ip=ip, entry_id=entry_id)
            if idx < 0:
                raise ValueError("entry not found")
            ips = list(self._cfg.get("ips") or [])
            e = _normalize_entry(ips[idx])
            if new_ip is not None and str(new_ip).strip():
                nip = _normalize_ip(str(new_ip))
                # conflict?
                for j, other in enumerate(ips):
                    if j == idx or not isinstance(other, dict):
                        continue
                    try:
                        if _normalize_ip(str(other.get("ip") or "")) == nip:
                            raise ValueError("IP already used by another entry: %s" % nip)
                    except ValueError:
                        raise
                    except Exception:
                        pass
                e["ip"] = nip
            if label is not None:
                e["label"] = str(label).strip()
            if group is not None:
                e["group"] = _normalize_group(group)
                self._remember_group(e["group"])
            if enabled is not None:
                e["enabled"] = bool(enabled)
            ips[idx] = e
            self._cfg["ips"] = ips
            self._save_unlocked()
            return e

    def set_ip_enabled(self, ip: str, enabled: bool) -> bool:
        with self._lock:
            idx = self._find_index(ip=ip)
            if idx < 0:
                return False
            ips = list(self._cfg.get("ips") or [])
            e = _normalize_entry(ips[idx])
            e["enabled"] = bool(enabled)
            ips[idx] = e
            self._cfg["ips"] = ips
            self._save_unlocked()
            return True

    def set_group_enabled(self, group: str, enabled: bool) -> int:
        """Enable/disable all IPs in a group. Returns count updated."""
        g = _normalize_group(group)
        with self._lock:
            n = 0
            ips = []
            for e in self._cfg.get("ips") or []:
                if not isinstance(e, dict):
                    continue
                ne = _normalize_entry(e)
                if ne.get("group") == g:
                    ne["enabled"] = bool(enabled)
                    n += 1
                ips.append(ne)
            self._cfg["ips"] = ips
            self._save_unlocked()
            return n

    def rename_group(self, old_name: str, new_name: str) -> int:
        old = _normalize_group(old_name)
        new = _normalize_group(new_name)
        if old == new:
            return 0
        with self._lock:
            n = 0
            ips = []
            for e in self._cfg.get("ips") or []:
                if not isinstance(e, dict):
                    continue
                ne = _normalize_entry(e)
                if ne.get("group") == old:
                    ne["group"] = new
                    n += 1
                ips.append(ne)
            self._cfg["ips"] = ips
            groups = [g for g in (self._cfg.get("groups") or []) if g != old]
            if new != UNGROUPED and new not in groups:
                groups.append(new)
            self._cfg["groups"] = groups
            self._remember_group(new)
            self._save_unlocked()
            return n

    def add_group(self, name: str) -> str:
        g = _normalize_group(name)
        if g == UNGROUPED:
            raise ValueError("invalid group name")
        with self._lock:
            self._remember_group(g)
            self._save_unlocked()
            return g

    def remove_group(self, name: str, reassign_to: str = UNGROUPED) -> int:
        """Remove group name; move members to reassign_to. Returns member count."""
        g = _normalize_group(name)
        if g == UNGROUPED:
            raise ValueError("cannot remove Ungrouped")
        dest = _normalize_group(reassign_to)
        with self._lock:
            n = 0
            ips = []
            for e in self._cfg.get("ips") or []:
                if not isinstance(e, dict):
                    continue
                ne = _normalize_entry(e)
                if ne.get("group") == g:
                    ne["group"] = dest
                    n += 1
                ips.append(ne)
            self._cfg["ips"] = ips
            self._cfg["groups"] = [
                x for x in (self._cfg.get("groups") or []) if x != g
            ]
            if dest != UNGROUPED:
                self._remember_group(dest)
            self._save_unlocked()
            return n

    def remove_ip(self, ip: str = "", entry_id: str = "") -> bool:
        with self._lock:
            idx = self._find_index(ip=ip or None, entry_id=entry_id or None)
            if idx < 0:
                return False
            ips = list(self._cfg.get("ips") or [])
            del ips[idx]
            self._cfg["ips"] = ips
            self._save_unlocked()
            return True

    def change_password(self, username: str, old_password: str, new_password: str) -> bool:
        info = self.authenticate(username, old_password)
        if not info:
            return False
        if len(new_password) < 8:
            raise ValueError("password too short (min 8)")
        self.update_user(username=username, new_password=new_password)
        return True

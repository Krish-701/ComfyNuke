"""
ComfyUI client for Nuke <-> Edit_Image workflow (PNG RGBA + crop/inpaint/stitch).

Sequential jobs only. Default workflow: Edit_Image_v05.json
  LoadImage → mask → InpaintCrop → LLM (node 289 user text) → Qwen edit → Stitch → Save
"""

from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import os
import socket
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# Fallback node IDs (Edit_Image_v05.json). Always auto-discovered when possible.
NODE_LOAD_IMAGE = "278"
NODE_PROMPT = "289"  # PrimitiveStringMultiline "Input Text" → LLM user_prompt
NODE_SEED = "242"  # Seed (rgthree)
NODE_SAVE = "299"

_JOB_LOCK = threading.Lock()
_JOB_BUSY = False


class ComfyError(RuntimeError):
    """Raised on ComfyUI HTTP / validation / job failures."""


def _find_node_id(wf: Dict[str, Any], class_type: str) -> Optional[str]:
    for nid, node in wf.items():
        if isinstance(node, dict) and node.get("class_type") == class_type:
            return str(nid)
    return None


def _find_all_node_ids(wf: Dict[str, Any], class_type: str) -> List[str]:
    return [
        str(nid)
        for nid, node in wf.items()
        if isinstance(node, dict) and node.get("class_type") == class_type
    ]


def _find_final_image_source(wf: Dict[str, Any]) -> Optional[Tuple[str, int]]:
    """
    Prefer InpaintStitchImproved / SmartImageStitcher / SaveImage input,
    else PreviewImage input.
    Returns (node_id, output_slot).
    """
    for ct in (
        "InpaintStitchImproved",
        "SmartImageStitcher",
        "ImageStitch",
        "SaveImage",
    ):
        nid = _find_node_id(wf, ct)
        if nid and ct != "SaveImage":
            return (nid, 0)
        if nid and ct == "SaveImage":
            inp = (wf[nid].get("inputs") or {}).get("images")
            if isinstance(inp, list) and len(inp) >= 2:
                return (str(inp[0]), int(inp[1]))
    # PreviewImage
    for nid in _find_all_node_ids(wf, "PreviewImage"):
        inp = (wf[nid].get("inputs") or {}).get("images")
        if isinstance(inp, list) and len(inp) >= 2:
            return (str(inp[0]), int(inp[1]))
    return None


def _trace_to_class(
    wf: Dict[str, Any],
    nid: str,
    class_type: str,
    seen: Optional[Set[str]] = None,
) -> Optional[str]:
    """Walk input links backward until class_type is found."""
    if seen is None:
        seen = set()
    nid = str(nid)
    if nid in seen or nid not in wf:
        return None
    seen.add(nid)
    node = wf[nid]
    if node.get("class_type") == class_type:
        return nid
    for _k, v in (node.get("inputs") or {}).items():
        if isinstance(v, list) and len(v) >= 1 and isinstance(v[0], (str, int)):
            hit = _trace_to_class(wf, str(v[0]), class_type, seen)
            if hit:
                return hit
    return None


def _find_positive_clip_encode(wf: Dict[str, Any]) -> Optional[str]:
    """
    Find CLIPTextEncode used as positive conditioning.
    v02: often node 1; v03/v04: positive is node 25 (wired through ReferenceLatent).
    """
    for ct in ("InpaintModelConditioning", "CFGGuider", "BasicGuider", "DualCFGGuider"):
        for nid in _find_all_node_ids(wf, ct):
            pos = (wf[nid].get("inputs") or {}).get("positive")
            if isinstance(pos, list) and len(pos) >= 1:
                hit = _trace_to_class(wf, str(pos[0]), "CLIPTextEncode")
                if hit:
                    return hit
    best, best_len = None, -1
    for nid in _find_all_node_ids(wf, "CLIPTextEncode"):
        text = (wf[nid].get("inputs") or {}).get("text")
        if isinstance(text, str) and len(text) > best_len:
            best_len = len(text)
            best = nid
    if best is not None:
        return best
    ids = _find_all_node_ids(wf, "CLIPTextEncode")
    return ids[0] if ids else None


def _find_user_prompt_inject(wf: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """
    Where to put the artist prompt from Nuke.

    v04: LLM.user_prompt_input ← PrimitiveStringMultiline (value)
         CLIPTextEncode.text is a LINK to LLM output — do not overwrite with a string.
    v03: CLIPTextEncode.text is a plain string.
    Returns (node_id, input_key).
    """
    # 1) LLM / chat nodes with user_prompt_input
    for nid, node in wf.items():
        if not isinstance(node, dict):
            continue
        ct = (node.get("class_type") or "").lower()
        if "llm" not in ct and "chat" not in ct:
            continue
        up = (node.get("inputs") or {}).get("user_prompt_input")
        if isinstance(up, list) and len(up) >= 1:
            tid = str(up[0])
            if tid in wf:
                tin = wf[tid].get("inputs") or {}
                if "value" in tin:
                    return tid, "value"
                if "text" in tin and not isinstance(tin.get("text"), list):
                    return tid, "text"

    # 2) Positive CLIP with string text (not a link)
    clip = _find_positive_clip_encode(wf)
    if clip:
        text = (wf[clip].get("inputs") or {}).get("text")
        if isinstance(text, str) or text is None:
            return clip, "text"
        # text is a link — walk to PrimitiveStringMultiline if any
        if isinstance(text, list) and len(text) >= 1:
            # Prefer any PrimitiveStringMultiline that is NOT system_prompt_input
            sys_ids = set()
            for nid, node in wf.items():
                if not isinstance(node, dict):
                    continue
                sp = (node.get("inputs") or {}).get("system_prompt_input")
                if isinstance(sp, list) and len(sp) >= 1:
                    sys_ids.add(str(sp[0]))
            for pnid in _find_all_node_ids(wf, "PrimitiveStringMultiline"):
                if pnid not in sys_ids:
                    return pnid, "value"

    # 3) First PrimitiveStringMultiline that is not system
    sys_ids = set()
    for nid, node in wf.items():
        if not isinstance(node, dict):
            continue
        sp = (node.get("inputs") or {}).get("system_prompt_input")
        if isinstance(sp, list) and len(sp) >= 1:
            sys_ids.add(str(sp[0]))
    for pnid in _find_all_node_ids(wf, "PrimitiveStringMultiline"):
        if pnid not in sys_ids:
            return pnid, "value"

    return None, "text"


class ComfyClient:
    def __init__(
        self,
        server: str = "http://192.168.91.13:8188",
        workflow_path: Optional[str] = None,
        timeout_sec: float = 600.0,
        poll_interval_sec: float = 2.0,
        client_id: Optional[str] = None,
    ):
        self.server = server.rstrip("/")
        self.timeout_sec = float(timeout_sec)
        self.poll_interval_sec = float(poll_interval_sec)
        self.client_id = client_id or f"nuke-{socket.gethostname()}-{os.getpid()}"
        self.workflow_path = workflow_path or self._default_workflow_path()
        self._workflow_template: Optional[Dict[str, Any]] = None
        # Resolved per-workflow (None until load_workflow)
        self.id_load: Optional[str] = None
        self.id_prompt: Optional[str] = None
        self.id_prompt_key = "value"  # or "text"
        self.id_prompt_neg: Optional[str] = None
        self.id_seed: Optional[str] = None
        self.id_seed_key = "seed"
        self.id_save: Optional[str] = None
        self.id_crop: Optional[str] = None

    @staticmethod
    def _default_workflow_path() -> str:
        here = Path(__file__).resolve().parent
        repo = here.parent
        for name in (
            "Edit_Image_v05.json",
            "Edit_Image_v04.json",
            "Edit_Image_v03.json",
            "Edit_Image_v02.json",
            "Edit_Image_v01.json",
            "Edit_Image_API.json",
            "Edit_Image.json",
        ):
            candidate = repo / name
            if candidate.is_file():
                return str(candidate)
        return str(repo / "Edit_Image_v05.json")

    def _url(self, path: str, query: Optional[Dict[str, str]] = None) -> str:
        base = f"{self.server}{path}"
        if query:
            return base + "?" + urllib.parse.urlencode(query)
        return base

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        query: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> bytes:
        req = urllib.request.Request(
            self._url(path, query),
            data=data,
            headers=headers or {},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_sec) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise ComfyError(f"HTTP {e.code} {path}: {body[:2000]}") from e
        except urllib.error.URLError as e:
            raise ComfyError(f"Cannot reach ComfyUI at {self.server}: {e}") from e

    def _get_json(self, path: str, query: Optional[Dict[str, str]] = None) -> Any:
        raw = self._request("GET", path, query=query, timeout=30)
        return json.loads(raw.decode("utf-8"))

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Any:
        data = json.dumps(payload).encode("utf-8")
        raw = self._request(
            "POST",
            path,
            data=data,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        return json.loads(raw.decode("utf-8"))

    def ping(self) -> Dict[str, Any]:
        return self._get_json("/system_stats")

    def queue_status(self) -> Dict[str, Any]:
        return self._get_json("/queue")

    def load_workflow(self, path: Optional[str] = None) -> Dict[str, Any]:
        p = path or self.workflow_path
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "prompt" in data and isinstance(data["prompt"], dict):
            data = data["prompt"]
        if not isinstance(data, dict) or not data:
            raise ComfyError(f"Invalid API workflow: {p}")
        sample = next(iter(data.values()))
        if not isinstance(sample, dict) or "class_type" not in sample:
            raise ComfyError(f"Not API format (missing class_type): {p}")
        self._workflow_template = data
        self.workflow_path = p

        # Auto-discover inject points (edit + txt2img)
        # LoadImage may be missing for pure text-to-image workflows
        self.id_load = _find_node_id(data, "LoadImage")  # None for Image_generation

        # User prompt: LLM user field (v04) or CLIP text string (v03)
        pid, pkey = _find_user_prompt_inject(data)
        self.id_prompt = pid or NODE_PROMPT
        self.id_prompt_key = pkey or "value"

        # Negative CLIP (string only) — optional
        self.id_prompt_neg = None
        pos_clip = _find_positive_clip_encode(data)
        for cid in _find_all_node_ids(data, "CLIPTextEncode"):
            if cid != pos_clip:
                t = (data[cid].get("inputs") or {}).get("text")
                if isinstance(t, str):
                    self.id_prompt_neg = cid
                    break

        # Seed: Seed (rgthree) / PrimitiveInt link / RandomNoise / KSampler.seed link
        seed_rg = None
        for nid, node in data.items():
            if not isinstance(node, dict):
                continue
            ct = node.get("class_type") or ""
            if "seed" in ct.lower() and "seed" in (node.get("inputs") or {}):
                # e.g. "Seed (rgthree)"
                seed_rg = str(nid)
                break
        prims = _find_all_node_ids(data, "PrimitiveInt")
        rn = _find_node_id(data, "RandomNoise")
        ksamplers = [
            str(nid)
            for nid, node in data.items()
            if isinstance(node, dict)
            and node.get("class_type") in ("KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced")
        ]

        if seed_rg:
            self.id_seed = seed_rg
            self.id_seed_key = "seed"
        elif prims and rn:
            link = (data[rn].get("inputs") or {}).get("noise_seed")
            if isinstance(link, list) and str(link[0]) in prims:
                self.id_seed = str(link[0])
                self.id_seed_key = "value"
            elif prims:
                self.id_seed = prims[0]
                self.id_seed_key = "value"
            else:
                self.id_seed = rn
                self.id_seed_key = "noise_seed"
        elif rn:
            link = (data[rn].get("inputs") or {}).get("noise_seed")
            if isinstance(link, list):
                # linked to Seed node
                self.id_seed = str(link[0])
                linked = data.get(self.id_seed, {})
                lin = linked.get("inputs") or {}
                if "seed" in lin:
                    self.id_seed_key = "seed"
                elif "value" in lin:
                    self.id_seed_key = "value"
                else:
                    self.id_seed = rn
                    self.id_seed_key = "noise_seed"
            else:
                self.id_seed = rn
                self.id_seed_key = "noise_seed"
        elif ksamplers:
            link = (data[ksamplers[0]].get("inputs") or {}).get("seed")
            if isinstance(link, list):
                self.id_seed = str(link[0])
                lin = (data.get(self.id_seed, {}).get("inputs") or {})
                if "seed" in lin:
                    self.id_seed_key = "seed"
                elif "value" in lin:
                    self.id_seed_key = "value"
                else:
                    self.id_seed = ksamplers[0]
                    self.id_seed_key = "seed"
            else:
                self.id_seed = ksamplers[0]
                self.id_seed_key = "seed"
        elif prims:
            # Prefer a seed-like primitive if titled, else first
            self.id_seed = prims[0]
            self.id_seed_key = "value"
        else:
            self.id_seed = NODE_SEED
            self.id_seed_key = "seed"

        self.id_save = _find_node_id(data, "SaveImage")
        self.id_crop = (
            _find_node_id(data, "SmartImageCrop")
            or _find_node_id(data, "InpaintCropImproved")
        )
        return data

    def upload_image(self, image_path: str, overwrite: bool = True) -> str:
        """Upload local image. Prefer .png with alpha for mask workflows."""
        image_path = os.path.abspath(image_path)
        if not os.path.isfile(image_path):
            raise ComfyError(f"Image not found: {image_path}")

        host = socket.gethostname().replace(" ", "_")
        ext = Path(image_path).suffix.lower() or ".png"
        # Force png extension in remote name if local is png
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
            ext = ".png"
        # Stable-ish name but unique per host so multi-Nuke doesn't clash;
        # overwrite=true so Comfy replaces same basename when reusing local fixed file
        remote_name = f"nuke_{host}_input_rgba{ext}"

        with open(image_path, "rb") as f:
            file_bytes = f.read()

        mime = mimetypes.guess_type(image_path)[0] or "image/png"
        if ext == ".png":
            mime = "image/png"
        boundary = f"----ComfyNuke{uuid.uuid4().hex}"
        body = self._multipart(
            boundary,
            fields={
                "overwrite": "true" if overwrite else "false",
                "type": "input",
            },
            files={
                "image": (remote_name, file_bytes, mime),
            },
        )
        raw = self._request(
            "POST",
            "/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=120,
        )
        result = json.loads(raw.decode("utf-8"))
        name = result.get("name") or remote_name
        sub = result.get("subfolder") or ""
        if sub:
            return f"{sub}/{name}".replace("\\", "/")
        return name

    def build_prompt(
        self,
        image_name: str,
        prompt: str,
        seed: Optional[int] = None,
        filename_prefix: Optional[str] = None,
        workflow: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if workflow is None:
            if self._workflow_template is None:
                self.load_workflow()
            workflow = self._workflow_template
        assert workflow is not None
        wf = copy.deepcopy(workflow)

        def _set(node_id: str, key: str, value: Any, required: bool = True) -> None:
            if not node_id:
                if required:
                    raise ComfyError(f"No node id to set {key}={value!r}")
                return
            if node_id not in wf:
                if required:
                    raise ComfyError(
                        f"Workflow missing node {node_id} (setting {key}). "
                        f"Known nodes: {', '.join(sorted(wf.keys())[:20])}..."
                    )
                return
            wf[node_id].setdefault("inputs", {})[key] = value

        # Image input only for img-edit workflows (txt2img has no LoadImage)
        if image_name:
            if self.id_load and self.id_load in wf:
                _set(self.id_load, "image", image_name, required=True)
            # else: pure txt2img — ignore image_name

        # Prompt: PrimitiveStringMultiline.value (e.g. node 73) or CLIP text
        if not self.id_prompt or self.id_prompt not in wf:
            # Last chance: PrimitiveStringMultiline titled input / LLM user field
            pid, pkey = _find_user_prompt_inject(wf)
            self.id_prompt = pid
            self.id_prompt_key = pkey or "value"
        if not self.id_prompt or self.id_prompt not in wf:
            raise ComfyError(
                "No prompt node found (need PrimitiveStringMultiline Input Text, e.g. 73)"
            )
        key = self.id_prompt_key or "text"
        cur = (wf.get(self.id_prompt, {}).get("inputs") or {}).get(key)
        if isinstance(cur, list):
            raise ComfyError(
                f"Prompt node {self.id_prompt}.{key} is a link, not a string. "
                "Cannot inject artist prompt — check workflow wiring."
            )
        _set(self.id_prompt, key, prompt, required=True)

        # Reset LLM session state so prior Nuke/Comfy jobs do not bleed into
        # this prompt (avoids "overlapped" / mixed generations + quality drop).
        for nid, node in wf.items():
            if not isinstance(node, dict):
                continue
            ct = (node.get("class_type") or "").lower()
            if "llm" not in ct:
                continue
            inp = node.setdefault("inputs", {})
            if "historical_record" in inp and not isinstance(
                inp.get("historical_record"), list
            ):
                inp["historical_record"] = ""
            if "is_memory" in inp and not isinstance(inp.get("is_memory"), list):
                inp["is_memory"] = "disable"
            if "conversation_rounds" in inp and not isinstance(
                inp.get("conversation_rounds"), list
            ):
                # Keep short history window for this run only
                try:
                    if int(inp.get("conversation_rounds") or 0) > 1:
                        inp["conversation_rounds"] = 1
                except Exception:
                    inp["conversation_rounds"] = 1
            # Clear any plain-string user/system fields (linked inputs untouched)
            for plain_key in ("user_prompt", "system_prompt"):
                if plain_key in inp and isinstance(inp.get(plain_key), str):
                    inp[plain_key] = ""

        if seed is None:
            seed = int(uuid.uuid4().int % (2**63))
        seed_val = int(seed) % (2**63)
        if self.id_seed and self.id_seed in wf:
            _set(self.id_seed, self.id_seed_key, seed_val, required=False)

        host = socket.gethostname().replace(" ", "_")
        if not filename_prefix:
            filename_prefix = (
                f"nuke/{host}/{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
            )
        prefix = filename_prefix.replace("\\", "/")
        # Stored so download can prefer this job's files only
        self._last_filename_prefix = prefix

        # Ensure a downloadable saver exists in history (/view):
        # image workflows → SaveImage; video (i2v) → SaveVideo / VHS / CreateVideo
        save_id = _find_node_id(wf, "SaveImage")
        video_saver_ids = [
            str(nid)
            for nid, node in wf.items()
            if isinstance(node, dict)
            and (node.get("class_type") or "")
            in ("SaveVideo", "VHS_VideoCombine", "CreateVideo")
        ]
        if save_id:
            self.id_save = save_id
            _set(save_id, "filename_prefix", prefix)
        elif video_saver_ids:
            # MiniMax / i2v: SaveVideo already in graph — do not inject SaveImage
            self.id_save = video_saver_ids[0]
        else:
            src = _find_final_image_source(wf)
            if not src:
                raise ComfyError(
                    "Workflow has no SaveImage / SaveVideo / stitch / preview source"
                )
            src_nid, src_slot = src
            save_id = "9001"
            while save_id in wf:
                save_id = str(int(save_id) + 1)
            wf[save_id] = {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": prefix,
                    "images": [src_nid, src_slot],
                },
                "_meta": {"title": "Save Image (Nuke inject)"},
            }
            self.id_save = save_id

        # EXR / Video savers — unique filename per job
        for nid, node in wf.items():
            if not isinstance(node, dict):
                continue
            ct = node.get("class_type") or ""
            ct_u = ct.upper()
            if not (
                "EXR" in ct_u
                or "ACESIO" in ct_u
                or ct in ("SaveVideo", "VHS_VideoCombine", "CreateVideo")
            ):
                continue
            inp = node.setdefault("inputs", {})
            base = Path(prefix).name
            if "filename" in inp and not isinstance(inp.get("filename"), list):
                inp["filename"] = f"{base}_%04d"
            if "filename_prefix" in inp and not isinstance(
                inp.get("filename_prefix"), list
            ):
                # SaveVideo uses filename_prefix under video/
                if ct == "SaveVideo":
                    inp["filename_prefix"] = f"video/{base}"
                else:
                    inp["filename_prefix"] = prefix

        # Optional EmptyLatentImage size (txt2img) via kwargs on instance.
        # Snap to multiples of 8 (keeps 1920x1080 exact; avoids odd-size artifacts).
        w = getattr(self, "_inject_width", None)
        h = getattr(self, "_inject_height", None)
        empty = _find_node_id(wf, "EmptyLatentImage") or _find_node_id(
            wf, "EmptySD3LatentImage"
        )
        if empty and empty in wf:
            def _snap8(n: int) -> int:
                n = max(64, int(n))
                return max(64, int(round(n / 8.0)) * 8)

            if w is not None:
                wf[empty].setdefault("inputs", {})["width"] = _snap8(w)
            if h is not None:
                wf[empty].setdefault("inputs", {})["height"] = _snap8(h)
            # Never leave batch > 1 from a hand-edited template (ghost/overlap frames)
            binp = wf[empty].setdefault("inputs", {})
            if "batch_size" in binp and not isinstance(binp.get("batch_size"), list):
                try:
                    if int(binp.get("batch_size") or 1) != 1:
                        binp["batch_size"] = 1
                except Exception:
                    binp["batch_size"] = 1

        # Optional v01 SmartImageCrop tweaks only
        if self.id_crop and self.id_crop in wf:
            ct = wf[self.id_crop].get("class_type")
            if ct == "SmartImageCrop":
                crop_in = wf[self.id_crop].setdefault("inputs", {})
                crop_in["resolution_mode"] = "Automatic"
                if int(crop_in.get("mask_grow_pixels") or 0) < 16:
                    crop_in["mask_grow_pixels"] = 32
                crop_in["no_mask_mode"] = "Resize Full Image"

        return wf

    def run_text_to_image(
        self,
        prompt: str,
        seed: Optional[int] = None,
        output_dir: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        progress: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Text-to-image: no upload, inject prompt only, queue + wait + download."""
        global _JOB_BUSY

        if not _JOB_LOCK.acquire(blocking=False):
            raise ComfyError(
                "Another Comfy job is already running in this session. "
                "Wait for it to finish (one request at a time)."
            )
        _JOB_BUSY = True
        try:
            def log(msg: str) -> None:
                if progress:
                    progress(msg)

            log("ping")
            self.ping()
            log("load_workflow")
            self.load_workflow()
            if width is not None:
                self._inject_width = width
            if height is not None:
                self._inject_height = height
            log(
                f"nodes prompt={self.id_prompt}.{self.id_prompt_key} "
                f"seed={self.id_seed}.{self.id_seed_key}"
            )

            host = socket.gethostname().replace(" ", "_")
            prefix = (
                f"nuke/{host}/t2i_{time.strftime('%Y%m%d_%H%M%S')}_"
                f"{uuid.uuid4().hex[:8]}"
            )
            log("build_prompt")
            wf = self.build_prompt(
                image_name=None,
                prompt=prompt,
                seed=seed,
                filename_prefix=prefix,
            )
            # clear inject size after build
            self._inject_width = None
            self._inject_height = None

            log("queue")
            prompt_id = self.queue_prompt(wf)
            log(f"prompt_id={prompt_id}")

            entry = self.wait_for_result(prompt_id, progress=progress)
            files = self.outputs_from_history(entry)
            if not files:
                raise ComfyError(
                    f"Job finished but no output images. status={entry.get('status')}"
                )

            out_dir = output_dir or str(Path(__file__).resolve().parent / "out")
            os.makedirs(out_dir, exist_ok=True)

            primary = self.prefer_output_file(files)
            stamp = Path(prefix).name
            src_ext = Path(primary.get("filename") or "out.png").suffix.lower() or ".png"
            dest = os.path.join(out_dir, f"t2i_{stamp}{src_ext}")
            if os.path.isfile(dest):
                dest = os.path.join(
                    out_dir, f"t2i_{stamp}_{uuid.uuid4().hex[:6]}{src_ext}"
                )
            log("download (%s)" % (primary.get("filename") or "?"))
            dest = self.download_image(primary, dest)
            try:
                import shutil

                shutil.copy2(
                    dest, os.path.join(out_dir, "last_t2i_result" + src_ext)
                )
            except Exception:
                pass

            return {
                "prompt_id": prompt_id,
                "output_path": dest,
                "seed": seed,
                "prefix": prefix,
                "all_files": files,
            }
        finally:
            self._inject_width = None
            self._inject_height = None
            _JOB_BUSY = False
            _JOB_LOCK.release()

    def queue_prompt(self, workflow: Dict[str, Any]) -> str:
        payload = {
            "prompt": workflow,
            "client_id": self.client_id,
        }
        result = self._post_json("/prompt", payload)
        if "error" in result:
            raise ComfyError(f"Queue error: {json.dumps(result)[:2000]}")
        if "node_errors" in result and result["node_errors"]:
            raise ComfyError(f"Node errors: {json.dumps(result['node_errors'])[:2000]}")
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise ComfyError(f"No prompt_id in response: {result}")
        return prompt_id

    def wait_for_result(
        self,
        prompt_id: str,
        progress: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        t0 = time.time()
        while True:
            elapsed = time.time() - t0
            if elapsed > self.timeout_sec:
                raise ComfyError(
                    f"Timeout after {self.timeout_sec:.0f}s waiting for {prompt_id}"
                )

            hist = self._get_json(f"/history/{prompt_id}")
            if prompt_id in hist:
                entry = hist[prompt_id]
                status = entry.get("status") or {}
                if status.get("status_str") == "error" or status.get("completed") is False:
                    msgs = status.get("messages") or entry.get("messages") or []
                    raise ComfyError(f"Job failed: {msgs!r}"[:2000])
                if entry.get("outputs"):
                    if progress:
                        progress("done")
                    return entry
                if status.get("completed") is True:
                    if progress:
                        progress("done")
                    return entry

            if progress:
                try:
                    q = self.queue_status()
                    running = q.get("queue_running") or []
                    pending = q.get("queue_pending") or []
                    pos = None
                    for i, item in enumerate(pending):
                        if len(item) > 1 and item[1] == prompt_id:
                            pos = i + 1
                            break
                    if any(len(item) > 1 and item[1] == prompt_id for item in running):
                        progress(f"running ({elapsed:.0f}s)")
                    elif pos is not None:
                        progress(f"queued #{pos} ({elapsed:.0f}s)")
                    else:
                        progress(f"waiting ({elapsed:.0f}s)")
                except ComfyError:
                    progress(f"waiting ({elapsed:.0f}s)")

            time.sleep(self.poll_interval_sec)

    def outputs_from_history(self, entry: Dict[str, Any]) -> list:
        """Collect all file outputs (PNG/EXR/etc.) from history node results."""
        files = []
        outputs = entry.get("outputs") or {}
        for _node_id, node_out in outputs.items():
            if not isinstance(node_out, dict):
                continue
            for key, val in node_out.items():
                if not isinstance(val, list):
                    continue
                for item in val:
                    if isinstance(item, dict) and item.get("filename"):
                        files.append(item)
        return files

    @staticmethod
    def prefer_output_file(
        files: list,
        prefer_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Prefer this job's output (token match), then video (i2v), EXR, PNG.
        Prefer type=output over temp/preview to avoid low-quality previews.
        """
        if not files:
            raise ComfyError("No output files in history")

        token = (prefer_token or "").lower().strip()

        def score(f: Dict[str, Any]) -> tuple:
            name = (f.get("filename") or "").lower()
            sub = (f.get("subfolder") or "").replace("\\", "/").lower()
            path = f"{sub}/{name}" if sub else name
            typ = (f.get("type") or "").lower()
            is_vid = 0 if name.endswith(
                (".mp4", ".webm", ".mov", ".mkv", ".avi", ".gif")
            ) else 1
            is_exr = 0 if name.endswith(".exr") else 1
            # output folder first, never prefer temp/preview for final quality
            if typ == "output":
                is_out = 0
            elif typ in ("temp", "input"):
                is_out = 2
            else:
                is_out = 1
            is_preview = 0 if ("preview" in name or "preview" in sub) else 1
            # 0 = token matches this job's unique stamp/prefix
            if token and (token in path or token in name or token in sub):
                match = 0
            elif token:
                match = 1
            else:
                match = 0
            return (match, is_out, is_preview, is_vid, is_exr, name)

        ranked = sorted(files, key=score)
        best = ranked[0]
        # If a token was given and best still does not match, still return best
        # but prefer any matching file if present
        if token:
            for f in ranked:
                name = (f.get("filename") or "").lower()
                sub = (f.get("subfolder") or "").replace("\\", "/").lower()
                path = f"{sub}/{name}" if sub else name
                if token in path or token in name or token in sub:
                    return f
        return best

    def download_image(self, file_info: Dict[str, Any], dest_path: str) -> str:
        filename = file_info.get("filename")
        if not filename:
            raise ComfyError(f"Bad file info: {file_info}")
        query = {
            "filename": filename,
            "subfolder": file_info.get("subfolder") or "",
            "type": file_info.get("type") or "output",
        }
        raw = self._request("GET", "/view", query=query, timeout=300)
        dest_path = os.path.abspath(dest_path)
        # Keep real extension (.exr / .png)
        src_ext = Path(filename).suffix.lower()
        dest_ext = Path(dest_path).suffix.lower()
        if src_ext and dest_ext != src_ext:
            dest_path = str(Path(dest_path).with_suffix(src_ext))
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(raw)
        return dest_path

    def run_edit(
        self,
        image_path: str,
        prompt: str,
        seed: Optional[int] = None,
        output_dir: Optional[str] = None,
        progress: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        global _JOB_BUSY

        if not _JOB_LOCK.acquire(blocking=False):
            raise ComfyError(
                "Another Comfy job is already running in this session. "
                "Wait for it to finish (one request at a time)."
            )
        _JOB_BUSY = True
        try:
            def log(msg: str) -> None:
                if progress:
                    progress(msg)

            log("ping")
            self.ping()

            log("load_workflow")
            self.load_workflow()
            log(
                f"nodes load={self.id_load} prompt={self.id_prompt}."
                f"{self.id_prompt_key} crop={self.id_crop} seed={self.id_seed}."
                f"{self.id_seed_key}"
            )

            # Guard: must upload PNG for alpha mask
            ext = Path(image_path).suffix.lower()
            if ext not in (".png", ".webp"):
                log(f"WARNING: input is {ext} — prefer PNG RGBA for mask")

            log("upload")
            image_name = self.upload_image(image_path)

            host = socket.gethostname().replace(" ", "_")
            prefix = (
                f"nuke/{host}/{time.strftime('%Y%m%d_%H%M%S')}_"
                f"{uuid.uuid4().hex[:8]}"
            )
            log("build_prompt")
            wf = self.build_prompt(
                image_name=image_name,
                prompt=prompt,
                seed=seed,
                filename_prefix=prefix,
            )

            log("queue")
            prompt_id = self.queue_prompt(wf)
            log(f"prompt_id={prompt_id}")

            entry = self.wait_for_result(prompt_id, progress=progress)
            files = self.outputs_from_history(entry)
            if not files:
                raise ComfyError(
                    f"Job finished but no output images. status={entry.get('status')}"
                )

            out_dir = output_dir or str(Path(__file__).resolve().parent / "out")
            os.makedirs(out_dir, exist_ok=True)

            primary = self.prefer_output_file(files)
            # Unique file per generation — never overwrite previous results
            stamp = Path(prefix).name  # e.g. 20260728_175812_8020925e
            src_ext = Path(primary.get("filename") or "out.png").suffix.lower() or ".png"
            unique_name = f"comfy_{stamp}{src_ext}"
            dest = os.path.join(out_dir, unique_name)
            if os.path.isfile(dest):
                dest = os.path.join(
                    out_dir, f"comfy_{stamp}_{uuid.uuid4().hex[:6]}{src_ext}"
                )
            log("download (%s)" % (primary.get("filename") or "?"))
            dest = self.download_image(primary, dest)

            # Optional pointer for convenience (does not replace unique files)
            try:
                import shutil

                latest_ext = Path(dest).suffix.lower() or ".png"
                latest = os.path.join(out_dir, "last_comfy_result" + latest_ext)
                shutil.copy2(dest, latest)
            except Exception:
                pass

            return {
                "prompt_id": prompt_id,
                "output_path": dest,  # unique path — safe for Nuke Read
                "output_path_dated": dest,
                "remote_image": image_name,
                "seed": seed,
                "prefix": prefix,
                "all_files": files,
            }
        finally:
            _JOB_BUSY = False
            _JOB_LOCK.release()

    @staticmethod
    def is_busy() -> bool:
        return _JOB_BUSY

    @staticmethod
    def _multipart(
        boundary: str,
        fields: Dict[str, str],
        files: Dict[str, Tuple[str, bytes, str]],
    ) -> bytes:
        lines: list[bytes] = []
        for name, value in fields.items():
            lines.append(f"--{boundary}\r\n".encode())
            lines.append(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            )
            lines.append(f"{value}\r\n".encode())
        for name, (filename, content, mime) in files.items():
            lines.append(f"--{boundary}\r\n".encode())
            lines.append(
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n'
                    f"Content-Type: {mime}\r\n\r\n"
                ).encode()
            )
            lines.append(content)
            lines.append(b"\r\n")
        lines.append(f"--{boundary}--\r\n".encode())
        return b"".join(lines)


def _cli(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="ComfyUI Edit_Image client (sequential)")
    parser.add_argument("--server", default="http://192.168.91.13:8188")
    parser.add_argument("--workflow", default=None)
    parser.add_argument("--image", default=None)
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--ping-only", action="store_true")
    args = parser.parse_args(argv)

    client = ComfyClient(
        server=args.server,
        workflow_path=args.workflow,
        timeout_sec=args.timeout,
    )

    def progress(msg: str) -> None:
        print(f"[comfy] {msg}", flush=True)

    if args.ping_only:
        stats = client.ping()
        print(json.dumps(stats, indent=2)[:2000])
        return 0

    if not args.image or not args.prompt:
        parser.error("--image and --prompt are required unless --ping-only")

    result = client.run_edit(
        image_path=args.image,
        prompt=args.prompt,
        seed=args.seed,
        output_dir=args.output_dir,
        progress=progress,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "all_files"}, indent=2))
    print(f"OK -> {result['output_path']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_cli())
    except ComfyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        sys.exit(130)

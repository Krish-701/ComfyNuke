"""
Nuke panel for ComfyUI Edit_Image (PNG RGBA + crop/inpaint/stitch).

Always uploads PNG with alpha (never raw JPG).
Per-session temp files under %TEMP%/comfy_nuke/<host>_<pid>/ (multi-user safe).

Workflow default: Edit_Image_v05.json
  LoadImage → mask → InpaintCrop → LLM (node 289 user text) → Qwen edit → Stitch → Save

LOAD (artists — multi-user hub):
  exec(open(r"//YOUR_SERVER/ComfyNuke/nuke/launch.py", encoding="utf-8").read())
or local:
  exec(open(r"D:/AI-Dev/Krish-ComfyNuke/nuke/launch.py", encoding="utf-8").read())
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import uuid

# ---------------------------------------------------------------------------
# Paths + multi-user studio config
# ---------------------------------------------------------------------------
def _this_file():
    try:
        return os.path.abspath(__file__)
    except NameError:
        return r"D:/AI-Dev/Krish-ComfyNuke/nuke/ComfyEdit.py"


def _load_studio_config(repo_root):
    """
    Resolve ComfyUI URL and options for multi-user studio.

    Order (first hit wins for each key):
      1) env COMFYNUKE_SERVER / COMFYNUKE_OUT
      2) studio_config.json in repo root (shared from Ubuntu)
      3) %USERPROFILE%/.comfynuke/config.json or ~/.comfynuke/config.json
      4) built-in defaults
    """
    cfg = {
        "server": "http://192.168.91.13:8188",
        "output_dir": "",
        "studio_name": "ComfyNuke",
    }
    candidates = []
    env_root = (os.environ.get("COMFYNUKE_ROOT") or "").strip()
    if env_root:
        candidates.append(os.path.join(env_root, "studio_config.json"))
    candidates.append(os.path.join(repo_root, "studio_config.json"))
    home = os.path.expanduser("~")
    candidates.append(os.path.join(home, ".comfynuke", "config.json"))
    # Windows user profile
    up = os.environ.get("USERPROFILE") or ""
    if up:
        candidates.append(os.path.join(up, ".comfynuke", "config.json"))

    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: data[k] for k in data if data[k] not in (None, "")})
                cfg["_config_path"] = path
                break
        except Exception:
            continue

    env_server = (os.environ.get("COMFYNUKE_SERVER") or "").strip()
    if env_server:
        cfg["server"] = env_server
    env_out = (os.environ.get("COMFYNUKE_OUT") or "").strip()
    if env_out:
        cfg["output_dir"] = env_out
    return cfg


_THIS_DIR = os.path.dirname(_this_file())
# Allow bootstrap to pin repo root (network share / Ubuntu mount)
_env_root = (os.environ.get("COMFYNUKE_ROOT") or "").strip()
if _env_root and os.path.isdir(_env_root):
    REPO_ROOT = os.path.normpath(_env_root)
    _THIS_DIR = os.path.join(REPO_ROOT, "nuke")
else:
    REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, ".."))

CLIENT_DIR = os.path.join(REPO_ROOT, "client")
_STUDIO = _load_studio_config(REPO_ROOT)

DEFAULT_WORKFLOW = os.path.join(REPO_ROOT, "Edit_Image_v05.json").replace("\\", "/")
IMAGE_GEN_WORKFLOW = os.path.join(REPO_ROOT, "Image_generation_v01.json").replace(
    "\\", "/"
)
I2V_WORKFLOW = os.path.join(REPO_ROOT, "video_minimax_h3_i2v.json").replace("\\", "/")
DEFAULT_SERVER = str(_STUDIO.get("server") or "http://192.168.91.13:8188").rstrip("/")

# Downloads: prefer local per-user folder (never write into shared server tree)
_host = socket.gethostname().replace(" ", "_")
_pid = str(os.getpid())
if _STUDIO.get("output_dir"):
    DEFAULT_OUT = str(_STUDIO["output_dir"]).replace("\\", "/")
else:
    DEFAULT_OUT = os.path.join(
        os.path.expanduser("~"), "ComfyNuke_out", _host
    ).replace("\\", "/")

# Per Nuke process temp (multi-user / multi-Nuke safe)
TEMP_DIR = os.path.join(tempfile.gettempdir(), "comfy_nuke", "%s_%s" % (_host, _pid))
TEMP_PLATE_SRGB = os.path.join(TEMP_DIR, "plate_srgb.png")     # RGB display-referred
TEMP_INPUT_RGBA = os.path.join(TEMP_DIR, "input_rgba.png")     # RGB + mask alpha → Comfy
TEMP_ALPHA_PREVIEW = os.path.join(TEMP_DIR, "alpha_preview.png")  # grayscale A for QC
TEMP_ROTO_WRITE = os.path.join(TEMP_DIR, "roto_write_rgba.png")   # raw Nuke Write
TEMP_MASK_LUMA = os.path.join(TEMP_DIR, "mask_luma.png")          # Roto alpha as gray RGB
TEMP_I2V_FRAME = os.path.join(TEMP_DIR, "i2v_frame.png")          # current frame for i2v
# Reference style (user's known-good alpha) — optional local QC only
REF_ALPHA_EXAMPLE = r"D:\bear-alpha.png"

STUDIO_NAME = str(_STUDIO.get("studio_name") or "ComfyNuke")
STUDIO_CONFIG_PATH = _STUDIO.get("_config_path") or ""

# True while export+Comfy job is active (one at a time PER Nuke session)
_BG_JOB_ACTIVE = False
# Active QTimer poll state (main-thread only — no Python threads)
_POLL_STATE = None  # type: ignore

if CLIENT_DIR not in sys.path:
    sys.path.insert(0, CLIENT_DIR)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    import nuke  # type: ignore
except ImportError:
    nuke = None


def _qt_timer():
    for mod in ("PySide2.QtCore", "PySide6.QtCore"):
        try:
            return __import__(mod, fromlist=["QTimer"]).QTimer
        except Exception:
            continue
    return None


def _schedule_ms(delay_ms, fn):
    QTimer = _qt_timer()
    if QTimer is not None:
        QTimer.singleShot(int(max(0, delay_ms)), fn)
        return True
    if nuke is not None and hasattr(nuke, "executeDeferred"):
        nuke.executeDeferred(fn)
        return True
    fn()
    return False


def _log(msg):
    """Log only — no popups. Main-thread only (never call from Python threads)."""
    if nuke is not None:
        try:
            nuke.tprint("[ComfyEdit] %s" % msg)
            return
        except Exception:
            pass
    print("[ComfyEdit] %s" % msg)


def _ensure_sys_module():
    mod = sys.modules.get(__name__)
    if mod is not None and "ComfyEdit" not in sys.modules:
        sys.modules["ComfyEdit"] = mod


def _get_client():
    from comfy_client import ComfyClient, ComfyError

    return ComfyClient, ComfyError


def _qt_gui():
    for base in ("PySide2", "PySide6"):
        try:
            QtGui = __import__(base + ".QtGui", fromlist=["QtGui"])
            QtCore = __import__(base + ".QtCore", fromlist=["QtCore"])
            return QtGui, QtCore
        except Exception:
            continue
    return None, None


def _ensure_temp_dir():
    if not os.path.isdir(TEMP_DIR):
        os.makedirs(TEMP_DIR)


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------
def _walk_upstream(node, max_depth=40):
    seen = set()
    stack = [(node, 0)]
    while stack:
        n, depth = stack.pop()
        if n is None or n in seen or depth > max_depth:
            continue
        seen.add(n)
        yield n
        for i in range(n.inputs() - 1, -1, -1):
            try:
                up = n.input(i)
            except Exception:
                up = None
            if up is not None:
                stack.append((up, depth + 1))


def find_upstream_read(node):
    for n in _walk_upstream(node):
        try:
            if n.Class() == "Read":
                return n
        except Exception:
            continue
    return None


def evaluate_read_path(read_node):
    if read_node is None:
        return None
    try:
        raw = read_node["file"].getEvaluatedValue()
    except Exception:
        try:
            raw = read_node["file"].evaluate()
        except Exception:
            raw = read_node["file"].value()
    if not raw:
        return None
    path = os.path.normpath(str(raw).replace("/", os.sep))
    frame = int(nuke.frame()) if nuke else 1
    if "%" in path:
        try:
            path = path % frame
        except Exception:
            pass
    if "#" in path:
        import re

        def repl(m):
            return str(frame).zfill(len(m.group(0)))

        path = re.sub(r"#+", repl, path)
    if os.path.isfile(path):
        return path
    return None


def _read_colorspace(read_node):
    if read_node is None:
        return ""
    for key in ("colorspace", "ocio_colorspace", "input_colorspace"):
        if key in read_node.knobs():
            try:
                return str(read_node[key].value())
            except Exception:
                pass
    return ""


def _is_linearish_colorspace(cs):
    s = (cs or "").lower()
    if not s:
        return False
    keys = (
        "linear",
        "raw",
        "aces",
        "acescg",
        "scene-linear",
        "scene linear",
        "utility - linear",
        "role_scene_linear",
        "nuke working space",
    )
    return any(k in s for k in keys)


def _ext_needs_nuke_convert(path):
    ext = os.path.splitext(path)[1].lower()
    return ext in (
        ".exr",
        ".sxr",
        ".dpx",
        ".cin",
        ".hdr",
        ".tif",
        ".tiff",
        ".iff",
        ".rla",
        ".sgi",
    )


def _qimage_can_load(path):
    QtGui, _ = _qt_gui()
    if QtGui is None:
        return False
    img = QtGui.QImage(path)
    return not img.isNull()


def _node_format_size(node):
    try:
        fmt = node.format()
        return int(fmt.width()), int(fmt.height())
    except Exception:
        pass
    try:
        fmt = nuke.root().format()
        return int(fmt.width()), int(fmt.height())
    except Exception:
        return None, None


def _box_to_tl(bb, width, height):
    """Nuke Box (bottom-left origin) → top-left (x0,y0,x1,y1)."""
    if bb is None:
        return None
    try:
        x = float(bb.x())
        y = float(bb.y())
        r = float(bb.r())
        t = float(bb.t())
    except Exception:
        try:
            x, y, r, t = [float(v) for v in bb]
        except Exception:
            return None
    if (r - x) < 2 or (t - y) < 2:
        return None
    x0 = int(max(0, min(width, round(x))))
    x1 = int(max(0, min(width, round(r))))
    y0 = int(max(0, min(height, round(height - t))))
    y1 = int(max(0, min(height, round(height - y))))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _is_near_full_frame(box, width, height, frac=0.97):
    if box is None:
        return True
    x0, y0, x1, y1 = box
    return (x1 - x0) >= width * frac and (y1 - y0) >= height * frac


def _roto_curves_points_bbox(roto_node, frame, width, height):
    """
    Bbox from Roto curve control points.
    node.bbox() on Roto is often the FULL format (5000x2727) — ignore that.
    """
    try:
        curves = roto_node["curves"]
    except Exception:
        return None

    xs, ys = [], []

    def _add(x, y):
        try:
            xs.append(float(x))
            ys.append(float(y))
        except Exception:
            pass

    def _point_xy(pt):
        try:
            if hasattr(pt, "getPosition"):
                pos = pt.getPosition(frame)
                return float(pos.x), float(pos.y)
        except Exception:
            pass
        try:
            if hasattr(pt, "center"):
                c = pt.center
                if callable(c):
                    c = c()
                # center may be animated
                try:
                    return float(c.x), float(c.y)
                except Exception:
                    if hasattr(c, "getValue"):
                        v = c.getValue(frame) if hasattr(c, "getValue") else c
                        return float(v[0]), float(v[1])
        except Exception:
            pass
        try:
            if hasattr(pt, "x") and hasattr(pt, "y"):
                return float(pt.x), float(pt.y)
        except Exception:
            pass
        try:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                return float(pt[0]), float(pt[1])
        except Exception:
            pass
        return None

    def _walk(el, depth=0):
        if el is None or depth > 50:
            return
        # Collect points on this element
        try:
            if hasattr(el, "getPointCount"):
                for i in range(int(el.getPointCount())):
                    pt = None
                    for getter in ("getPoint", "getControlPoint", "controlPoint"):
                        if hasattr(el, getter):
                            try:
                                pt = getattr(el, getter)(i)
                                break
                            except Exception:
                                pass
                    if pt is not None:
                        xy = _point_xy(pt)
                        if xy:
                            _add(xy[0], xy[1])
        except Exception:
            pass

        # Children
        kids = []
        try:
            if hasattr(el, "getNumOfChildren"):
                for i in range(int(el.getNumOfChildren())):
                    try:
                        kids.append(el[i])
                    except Exception:
                        try:
                            kids.append(el.getChild(i))
                        except Exception:
                            pass
            elif hasattr(el, "__iter__") and not isinstance(el, (str, bytes)):
                try:
                    kids = list(el)
                except Exception:
                    kids = []
        except Exception:
            kids = []
        for k in kids:
            _walk(k, depth + 1)

    try:
        root = curves.rootLayer
        _walk(root, 0)
    except Exception:
        try:
            _walk(curves.toElement("Root"), 0)
        except Exception:
            pass

    # Script parse fallback
    if len(xs) < 2:
        try:
            import re

            script = curves.toScript()
            for m in re.finditer(
                r"(?:^|[\s\{])x\s+(-?\d+\.?\d*)\s+y\s+(-?\d+\.?\d*)",
                script,
                flags=re.MULTILINE,
            ):
                _add(m.group(1), m.group(2))
        except Exception:
            pass

    if len(xs) < 2:
        return None

    x0 = int(max(0, min(width, round(min(xs)))))
    x1 = int(max(0, min(width, round(max(xs)))))
    # Nuke y-up → top-left
    y0 = int(max(0, min(height, round(height - max(ys)))))
    y1 = int(max(0, min(height, round(height - min(ys)))))
    if x1 <= x0 + 2 or y1 <= y0 + 2:
        return None
    box = (x0, y0, x1, y1)
    if _is_near_full_frame(box, width, height):
        return None
    return box


def _sample_alpha_bbox(node, frame, width, height, step=32, thr=0.5):
    """
    Non-zero alpha bbox via node.sample (origin bottom-left).
    Uses thr=0.5 so EXR full-frame alpha~1 noise does not dominate —
    Roto shape is typically near 1.0, outside should be ~0.
    Returns None if mask covers ~full frame (unusable for crop).
    """
    try:
        nuke.frame(int(frame))
    except Exception:
        pass

    xmin, ymin = width, height
    xmax, ymax = -1, -1
    found = 0
    total = 0
    for jy in range(0, height, step):
        for jx in range(0, width, step):
            total += 1
            a = 0.0
            try:
                a = float(node.sample("alpha", jx + 0.5, jy + 0.5))
            except Exception:
                try:
                    a = float(node.sample("rgba.alpha", jx + 0.5, jy + 0.5))
                except Exception:
                    a = 0.0
            if a > thr:
                found += 1
                if jx < xmin:
                    xmin = jx
                if jx > xmax:
                    xmax = jx
                if jy < ymin:
                    ymin = jy
                if jy > ymax:
                    ymax = jy
    if found < 3:
        return None
    # If almost every sample is "mask", EXR/plate alpha is full-frame — reject
    if total > 0 and (found / float(total)) > 0.90:
        _log(
            "alpha sample covers %.0f%% of frame — full-frame alpha (EXR?), ignored"
            % (100.0 * found / total)
        )
        return None
    xmin = max(0, xmin - step)
    ymin = max(0, ymin - step)
    xmax = min(width - 1, xmax + step)
    ymax = min(height - 1, ymax + step)
    x0, x1 = int(xmin), int(xmax + 1)
    y0 = int(height - (ymax + 1))
    y1 = int(height - ymin)
    y0 = max(0, min(height, y0))
    y1 = max(0, min(height, y1))
    if x1 <= x0 or y1 <= y0:
        return None
    box = (x0, y0, x1, y1)
    if _is_near_full_frame(box, width, height, frac=0.95):
        _log("alpha sample bbox full-frame %s — ignored" % (box,))
        return None
    return box


def _roto_bbox_image_space(node, frame, width, height):
    """
    Mask bbox in top-left image coords.
    Never return full-frame (EXR alpha=1 everywhere looks like full mask).
    """
    # 1) Curve points (best for Bezier shapes)
    try:
        cls = node.Class()
    except Exception:
        cls = ""
    if cls in ("Roto", "RotoPaint"):
        try:
            box = _roto_curves_points_bbox(node, frame, width, height)
            if box is not None and not _is_near_full_frame(box, width, height):
                _log("mask bbox from curves: %s" % (box,))
                return box
        except Exception as e:
            _log("curves bbox failed: %s" % e)

    # 2) Sample alpha (reject full-frame)
    try:
        step = max(16, min(width, height) // 100)
        box = _sample_alpha_bbox(node, frame, width, height, step=step, thr=0.5)
        if box is not None:
            _log("mask bbox from alpha sample: %s" % (box,))
            return box
    except Exception as e:
        _log("alpha sample failed: %s" % e)

    # 3) node.bbox — only if NOT full-frame
    try:
        try:
            bb = node.bbox(frame)
        except TypeError:
            bb = node.bbox()
        box = _box_to_tl(bb, width, height)
        if box is not None and not _is_near_full_frame(box, width, height):
            _log("mask bbox from node.bbox: %s" % (box,))
            return box
        _log("node.bbox full-frame or empty — ignored (normal for Roto/EXR)")
    except Exception:
        pass

    return None


def _expand_bbox(box, pad, width, height):
    x0, y0, x1, y1 = box
    return (
        max(0, x0 - pad),
        max(0, y0 - pad),
        min(width, x1 + pad),
        min(height, y1 + pad),
    )


def _find_roto_node(node):
    for n in _walk_upstream(node):
        try:
            if n.Class() in ("Roto", "RotoPaint"):
                return n
        except Exception:
            continue
    return None


def _set_write_colorspace_srgb(write_node):
    """Best-effort: encode PNG as display sRGB for Comfy."""
    candidates = (
        "sRGB",
        "Output - sRGB",
        "Output - sRGB - Texture",
        "Utility - sRGB - Texture",
        "rec709",
        "rec709 (scene)",
        "Gamma2.2",
        "gamma2.2",
    )
    for key in ("colorspace", "ocio_colorspace", "float_colorspace"):
        if key not in write_node.knobs():
            continue
        kn = write_node[key]
        for name in candidates:
            try:
                kn.setValue(name)
                return name
            except Exception:
                continue
        # leave default if nothing matched
    return None


def _make_write_node(source_node):
    """
    Create a temporary Write. Returns (write_node, prev_selection_NAMES).
    Never store Python attrs on Nuke nodes; never hold Node refs for restore.
    """
    prev_names = []
    try:
        prev_names = [n.name() for n in nuke.selectedNodes()]
        for n in nuke.selectedNodes():
            n.setSelected(False)
    except Exception:
        prev_names = []

    # Prefer createNode(inpanel=False) — more reliable across Nuke 15 builds
    try:
        w = nuke.createNode("Write", inpanel=False)
        w.setInput(0, source_node)
    except Exception:
        w = nuke.nodes.Write()
        w.setInput(0, source_node)

    try:
        # Unique name each time (avoid clash if previous delete failed)
        w.setName("__comfy_tmp_write_%s" % uuid.uuid4().hex[:8])
    except Exception:
        pass
    try:
        w.setXYpos(int(source_node.xpos()) - 2000, int(source_node.ypos()))
        w.setSelected(False)
        if "postage_stamp" in w.knobs():
            w["postage_stamp"].setValue(False)
    except Exception:
        pass

    return w, prev_names


def _delete_write_node(w, prev_names=None):
    """Delete temp Write; restore selection by name only (safe)."""
    if w is not None:
        try:
            name = w.name()
        except Exception:
            name = None
        try:
            nuke.delete(w)
        except Exception:
            # Force remove by name if object delete failed
            if name:
                try:
                    n2 = nuke.toNode(name)
                    if n2 is not None:
                        nuke.delete(n2)
                except Exception:
                    pass
    if prev_names:
        try:
            for n in nuke.selectedNodes():
                n.setSelected(False)
            for name in prev_names:
                try:
                    n = nuke.toNode(name)
                    if n is not None:
                        n.setSelected(True)
                except Exception:
                    pass
        except Exception:
            pass


def write_plate_srgb_png(source_node, frame, out_path):
    """
    Write RGB PNG (display/sRGB) from any Nuke node (Read EXR, etc.).
    Does NOT disable Viewers (that can crash Nuke 15 mid-render).
    """
    _ensure_temp_dir()
    out_path = out_path.replace("\\", "/")
    w = None
    prev_names = None
    cs_set = None
    try:
        _log("Write progress: plate sRGB PNG…")
        w, prev_names = _make_write_node(source_node)
        w["file"].setValue(out_path)
        w["file_type"].setValue("png")
        if "channels" in w.knobs():
            try:
                w["channels"].setValue("rgb")
            except Exception:
                pass
        if "datatype" in w.knobs():
            try:
                w["datatype"].setValue("8 bit")
            except Exception:
                pass
        if "raw" in w.knobs():
            try:
                w["raw"].setValue(False)
            except Exception:
                pass
        cs_set = _set_write_colorspace_srgb(w)
        nuke.execute(w, int(frame), int(frame))
        _log("Write done: plate sRGB PNG")
    finally:
        _delete_write_node(w, prev_names)

    if not os.path.isfile(out_path) or os.path.getsize(out_path) < 32:
        raise RuntimeError("Failed to write sRGB plate PNG: %s" % out_path)
    return out_path, cs_set


def write_tree_rgba_png(source_node, frame, out_path):
    """Write full tree as PNG RGBA (includes roto alpha if present)."""
    _ensure_temp_dir()
    out_path = out_path.replace("\\", "/")
    if os.path.isfile(out_path):
        try:
            os.remove(out_path)
        except Exception:
            pass
    w = None
    prev_names = None
    try:
        _log("Write progress: Roto RGBA PNG…")
        w, prev_names = _make_write_node(source_node)
        w["file"].setValue(out_path)
        w["file_type"].setValue("png")
        if "channels" in w.knobs():
            try:
                w["channels"].setValue("rgba")
            except Exception:
                pass
        if "datatype" in w.knobs():
            try:
                w["datatype"].setValue("8 bit")
            except Exception:
                pass
        if "raw" in w.knobs():
            try:
                w["raw"].setValue(False)
            except Exception:
                pass
        _set_write_colorspace_srgb(w)
        nuke.execute(w, int(frame), int(frame))
        _log("Write done — temp Write closed")
    finally:
        _delete_write_node(w, prev_names)

    if not os.path.isfile(out_path) or os.path.getsize(out_path) < 32:
        raise RuntimeError("Failed to write RGBA PNG: %s" % out_path)
    return out_path


# ---------------------------------------------------------------------------
# Alpha QC — must look like D:\bear-alpha.png (variation, not all-255)
# ---------------------------------------------------------------------------
def get_alpha_stats(png_path, step=2):
    """
    Return alpha stats for a PNG. Uses Qt (available in Nuke).
    step>1 samples for speed on 5K frames.
    """
    QtGui, _ = _qt_gui()
    if QtGui is None:
        raise RuntimeError("QtGui required for alpha validation")
    img = QtGui.QImage(png_path)
    if img.isNull():
        raise RuntimeError("Cannot open for alpha check: %s" % png_path)
    # Always convert — hasAlphaChannel() can lie on some PNG writers
    reported_alpha = bool(img.hasAlphaChannel())
    img = img.convertToFormat(QtGui.QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    amin, amax = 255, 0
    total = 0
    count = 0
    low = 0   # a < 16
    high = 0  # a > 240
    # qAlpha via pixel
    for y in range(0, h, step):
        for x in range(0, w, step):
            a = (img.pixel(x, y) >> 24) & 0xFF
            if a < amin:
                amin = a
            if a > amax:
                amax = a
            total += a
            count += 1
            if a < 16:
                low += 1
            if a > 240:
                high += 1
    mean = (total / float(count)) if count else 0.0
    frac_low = low / float(count) if count else 0.0
    frac_high = high / float(count) if count else 0.0
    # Valid like bear-alpha: both dark and bright alpha present
    ok = (amax - amin) >= 32 and frac_low > 0.01 and frac_high > 0.001
    reason = "ok"
    if amin == amax == 255:
        reason = "alpha all 255 (fully opaque — no mask)"
        ok = False
    elif amin == amax == 0:
        reason = "alpha all 0 (fully transparent — no plate)"
        ok = False
    elif (amax - amin) < 32:
        reason = "alpha almost flat (min=%s max=%s)" % (amin, amax)
        ok = False
    elif frac_high < 0.001:
        reason = "almost no opaque mask pixels (frac_high=%.4f)" % frac_high
        ok = False
    if not reported_alpha and amin == amax == 255:
        reason = "no alpha channel (RGB only — all A=255 after convert)"
        ok = False
    return {
        "has_alpha": reported_alpha or (amin != amax),
        "min": amin,
        "max": amax,
        "mean": mean,
        "frac_low": frac_low,
        "frac_high": frac_high,
        "ok": ok,
        "reason": reason,
        "width": w,
        "height": h,
    }


def write_alpha_preview(rgba_path, preview_path):
    """Grayscale of alpha (scaled if huge) for QC in any viewer."""
    QtGui, QtCore = _qt_gui()
    img = QtGui.QImage(rgba_path)
    if img.isNull():
        return None
    img = img.convertToFormat(QtGui.QImage.Format_ARGB32)
    if img.width() * img.height() > 2_500_000:
        img = img.scaled(
            max(1, img.width() // 2),
            max(1, img.height() // 2),
            QtCore.Qt.IgnoreAspectRatio,
            QtCore.Qt.FastTransformation,
        )
    return _argb_alpha_to_gray_path(img, preview_path)


def _argb_alpha_to_gray_path(img, preview_path):
    """Build gray preview: extract alpha via Qt (no per-pixel Python loop)."""
    QtGui, _ = _qt_gui()
    # Create opaque white image, use DestinationIn with source alpha only
    # Simpler: convert by taking alpha channel into grayscale Format
    w, h = img.width(), img.height()
    # Use createAlphaMask / convert — paint plate as white*alpha
    # Fast path: Format_Alpha8 from image
    alpha = img.convertToFormat(QtGui.QImage.Format_Alpha8)
    # Convert Alpha8 → RGB by drawing onto white/black
    out = QtGui.QImage(w, h, QtGui.QImage.Format_RGB32)
    out.fill(QtGui.QColor(0, 0, 0))
    # Draw alpha as luminance: use ARGB image where RGB=alpha
    # Reconstruct via setAlphaChannel reverse:
    tmp = QtGui.QImage(w, h, QtGui.QImage.Format_ARGB32)
    tmp.fill(QtGui.QColor(255, 255, 255, 255))
    tmp.setAlphaChannel(alpha)
    # Premultiply visually: draw on black
    out.fill(QtGui.QColor(0, 0, 0))
    p = QtGui.QPainter(out)
    p.drawImage(0, 0, tmp)
    p.end()
    if os.path.isfile(preview_path):
        try:
            os.remove(preview_path)
        except Exception:
            pass
    out.save(preview_path, "PNG")
    return preview_path


def ensure_alpha_like_bear(png_path):
    """
    Validate alpha. If inverted (mostly opaque), flip with PIL only (safe).
    bear-alpha: outside ~0, inside ~255, frac_high small.
    """
    stats = get_alpha_stats(png_path)
    _log(
        "alpha stats: min=%s max=%s mean=%.1f frac_low=%.3f frac_high=%.3f — %s"
        % (
            stats.get("min"),
            stats.get("max"),
            stats.get("mean") or 0.0,
            stats.get("frac_low") or 0.0,
            stats.get("frac_high") or 0.0,
            stats.get("reason"),
        )
    )
    if not stats.get("has_alpha") and stats.get("min") is None:
        return stats

    fh = stats.get("frac_high") or 0.0
    fl = stats.get("frac_low") or 0.0
    # Mostly opaque → invert alpha (keep RGB) via PIL — never Qt bits hacks (crashed Nuke)
    if fh > 0.5 and (stats.get("max") or 0) - (stats.get("min") or 0) >= 32:
        _log("alpha mostly opaque (%.0f%%) — PIL invert to match bear-alpha" % (fh * 100))
        if _invert_png_alpha_pil(png_path):
            stats = get_alpha_stats(png_path)
            _log(
                "after invert: min=%s max=%s frac_high=%.3f — %s"
                % (
                    stats.get("min"),
                    stats.get("max"),
                    stats.get("frac_high") or 0,
                    stats.get("reason"),
                )
            )
        else:
            _log("PIL invert failed — leaving alpha as-is")
    return stats


def _invert_png_alpha_pil(png_path):
    """Invert alpha channel only. Safe PIL — no Nuke Qt hacks, no numpy."""
    try:
        from PIL import Image

        im = Image.open(png_path).convert("RGBA")
        r, g, b, a = im.split()
        a_inv = a.point(lambda x: 255 - x)
        out = Image.merge("RGBA", (r, g, b, a_inv))
        out.save(png_path, "PNG")
        return True
    except Exception as e:
        _log("PIL alpha invert error: %s" % e)
        return False


def _invert_mask_luma_pil(mask_path):
    """Invert grayscale mask PNG (white↔black). PIL only."""
    try:
        from PIL import Image

        im = Image.open(mask_path).convert("L")
        inv = im.point(lambda x: 255 - x)
        inv.convert("RGB").save(mask_path, "PNG")
        return True
    except Exception as e:
        _log("PIL mask invert error: %s" % e)
        return False


# ---------------------------------------------------------------------------
# RGBA builders — match bear-alpha.png (A=255 inside, A=0 outside)
# ---------------------------------------------------------------------------
def write_roto_mask_luma(roto_node, frame, out_path):
    """
    Write Roto alpha as grayscale RGB PNG (white = mask, black = outside).
    Prefer Shuffle (in=alpha) so EXR rgb is not written; Expression fallback.
    """
    _ensure_temp_dir()
    out_path = out_path.replace("\\", "/")
    if os.path.isfile(out_path):
        try:
            os.remove(out_path)
        except Exception:
            pass

    prev_names = []
    try:
        prev_names = [n.name() for n in nuke.selectedNodes()]
        for n in nuke.selectedNodes():
            n.setSelected(False)
    except Exception:
        prev_names = []

    helper = None
    w = None
    try:
        # --- Try classic Shuffle: put alpha into RGB ---
        helper = None
        try:
            helper = nuke.createNode("Shuffle", inpanel=False)
            helper.setInput(0, roto_node)
            # Map all RGB from alpha, alpha white
            for kn, val in (
                ("red", "alpha"),
                ("green", "alpha"),
                ("blue", "alpha"),
                ("alpha", "white"),
            ):
                if kn in helper.knobs():
                    try:
                        helper[kn].setValue(val)
                    except Exception:
                        pass
            if "in" in helper.knobs():
                try:
                    helper["in"].setValue("rgba")
                except Exception:
                    pass
        except Exception:
            if helper is not None:
                try:
                    nuke.delete(helper)
                except Exception:
                    pass
            helper = None

        if helper is None:
            # Expression fallback: r=g=b=a
            helper = nuke.createNode("Expression", inpanel=False)
            helper.setInput(0, roto_node)
            for i, expr in enumerate(("a", "a", "a", "1")):
                kn = "expr%d" % i
                if kn in helper.knobs():
                    helper[kn].setValue(expr)
            # Also try channel-qualified form if plain 'a' fails at render
            for i, expr in enumerate(
                ("rgba.alpha", "rgba.alpha", "rgba.alpha", "1")
            ):
                kn = "expr%d" % i
                # Prefer rgba.alpha if supported — set after
                if kn in helper.knobs():
                    try:
                        helper[kn].setValue(expr)
                    except Exception:
                        pass

        try:
            helper.setName("__comfy_mask_%s" % uuid.uuid4().hex[:6])
            helper.setSelected(False)
            helper.setXYpos(int(roto_node.xpos()) - 2500, int(roto_node.ypos()))
        except Exception:
            pass

        w, _ = _make_write_node(helper)
        w["file"].setValue(out_path)
        w["file_type"].setValue("png")
        if "channels" in w.knobs():
            try:
                w["channels"].setValue("rgb")
            except Exception:
                pass
        if "datatype" in w.knobs():
            try:
                w["datatype"].setValue("8 bit")
            except Exception:
                pass
        if "raw" in w.knobs():
            try:
                w["raw"].setValue(True)  # write mask values as-is (0–1 → 0–255)
            except Exception:
                pass
        # Do NOT force sRGB transform on mask — raw data
        _log("Write progress: Roto mask luma PNG…")
        nuke.execute(w, int(frame), int(frame))
        _log("Write done: mask_luma.png")
    finally:
        if w is not None:
            try:
                nuke.delete(w)
            except Exception:
                pass
        if helper is not None:
            try:
                nuke.delete(helper)
            except Exception:
                pass
        try:
            for n in nuke.selectedNodes():
                n.setSelected(False)
            for name in prev_names:
                n = nuke.toNode(name)
                if n is not None:
                    n.setSelected(True)
        except Exception:
            pass

    if not os.path.isfile(out_path) or os.path.getsize(out_path) < 32:
        raise RuntimeError("Failed to write mask luma PNG")
    return out_path


def _mask_luma_stats(mask_path, step=4):
    """Stats on grayscale mask image (white=edit)."""
    QtGui, _ = _qt_gui()
    img = QtGui.QImage(mask_path)
    if img.isNull():
        return {"ok": False, "reason": "cannot open mask", "frac_high": 0.0}
    img = img.convertToFormat(QtGui.QImage.Format_RGB32)
    w, h = img.width(), img.height()
    high = total = 0
    amin, amax = 255, 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            g = (img.pixel(x, y) >> 16) & 0xFF
            total += 1
            if g < amin:
                amin = g
            if g > amax:
                amax = g
            if g > 200:
                high += 1
    frac = high / float(total) if total else 0.0
    ok = (amax - amin) >= 32 and 0.001 < frac < 0.95
    reason = "ok"
    if frac >= 0.95:
        reason = "mask almost full-frame (%.0f%% white)" % (frac * 100)
        ok = False
    elif frac <= 0.001:
        reason = "mask empty (no white)"
        ok = False
    return {
        "ok": ok,
        "reason": reason,
        "min": amin,
        "max": amax,
        "frac_high": frac,
        "width": w,
        "height": h,
    }


def _build_rgba_from_plate_and_mask_luma(plate_path, mask_path, out_path):
    """
    RGB from plate, A from mask luma (white→255). Like bear-alpha.png.
    Prefer PIL (fast); Qt fallback.
    """
    _ensure_temp_dir()
    if os.path.isfile(out_path):
        try:
            os.remove(out_path)
        except Exception:
            pass

    # --- Fast path: PIL ---
    try:
        from PIL import Image

        plate_p = Image.open(plate_path).convert("RGB")
        mask_p = Image.open(mask_path).convert("L")
        if mask_p.size != plate_p.size:
            mask_p = mask_p.resize(plate_p.size, Image.BILINEAR)
        plate_p.putalpha(mask_p)
        plate_p.save(out_path, "PNG")
        return out_path
    except Exception as e:
        _log("PIL composite unavailable (%s) — Qt fallback" % e)

    QtGui, QtCore = _qt_gui()
    if QtGui is None:
        raise RuntimeError("PySide QtGui not available")

    plate = QtGui.QImage(plate_path)
    mask = QtGui.QImage(mask_path)
    if plate.isNull():
        raise RuntimeError("Failed to load plate: %s" % plate_path)
    if mask.isNull():
        raise RuntimeError("Failed to load mask: %s" % mask_path)

    plate = plate.convertToFormat(QtGui.QImage.Format_ARGB32)
    w, h = plate.width(), plate.height()
    if mask.width() != w or mask.height() != h:
        mask = mask.scaled(
            w,
            h,
            QtCore.Qt.IgnoreAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )
    mask = mask.convertToFormat(QtGui.QImage.Format_RGB32)

    # Build Alpha8: white ARGB with A=luma, convert
    tmp = QtGui.QImage(w, h, QtGui.QImage.Format_ARGB32)
    # Row-wise via pixel — acceptable for one-shot export
    for y in range(h):
        for x in range(w):
            g = (mask.pixel(x, y) >> 16) & 0xFF
            tmp.setPixel(x, y, QtGui.qRgba(255, 255, 255, g))
    alpha = tmp.convertToFormat(QtGui.QImage.Format_Alpha8)
    plate.setAlphaChannel(alpha)

    if not plate.save(out_path, "PNG"):
        raise RuntimeError("Failed to save PNG RGBA: %s" % out_path)
    return out_path


def _build_rgba_full_frame_alpha(plate_path, out_path):
    """
    Full-frame edit: RGB = plate, A = 255 everywhere (whole image is the mask).
    Used when user selects Read only (no Roto).
    """
    _ensure_temp_dir()
    if os.path.isfile(out_path):
        try:
            os.remove(out_path)
        except Exception:
            pass

    try:
        from PIL import Image

        im = Image.open(plate_path).convert("RGB")
        a = Image.new("L", im.size, 255)
        im.putalpha(a)
        im.save(out_path, "PNG")
        return out_path
    except Exception as e:
        _log("PIL full-frame RGBA failed (%s) — Qt" % e)

    QtGui, _ = _qt_gui()
    if QtGui is None:
        raise RuntimeError("Cannot build full-frame RGBA")
    plate = QtGui.QImage(plate_path)
    if plate.isNull():
        raise RuntimeError("Failed to load plate: %s" % plate_path)
    plate = plate.convertToFormat(QtGui.QImage.Format_ARGB32)
    alpha = QtGui.QImage(plate.width(), plate.height(), QtGui.QImage.Format_Alpha8)
    alpha.fill(255)
    plate.setAlphaChannel(alpha)
    if not plate.save(out_path, "PNG"):
        raise RuntimeError("Failed to save full-frame RGBA PNG")
    return out_path


def _build_rgba_full_rgb_masked_alpha_fast(plate_path, mask_box, out_path, feather=8):
    """
    RGB = full plate. Alpha = rect mask (255 inside, 0 outside).
    Reject full-frame boxes (would make all A=255) — use _build_rgba_full_frame_alpha instead.
    """
    QtGui, QtCore = _qt_gui()
    if QtGui is None:
        raise RuntimeError("PySide QtGui not available")

    plate = QtGui.QImage(plate_path)
    if plate.isNull():
        raise RuntimeError("Failed to load plate: %s" % plate_path)
    plate = plate.convertToFormat(QtGui.QImage.Format_ARGB32)
    w, h = plate.width(), plate.height()

    x0, y0, x1, y1 = mask_box
    x0 = max(0, min(w, int(x0)))
    x1 = max(0, min(w, int(x1)))
    y0 = max(0, min(h, int(y0)))
    y1 = max(0, min(h, int(y1)))
    if x1 <= x0 or y1 <= y0:
        raise RuntimeError("Invalid mask box")
    if _is_near_full_frame((x0, y0, x1, y1), w, h, frac=0.95):
        raise RuntimeError(
            "Mask box is full-frame %s — not a real roto (often EXR alpha=1).\n"
            "Draw a Bezier on Roto, or select Read only for full-frame edit."
            % ((x0, y0, x1, y1),)
        )

    alpha = QtGui.QImage(w, h, QtGui.QImage.Format_Alpha8)
    alpha.fill(0)
    painter = QtGui.QPainter(alpha)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    painter.setPen(QtCore.Qt.NoPen)
    if feather > 0:
        steps = max(1, int(feather))
        for i in range(steps, 0, -1):
            g = int(255 * (1.0 - (i / float(steps + 1))))
            painter.setBrush(QtGui.QColor(0, 0, 0, g))
            painter.drawRoundedRect(
                x0 - i,
                y0 - i,
                (x1 - x0) + 2 * i,
                (y1 - y0) + 2 * i,
                max(1.0, i * 0.5),
                max(1.0, i * 0.5),
            )
    painter.setBrush(QtGui.QColor(0, 0, 0, 255))
    painter.drawRect(x0, y0, x1 - x0, y1 - y0)
    painter.end()

    plate.setAlphaChannel(alpha)

    _ensure_temp_dir()
    if os.path.isfile(out_path):
        try:
            os.remove(out_path)
        except Exception:
            pass
    if not plate.save(out_path, "PNG"):
        raise RuntimeError("Failed to save PNG RGBA: %s" % out_path)
    return out_path


def prepare_display_plate(read_node, plate_path, frame, fallback_node=None):
    """
    Produce TEMP_PLATE_SRGB (PNG RGB), overwrite each run.

    - JPG/PNG/etc on disk: load with Qt (ignore Read colorspace 'linear' label)
    - EXR / unloadable / no path: Nuke Write from Read or fallback (Roto)
    """
    _ensure_temp_dir()
    cs = _read_colorspace(read_node)
    src_for_write = read_node or fallback_node

    # Fast path: 8-bit display files — always prefer disk load
    if plate_path and os.path.isfile(plate_path):
        ext = os.path.splitext(plate_path)[1].lower()
        light = ext in (
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".bmp",
            ".gif",
            ".tif",
            ".tiff",
        )
        if light and _qimage_can_load(plate_path) and not _ext_needs_nuke_convert(
            plate_path
        ):
            # tif can be 32-bit; only skip if _ext_needs already false for common tif
            if ext not in (".tif", ".tiff") or not _is_linearish_colorspace(cs):
                QtGui, _ = _qt_gui()
                img = QtGui.QImage(plate_path)
                if not img.isNull():
                    img = img.convertToFormat(QtGui.QImage.Format_RGB32)
                    if os.path.isfile(TEMP_PLATE_SRGB):
                        try:
                            os.remove(TEMP_PLATE_SRGB)
                        except Exception:
                            pass
                    if img.save(TEMP_PLATE_SRGB, "PNG"):
                        _log(
                            "plate → plate_srgb.png (%s bytes from %s)"
                            % (
                                os.path.getsize(TEMP_PLATE_SRGB),
                                os.path.basename(plate_path),
                            )
                        )
                        return TEMP_PLATE_SRGB, "qt_png"

    # Nuke Write path (EXR / linear / no file / Qt failed)
    if src_for_write is None:
        raise RuntimeError(
            "Cannot build plate PNG: no Read file and no node to Write from.\n"
            "Connect Read → Roto (bg) and select Roto1."
        )
    _log(
        "Write plate via Nuke from '%s' (colorspace='%s', path=%s)"
        % (
            src_for_write.name(),
            cs or "?",
            os.path.basename(plate_path) if plate_path else "—",
        )
    )
    path, cs_set = write_plate_srgb_png(src_for_write, frame, TEMP_PLATE_SRGB)
    _log("plate_srgb.png ready (%s bytes, write_cs=%s)" % (os.path.getsize(path), cs_set))
    return path, "nuke_srgb"


def export_frame_for_comfy(node, frame, tmp_dir=None):
    """
    ALWAYS produce TEMP_INPUT_RGBA with real alpha (like D:\\bear-alpha.png).

    Reliable pipeline (Nuke Write of alpha is flaky — often RGB-only PNG):
      1) Write/get RGB plate (from Read file or Nuke Write of Read/Roto)
      2) Detect roto mask bbox (curves / alpha sample)
      3) Paint alpha onto plate → input_rgba.png
      4) Validate alpha before return (blocks Comfy until OK)
    """
    import shutil

    _ensure_temp_dir()
    for name in os.listdir(TEMP_DIR) if os.path.isdir(TEMP_DIR) else []:
        if name.startswith(("rgba_f", "plate_f", "in_f", "nuke_")):
            try:
                os.remove(os.path.join(TEMP_DIR, name))
            except Exception:
                pass

    read = find_upstream_read(node)
    plate_src = evaluate_read_path(read) if read else None
    roto = _find_roto_node(node)
    source = roto if roto is not None else node

    _log(
        "Export: source=%s read=%s plate=%s"
        % (
            source.name(),
            read.name() if read else "None",
            os.path.basename(plate_src) if plate_src else "None",
        )
    )

    # --- 1) RGB plate (must succeed before Comfy) ---
    plate_png, plate_method = prepare_display_plate(
        read, plate_src, frame, fallback_node=source
    )

    QtGui, _ = _qt_gui()
    width = height = None
    if QtGui is not None:
        qi = QtGui.QImage(plate_png)
        if not qi.isNull():
            width, height = qi.width(), qi.height()
    if not width:
        width, height = _node_format_size(source)
    if not width or not height:
        raise RuntimeError("Could not determine image size for export")

    # Detect Read-only full-frame edit (no Roto in selection chain)
    selected_is_read = False
    try:
        selected_is_read = node.Class() == "Read"
    except Exception:
        selected_is_read = False
    full_frame_mode = roto is None or selected_is_read

    mask_node = roto if roto is not None else node
    method = None
    stats = None
    full_frame_ok = False  # full A=255 is valid for whole-image edit

    # --- 2a) FULL FRAME: Read only / no Roto → whole image alpha = 255 ---
    if full_frame_mode:
        _log("No Roto — FULL FRAME edit (alpha solid white on whole image)")
        _build_rgba_full_frame_alpha(plate_png, TEMP_INPUT_RGBA)
        stats = get_alpha_stats(TEMP_INPUT_RGBA)
        # Full frame: all A=255 is intentional and valid
        if stats.get("min") == 255 and stats.get("max") == 255:
            stats = dict(stats)
            stats["ok"] = True
            stats["reason"] = "ok (full-frame edit)"
            stats["has_alpha"] = True
        method = "full_frame"
        full_frame_ok = True
        _log("full-frame RGBA ready (A=255 everywhere)")

    # --- 2b) ROTO: shape mask (unchanged path — do not alter when Roto present) ---
    if method is None and roto is not None and not selected_is_read:
        try:
            _log("Write Roto alpha → mask_luma.png (shape matte)…")
            write_roto_mask_luma(mask_node, frame, TEMP_MASK_LUMA)
            ms = _mask_luma_stats(TEMP_MASK_LUMA)
            _log(
                "mask_luma stats: min=%s max=%s frac_white=%.3f — %s"
                % (
                    ms.get("min"),
                    ms.get("max"),
                    ms.get("frac_high") or 0,
                    ms.get("reason"),
                )
            )
            if (ms.get("frac_high") or 0) > 0.5 and (ms.get("max") or 0) > (
                ms.get("min") or 0
            ):
                _log("mask_luma mostly white — inverting so white=subject")
                if _invert_mask_luma_pil(TEMP_MASK_LUMA):
                    ms = _mask_luma_stats(TEMP_MASK_LUMA)
                    _log(
                        "mask_luma after invert: frac_white=%.3f — %s"
                        % (ms.get("frac_high") or 0, ms.get("reason"))
                    )
            try:
                shutil.copy2(TEMP_MASK_LUMA, TEMP_ALPHA_PREVIEW)
            except Exception:
                pass

            if ms.get("ok") or (
                (ms.get("frac_high") or 0) > 0.001
                and (ms.get("frac_high") or 0) < 0.95
                and (ms.get("max") or 0) - (ms.get("min") or 0) >= 32
            ):
                _log("Building RGBA from plate + mask_luma…")
                _build_rgba_from_plate_and_mask_luma(
                    plate_png, TEMP_MASK_LUMA, TEMP_INPUT_RGBA
                )
                stats = get_alpha_stats(TEMP_INPUT_RGBA)
                _log(
                    "alpha stats: min=%s max=%s mean=%.1f frac_high=%.3f — %s"
                    % (
                        stats.get("min"),
                        stats.get("max"),
                        stats.get("mean") or 0,
                        stats.get("frac_high") or 0,
                        stats.get("reason"),
                    )
                )
                fh = stats.get("frac_high") or 0
                if fh > 0.5 and stats.get("ok") is False:
                    _log("composite still mostly opaque — PIL invert alpha once")
                    _invert_png_alpha_pil(TEMP_INPUT_RGBA)
                    stats = get_alpha_stats(TEMP_INPUT_RGBA)
                if stats.get("ok") or (
                    (stats.get("frac_high") or 0) > 0.001
                    and (stats.get("frac_high") or 0) < 0.95
                ):
                    if (stats.get("max") or 0) - (stats.get("min") or 0) >= 32:
                        method = "plate+roto_mask_luma"
                        if not stats.get("ok"):
                            stats = dict(stats)
                            stats["ok"] = True
                            stats["reason"] = "ok (accepted after mask_luma)"
                if method is None:
                    _log(
                        "Composite alpha failed (%s) — try bbox fallback"
                        % stats.get("reason")
                    )
            else:
                _log("mask_luma not usable (%s) — try bbox fallback" % ms.get("reason"))
        except Exception as e:
            _log("mask_luma write failed: %s — try bbox fallback" % e)

        # --- 3) ROTO FALLBACK: bbox paint (never full-frame) ---
        if method is None:
            mask_box = _roto_bbox_image_space(mask_node, frame, width, height)
            if mask_box is None and mask_node is not node:
                mask_box = _roto_bbox_image_space(node, frame, width, height)

            if mask_box is None or _is_near_full_frame(
                mask_box, width, height, frac=0.95
            ):
                raise RuntimeError(
                    "No usable roto mask.\n\n"
                    "EXR plates often have alpha=1 everywhere — full-frame is rejected.\n"
                    "1) Select Roto1 with a Bezier on the subject (not full frame)\n"
                    "2) Or select Read only for full-frame edit (no roto)\n"
                    "3) Check %%TEMP%%/comfy_nuke/mask_luma.png / alpha_preview.png"
                )

            mask_box = _expand_bbox(mask_box, pad=16, width=width, height=height)
            _log("Building RGBA (plate=%s box=%s)…" % (plate_method, mask_box))
            _build_rgba_full_rgb_masked_alpha_fast(
                plate_png, mask_box, TEMP_INPUT_RGBA, feather=12
            )
            stats = ensure_alpha_like_bear(TEMP_INPUT_RGBA)
            if not stats.get("ok"):
                raise RuntimeError(
                    "Alpha validation FAILED after paint: %s\n"
                    "min=%s max=%s\n"
                    "Check %s and %s"
                    % (
                        stats.get("reason"),
                        stats.get("min"),
                        stats.get("max"),
                        TEMP_INPUT_RGBA,
                        TEMP_ALPHA_PREVIEW,
                    )
                )
            method = "plate+bbox_alpha"

    if method is None:
        raise RuntimeError(
            "Could not build export image.\n"
            "Select Roto1 (with shape) or Read1 (full-frame edit)."
        )

    # --- 4) QC ---
    try:
        write_alpha_preview(TEMP_INPUT_RGBA, TEMP_ALPHA_PREVIEW)
        _log("alpha preview → %s" % TEMP_ALPHA_PREVIEW)
        _log("RGBA file     → %s" % TEMP_INPUT_RGBA)
    except Exception as e:
        _log("alpha preview failed: %s" % e)

    stats = get_alpha_stats(TEMP_INPUT_RGBA)
    if full_frame_ok and stats.get("min") == 255 and stats.get("max") == 255:
        stats = dict(stats)
        stats["ok"] = True
        stats["reason"] = "ok (full-frame edit)"
        stats["has_alpha"] = True

    if not stats.get("ok"):
        raise RuntimeError(
            "REFUSING upload — alpha invalid: %s\n"
            "Roto: outside A≈0, inside A≈255.\n"
            "Full-frame (Read only): A=255 everywhere is OK.\n"
            "Got min=%s max=%s mean=%.1f\n"
            "Open: %s\nPreview: %s"
            % (
                stats.get("reason"),
                stats.get("min"),
                stats.get("max"),
                stats.get("mean") or 0,
                TEMP_INPUT_RGBA,
                TEMP_ALPHA_PREVIEW,
            )
        )

    _log(
        "WRITE COMPLETE — ALPHA OK method=%s min=%s max=%s frac_mask=%.3f"
        % (method, stats["min"], stats["max"], stats.get("frac_high") or 0)
    )
    _log("Next: upload + queue Comfy (then poll in background)")
    return TEMP_INPUT_RGBA, method


def _finish_job_ok(result, source_name, frame_i):
    """Main-thread only: create Read + popup."""
    global _BG_JOB_ACTIVE, _POLL_STATE
    _BG_JOB_ACTIVE = False
    _POLL_STATE = None
    out_path = result["output_path"].replace("\\", "/")
    _log("Result: %s" % out_path)
    try:
        src = nuke.toNode(source_name)
    except Exception:
        src = None
    if src is None:

        class _Dummy(object):
            def xpos(self):
                return 0

            def ypos(self):
                return 0

            def name(self):
                return source_name

        src = _Dummy()
    read = _create_result_read(src, out_path, frame=frame_i)
    _log("New Read: %s → %s" % (read.name(), out_path))
    try:
        kind = "video" if out_path.lower().endswith(
            (".mp4", ".webm", ".mov", ".mkv", ".avi", ".gif")
        ) else "image"
        nuke.message(
            "ComfyUI done (%s).\n\n"
            "New Read: %s\n%s\n\n"
            "Previous results kept.\n"
            "prompt_id: %s"
            % (kind, read.name(), out_path, result.get("prompt_id"))
        )
    except Exception:
        pass


def _finish_job_err(err_msg):
    global _BG_JOB_ACTIVE, _POLL_STATE
    _BG_JOB_ACTIVE = False
    _POLL_STATE = None
    _log("ERROR: %s" % err_msg)
    try:
        nuke.message("ComfyUI error:\n%s" % err_msg)
    except Exception:
        pass


def _poll_comfy_tick():
    """
    Main-thread QTimer poll — NO Python threads, NO executeInMainThread.
    This is the crash-safe way to wait for Comfy while Nuke stays responsive.
    """
    global _POLL_STATE
    st = _POLL_STATE
    if not st:
        return

    client = st["client"]
    prompt_id = st["prompt_id"]
    t0 = st["t0"]
    timeout = st.get("timeout", 600.0)
    import time as _time

    elapsed = _time.time() - t0
    if elapsed > timeout:
        _finish_job_err("Timeout after %.0fs waiting for Comfy (%s)" % (timeout, prompt_id))
        return

    try:
        hist = client._get_json("/history/%s" % prompt_id)
    except Exception as e:
        # Network blip — keep polling
        _log("poll retry (%.0fs): %s" % (elapsed, e))
        _schedule_ms(2000, _poll_comfy_tick)
        return

    if prompt_id not in hist:
        # still queued/running
        if int(elapsed) % 10 < 3:  # light log, not every tick
            try:
                q = client.queue_status()
                running = q.get("queue_running") or []
                pending = q.get("queue_pending") or []
                if any(len(i) > 1 and i[1] == prompt_id for i in running):
                    _log("Comfy running (%.0fs)…" % elapsed)
                elif any(len(i) > 1 and i[1] == prompt_id for i in pending):
                    _log("Comfy queued (%.0fs)…" % elapsed)
                else:
                    _log("Comfy waiting (%.0fs)…" % elapsed)
            except Exception:
                _log("Comfy waiting (%.0fs)…" % elapsed)
        _schedule_ms(2000, _poll_comfy_tick)
        return

    entry = hist[prompt_id]
    status = entry.get("status") or {}
    if status.get("status_str") == "error":
        msgs = status.get("messages") or []
        _finish_job_err("Job failed: %s" % (msgs,))
        return

    if not entry.get("outputs") and status.get("completed") is not True:
        _schedule_ms(2000, _poll_comfy_tick)
        return

    # Done — download on main thread (prefer this job's stamp + video/EXR/PNG)
    try:
        files = client.outputs_from_history(entry)
        if not files:
            _finish_job_err("Job finished but no output media (image/video)")
            return
        stamp = st.get("stamp") or uuid.uuid4().hex[:12]
        # Match unique job prefix so we never pull another job's / preview file
        prefer_token = stamp
        try:
            primary = client.prefer_output_file(files, prefer_token=prefer_token)
        except TypeError:
            # Older client without prefer_token kwarg
            primary = client.prefer_output_file(files)
        out_dir = st["out_dir"]
        os.makedirs(out_dir, exist_ok=True)
        src_ext = (
            os.path.splitext(primary.get("filename") or "out.png")[1].lower() or ".png"
        )
        dest = os.path.join(out_dir, "comfy_%s%s" % (stamp, src_ext))
        if os.path.isfile(dest):
            dest = os.path.join(
                out_dir, "comfy_%s_%s%s" % (stamp, uuid.uuid4().hex[:6], src_ext)
            )
        _log(
            "Downloading result (%s type=%s sub=%s) — %s file(s) in history"
            % (
                primary.get("filename") or "?",
                primary.get("type") or "?",
                primary.get("subfolder") or "",
                len(files),
            )
        )
        if len(files) > 1:
            for i, f in enumerate(files[:8]):
                _log(
                    "  hist[%s]: %s/%s type=%s"
                    % (
                        i,
                        f.get("subfolder") or "",
                        f.get("filename") or "?",
                        f.get("type") or "?",
                    )
                )
        dest = client.download_image(primary, dest)
        # Do NOT write a shared last_comfy_result.* path — overwriting that
        # mutates any Nuke Read already pointing at it (looks like overlap).
        result = {
            "prompt_id": prompt_id,
            "output_path": dest,
            "seed": st.get("seed"),
        }
        _finish_job_ok(result, st["node_name"], st["frame"])
    except Exception as e:
        _finish_job_err(str(e))


def run_edit_on_node(
    node=None,
    prompt=None,
    server=DEFAULT_SERVER,
    workflow=DEFAULT_WORKFLOW,
    seed=None,
    output_dir=DEFAULT_OUT,
    frame=None,
    background=True,
):
    """
    ALL on Nuke main thread (crash-safe):
      1) Write PNG (log only, temp Write deleted)
      2) Upload + queue Comfy (short)
      3) Poll with QTimer every 2s — Nuke stays usable
      4) Popup + new Read when done
    No Python background threads / executeInMainThread.
    """
    global _BG_JOB_ACTIVE, _POLL_STATE

    if nuke is None:
        raise RuntimeError("Must run inside Nuke")

    ComfyClient, ComfyError = _get_client()

    if node is None:
        nodes = nuke.selectedNodes()
        if not nodes:
            raise RuntimeError(
                "Select a node first.\nRead1 -> Roto1 -> select Roto1"
            )
        node = nodes[0]

    if isinstance(node, str):
        n = nuke.toNode(node)
        if n is None:
            raise RuntimeError("Node gone: %s" % node)
        node = n

    if not prompt or not str(prompt).strip():
        raise RuntimeError("Prompt is empty")

    if _BG_JOB_ACTIVE or ComfyClient.is_busy() or _POLL_STATE is not None:
        raise RuntimeError(
            "A Comfy job is already running in this Nuke session. Wait for it."
        )

    if frame is None:
        frame = int(nuke.frame())
    else:
        frame = int(frame)

    out_dir = output_dir or DEFAULT_OUT
    os.makedirs(out_dir, exist_ok=True)

    wf_path = workflow or DEFAULT_WORKFLOW
    for old in (
        "Edit_Image_API.json",
        "Edit_Image_v01.json",
        "Edit_Image_v02.json",
        "Edit_Image_v03.json",
        "Edit_Image_v04.json",
    ):
        if wf_path.endswith(old):
            newer = os.path.join(REPO_ROOT, "Edit_Image_v05.json")
            if os.path.isfile(newer):
                wf_path = newer
            break

    # --- Phase 1: Nuke Write ---
    _BG_JOB_ACTIVE = True
    try:
        _log("Export starting (Write)…")
        in_path, method = export_frame_for_comfy(node, frame)
        if not in_path.lower().endswith(".png"):
            raise RuntimeError("Internal error: export must be PNG, got %s" % in_path)
    except Exception:
        _BG_JOB_ACTIVE = False
        raise

    node_name = node.name()
    prompt_s = str(prompt).strip()
    _log(
        "Export done (%s, %s bytes). Temp Write closed."
        % (method, os.path.getsize(in_path))
    )
    _log("Workflow: %s" % wf_path)
    _log("Server: %s" % server)

    # --- Phase 2: upload + queue (main thread, usually a few seconds) ---
    try:
        client = ComfyClient(
            server=server,
            workflow_path=wf_path,
            timeout_sec=600.0,
            poll_interval_sec=2.0,
        )
        _log("Uploading to Comfy…")
        client.load_workflow()
        image_name = client.upload_image(in_path)
        import time as _time
        import socket as _socket

        host = _socket.gethostname().replace(" ", "_")
        stamp = "%s_%s" % (
            _time.strftime("%Y%m%d_%H%M%S"),
            uuid.uuid4().hex[:8],
        )
        prefix = "nuke/%s/%s" % (host, stamp)
        wf = client.build_prompt(
            image_name=image_name,
            prompt=prompt_s,
            seed=seed,
            filename_prefix=prefix,
        )
        _log(
            "Inject: load=%s prompt=%s.%s seed=%s.%s"
            % (
                client.id_load,
                client.id_prompt,
                client.id_prompt_key,
                client.id_seed,
                client.id_seed_key,
            )
        )
        _log("Prompt text → node %s: %s" % (client.id_prompt, prompt_s[:120]))
        _log("Queueing Comfy job…")
        prompt_id = client.queue_prompt(wf)
        _log("prompt_id=%s" % prompt_id)
    except Exception as e:
        _BG_JOB_ACTIVE = False
        _log("ERROR: %s" % e)
        try:
            nuke.message("ComfyUI error:\n%s" % e)
        except Exception:
            pass
        raise

    if not background:
        # Blocking wait (debug)
        try:
            entry = client.wait_for_result(prompt_id, progress=lambda m: _log(m))
            files = client.outputs_from_history(entry)
            primary = sorted(
                files,
                key=lambda x: (0 if x.get("type") == "output" else 1, x.get("filename") or ""),
            )[0]
            dest = os.path.join(out_dir, "comfy_%s.png" % stamp)
            dest = client.download_image(primary, dest)
            _finish_job_ok(
                {"prompt_id": prompt_id, "output_path": dest, "seed": seed},
                node_name,
                frame,
            )
            return dest
        except Exception as e:
            _finish_job_err(str(e))
            raise

    # --- Phase 3: non-blocking poll via QTimer (main thread) ---
    import time as _time

    _POLL_STATE = {
        "client": client,
        "prompt_id": prompt_id,
        "t0": _time.time(),
        "timeout": 600.0,
        "out_dir": out_dir,
        "node_name": node_name,
        "frame": frame,
        "seed": seed,
        "stamp": stamp,
        "method": method,
    }
    _log("Comfy queued — keep working in Nuke. Progress in this log only.")
    _schedule_ms(2000, _poll_comfy_tick)
    return None


def _probe_video_media(path):
    """
    Best-effort width/height/fps/frame_count for a movie file.
    Tries ffprobe, then OpenCV. Returns dict or {}.
    """
    info = {}
    if not path or not os.path.isfile(path):
        return info

    # --- ffprobe ---
    try:
        import json as _json
        import subprocess

        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,nb_frames,duration,r_frame_rate,avg_frame_rate",
            "-of",
            "json",
            path,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0 and proc.stdout:
            data = _json.loads(proc.stdout)
            streams = data.get("streams") or []
            if streams:
                s = streams[0]
                if s.get("width"):
                    info["width"] = int(s["width"])
                if s.get("height"):
                    info["height"] = int(s["height"])
                # fps from r_frame_rate "24/1"
                for key in ("r_frame_rate", "avg_frame_rate"):
                    rate = s.get(key) or ""
                    if isinstance(rate, str) and "/" in rate:
                        a, b = rate.split("/", 1)
                        try:
                            num, den = float(a), float(b)
                            if den:
                                info["fps"] = num / den
                                break
                        except Exception:
                            pass
                nb = s.get("nb_frames")
                if nb not in (None, "N/A", ""):
                    try:
                        info["frames"] = int(nb)
                    except Exception:
                        pass
                if "frames" not in info and s.get("duration") and info.get("fps"):
                    try:
                        info["frames"] = max(
                            1, int(round(float(s["duration"]) * float(info["fps"])))
                        )
                    except Exception:
                        pass
                if "frames" not in info and s.get("duration"):
                    # format duration sometimes only on container — re-query
                    pass
    except Exception as e:
        _log("ffprobe skip: %s" % e)

    # container duration via ffprobe format if still no frames
    if "frames" not in info:
        try:
            import json as _json
            import subprocess

            cmd = [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                path,
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and proc.stdout:
                data = _json.loads(proc.stdout)
                dur = (data.get("format") or {}).get("duration")
                fps = info.get("fps") or 24.0
                if dur not in (None, "N/A", ""):
                    info["frames"] = max(1, int(round(float(dur) * float(fps))))
                    if "fps" not in info:
                        info["fps"] = float(fps)
        except Exception:
            pass

    # --- OpenCV fallback ---
    if not info.get("width") or not info.get("frames"):
        try:
            import cv2  # type: ignore

            cap = cv2.VideoCapture(path)
            if cap.isOpened():
                if not info.get("width"):
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                    if w > 0 and h > 0:
                        info["width"] = w
                        info["height"] = h
                if not info.get("fps"):
                    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
                    if fps > 0.1:
                        info["fps"] = fps
                if not info.get("frames"):
                    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                    if n > 0:
                        info["frames"] = n
            cap.release()
        except Exception as e:
            _log("opencv probe skip: %s" % e)

    return info


def _apply_video_read_range(read, out_path):
    """
    Set Read first/last/orig* and format from the real movie length/size.
    Avoids single-frame hold (first=last=1) on i2v results.
    """
    # Reload so Nuke opens the container
    try:
        if "reload" in read.knobs():
            read["reload"].execute()
    except Exception:
        pass

    probe = _probe_video_media(out_path)
    first = 1
    last = 1
    w = probe.get("width")
    h = probe.get("height")
    fps = probe.get("fps")
    nframes = probe.get("frames")

    # Prefer Nuke's own orig range when it looks valid (multi-frame)
    try:
        of = int(read["origfirst"].value()) if "origfirst" in read.knobs() else 1
        ol = int(read["origlast"].value()) if "origlast" in read.knobs() else 1
        if ol > of:
            first, last = of, ol
            nframes = last - first + 1
        elif nframes and int(nframes) > 1:
            first = 1
            last = int(nframes)
        elif ol == of == 1 and nframes and int(nframes) > 1:
            first = 1
            last = int(nframes)
    except Exception:
        if nframes and int(nframes) > 1:
            first, last = 1, int(nframes)

    if last < first:
        last = first

    try:
        if "first" in read.knobs():
            read["first"].setValue(int(first))
        if "last" in read.knobs():
            read["last"].setValue(int(last))
        if "origfirst" in read.knobs():
            read["origfirst"].setValue(int(first))
        if "origlast" in read.knobs():
            read["origlast"].setValue(int(last))
    except Exception as e:
        _log("video range knobs: %s" % e)

    # frame_mode: start at first (play full clip in timeline)
    try:
        if "frame_mode" in read.knobs():
            # enum: expression / start at / offset
            read["frame_mode"].setValue("start at")
        if "frame" in read.knobs():
            read["frame"].setValue(str(int(first)))
    except Exception:
        pass

    # before/after outside range
    try:
        if "before" in read.knobs():
            read["before"].setValue("hold")
        if "after" in read.knobs():
            read["after"].setValue("hold")
    except Exception:
        pass

    # Format size from media (not project root)
    if w and h and int(w) > 0 and int(h) > 0:
        try:
            fmt_name = "comfy_vid_%dx%d" % (int(w), int(h))
            try:
                nuke.addFormat("%d %d 0 0 %d %d 1 %s" % (int(w), int(h), int(w), int(h), fmt_name))
            except Exception:
                # already exists or short form
                try:
                    nuke.addFormat("%d %d %s" % (int(w), int(h), fmt_name))
                except Exception:
                    pass
            if "format" in read.knobs():
                read["format"].setValue(fmt_name)
            _log("Video Read format → %dx%d (%s)" % (int(w), int(h), fmt_name))
        except Exception as e:
            _log("video format set: %s" % e)
    else:
        # Fall back to whatever Nuke detected after reload
        try:
            fmt = read.format()
            if fmt:
                w = int(fmt.width())
                h = int(fmt.height())
                _log("Video Read format (from Nuke) → %dx%d" % (w, h))
        except Exception:
            pass

    if fps and float(fps) > 0.1:
        try:
            # Optional: only log — do not force root fps without asking
            _log("Video fps ≈ %.3f" % float(fps))
        except Exception:
            pass

    # Second reload after range/format knobs
    try:
        if "reload" in read.knobs():
            read["reload"].execute()
    except Exception:
        pass

    _log(
        "Video Read range %s–%s (%s frames) path=%s"
        % (first, last, last - first + 1, out_path)
    )
    return first, last


def _create_result_read(source_node, out_path, frame=1):
    """
    Create a new Read for this generation only.
    Names: ComfyEdit_Result_001, _002, ... — never reuses an existing Read file path.
    Stacks to the right of previous ComfyEdit_Result_* nodes.
    Videos: full duration range + media resolution (not 1-frame hold).
    """
    # Next index among existing ComfyEdit_Result_* nodes
    idx = 1
    existing = []
    for n in nuke.allNodes("Read"):
        name = n.name()
        if name == "ComfyEdit_Result" or name.startswith("ComfyEdit_Result_"):
            existing.append(n)
            # parse trailing number
            try:
                if name == "ComfyEdit_Result":
                    idx = max(idx, 2)
                else:
                    tail = name.split("_")[-1]
                    if tail.isdigit():
                        idx = max(idx, int(tail) + 1)
            except Exception:
                idx = max(idx, len(existing) + 1)

    read = nuke.createNode("Read", inpanel=False)
    base_name = "ComfyEdit_Result_%03d" % idx
    try:
        read.setName(base_name)
    except Exception:
        # name taken — try with uuid
        try:
            read.setName("ComfyEdit_Result_%03d_%s" % (idx, uuid.uuid4().hex[:4]))
        except Exception:
            pass

    read["file"].setValue(out_path)
    is_video = out_path.lower().endswith(
        (".mp4", ".webm", ".mov", ".mkv", ".avi", ".gif")
    )
    if is_video:
        _apply_video_read_range(read, out_path)
    else:
        # Stills: pin first/last to current frame so it holds on that frame
        try:
            f = int(frame)
            if "first" in read.knobs():
                read["first"].setValue(f)
            if "last" in read.knobs():
                read["last"].setValue(f)
            if "origfirst" in read.knobs():
                read["origfirst"].setValue(f)
            if "origlast" in read.knobs():
                read["origlast"].setValue(f)
        except Exception:
            pass
        try:
            if "reload" in read.knobs():
                read["reload"].execute()
        except Exception:
            pass

    # Colorspace: EXR → scene-linear; PNG/JPG from Comfy → sRGB display-referred
    try:
        low = out_path.lower()
        if low.endswith(".exr"):
            if "raw" in read.knobs():
                try:
                    read["raw"].setValue(False)
                except Exception:
                    pass
            for key in ("colorspace", "ocio_colorspace"):
                if key not in read.knobs():
                    continue
                for name in (
                    "linear",
                    "scene_linear",
                    "Linear",
                    "ACES - ACEScg",
                    "Utility - Linear - sRGB",
                    "default (linear)",
                ):
                    try:
                        read[key].setValue(name)
                        _log("Read colorspace → %s (%s)" % (name, key))
                        break
                    except Exception:
                        continue
                break
        elif low.endswith((".png", ".jpg", ".jpeg", ".webp")):
            if "raw" in read.knobs():
                try:
                    read["raw"].setValue(False)
                except Exception:
                    pass
            for key in ("colorspace", "ocio_colorspace"):
                if key not in read.knobs():
                    continue
                for name in (
                    "sRGB",
                    "srgb",
                    "Output - sRGB",
                    "Utility - sRGB - Texture",
                    "default (sRGB)",
                    "Gamma2.2",
                ):
                    try:
                        read[key].setValue(name)
                        _log("Read colorspace → %s (%s) [png/jpg]" % (name, key))
                        break
                    except Exception:
                        continue
                break
    except Exception:
        pass

    # Place: to the right of source, offset down by how many results already exist
    try:
        base_x = int(source_node.xpos()) + 200
        base_y = int(source_node.ypos())
        # Stack under previous results if any
        if existing:
            # right of the rightmost existing result
            max_x = max(int(n.xpos()) for n in existing)
            max_y = max(int(n.ypos()) for n in existing)
            base_x = max_x + 140
            base_y = max_y  # same row, step right; or stack down:
            # Prefer horizontal row of results
            base_y = int(source_node.ypos()) + 100
            # Count how many on that row-ish
            count = len(existing)
            base_x = int(source_node.xpos()) + 200 + (count % 6) * 140
            base_y = int(source_node.ypos()) + 100 + (count // 6) * 120
        else:
            base_x = int(source_node.xpos()) + 200
            base_y = int(source_node.ypos()) + 100
        read.setXYpos(base_x, base_y)
    except Exception:
        pass

    # Do not steal current selection — user may still be working.
    # They can click the new Read after the popup.
    try:
        read.setSelected(False)
    except Exception:
        pass

    return read


def schedule_edit(
    node=None,
    prompt=None,
    server=DEFAULT_SERVER,
    workflow=DEFAULT_WORKFLOW,
    seed=None,
    output_dir=DEFAULT_OUT,
    frame=None,
    _attempt=0,
):
    if nuke is None:
        raise RuntimeError("Must run inside Nuke")

    if node is None:
        nodes = nuke.selectedNodes()
        if not nodes:
            nuke.message(
                "Select a node first.\n\nRead1 -> Roto1 (draw shape) -> SELECT Roto1"
            )
            return
        node = nodes[0]

    node_name = node if isinstance(node, str) else node.name()
    if frame is None:
        frame = int(nuke.frame())
    prompt = str(prompt or "").strip()
    if not prompt:
        nuke.message("Prompt is empty")
        return

    server = (server or DEFAULT_SERVER).strip()
    workflow = workflow or DEFAULT_WORKFLOW
    output_dir = output_dir or DEFAULT_OUT

    if _attempt == 0:
        _log("Scheduled: node=%s frame=%s (export then background Comfy)" % (
            node_name, frame
        ))

    def _job():
        try:
            run_edit_on_node(
                node=node_name,
                prompt=prompt,
                server=server,
                workflow=workflow,
                seed=seed,
                output_dir=output_dir,
                frame=frame,
                background=True,
            )
        except Exception as e:
            msg = str(e).lower()
            if "already executing" in msg and _attempt < 25:
                _log("Execute lock — retry %s/25" % (_attempt + 1))
                _schedule_ms(
                    250,
                    lambda: schedule_edit(
                        node=node_name,
                        prompt=prompt,
                        server=server,
                        workflow=workflow,
                        seed=seed,
                        output_dir=output_dir,
                        frame=frame,
                        _attempt=_attempt + 1,
                    ),
                )
                return
            _log("ERROR: %s" % e)
            try:
                nuke.message(str(e))
            except Exception:
                pass

    _schedule_ms(50 if _attempt == 0 else 250, _job)


def show_panel():
    if nuke is None:
        raise RuntimeError("Must run inside Nuke")

    import nukescripts  # type: ignore

    class ComfyEditPanel(nukescripts.PythonPanel):
        def __init__(self):
            nukescripts.PythonPanel.__init__(self, "ComfyUI Edit Image")
            self.server = nuke.String_Knob("server", "Server")
            self.server.setValue(DEFAULT_SERVER)
            self.workflow = nuke.File_Knob("workflow", "Workflow")
            self.workflow.setValue(DEFAULT_WORKFLOW)
            self.prompt = nuke.Multiline_Eval_String_Knob("prompt", "Prompt")
            self.prompt.setValue("remove bear and shadow, dont change the color")
            self.seed = nuke.Int_Knob("seed", "Seed")
            self.seed.setValue(42)
            self.use_random_seed = nuke.Boolean_Knob("use_random", "Random seed")
            self.use_random_seed.setValue(True)  # default ON
            self.use_random_seed.setFlag(nuke.STARTLINE)
            self.out_dir = nuke.String_Knob("output_dir", "Output dir")
            self.out_dir.setValue(DEFAULT_OUT)
            self.help_txt = nuke.Text_Knob(
                "help_txt",
                "",
                "<b>Roto1</b> = edit masked region only.<br>"
                "<b>Read1 only</b> (no Roto) = full-frame edit (whole image alpha).<br>"
                "Write → Comfy poll (log). Popup when done. Random seed ON.",
            )
            for k in (
                self.server,
                self.workflow,
                self.prompt,
                self.seed,
                self.use_random_seed,
                self.out_dir,
                self.help_txt,
            ):
                self.addKnob(k)

    sel = nuke.selectedNodes()
    node = sel[0] if sel else None
    frame = int(nuke.frame())

    p = ComfyEditPanel()
    if not p.showModalDialog():
        nuke.tprint("[ComfyEdit] Cancelled")
        return

    if node is None:
        sel = nuke.selectedNodes()
        node = sel[0] if sel else None
    if node is None:
        nuke.message("No node selected. Select Roto1, open again, OK.")
        return

    seed = None if p.use_random_seed.value() else int(p.seed.value())
    schedule_edit(
        node=node,
        prompt=p.prompt.value(),
        server=p.server.value().strip(),
        workflow=p.workflow.value(),
        seed=seed,
        output_dir=p.out_dir.value() or DEFAULT_OUT,
        frame=frame,
    )


def ping_server(server=None):
    if server is None:
        server = DEFAULT_SERVER
    if nuke is None:
        return
    ComfyClient, ComfyError = _get_client()
    try:
        c = ComfyClient(server=server)
        stats = c.ping()
        q = c.queue_status()
        dev = (stats.get("devices") or [{}])[0]
        msg = (
            "OK %s\nComfyUI %s\nGPU: %s\nQueue running=%s pending=%s\n"
            "Edit: %s\nImage gen: %s"
            % (
                server,
                (stats.get("system") or {}).get("comfyui_version"),
                dev.get("name", "?"),
                len(q.get("queue_running") or []),
                len(q.get("queue_pending") or []),
                DEFAULT_WORKFLOW,
                IMAGE_GEN_WORKFLOW,
            )
        )
        nuke.message(msg)
        nuke.tprint("[ComfyEdit] %s" % msg)
    except Exception as e:
        nuke.message("Ping failed:\n%s" % e)


def schedule_image_gen(
    prompt=None,
    server=DEFAULT_SERVER,
    workflow=IMAGE_GEN_WORKFLOW,
    seed=None,
    output_dir=DEFAULT_OUT,
    width=1920,
    height=1080,
    _attempt=0,
):
    """
    Text-to-image: no Read/Roto required.
    Injects prompt into node 73 (PrimitiveStringMultiline value),
    queues Comfy, polls in background (log only), popup + new Read when done.
    """
    global _BG_JOB_ACTIVE, _POLL_STATE

    if nuke is None:
        raise RuntimeError("Must run inside Nuke")

    ComfyClient, ComfyError = _get_client()

    prompt_s = str(prompt or "").strip()
    if not prompt_s:
        nuke.message("Prompt is empty")
        return

    if _BG_JOB_ACTIVE or ComfyClient.is_busy() or _POLL_STATE is not None:
        nuke.message(
            "A Comfy job is already running in this Nuke session. Wait for it."
        )
        return

    server = (server or DEFAULT_SERVER).strip()
    wf_path = workflow or IMAGE_GEN_WORKFLOW
    if not os.path.isfile(wf_path):
        nuke.message("Workflow not found:\n%s" % wf_path)
        return
    out_dir = output_dir or DEFAULT_OUT
    os.makedirs(out_dir, exist_ok=True)
    frame = int(nuke.frame())

    # Anchor position: selected node or origin
    try:
        sel = nuke.selectedNodes()
        node_name = sel[0].name() if sel else None
    except Exception:
        node_name = None

    _BG_JOB_ACTIVE = True
    try:
        import time as _time
        import socket as _socket

        host = _socket.gethostname().replace(" ", "_")
        stamp = "t2i_%s_%s" % (
            _time.strftime("%Y%m%d_%H%M%S"),
            uuid.uuid4().hex[:8],
        )
        # Unique client_id per job — avoids progress/history cross-talk between modes
        client = ComfyClient(
            server=server,
            workflow_path=wf_path,
            timeout_sec=600.0,
            poll_interval_sec=2.0,
            client_id="nuke-t2i-%s-%s" % (host, stamp),
        )
        _log("Image Gen — load workflow %s" % wf_path)
        client.load_workflow()

        # Snap to nearest multiple of 8 (keeps 1920x1080; avoids odd-size artifacts)
        def _snap8(n):
            n = max(64, int(n))
            return max(64, int(round(n / 8.0)) * 8)

        w_i = _snap8(width or 1920)
        h_i = _snap8(height or 1080)
        client._inject_width = w_i
        client._inject_height = h_i

        prefix = "nuke/%s/%s" % (host, stamp)

        _log(
            "Inject: prompt=%s.%s seed=%s.%s"
            % (
                client.id_prompt,
                client.id_prompt_key,
                client.id_seed,
                client.id_seed_key,
            )
        )
        _log("Prompt text → node %s: %s" % (client.id_prompt, prompt_s[:120]))
        _log("Size: %sx%s (EmptyLatent, x8)" % (w_i, h_i))
        if client.id_prompt != "73":
            _log(
                "WARNING: expected prompt node 73 for Image_generation_v01, got %s"
                % client.id_prompt
            )

        wf = client.build_prompt(
            image_name=None,
            prompt=prompt_s,
            seed=seed,
            filename_prefix=prefix,
        )
        # Safety: never send an image into a pure txt2img graph
        for nid, node in wf.items():
            if isinstance(node, dict) and node.get("class_type") == "LoadImage":
                _log("WARNING: stripping unexpected LoadImage node %s from t2i" % nid)
                # leave node but do not reference — txt2img graph should not have it
        client._inject_width = None
        client._inject_height = None

        # Confirm inject values after build
        try:
            if "73" in wf:
                _log("Confirm 73.value len=%s" % len(str(wf["73"]["inputs"].get("value") or "")))
            if "53" in wf:
                _log("Confirm 53.seed=%s steps=%s cfg=%s" % (
                    wf["53"]["inputs"].get("seed"),
                    wf["53"]["inputs"].get("steps"),
                    wf["53"]["inputs"].get("cfg"),
                ))
            if "52" in wf:
                _log(
                    "Confirm 52 size=%sx%s batch=%s"
                    % (
                        wf["52"]["inputs"].get("width"),
                        wf["52"]["inputs"].get("height"),
                        wf["52"]["inputs"].get("batch_size"),
                    )
                )
            if "29" in wf:
                _log("Confirm 29.prefix=%s" % wf["29"]["inputs"].get("filename_prefix"))
        except Exception:
            pass

        _log("Queueing text-to-image job…")
        prompt_id = client.queue_prompt(wf)
        _log("prompt_id=%s" % prompt_id)
    except Exception as e:
        _BG_JOB_ACTIVE = False
        client = None
        _log("ERROR: %s" % e)
        try:
            nuke.message("ComfyUI Image Gen error:\n%s" % e)
        except Exception:
            pass
        return

    import time as _time

    _POLL_STATE = {
        "client": client,
        "prompt_id": prompt_id,
        "t0": _time.time(),
        "timeout": 600.0,
        "out_dir": out_dir,
        "node_name": node_name or "ImageGen",
        # Still holds on frame 1 so result is visible without scrubbing
        "frame": 1,
        "seed": seed,
        "stamp": stamp,
        "method": "txt2img",
    }
    _log(
        "Image Gen queued (stamp=%s) — keep working. Progress in this log only."
        % stamp
    )
    _schedule_ms(2000, _poll_comfy_tick)


def show_image_gen_panel():
    """Panel for text-to-image (no plate / no roto)."""
    if nuke is None:
        raise RuntimeError("Must run inside Nuke")

    import nukescripts  # type: ignore

    class ImageGenPanel(nukescripts.PythonPanel):
        def __init__(self):
            nukescripts.PythonPanel.__init__(self, "ComfyUI Image Gen")
            self.server = nuke.String_Knob("server", "Server")
            self.server.setValue(DEFAULT_SERVER)
            self.workflow = nuke.File_Knob("workflow", "Workflow")
            self.workflow.setValue(IMAGE_GEN_WORKFLOW)
            self.prompt = nuke.Multiline_Eval_String_Knob("prompt", "Prompt")
            self.prompt.setValue("mountain landscape")
            self.width = nuke.Int_Knob("width", "Width")
            self.width.setValue(1920)
            self.height = nuke.Int_Knob("height", "Height")
            self.height.setValue(1080)
            self.seed = nuke.Int_Knob("seed", "Seed")
            self.seed.setValue(42)
            self.use_random_seed = nuke.Boolean_Knob("use_random", "Random seed")
            self.use_random_seed.setValue(True)
            self.use_random_seed.setFlag(nuke.STARTLINE)
            self.out_dir = nuke.String_Knob("output_dir", "Output dir")
            self.out_dir.setValue(DEFAULT_OUT)
            self.help_txt = nuke.Text_Knob(
                "help_txt",
                "",
                "Text-to-image (no Read/Roto).<br>"
                "Prompt → node <b>73</b> → LLM → Krea2 turbo → SaveImage <b>29</b>.<br>"
                "Each job uses a unique file path (no shared last_ overwrite).<br>"
                "Size snapped to ×8. Background poll; new Read when done.",
            )
            for k in (
                self.server,
                self.workflow,
                self.prompt,
                self.width,
                self.height,
                self.seed,
                self.use_random_seed,
                self.out_dir,
                self.help_txt,
            ):
                self.addKnob(k)

    p = ImageGenPanel()
    if not p.showModalDialog():
        _log("Image Gen cancelled")
        return

    seed = None if p.use_random_seed.value() else int(p.seed.value())
    schedule_image_gen(
        prompt=p.prompt.value(),
        server=p.server.value().strip(),
        workflow=p.workflow.value(),
        seed=seed,
        output_dir=p.out_dir.value() or DEFAULT_OUT,
        width=int(p.width.value()),
        height=int(p.height.value()),
    )


def export_frame_for_i2v(node, frame):
    """
    Export current frame RGB PNG for MiniMax image-to-video.
    Works from Read / Merge / Roto (any selected node with image).
    No mask required — first_frame is the picture only.
    """
    import shutil

    _ensure_temp_dir()
    read = find_upstream_read(node)
    plate_src = evaluate_read_path(read) if read else None
    # Prefer writing from selected node (Merge/Roto result), else Read
    source = node
    _log(
        "I2V export frame %s from %s (read=%s)"
        % (
            frame,
            source.name(),
            read.name() if read else "None",
        )
    )
    plate_png, method = prepare_display_plate(
        read, plate_src, frame, fallback_node=source
    )
    # Always land on fixed i2v path (overwrite)
    if os.path.abspath(plate_png) != os.path.abspath(TEMP_I2V_FRAME):
        shutil.copy2(plate_png, TEMP_I2V_FRAME)
    _log(
        "I2V frame ready: %s (%s bytes, method=%s)"
        % (TEMP_I2V_FRAME, os.path.getsize(TEMP_I2V_FRAME), method)
    )
    return TEMP_I2V_FRAME, method


def schedule_image_to_video(
    node=None,
    prompt=None,
    server=DEFAULT_SERVER,
    workflow=I2V_WORKFLOW,
    seed=None,
    output_dir=DEFAULT_OUT,
    frame=None,
):
    """
    Image-to-video (MiniMax H3):
      Select Read / Merge / Roto → current frame → Comfy LoadImage 114
      Prompt → node 141 → LLM → i2v → SaveVideo
      Background poll → new Read of video in Nuke
    """
    global _BG_JOB_ACTIVE, _POLL_STATE

    if nuke is None:
        raise RuntimeError("Must run inside Nuke")

    ComfyClient, ComfyError = _get_client()

    if node is None:
        nodes = nuke.selectedNodes()
        if not nodes:
            nuke.message(
                "Select a node first.\n\n"
                "Read1, Merge, or Roto1 (current frame is sent as first_frame)."
            )
            return
        node = nodes[0]

    if isinstance(node, str):
        n = nuke.toNode(node)
        if n is None:
            nuke.message("Node gone: %s" % node)
            return
        node = n

    prompt_s = str(prompt or "").strip()
    if not prompt_s:
        nuke.message("Prompt is empty")
        return

    if _BG_JOB_ACTIVE or ComfyClient.is_busy() or _POLL_STATE is not None:
        nuke.message(
            "A Comfy job is already running in this Nuke session. Wait for it."
        )
        return

    server = (server or DEFAULT_SERVER).strip()
    wf_path = workflow or I2V_WORKFLOW
    if not os.path.isfile(wf_path):
        nuke.message("Workflow not found:\n%s" % wf_path)
        return
    out_dir = output_dir or DEFAULT_OUT
    os.makedirs(out_dir, exist_ok=True)
    if frame is None:
        frame = int(nuke.frame())
    else:
        frame = int(frame)

    node_name = node.name()
    _BG_JOB_ACTIVE = True

    # --- Phase 1: Write current frame ---
    try:
        _log("Image→Video export starting…")
        in_path, method = export_frame_for_i2v(node, frame)
    except Exception as e:
        _BG_JOB_ACTIVE = False
        _log("ERROR: %s" % e)
        try:
            nuke.message("I2V export error:\n%s" % e)
        except Exception:
            pass
        return

    # --- Phase 2: upload + queue ---
    try:
        client = ComfyClient(
            server=server,
            workflow_path=wf_path,
            timeout_sec=1800.0,  # video can be long
            poll_interval_sec=3.0,
        )
        _log("I2V — load workflow %s" % wf_path)
        client.load_workflow()
        _log(
            "Inject: load=%s prompt=%s.%s seed=%s.%s"
            % (
                client.id_load,
                client.id_prompt,
                client.id_prompt_key,
                client.id_seed,
                client.id_seed_key,
            )
        )
        _log("Prompt text → node %s: %s" % (client.id_prompt, prompt_s[:120]))
        _log("Uploading frame…")
        image_name = client.upload_image(in_path)

        import time as _time
        import socket as _socket

        host = _socket.gethostname().replace(" ", "_")
        stamp = "i2v_%s_%s" % (
            _time.strftime("%Y%m%d_%H%M%S"),
            uuid.uuid4().hex[:8],
        )
        prefix = "nuke/%s/%s" % (host, stamp)

        wf = client.build_prompt(
            image_name=image_name,
            prompt=prompt_s,
            seed=seed,
            filename_prefix=prefix,
        )
        _log("Queueing image-to-video job…")
        prompt_id = client.queue_prompt(wf)
        _log("prompt_id=%s" % prompt_id)
    except Exception as e:
        _BG_JOB_ACTIVE = False
        _log("ERROR: %s" % e)
        try:
            nuke.message("ComfyUI Image→Video error:\n%s" % e)
        except Exception:
            pass
        return

    import time as _time

    _POLL_STATE = {
        "client": client,
        "prompt_id": prompt_id,
        "t0": _time.time(),
        "timeout": 1800.0,
        "out_dir": out_dir,
        "node_name": node_name,
        "frame": frame,
        "seed": seed,
        "stamp": stamp,
        "method": "i2v",
    }
    _log("Image→Video queued — keep working in Nuke. Progress in this log only.")
    _schedule_ms(3000, _poll_comfy_tick)


def show_image_to_video_panel():
    """Panel: select Read/Merge/Roto → prompt → MiniMax i2v."""
    if nuke is None:
        raise RuntimeError("Must run inside Nuke")

    import nukescripts  # type: ignore

    class I2VPanel(nukescripts.PythonPanel):
        def __init__(self):
            nukescripts.PythonPanel.__init__(self, "ComfyUI Image to Video")
            self.server = nuke.String_Knob("server", "Server")
            self.server.setValue(DEFAULT_SERVER)
            self.workflow = nuke.File_Knob("workflow", "Workflow")
            self.workflow.setValue(I2V_WORKFLOW)
            self.prompt = nuke.Multiline_Eval_String_Knob("prompt", "Prompt")
            self.prompt.setValue("gentle camera move, natural motion")
            self.seed = nuke.Int_Knob("seed", "Seed")
            self.seed.setValue(42)
            self.use_random_seed = nuke.Boolean_Knob("use_random", "Random seed")
            self.use_random_seed.setValue(True)
            self.use_random_seed.setFlag(nuke.STARTLINE)
            self.out_dir = nuke.String_Knob("output_dir", "Output dir")
            self.out_dir.setValue(DEFAULT_OUT)
            self.help_txt = nuke.Text_Knob(
                "help_txt",
                "",
                "<b>Select Read / Merge / Roto</b> (current frame).<br>"
                "Frame → LoadImage <b>114</b>, Prompt → node <b>141</b>.<br>"
                "MiniMax H3 i2v → video back as new Read.",
            )
            for k in (
                self.server,
                self.workflow,
                self.prompt,
                self.seed,
                self.use_random_seed,
                self.out_dir,
                self.help_txt,
            ):
                self.addKnob(k)

    sel = nuke.selectedNodes()
    node = sel[0] if sel else None
    frame = int(nuke.frame())

    p = I2VPanel()
    if not p.showModalDialog():
        _log("Image→Video cancelled")
        return

    if node is None:
        sel = nuke.selectedNodes()
        node = sel[0] if sel else None
    if node is None:
        nuke.message(
            "Select a node first (Read, Merge, or Roto), then open Image to Video."
        )
        return

    seed = None if p.use_random_seed.value() else int(p.seed.value())
    schedule_image_to_video(
        node=node,
        prompt=p.prompt.value(),
        server=p.server.value().strip(),
        workflow=p.workflow.value(),
        seed=seed,
        output_dir=p.out_dir.value() or DEFAULT_OUT,
        frame=frame,
    )


def register_menu():
    if nuke is None:
        return
    _ensure_sys_module()
    menubar = nuke.menu("Nuke")
    try:
        menubar.removeItem("ComfyUI")
    except Exception:
        pass
    m = menubar.addMenu("ComfyUI")
    m.addCommand("Edit Image...", show_panel)
    m.addCommand("Image Gen...", show_image_gen_panel)
    m.addCommand("Image to Video...", show_image_to_video_panel)
    m.addCommand("Ping Server", ping_server)
    nuke.tprint(
        "[ComfyEdit] Menu OK — Edit Image / Image Gen / Image to Video / Ping | "
        "server=%s root=%s"
        % (DEFAULT_SERVER, REPO_ROOT)
    )


def load():
    register_menu()
    show_panel()


if nuke is not None and __name__ == "ComfyEdit":
    try:
        register_menu()
    except Exception as _e:
        try:
            nuke.tprint("[ComfyEdit] register_menu failed: %s" % _e)
        except Exception:
            pass

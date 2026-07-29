"""
jd_ShotInfoPanel_forBlender_v1_0_0.py
=====================================

Blender add-on that pulls per-shot production data from a Google Sheet into
the 3D Viewport sidebar: a thumbnail, the shot's camera/frame/resolution
info, a viewport HUD, and one-click reference-image setup. The Blender
counterpart of the Maya "Shot Info" tool.

WORKFLOW
--------
1. Open the "Shot Info" tab in the N-panel and set up the Configuration
   sub-panel once (sheet URL + column mapping). "Run Sanity Check" verifies
   the sheet is reachable and every mapped column exists.
2. Press "Fetch Sheet" -- the one network step for the data. It downloads
   the sheet once into memory; shot lookups after that are local.
3. Type a shot and press "Query", or press "AUTO" to read the shot number
   from the .blend filename (the digits after SH / SHOT).

HOW THE DATA IS READ
--------------------
The sheet is read via Google's CSV export endpoint (no OAuth / API key):

    https://docs.google.com/spreadsheets/d/<ID>/export?format=csv&gid=<GID>

so the sheet just needs to be shared "Anyone with the link -> Viewer".
Each ROW is a shot; each COLUMN a field, mapped by header name in Config.

If your thumbnail column uses =IMAGE("url") formulas, note that Google's
CSV export returns those cells EMPTY. The tool detects this and recovers the
URLs from the XLSX export (which keeps the formula text) using only the
Python standard library -- transparently, and only when needed.

THUMBNAIL & REFERENCE IMAGE
---------------------------
Thumbnails download only when a shot resolves, and are cached in Blender's
temp dir (bpy.app.tempdir), NOT in the project folder. "Create Reference
Image" adds the thumbnail to the scene as an image-empty. If a camera is
selected, the empty is parented to it and LETTERBOXED to the camera frame
(aspect preserved). Its depth is driven by a "camera_distance" custom
property on the empty itself -- scrub that value to slide it in/out and it
re-fits automatically.

HUD (playblast capture)
-----------------------
Blender has no Maya-style HUD, so the toggle does two things: it draws the
info live in the viewport (a blf overlay, for you while working) AND writes
it into the render Stamp "Note" with burn-in. The Stamp is what gets
captured by Viewport / OpenGL renders -- the playblast equivalent. The live
overlay is screen-space UI and is not in the rendered frame.

WHERE STATE LIVES
-----------------
* Config, current shot, and resolved info are Scene properties -> saved in
  the .blend automatically and restored on open (a load handler also
  re-hydrates the thumbnail and HUD).
* "Save as Default" writes the config to a JSON file in the Blender config
  dir, so brand-new files inherit it.
* The fetched sheet rows are a session-only in-memory cache.

INSTALL
-------
Edit > Preferences > Add-ons > Install..., select this file, and enable it
(or open it in the Text Editor and Run Script). The panel appears under
3D Viewport > N-panel > "Shot Info".

Tested on Blender 5.2.
"""

bl_info = {
    "name": "jd_ShotInfoPanel_forBlender",
    "author": "generated for Jean Delaunay's pipeline",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar (N) > Shot Info",
    "description": "Pull per-shot data (thumbnail, camera, frame range, "
                   "resolution) from a Google Sheet.",
    "category": "Pipeline",
}

import os
import re
import io
import csv
import ssl
import json
import math
import zipfile
import tempfile
import urllib.request
from xml.etree import ElementTree as ET

import bpy
import blf
import bpy.utils.previews
from mathutils import Matrix
from bpy.app.handlers import persistent
from bpy.props import StringProperty, FloatProperty, BoolProperty, PointerProperty


# ----------------------------------------------------------------------------
# Module-level session state (not saved with the file)
# ----------------------------------------------------------------------------
_ROWS = None            # cached fetched sheet: list of dicts
_PREVIEWS = None        # bpy.utils.previews collection for the thumbnail
_DRAW_HANDLE = None     # viewport HUD draw handler
_HUD_LINES = []         # lines drawn by the live overlay


# (config key, UI label, required?)
FIELD_DEFS = [
    ("col_shot",  "Shot Number",             True),
    ("col_thumb", "Thumbnail (URL)",         False),
    ("col_focal", "Camera Focal Length",     False),
    ("col_start", "Start Frame",             False),
    ("col_end",   "End Frame",               False),
    ("col_fps",   "Frame Rate",              False),
    ("col_res",   "Resolution (e.g. 1920x1080)", False),
    ("col_res_w", "Res Width (optional)",    False),
    ("col_res_h", "Res Height (optional)",   False),
]

DEFAULT_CONFIG = {
    "url": "", "gid": "",
    "col_shot": "shot", "col_thumb": "thumbnail", "col_focal": "focal",
    "col_start": "start", "col_end": "end", "col_fps": "fps",
    "col_res": "resolution", "col_res_w": "", "col_res_h": "",
}


# ============================================================================
# Pure helpers (ported straight from the Maya version; no bpy dependency)
# ============================================================================
def extract_sheet_id(url):
    if not url:
        return None
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9\-_]+)", url)
    return m.group(1) if m else None


def extract_gid(url):
    m = re.search(r"[#&?]gid=(\d+)", url or "")
    return m.group(1) if m else None


def csv_export_url(cfg):
    sid = extract_sheet_id(cfg.get("url", ""))
    if not sid:
        return None
    gid = (cfg.get("gid") or "").strip() or extract_gid(cfg.get("url", "")) or "0"
    return "https://docs.google.com/spreadsheets/d/%s/export?format=csv&gid=%s" % (sid, gid)


def direct_image_url(url):
    """Turn common Google Drive share links into a direct image URL. The
    /thumbnail endpoint returns real image bytes, unlike uc?export=download
    which often serves an HTML confirmation page."""
    if not url:
        return url
    fid = None
    for pat in (r"drive\.google\.com/file/d/([a-zA-Z0-9\-_]+)",
                r"drive\.google\.com/open\?id=([a-zA-Z0-9\-_]+)",
                r"drive\.google\.com/uc\?[^\s]*id=([a-zA-Z0-9\-_]+)"):
        m = re.search(pat, url)
        if m:
            fid = m.group(1)
            break
    if fid:
        return "https://drive.google.com/thumbnail?id=%s&sz=w1024" % fid
    return url


def _fetch_bytes(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except ssl.SSLError:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read()


def fetch_sheet(cfg):
    url = csv_export_url(cfg)
    if not url:
        raise ValueError("Could not parse a spreadsheet ID from the URL.")
    try:
        data = _fetch_bytes(url)
    except Exception as e:
        raise RuntimeError(
            "Failed to download the sheet. Check the URL and that it is "
            "shared as 'Anyone with the link -> Viewer'. (%s)" % e)
    text = data.decode("utf-8", errors="replace")
    if "<html" in text[:200].lower():
        raise RuntimeError("The sheet returned an HTML page instead of CSV. "
                           "It is probably not shared publicly.")
    reader = csv.reader(io.StringIO(text))
    all_rows = [r for r in reader]
    if not all_rows:
        raise RuntimeError("The sheet appears to be empty.")
    headers = [h.strip() for h in all_rows[0]]
    rows = [dict(zip(headers, r)) for r in all_rows[1:]]

    # Google exports =IMAGE("url") formula cells as EMPTY in CSV. If the
    # thumbnail column came through blank, recover the URLs from the XLSX
    # export (which keeps the formula text). Best-effort: never fatal.
    thumb_col = (cfg.get("col_thumb") or "").strip()
    shot_col = (cfg.get("col_shot") or "").strip()
    if thumb_col and shot_col and any(
            not (r.get(thumb_col, "") or "").strip() for r in rows):
        try:
            url_map = fetch_image_formula_urls(cfg, headers)
        except Exception:
            url_map = {}
        if url_map:
            for r in rows:
                if not (r.get(thumb_col, "") or "").strip():
                    sv = (r.get(shot_col, "") or "").strip()
                    if sv in url_map:
                        r[thumb_col] = url_map[sv]
    return headers, rows


# ----------------------------------------------------------------------------
# Recover =IMAGE("url") URLs from the XLSX export (CSV drops them)
# ----------------------------------------------------------------------------
_XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def xlsx_export_url(cfg):
    sid = extract_sheet_id(cfg.get("url", ""))
    if not sid:
        return None
    return "https://docs.google.com/spreadsheets/d/%s/export?format=xlsx" % sid


def _col_to_index(col_letters):
    idx = 0
    for ch in col_letters:
        idx = idx * 26 + (ord(ch.upper()) - 64)
    return idx - 1


def _split_cellref(ref):
    m = re.match(r"([A-Za-z]+)(\d+)", ref or "")
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def _read_shared_strings(z):
    strings = []
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return strings
    for si in root.findall("m:si", _XLSX_NS):
        strings.append("".join(t.text or "" for t in si.findall(".//m:t", _XLSX_NS)))
    return strings


def _worksheet_paths(z):
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {}
    for rel in rels:
        target = rel.get("Target") or ""
        if target.startswith("/"):
            target = target[1:]
        elif not target.startswith("xl/"):
            target = "xl/" + target
        rid_to_target[rel.get("Id")] = target
    out = []
    sheets = wb.find("m:sheets", _XLSX_NS)
    if sheets is None:
        return out
    for sh in sheets.findall("m:sheet", _XLSX_NS):
        out.append(rid_to_target.get(sh.get(_R_ID), ""))
    return out


def _parse_worksheet(z, path, shared):
    """Return {row_number: {col_index: (value, formula)}}."""
    root = ET.fromstring(z.read(path))
    sd = root.find("m:sheetData", _XLSX_NS)
    grid = {}
    if sd is None:
        return grid
    for row in sd.findall("m:row", _XLSX_NS):
        try:
            rnum = int(row.get("r"))
        except (TypeError, ValueError):
            continue
        cells = {}
        for c in row.findall("m:c", _XLSX_NS):
            col, _ = _split_cellref(c.get("r"))
            if col is None:
                continue
            cidx = _col_to_index(col)
            t = c.get("t")
            f = c.find("m:f", _XLSX_NS)
            v = c.find("m:v", _XLSX_NS)
            inline = c.find("m:is", _XLSX_NS)
            val = ""
            if t == "s" and v is not None:
                try:
                    val = shared[int(v.text)]
                except (ValueError, IndexError, TypeError):
                    val = ""
            elif t == "inlineStr" and inline is not None:
                tnode = inline.find(".//m:t", _XLSX_NS)
                val = tnode.text if tnode is not None else ""
            elif v is not None:
                val = v.text or ""
            cells[cidx] = (val, f.text if f is not None else "")
        grid[rnum] = cells
    return grid


def fetch_image_formula_urls(cfg, csv_headers):
    """Map {shot_value: image_url} by reading the =IMAGE() formulas in XLSX."""
    url = xlsx_export_url(cfg)
    if not url:
        return {}
    data = _fetch_bytes(url)
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = _read_shared_strings(z)

    # Pick the worksheet whose header row matches the CSV headers (or the
    # first non-empty sheet if nothing matches).
    chosen = None
    fallback = None
    for path in _worksheet_paths(z):
        if not path:
            continue
        grid = _parse_worksheet(z, path, shared)
        header = grid.get(1, {})
        texts = [(header.get(i, ("", ""))[0] or "").strip()
                 for i in range(len(csv_headers))]
        if fallback is None and grid:
            fallback = (grid, header)
        if texts == csv_headers:
            chosen = (grid, header)
            break
    if chosen is None:
        chosen = fallback
    if not chosen:
        return {}
    grid, header = chosen

    name_to_idx = {(val or "").strip(): idx
                   for idx, (val, _f) in header.items()}
    shot_idx = name_to_idx.get((cfg.get("col_shot") or "").strip())
    thumb_idx = name_to_idx.get((cfg.get("col_thumb") or "").strip())
    if shot_idx is None or thumb_idx is None:
        return {}

    out = {}
    for rnum, cells in grid.items():
        if rnum == 1:
            continue
        shot_val = (cells.get(shot_idx, ("", ""))[0] or "").strip()
        val, formula = cells.get(thumb_idx, ("", ""))
        src = formula or val
        m = re.search(r'https?://[^"\'\s\)]+', src or "")
        if shot_val and m:
            out[shot_val] = m.group(0)
    return out


def sanity_check(cfg):
    problems = []
    if not (cfg.get("url") or "").strip():
        return ["No spreadsheet URL set."]
    if not extract_sheet_id(cfg["url"]):
        problems.append("Could not parse a spreadsheet ID from the URL.")
    for key, label, required in FIELD_DEFS:
        if required and not (cfg.get(key) or "").strip():
            problems.append("Required field '%s' is empty." % label)
    try:
        headers, rows = fetch_sheet(cfg)
    except Exception as e:
        problems.append(str(e))
        return problems
    if not rows:
        problems.append("The sheet has headers but no data rows.")
    for key, label, _r in FIELD_DEFS:
        col = (cfg.get(key) or "").strip()
        if col and col not in headers:
            problems.append("Column '%s' (%s) not found. Available: %s"
                            % (col, label, ", ".join(headers)))
    return problems


def _digits(s):
    return re.sub(r"\D", "", s or "")


def find_shot_row(rows, cfg, shot):
    col = cfg.get("col_shot", "")
    shot = (shot or "").strip()
    if not col or not shot:
        return None
    for r in rows:
        if (r.get(col, "") or "").strip() == shot:
            return r
    target = _digits(shot)
    if target:
        try:
            ti = int(target)
            for r in rows:
                d = _digits(r.get(col, ""))
                if d and int(d) == ti:
                    return r
        except ValueError:
            pass
    return None


def parse_resolution(row, cfg):
    w = (row.get(cfg.get("col_res_w", ""), "") or "").strip()
    h = (row.get(cfg.get("col_res_h", ""), "") or "").strip()
    if w and h:
        return w, h
    combined = (row.get(cfg.get("col_res", ""), "") or "").strip()
    if combined:
        parts = [p for p in re.split(r"[xX,\s]+", combined) if p]
        if len(parts) >= 2:
            return parts[0], parts[1]
    return "", ""


def extract_shot_info(row, cfg):
    get = lambda k: (row.get(cfg.get(k, ""), "") or "").strip()
    w, h = parse_resolution(row, cfg)
    return {
        "focal": get("col_focal"), "start": get("col_start"),
        "end": get("col_end"), "fps": get("col_fps"),
        "res_w": w, "res_h": h, "thumb": get("col_thumb"),
    }


def detect_shot_from_blend():
    base = os.path.basename(bpy.data.filepath or "")
    m = re.search(r"(?:SHOT|SH)[ _\-]?(\d+)", base, re.IGNORECASE)
    return m.group(1) if m else None


def thumb_cache_dir():
    """Where downloaded thumbnails go. NOT the project folder: Blender's own
    per-session temp dir first, then the OS temp dir as a fallback."""
    base = bpy.app.tempdir or tempfile.gettempdir()
    cache = os.path.join(base, "jd_ShotInfoPanel_forBlender_thumbs")
    try:
        os.makedirs(cache, exist_ok=True)
    except OSError:
        cache = tempfile.gettempdir()
    return cache


def _looks_like_image(data):
    """Sniff magic bytes so we don't save an HTML error page as a .jpg."""
    if not data or len(data) < 12:
        return False
    head = data[:12]
    if head.startswith((b"\xff\xd8\xff",          # JPEG
                        b"\x89PNG\r\n\x1a\n",       # PNG
                        b"GIF87a", b"GIF89a",       # GIF
                        b"BM",                       # BMP
                        b"II*\x00", b"MM\x00*")):    # TIFF
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":  # WEBP
        return True
    return False


# ============================================================================
# Global default config (JSON in the Blender config dir; ~optionVar analog)
# ============================================================================
def _default_config_file():
    cfg_dir = bpy.utils.user_resource('CONFIG')
    try:
        os.makedirs(cfg_dir, exist_ok=True)
    except OSError:
        pass
    return os.path.join(cfg_dir, "jd_ShotInfoPanel_forBlender_config.json")


def load_default_config():
    cfg = dict(DEFAULT_CONFIG)
    path = _default_config_file()
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_default_config(cfg):
    try:
        with open(_default_config_file(), "w") as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception:
        return False


# ============================================================================
# Settings <-> dict bridge
# ============================================================================
CONFIG_KEYS = ["url", "gid"] + [k for k, _l, _r in FIELD_DEFS]


def cfg_from_settings(s):
    return {k: getattr(s, k) for k in CONFIG_KEYS}


def apply_cfg_to_settings(s, cfg):
    for k in CONFIG_KEYS:
        if k in cfg:
            setattr(s, k, cfg[k])


# ============================================================================
# Thumbnail (bpy.utils.previews)
# ============================================================================
def thumb_icon_id():
    if _PREVIEWS and "thumb" in _PREVIEWS:
        return _PREVIEWS["thumb"].icon_id
    return 0


def _load_preview_from_path(path):
    global _PREVIEWS
    if _PREVIEWS is None:
        return
    _PREVIEWS.clear()
    if path and os.path.isfile(path):
        try:
            _PREVIEWS.load("thumb", path, 'IMAGE')
        except Exception:
            pass


def load_thumbnail(s, url, shot):
    """Download + cache the thumbnail. Only ever called on a successful
    query, so this is the single per-shot network hit."""
    if not url:
        if _PREVIEWS:
            _PREVIEWS.clear()
        s.thumb_url = ""
        s.thumb_path = ""
        return
    # Already have this exact image on disk? Don't re-download.
    if url == s.thumb_url and s.thumb_path and os.path.isfile(s.thumb_path):
        _load_preview_from_path(s.thumb_path)
        return
    real = direct_image_url(url)
    data = _fetch_bytes(real)
    if not _looks_like_image(data):
        raise RuntimeError(
            "the URL did not return an image (%d bytes). It may be an HTML "
            "page rather than a direct image link. For Google Drive, share "
            "the image publicly; for other hosts use a direct image URL."
            % len(data))
    ext = ".jpg"
    m = re.search(r"\.(png|jpg|jpeg|tif|tiff|bmp)(\?|$)", real, re.IGNORECASE)
    if m:
        ext = "." + m.group(1).lower()
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", shot or "shot")
    path = os.path.join(thumb_cache_dir(), "thumb_%s%s" % (safe, ext))
    with open(path, "wb") as f:
        f.write(data)
    s.thumb_url = url
    s.thumb_path = path
    _load_preview_from_path(path)


# ============================================================================
# HUD: live viewport overlay + render Stamp (captured in viewport renders)
# ============================================================================
def _hud_compose(s):
    parts = []
    if s.shot:
        parts.append("SH %s" % s.shot)
    if s.info_focal:
        parts.append("Focal %smm" % s.info_focal)
    if s.info_start or s.info_end:
        parts.append("%s-%s" % (s.info_start or "?", s.info_end or "?"))
    if s.info_fps:
        parts.append("%sfps" % s.info_fps)
    if s.info_res_w and s.info_res_h:
        parts.append("%sx%s" % (s.info_res_w, s.info_res_h))
    return parts


def _tag_redraw():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _draw_hud():
    if not _HUD_LINES:
        return
    font_id = 0
    try:
        blf.size(font_id, 15, 72)   # Blender < 4.0 signature
    except TypeError:
        blf.size(font_id, 15)       # Blender 4.0+ dropped the dpi arg
    y = 30
    for line in reversed(_HUD_LINES):
        blf.color(font_id, 0.0, 0.0, 0.0, 0.6)
        blf.position(font_id, 21, y - 1, 0)
        blf.draw(font_id, line)
        blf.color(font_id, 1.0, 1.0, 1.0, 1.0)
        blf.position(font_id, 20, y, 0)
        blf.draw(font_id, line)
        y += 22


def hud_enable_draw():
    global _DRAW_HANDLE
    if _DRAW_HANDLE is None:
        _DRAW_HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw_hud, (), 'WINDOW', 'POST_PIXEL')


def hud_disable_draw():
    global _DRAW_HANDLE
    if _DRAW_HANDLE is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_DRAW_HANDLE, 'WINDOW')
        except Exception:
            pass
        _DRAW_HANDLE = None


def hud_refresh(s):
    """Push current info into the live overlay + render stamp note."""
    global _HUD_LINES
    parts = _hud_compose(s)
    _HUD_LINES = parts
    scene = getattr(bpy.context, "scene", None)
    if s.hud_on and scene is not None:
        rd = scene.render
        rd.use_stamp = True
        rd.use_stamp_note = True
        rd.stamp_note_text = "  |  ".join(parts)
    _tag_redraw()


def hud_set(s, on):
    s.hud_on = on
    if on:
        hud_enable_draw()
        hud_refresh(s)
    else:
        hud_disable_draw()
        try:
            bpy.context.scene.render.use_stamp_note = False
        except Exception:
            pass
        _tag_redraw()


# ============================================================================
# Core query (shared by Query / AUTO)
# ============================================================================
def do_query(context):
    s = context.scene.shot_info
    shot = (s.shot or "").strip()
    if not shot:
        return False, "Enter a shot number (or use AUTO)."
    if _ROWS is None:
        return False, "No sheet data yet - press 'Fetch Sheet' first."
    cfg = cfg_from_settings(s)
    row = find_shot_row(_ROWS, cfg, shot)
    if row is None:
        _clear_info(s)
        return False, "Shot '%s' not found in the sheet." % shot
    info = extract_shot_info(row, cfg)
    s.info_focal = info["focal"]
    s.info_start = info["start"]
    s.info_end = info["end"]
    s.info_fps = info["fps"]
    s.info_res_w = info["res_w"]
    s.info_res_h = info["res_h"]
    try:
        load_thumbnail(s, info["thumb"], shot)          # network only on success
    except Exception as e:
        return True, "Loaded shot %s (thumbnail failed: %s)." % (shot, e)
    if s.hud_on:
        hud_refresh(s)
    return True, "Loaded shot %s." % shot


def _clear_info(s):
    for attr in ("info_focal", "info_start", "info_end", "info_fps",
                 "info_res_w", "info_res_h", "thumb_url", "thumb_path"):
        setattr(s, attr, "")
    if _PREVIEWS:
        _PREVIEWS.clear()


# ============================================================================
# Property group  (all of this saves inside the .blend automatically)
# ============================================================================
class ShotInfoSettings(bpy.types.PropertyGroup):
    url: StringProperty(name="Spreadsheet URL", default="")
    gid: StringProperty(name="Sheet gid", default="")
    col_shot: StringProperty(name="Shot Number", default="shot")
    col_thumb: StringProperty(name="Thumbnail", default="thumbnail")
    col_focal: StringProperty(name="Focal Length", default="focal")
    col_start: StringProperty(name="Start Frame", default="start")
    col_end: StringProperty(name="End Frame", default="end")
    col_fps: StringProperty(name="Frame Rate", default="fps")
    col_res: StringProperty(name="Resolution", default="resolution")
    col_res_w: StringProperty(name="Res Width", default="")
    col_res_h: StringProperty(name="Res Height", default="")

    shot: StringProperty(name="Shot", default="")
    thumb_scale: FloatProperty(name="Thumbnail Size", default=6.0,
                               min=1.0, max=20.0)
    thumb_url: StringProperty(default="")
    thumb_path: StringProperty(default="")

    info_focal: StringProperty(default="")
    info_start: StringProperty(default="")
    info_end: StringProperty(default="")
    info_fps: StringProperty(default="")
    info_res_w: StringProperty(default="")
    info_res_h: StringProperty(default="")

    hud_on: BoolProperty(default=False)

    status: StringProperty(default="")
    cfg_status: StringProperty(default="")
    fetch_status: StringProperty(default="Not fetched")


# ============================================================================
# Operators
# ============================================================================
class SHOTINFO_OT_fetch(bpy.types.Operator):
    bl_idname = "shotinfo.fetch"
    bl_label = "Fetch Sheet"
    bl_description = "Download the spreadsheet once; lookups then run locally"

    def execute(self, context):
        global _ROWS
        s = context.scene.shot_info
        cfg = cfg_from_settings(s)
        try:
            _headers, rows = fetch_sheet(cfg)
        except Exception as e:
            _ROWS = None
            s.fetch_status = "Not fetched"
            s.status = str(e)
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        _ROWS = rows
        s.fetch_status = "%d shots loaded" % len(rows)
        s.status = "Sheet fetched. Lookups now run locally."
        if (s.shot or "").strip():
            ok, msg = do_query(context)
            s.status = msg
        return {'FINISHED'}


class SHOTINFO_OT_query(bpy.types.Operator):
    bl_idname = "shotinfo.query"
    bl_label = "Query"
    bl_description = "Look up the shot in the fetched sheet (local)"

    def execute(self, context):
        ok, msg = do_query(context)
        context.scene.shot_info.status = msg
        if not ok:
            self.report({'WARNING'}, msg)
        return {'FINISHED'}


class SHOTINFO_OT_auto(bpy.types.Operator):
    bl_idname = "shotinfo.auto"
    bl_label = "AUTO"
    bl_description = "Detect the shot number from the .blend filename (after SH / SHOT)"

    def execute(self, context):
        s = context.scene.shot_info
        shot = detect_shot_from_blend()
        if not shot:
            s.status = "No SH/SHOT number found in the .blend filename (saved?)."
            self.report({'WARNING'}, s.status)
            return {'CANCELLED'}
        s.shot = shot
        ok, msg = do_query(context)
        s.status = msg
        return {'FINISHED'}


class SHOTINFO_OT_sanity(bpy.types.Operator):
    bl_idname = "shotinfo.sanity"
    bl_label = "Run Sanity Check"
    bl_description = "Validate the URL and that every mapped column exists"

    def execute(self, context):
        s = context.scene.shot_info
        problems = sanity_check(cfg_from_settings(s))
        if not problems:
            s.cfg_status = "All good. Sheet reachable and every mapped column found."
            self.report({'INFO'}, "Sanity check passed.")
        else:
            s.cfg_status = "\n".join("- " + p for p in problems)
            self.report({'WARNING'}, "%d problem(s) found." % len(problems))
        return {'FINISHED'}


class SHOTINFO_OT_save_default(bpy.types.Operator):
    bl_idname = "shotinfo.save_default"
    bl_label = "Save as Default"
    bl_description = "Store this config so new .blend files inherit it"

    def execute(self, context):
        s = context.scene.shot_info
        if save_default_config(cfg_from_settings(s)):
            s.cfg_status = "Saved as default for new files."
            self.report({'INFO'}, "Saved default config.")
        else:
            self.report({'ERROR'}, "Could not write the default config file.")
        return {'FINISHED'}


def create_image_empty(context, path):
    """Add the image to the scene as an image-empty (the reference-image
    workflow), using whatever mechanism the running Blender supports."""
    img = bpy.data.images.load(path, check_existing=True)

    # 1) Modern, version-stable: empty_add with the IMAGE type.
    try:
        bpy.ops.object.empty_add(type='IMAGE')
        ob = context.active_object
        ob.data = img
        ob.name = "ShotThumbnail"
        return ob
    except Exception:
        pass

    # 2) Legacy operators (older Blender had these; newer builds dropped them).
    for opname in ("load_reference_image", "load_background_image"):
        try:
            getattr(bpy.ops.object, opname)(filepath=path)
            return context.active_object
        except Exception:
            pass

    # 3) Pure data API, no operator at all.
    ob = bpy.data.objects.new("ShotThumbnail", None)
    ob.empty_display_type = 'IMAGE'
    ob.data = img
    coll = getattr(context, "collection", None) or context.scene.collection
    coll.objects.link(ob)
    return ob


def _selected_camera(context):
    """The camera to parent to: the active object if it's a camera, else the
    first camera among the selected objects. Captured BEFORE we make the
    empty, since empty_add steals the active object."""
    ob = context.active_object
    if ob and ob.type == 'CAMERA':
        return ob
    for o in context.selected_objects:
        if o.type == 'CAMERA':
            return o
    return None


def _add_self_driver(obj, data_path, index, expression, prop="camera_distance"):
    """Scripted driver on obj.<data_path>[index] that reads a custom property
    on the SAME object. Plain arithmetic expressions evaluate via Blender's
    safe path, so 'Auto Run Python Scripts' is not required."""
    fcurve = obj.driver_add(data_path, index)
    drv = fcurve.driver
    drv.type = 'SCRIPTED'
    for v in list(drv.variables):
        drv.variables.remove(v)
    var = drv.variables.new()
    var.name = "d"
    var.type = 'SINGLE_PROP'
    tgt = var.targets[0]
    tgt.id = obj
    tgt.data_path = '["%s"]' % prop
    drv.expression = expression
    return fcurve


def _fit_empty_to_camera(empty, cam, scene, distance=None):
    """Parent the image-empty to the camera, letterbox it inside the frame
    (no distortion), and drive its depth from a 'camera_distance' custom
    property ON THE EMPTY -- scrub that property to slide it in/out and it
    re-fits automatically."""
    camd = cam.data
    frame = camd.view_frame(scene=scene)   # 4 corners in camera-local space
    xs = [v.x for v in frame]
    ys = [v.y for v in frame]
    fw = max(xs) - min(xs)                  # frame width  (per unit for persp)
    fh = max(ys) - min(ys)                  # frame height
    cx = (max(xs) + min(xs)) / 2.0          # centre (accounts for lens shift)
    cy = (max(ys) + min(ys)) / 2.0

    # Image displayed size at empty_display_size = 1 (longer side -> 1).
    size = empty.data.size if empty.data else (1, 1)
    w_px = size[0] or 1
    h_px = size[1] or 1
    a = w_px / h_px
    if a >= 1.0:
        base_w, base_h = 1.0, 1.0 / a
    else:
        base_w, base_h = a, 1.0

    # Letterbox: one uniform factor that fits the image INSIDE the frame.
    k = min(fw / base_w, fh / base_h)

    if distance is None:
        distance = min(10.0, camd.clip_end * 0.9)
        distance = max(distance, camd.clip_start * 1.1)

    empty.parent = cam
    empty.matrix_parent_inverse = Matrix.Identity(4)
    empty.empty_display_size = 1.0
    empty.rotation_euler = (0.0, 0.0, 0.0)

    # Depth lives on the object as a custom property (edit it under the
    # object's Custom Properties); the drivers below react to it.
    empty["camera_distance"] = float(distance)
    try:
        empty.id_properties_ui("camera_distance").update(
            min=0.0001, soft_min=0.1, soft_max=10000.0,
            description="Distance from the camera; drives the fit depth")
    except Exception:
        pass

    empty.driver_remove("location")   # clear priors (harmless if none)
    empty.driver_remove("scale")

    if camd.type == 'ORTHO':
        # Ortho frame size is constant with depth; only Z follows distance.
        empty.location = (cx, cy, 0.0)
        empty.scale = (k, k, 1.0)
        _add_self_driver(empty, "location", 2, "-d")
    else:
        # Perspective: extents and centre scale linearly with depth, so each
        # driver is a baked constant times the distance.
        _add_self_driver(empty, "location", 0, "%r * d" % cx)
        _add_self_driver(empty, "location", 1, "%r * d" % cy)
        _add_self_driver(empty, "location", 2, "-d")
        _add_self_driver(empty, "scale", 0, "%r * d" % k)
        _add_self_driver(empty, "scale", 1, "%r * d" % k)
        empty.scale[2] = 1.0

    empty.update_tag()


class SHOTINFO_OT_make_ref(bpy.types.Operator):
    bl_idname = "shotinfo.make_ref"
    bl_label = "Create Reference Image"
    bl_description = ("Add the thumbnail as a reference-image empty. If a "
                     "camera is selected, parent it and fit the camera view")

    def execute(self, context):
        s = context.scene.shot_info
        path = s.thumb_path
        if not path or not os.path.isfile(path):
            self.report({'ERROR'}, "No downloaded thumbnail to place.")
            return {'CANCELLED'}
        cam = _selected_camera(context)  # capture before creating the empty
        try:
            empty = create_image_empty(context, path)
        except Exception as e:
            self.report({'ERROR'}, "Failed to add reference image: %s" % e)
            return {'CANCELLED'}
        if cam:
            try:
                _fit_empty_to_camera(empty, cam, context.scene)
                s.status = ("Parented to '%s', letterboxed to view. Scrub "
                            "'camera_distance' on the empty to slide it."
                            % cam.name)
            except Exception as e:
                s.status = "Reference image created (camera fit failed: %s)." % e
        else:
            s.status = "Reference image created from %s" % os.path.basename(path)
        return {'FINISHED'}


class SHOTINFO_OT_toggle_hud(bpy.types.Operator):
    bl_idname = "shotinfo.toggle_hud"
    bl_label = "Toggle HUD"
    bl_description = ("Show shot info in the viewport and burn it into the "
                      "render Stamp (captured by viewport/OpenGL renders)")

    def execute(self, context):
        s = context.scene.shot_info
        hud_set(s, not s.hud_on)
        s.status = "HUD on." if s.hud_on else "HUD off."
        return {'FINISHED'}


class SHOTINFO_OT_apply_scene(bpy.types.Operator):
    bl_idname = "shotinfo.apply_scene"
    bl_label = "Apply to Scene"
    bl_description = "Set frame range, fps and resolution from the queried shot"

    def execute(self, context):
        s = context.scene.shot_info
        scene = context.scene
        applied = []
        if s.info_start and s.info_end:
            try:
                scene.frame_start = int(float(s.info_start))
                scene.frame_end = int(float(s.info_end))
                applied.append("frame range")
            except ValueError:
                pass
        if s.info_fps:
            try:
                f = float(s.info_fps)
                if abs(f - round(f)) < 0.01:
                    scene.render.fps = int(round(f))
                    scene.render.fps_base = 1.0
                else:
                    scene.render.fps = int(math.ceil(f))
                    scene.render.fps_base = math.ceil(f) / f
                applied.append("fps")
            except ValueError:
                pass
        if s.info_res_w and s.info_res_h:
            try:
                scene.render.resolution_x = int(s.info_res_w)
                scene.render.resolution_y = int(s.info_res_h)
                applied.append("resolution")
            except ValueError:
                pass
        if applied:
            s.status = "Applied to scene: " + ", ".join(applied) + "."
        else:
            s.status = "Nothing to apply (no valid values)."
            self.report({'WARNING'}, s.status)
        return {'FINISHED'}


# ============================================================================
# Panels
# ============================================================================
class SHOTINFO_PT_main(bpy.types.Panel):
    bl_label = "Shot Info"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Shot Info"

    def draw(self, context):
        s = context.scene.shot_info
        layout = self.layout

        # Fetch row (the only network action for sheet data)
        row = layout.row(align=True)
        row.operator("shotinfo.fetch", icon='FILE_REFRESH')
        row.label(text=s.fetch_status)

        # Lookup row (pure local search)
        row = layout.row(align=True)
        row.prop(s, "shot", text="Shot")
        row.operator("shotinfo.auto", text="AUTO")
        row.operator("shotinfo.query", text="Query")

        # Thumbnail
        box = layout.box()
        icon = thumb_icon_id()
        if icon:
            box.template_icon(icon_value=icon, scale=s.thumb_scale)
            box.prop(s, "thumb_scale", text="Size", slider=True)
            box.operator("shotinfo.make_ref", icon='IMAGE_REFERENCE')
        else:
            box.label(text="No thumbnail", icon='IMAGE_DATA')

        # Info -- shown as fields so each value is selectable / copyable
        box = layout.box()
        box.label(text="Shot Info", icon='INFO')
        col = box.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        col.scale_y = 1.2
        col.prop(s, "info_focal", text="Focal (mm)")
        row = col.row(align=True)
        row.prop(s, "info_start", text="Start")
        row.prop(s, "info_end", text="End")
        col.prop(s, "info_fps", text="FPS")
        row = col.row(align=True)
        row.prop(s, "info_res_w", text="Width")
        row.prop(s, "info_res_h", text="Height")

        # Actions
        row = layout.row(align=True)
        row.operator("shotinfo.toggle_hud",
                     text="Hide HUD" if s.hud_on else "Show HUD",
                     icon='OVERLAY', depress=s.hud_on)
        row.operator("shotinfo.apply_scene", icon='SCENE_DATA')

        if s.status:
            layout.label(text=s.status, icon='INFO')


class SHOTINFO_PT_config(bpy.types.Panel):
    bl_label = "Configuration"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Shot Info"
    bl_parent_id = "SHOTINFO_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        s = context.scene.shot_info
        layout = self.layout

        box = layout.box()
        box.label(text="Source")
        box.prop(s, "url", text="URL")
        box.prop(s, "gid", text="gid")

        box = layout.box()
        box.label(text="Column mapping (your header names)")
        for key, label, required in FIELD_DEFS:
            box.prop(s, key, text=label + (" *" if required else ""))

        row = layout.row(align=True)
        row.operator("shotinfo.sanity", icon='CHECKMARK')
        row.operator("shotinfo.save_default", icon='PINNED')

        if s.cfg_status:
            box = layout.box()
            for line in s.cfg_status.split("\n"):
                box.label(text=line)


class SHOTINFO_OT_reset(bpy.types.Operator):
    bl_idname = "shotinfo.reset"
    bl_label = "Reset to Defaults"
    bl_description = ("Reset all parameters to the script defaults (clears the "
                      "URL, gid, column mapping and current shot)")
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        wm = context.window_manager
        try:  # Blender 4.1+ supports a custom message/title
            return wm.invoke_confirm(
                self, event,
                title="Reset Shot Info",
                message="Reset all parameters to script defaults? This clears "
                        "the URL, column mapping and the current shot.",
                confirm_text="Reset", icon='WARNING')
        except TypeError:  # older signature: plain confirm
            return wm.invoke_confirm(self, event)

    def execute(self, context):
        global _ROWS
        s = context.scene.shot_info
        apply_cfg_to_settings(s, DEFAULT_CONFIG)  # url/gid/column mapping
        s.shot = ""
        _clear_info(s)                            # info fields + thumbnail
        _ROWS = None                              # drop the fetched cache
        s.fetch_status = "Not fetched"
        s.cfg_status = ""
        s.status = "Reset to script defaults."
        return {'FINISHED'}


class SHOTINFO_PT_reset(bpy.types.Panel):
    bl_label = "Reset"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Shot Info"
    bl_parent_id = "SHOTINFO_PT_config"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.label(text="Restore all settings to script defaults.", icon='ERROR')
        row = col.row()
        row.alert = True  # renders the button red
        row.operator("shotinfo.reset", text="Reset to Defaults", icon='TRASH')


# ============================================================================
# File-load handling (restore state, refresh session caches)
# ============================================================================
def _on_file_ready():
    global _ROWS
    _ROWS = None  # a freshly loaded file may point at a different sheet
    scene = getattr(bpy.context, "scene", None)
    if scene is None:  # restricted/odd context -> fall back to data
        scene = bpy.data.scenes[0] if bpy.data.scenes else None
    if not scene or not hasattr(scene, "shot_info"):
        return
    s = scene.shot_info
    # If this file has no source configured, seed it from the global default.
    if not (s.url or "").strip():
        apply_cfg_to_settings(s, load_default_config())
    # Restore the thumbnail preview from the path stored in the file.
    if s.thumb_path and os.path.isfile(s.thumb_path):
        _load_preview_from_path(s.thumb_path)
    elif _PREVIEWS:
        _PREVIEWS.clear()
    # Re-arm the HUD if it was on when the file was saved (draw handlers and
    # the live overlay don't survive a file load).
    if s.hud_on:
        hud_enable_draw()
        hud_refresh(s)
    else:
        hud_disable_draw()
    s.fetch_status = "Not fetched"


@persistent
def _load_post_handler(dummy):
    _on_file_ready()


# ============================================================================
# Registration
# ============================================================================
_CLASSES = (
    ShotInfoSettings,
    SHOTINFO_OT_fetch, SHOTINFO_OT_query, SHOTINFO_OT_auto,
    SHOTINFO_OT_sanity, SHOTINFO_OT_save_default, SHOTINFO_OT_make_ref,
    SHOTINFO_OT_toggle_hud, SHOTINFO_OT_apply_scene, SHOTINFO_OT_reset,
    SHOTINFO_PT_main, SHOTINFO_PT_config, SHOTINFO_PT_reset,
)


def _deferred_init():
    # Runs just after register(), when a real (non-restricted) context exists.
    try:
        _on_file_ready()
    except Exception:
        pass
    return None  # one-shot


def register():
    global _PREVIEWS
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.shot_info = PointerProperty(type=ShotInfoSettings)
    _PREVIEWS = bpy.utils.previews.new()
    if _load_post_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post_handler)
    # Do NOT touch context.scene here -- the context is restricted during
    # register(). Defer the initial pass to a one-shot timer instead.
    bpy.app.timers.register(_deferred_init, first_interval=0.0)


def unregister():
    global _PREVIEWS
    if bpy.app.timers.is_registered(_deferred_init):
        bpy.app.timers.unregister(_deferred_init)
    hud_disable_draw()
    if _load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post_handler)
    if _PREVIEWS is not None:
        bpy.utils.previews.remove(_PREVIEWS)
        _PREVIEWS = None
    del bpy.types.Scene.shot_info
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()

#!/usr/bin/env python3
"""
signal_sticker_gui.py — local/served browser GUI for the Signal sticker converter.

Multi-session, hardened for use behind a reverse proxy (e.g. Cloudflare Access
handles authentication; this app does not). Security posture:

  * Sessions are disk-backed under WORK_DIR/<sid>/ (sid = uuid4 hex, regex-checked
    on every request -> no path traversal). State is on disk, not in memory, so a
    worker restart doesn't strand uploads and a reaper can clean by directory age.
  * Strict input validation, server-side and authoritative:
      - upload size capped (MAX_CONTENT_LENGTH -> clean 413 JSON);
      - type confirmed by ffprobe, not just extension;
      - resolution capped (MAX_EDGE / MAX_PIXELS);
      - framing params clamped (scaled source dimension bounded -> ffmpeg can't be
        told to allocate a giant buffer);
      - pad colour must be 'transparent' or #rrggbb -> it flows into the ffmpeg
        filtergraph, so this closes a filter-injection vector;
      - trim window clamped to [0, duration] and <= 3 s.
  * Resource isolation: a BoundedSemaphore caps concurrent conversions (excess
    requests wait briefly then get 429); every external process has a timeout
    (504 on overrun); a disk-space guard rejects uploads when storage is low.
  * Client-side mirror checks (size/type/resolution) using limits fetched from the
    server, so oversized media is rejected before it is ever uploaded.

Run locally:   python3 signal_sticker_gui.py        (opens your browser)
Run served:    gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 signal_sticker_gui:app
Files needed beside it: signal_sticker.py, sticker_core.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, request, jsonify, send_file, Response

import sticker_core as core


# ----------------------------- configuration --------------------------------
def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


_wd = os.environ.get("WORK_DIR")
WORK_DIR = Path(_wd) if _wd else Path(tempfile.mkdtemp(prefix="signal_sticker_"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 100)      # also Cloudflare's free-plan ceiling
MAX_PIXELS = _int("MAX_PIXELS", 40_000_000)     # ~40 MP per frame
MAX_EDGE = _int("MAX_EDGE", 8192)               # max width or height
MAX_CONCURRENT = _int("MAX_CONCURRENT", 2)      # simultaneous conversions
QUEUE_TIMEOUT_S = _int("QUEUE_TIMEOUT_S", 20)   # wait for a slot before 429
SESSION_TTL_MIN = _int("SESSION_TTL_MIN", 20)   # reaper age
MIN_FREE_MB = _int("MIN_FREE_MB", 512)          # disk guard
MAX_SCALED_EDGE = _int("MAX_SCALED_EDGE", 8192) # clamp scaled source dimension

SID_RE = re.compile(r"^[0-9a-f]{32}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ALLOWED = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".mp4", ".mov",
           ".webm", ".mkv", ".m4v", ".avi"}


def _csv_env(name: str, default: str) -> set[str]:
    return {x.strip().lower() for x in os.environ.get(name, default).split(",") if x.strip()}


# Content-based allowlists (ffprobe-reported), checked on upload. The filename extension
# is attacker-controlled, so we also gate the *decoded codec* and *detected container* to
# the handful we actually support — keeping crafted inputs away from ffmpeg's obscure
# demuxers/decoders, where the bulk of its CVEs live. Override via env if needed.
ALLOWED_CODECS = _csv_env(
    "STICKER_ALLOWED_CODECS",
    "h264,hevc,av1,vp8,vp9,mpeg4,mjpeg,gif,png,apng,bmp,webp")
ALLOWED_CONTAINERS = _csv_env(
    "STICKER_ALLOWED_CONTAINERS",
    "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,gif,image2,"
    "png_pipe,apng,jpeg_pipe,mjpeg,webp_pipe,bmp_pipe")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

_sem = threading.BoundedSemaphore(MAX_CONCURRENT)


# ------------------------------ session store -------------------------------
def sess_dir(sid: str) -> Path:
    return WORK_DIR / sid


def load_meta(sid: str) -> dict | None:
    p = sess_dir(sid) / "session.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def save_meta(sid: str, meta: dict) -> None:
    (sess_dir(sid) / "session.json").write_text(json.dumps(meta))


def touch(sid: str) -> None:
    try:
        os.utime(sess_dir(sid), None)  # keep active sessions out of the reaper
    except OSError:
        pass


def reap_once() -> int:
    """Delete sessions older than the TTL. Returns count removed."""
    cutoff = time.time() - SESSION_TTL_MIN * 60
    removed = 0
    try:
        for d in WORK_DIR.iterdir():
            if d.is_dir() and SID_RE.match(d.name) and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
    except OSError:
        pass
    return removed


def _reaper() -> None:
    while True:
        reap_once()
        time.sleep(60)


threading.Thread(target=_reaper, daemon=True).start()


# ------------------------------ error handlers ------------------------------
@app.errorhandler(413)
def _too_big(_e):
    return jsonify(error=f"File exceeds the {MAX_UPLOAD_MB} MB upload limit."), 413


@app.errorhandler(400)
def _bad(_e):
    return jsonify(error="Bad request."), 400


# --------------------------------- routes -----------------------------------
@app.get("/health")
def health() -> Response:
    return Response("ok", mimetype="text/plain")


@app.get("/")
def index() -> Response:
    return Response(PAGE, mimetype="text/html")


@app.get("/api/config")
def api_config():
    return jsonify(max_upload_mb=MAX_UPLOAD_MB, max_pixels=MAX_PIXELS,
                   max_edge=MAX_EDGE, max_seconds=core.MAX_SECONDS,
                   fonts=list(core.FONTS.keys()), default_font=core.DEFAULT_FONT,
                   out_canvas=core.OUT_CANVAS, canvas=core.CANVAS,
                   text_bottom=core.TEXT_BOTTOM_MARGIN, text_side=core.TEXT_SIDE_MARGIN)


@app.post("/api/open")
def api_open():
    try:
        if shutil.disk_usage(WORK_DIR).free < MIN_FREE_MB * 1024 * 1024:
            return jsonify(error="Server storage is low; please try again later."), 503
    except OSError:
        pass
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="No file provided."), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED:
        return jsonify(error=f"Unsupported file type '{ext}'."), 400

    sid = uuid.uuid4().hex
    d = sess_dir(sid)
    d.mkdir(parents=True)
    src = d / ("input" + ext)
    try:
        f.save(src)
    except OSError:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error="Could not store the upload."), 500

    try:
        meta = core.probe(src)                 # ffprobe == content-based type check
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error="Could not read this file as an image or video."), 400

    w, h = int(meta["width"]), int(meta["height"])
    if w <= 0 or h <= 0:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error="No decodable image/video stream found."), 400

    # Content-based allowlist: the *actual* codec/container (not the filename) must be
    # one we support. Blocks files whose bytes don't match their extension (e.g. a .mp4
    # that is really a crafted .ts) from reaching an unexpected ffmpeg demuxer/decoder.
    codec = str(meta.get("codec", ""))
    containers = {c for c in str(meta.get("container", "")).split(",") if c}
    if codec and codec not in ALLOWED_CODECS:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error=f"Unsupported video codec '{codec}'."), 400
    if containers and containers.isdisjoint(ALLOWED_CONTAINERS):
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error="Unsupported container format."), 400
    if max(w, h) > MAX_EDGE or w * h > MAX_PIXELS:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error=f"Resolution {w}x{h} exceeds the limit "
                             f"({MAX_EDGE}px edge / {MAX_PIXELS // 1_000_000}MP)."), 400

    try:
        pad = core.dominant_edge(src)
    except Exception:
        pad = "transparent"

    save_meta(sid, {"ext": ext, "pad": pad, **meta})
    return jsonify(id=sid, pad=pad, **meta)


FPS_MIN, FPS_MAX = 3, 15
SAFETY_KB = core.ss.SAFETY / 1024
PREVIEW_KEYS = {"softer", "fewer", "balanced"}


def _parse_job(d: dict):
    """Validate session + framing + fps shared by /api/analyze and /api/convert.
    Returns (ctx, None) or (None, (response, code)). Every value that reaches the
    ffmpeg filtergraph is parsed/clamped here (the server is authoritative)."""
    sid = d.get("id", "")
    if not isinstance(sid, str) or not SID_RE.match(sid):
        return None, (jsonify(error="Invalid session id."), 400)
    meta = load_meta(sid)
    if not meta:
        return None, (jsonify(error="Session expired or not found; re-open the file."), 400)
    src = sess_dir(sid) / ("input" + meta["ext"])
    if not src.exists():
        return None, (jsonify(error="Session data missing; re-open the file."), 400)

    W, H = int(meta["width"]), int(meta["height"])
    try:
        scale = float(d.get("scale")); ox = float(d.get("ox")); oy = float(d.get("oy"))
    except (TypeError, ValueError):
        return None, (jsonify(error="Invalid framing parameters."), 400)
    if scale != scale or scale <= 0 or ox != ox or oy != oy:  # NaN / non-positive
        return None, (jsonify(error="Invalid framing parameters."), 400)
    scale = min(scale, MAX_SCALED_EDGE / max(W, H))
    scale = max(scale, 1.0 / max(W, H))
    ox = max(-100000.0, min(100000.0, ox))
    oy = max(-100000.0, min(100000.0, oy))

    pad = d.get("pad", "transparent")                  # STRICT: flows into filtergraph
    if pad != "transparent" and not HEX_RE.match(str(pad)):
        return None, (jsonify(error="Invalid pad colour."), 400)

    animated = bool(d.get("animated")) and bool(meta.get("animated"))
    dur = float(meta.get("duration") or 0.0)
    trim = None
    if animated and dur > core.MAX_SECONDS + 1e-3:
        try:
            ts = float(d.get("trim_start") or 0.0); td = float(d.get("trim_dur") or 0.0)
        except (TypeError, ValueError):
            return None, (jsonify(error="Invalid trim values."), 400)
        ts = max(0.0, min(ts, max(0.0, dur - 0.1)))
        td = max(0.1, min(td, core.MAX_SECONDS, dur - ts))
        trim = (ts, td)

    try:
        fps = int(d.get("fps", FPS_MAX))
    except (TypeError, ValueError):
        return None, (jsonify(error="Invalid fps."), 400)
    fps = max(FPS_MIN, min(FPS_MAX, fps))

    # boomerang loop: only meaningful for animation with room left under 3 s
    fwd = (trim[1] if trim else min(dur, core.MAX_SECONDS))
    loop = bool(d.get("loop")) and animated and fwd < core.MAX_SECONDS - 1e-3

    # text overlay: drawn with Pillow (no filtergraph), font from server whitelist,
    # colours strict #rrggbb, sizes clamped, text length/lines bounded.
    text = None
    raw = d.get("text")
    if isinstance(raw, str) and raw.strip():
        lines = raw.replace("\r\n", "\n").split("\n")[:core.MAX_TEXT_LINES]
        body = "\n".join(lines)[:core.MAX_TEXT_LEN]
        font = d.get("font")
        if font not in core.FONTS:
            font = core.DEFAULT_FONT
        if font:                                  # only if a font is actually present
            def _int(v, lo, hi, dflt):
                try:
                    return max(lo, min(hi, int(float(v))))
                except (TypeError, ValueError):
                    return dflt
            color = d.get("color", "#ffffff")
            ocolor = d.get("outline_color", "#000000")
            if not HEX_RE.match(str(color)):
                color = "#ffffff"
            if not HEX_RE.match(str(ocolor)):
                ocolor = "#000000"
            align = d.get("align", "center")
            if align not in ("left", "center", "right"):
                align = "center"
            text = dict(text=body, font=font, size=_int(d.get("size"), 6, 160, 32),
                        outline_w=_int(d.get("outline_w"), 0, 20, 2),
                        align=align, color=str(color), outline_color=str(ocolor))

    return dict(sid=sid, src=src, W=W, H=H, scale=scale, ox=ox, oy=oy,
                pad=str(pad), animated=animated, trim=trim, fps=fps, loop=loop,
                text=text), None


def _cleanup_intermediates(sd: Path) -> None:
    """Drop extracted frames, per-detail caches, quant scratch and preview tiles;
    keep input + sticker.png."""
    for p in list(sd.glob("f_*.png")) + list(sd.glob("preview_*.png")):
        try:
            p.unlink()
        except OSError:
            pass
    for sub in (list(sd.glob("f[0-9]*")) + list(sd.glob("txt*"))
                + [sd / "q", sd / "qfinal", sd / "_est", sd / "sp"]):
        if sub.is_dir():
            shutil.rmtree(sub, ignore_errors=True)


def _extract_base(ctx: dict):
    return core._extract_detail(
        ctx["src"], sess_dir(ctx["sid"]), width=ctx["W"], height=ctx["H"],
        scale=ctx["scale"], ox=ctx["ox"], oy=ctx["oy"], pad=ctx["pad"],
        animated=ctx["animated"], sample_fps=ctx["fps"], trim=ctx["trim"], detail=1.0,
        loop=ctx["loop"], text=ctx["text"])


def _calibrate(ctx: dict, base: list):
    """Per-clip calibration: encode one real ffmpeg-direct APNG at 256 colours and
    compare to the raw per-frame sample estimate. The shared-palette + inter-frame
    encode's size/sample ratio swings ~0.4 (static bg) to ~0.9 (full motion), so a
    fixed factor can't predict size — this anchor measures it per clip. Returns
    (cal, anchor_bytes, anchor_kb); the anchor doubles as the 256-colour result if
    it already fits."""
    sd = sess_dir(ctx["sid"])
    raw = core.raw_sample_kb(base, sd, 256)
    anchor = core.ss.assemble_shared_palette(base, ctx["fps"], 256, sd)
    kb = len(anchor) / 1024
    # The ratio rises toward low colour counts (the per-frame no-dither sample sum
    # shrinks faster than the real bayer-dithered encode), so bias the anchor ratio
    # up ~15% — the search predicts a touch conservative and rarely overshoots into a
    # retry. (The round-based retry still catches anything that slips through.)
    cal = 1.15 * kb / raw if raw > 0 else 1.0
    return cal, anchor, kb


def _build_options(ctx: dict, target_kb: float, cal: float | None = None,
                   base: list | None = None) -> list[dict]:
    """Search the three strategies, render a preview tile for each that fits, and
    return client-facing dicts with preview URLs."""
    sd = sess_dir(ctx["sid"])
    if base is None:
        base = _extract_base(ctx)
    if cal is None:
        cal, _, _ = _calibrate(ctx, base)
    opts = core.search_options(
        ctx["src"], sd, base, width=ctx["W"], height=ctx["H"], scale=ctx["scale"],
        ox=ctx["ox"], oy=ctx["oy"], pad=ctx["pad"], animated=ctx["animated"],
        sample_fps=ctx["fps"], trim=ctx["trim"], target_kb=target_kb, cal=cal,
        loop=ctx["loop"], text=ctx["text"])
    out = []
    for o in opts:
        if o["est_kb"] > target_kb:
            continue
        pv = sd / f"preview_{o['key']}.png"
        core.render_preview(Path(o["heavy"]), pv, o["colors"])
        out.append(dict(key=o["key"], label=o["label"], detail=o["detail"],
                        colors=o["colors"], est_kb=o["est_kb"],
                        url=f"/preview/{ctx['sid']}/{o['key']}?v={uuid.uuid4().hex[:8]}"))
    return out


@app.post("/api/analyze")
def api_analyze():
    """Predict whether the clip fits at full quality for the chosen fps. If it
    does, convert straight away. If not, return preview tiles to choose from (or
    ask for a lower fps when even the floor won't fit)."""
    ctx, err = _parse_job(request.get_json(silent=True) or {})
    if err:
        return err
    sd = sess_dir(ctx["sid"])
    if not _sem.acquire(timeout=QUEUE_TIMEOUT_S):
        return jsonify(error="Server is busy; please retry in a moment."), 429
    try:
        touch(ctx["sid"])
        base = _extract_base(ctx)
        if not ctx["animated"] or len(base) <= 1:
            # static -> single pngquant PNG (smooth alpha, no flicker possible)
            out = sd / "sticker.png"
            stats = core.encode_choice(
                ctx["src"], sd, out, width=ctx["W"], height=ctx["H"],
                scale=ctx["scale"], ox=ctx["ox"], oy=ctx["oy"], pad=ctx["pad"],
                animated=ctx["animated"], sample_fps=ctx["fps"], trim=ctx["trim"],
                detail=1.0, colors=256, text=ctx["text"])
            _cleanup_intermediates(sd)
            return jsonify(fits=True, url=f"/dl/{ctx['sid']}?v={uuid.uuid4().hex[:8]}",
                           name="sticker.png", over_budget=not stats["ok"], **stats)
        # animated: one real 256-colour encode both calibrates AND is the result if
        # it already fits (full quality, no prompt).
        cal, anchor, akb = _calibrate(ctx, base)
        if akb <= SAFETY_KB:
            out = sd / "sticker.png"; out.write_bytes(anchor)
            _cleanup_intermediates(sd)
            stats = dict(bytes=len(anchor), kb=round(akb, 1), fps=ctx["fps"],
                         colors=256, frames=len(base), animated=True, detail=1.0,
                         engine="shared-palette+oxipng+apng", ok=True)
            return jsonify(fits=True, url=f"/dl/{ctx['sid']}?v={uuid.uuid4().hex[:8]}",
                           name="sticker.png", over_budget=False, **stats)
        options = _build_options(ctx, SAFETY_KB, cal=cal, base=base)
        if not options:
            return jsonify(fits=False, need_lower_fps=True, fps=ctx["fps"],
                           predicted_kb=round(akb, 1))
        return jsonify(fits=False, fps=ctx["fps"], predicted_kb=round(akb, 1),
                       options=options)
    except subprocess.TimeoutExpired:
        return jsonify(error="Analysis timed out; try a shorter clip or lower fps."), 504
    except Exception as e:
        return jsonify(error=f"Analysis failed: {e}"), 500
    finally:
        _sem.release()


@app.post("/api/convert")
def api_convert():
    """Encode the full clip at an explicit (detail, colors, fps). If it lands
    under budget, return the sticker; otherwise re-predict harsher tiles from the
    actual size (or ask for a lower fps when nothing fits)."""
    d = request.get_json(silent=True) or {}
    ctx, err = _parse_job(d)
    if err:
        return err
    try:
        detail = float(d.get("detail", 1.0)); colors = int(d.get("colors", 256))
    except (TypeError, ValueError):
        return jsonify(error="Invalid quality parameters."), 400
    if detail != detail or not (0.0 < detail <= 1.0):
        return jsonify(error="Invalid detail."), 400
    detail = max(0.05, min(1.0, detail))
    colors = max(2, min(256, colors))
    try:
        rnd = int(d.get("round", 1))
    except (TypeError, ValueError):
        rnd = 1
    rnd = max(1, min(8, rnd))

    sd = sess_dir(ctx["sid"])
    if not _sem.acquire(timeout=QUEUE_TIMEOUT_S):
        return jsonify(error="Server is busy; please retry in a moment."), 429
    try:
        touch(ctx["sid"])
        out = sd / "sticker.png"
        stats = core.encode_choice(
            ctx["src"], sd, out, width=ctx["W"], height=ctx["H"], scale=ctx["scale"],
            ox=ctx["ox"], oy=ctx["oy"], pad=ctx["pad"], animated=ctx["animated"],
            sample_fps=ctx["fps"], trim=ctx["trim"], detail=detail, colors=colors,
            loop=ctx["loop"], text=ctx["text"])
        if stats["ok"]:
            _cleanup_intermediates(sd)
            return jsonify(url=f"/dl/{ctx['sid']}?v={uuid.uuid4().hex[:8]}",
                           name="sticker.png", over_budget=False, **stats)
        # Predictions are accurate (~+/-4%), so this is a rare safety net. Tighten
        # the target gently each round so the next pick is strictly harsher and the
        # loop always converges (-> tiles, or -> need_lower_fps at the floor).
        nxt = rnd + 1
        options = _build_options(ctx, SAFETY_KB * (0.9 ** rnd))
        if not options:
            return jsonify(ok=False, need_lower_fps=True, fps=ctx["fps"],
                           actual_kb=stats["kb"])
        return jsonify(ok=False, fps=ctx["fps"], actual_kb=stats["kb"],
                       round=nxt, options=options)
    except subprocess.TimeoutExpired:
        return jsonify(error="Conversion timed out; try a shorter clip or lower fps."), 504
    except Exception as e:
        return jsonify(error=f"Conversion failed: {e}"), 500
    finally:
        _sem.release()


@app.get("/preview/<sid>/<key>")
def preview(sid: str, key: str):
    if not SID_RE.match(sid or "") or key not in PREVIEW_KEYS:
        return "bad id", 400
    p = sess_dir(sid) / f"preview_{key}.png"
    if not p.exists():
        return "not found", 404
    touch(sid)
    return send_file(p, mimetype="image/png")


@app.get("/dl/<sid>")
def dl(sid: str):
    if not SID_RE.match(sid or ""):
        return "bad id", 400
    out = sess_dir(sid) / "sticker.png"
    if not out.exists():
        return "not found", 404
    touch(sid)
    return send_file(out, mimetype="image/png",
                     as_attachment=request.args.get("dl") == "1",
                     download_name="sticker.png")


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>catsec · Signal Sticker Studio</title>
<style>
:root{--card:#ffffff;--line:#e6e6ef;--fg:#333;--mut:#666;
--acc:#28a745;--acc-d:#218838;--acc2:#667eea;
--grad:linear-gradient(135deg,#667eea 0%,#764ba2 100%);--danger:#d63333;
--shadow:0 10px 30px rgba(40,30,90,.18)}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
background:var(--grad) fixed;color:var(--fg);min-height:100vh}
header{padding:18px 22px;display:flex;align-items:center;gap:10px;color:#fff}
header b{font-size:16px;letter-spacing:.3px}
header span{color:#ffffffcc;font-size:12px}
header button{background:#ffffff2b;color:#fff;border:1px solid #ffffff66}
header button:hover{background:#ffffff40;border-color:#fff;color:#fff}
main{max-width:980px;margin:0 auto;padding:8px 20px 60px}
.drop{background:var(--card);border:2px dashed #c9cce0;border-radius:16px;padding:52px 20px;text-align:center;
color:var(--mut);cursor:pointer;transition:.15s;box-shadow:var(--shadow)}
.drop:hover,.drop.hot{border-color:var(--acc2);color:var(--fg);background:#f7f8fc}
.drop b{color:var(--fg)}
.hide{display:none!important}
.wrap{display:grid;grid-template-columns:minmax(0,1fr);gap:22px;align-items:start}
.wrap.editing{grid-template-columns:minmax(0,1fr) 320px}
@media(max-width:820px){.wrap.editing{grid-template-columns:1fr}}
.dropov{position:absolute;inset:0;z-index:2;display:flex;align-items:center;justify-content:center;text-align:center;
cursor:pointer;background:var(--card);border:2px dashed #c9cce0;border-radius:10px;transition:.15s;padding:16px}
.dropov:hover,.dropov.hot{border-color:var(--acc2);background:#f7f8fc}
.dropov b{color:var(--fg)}
.stagebox{background:var(--card);border-radius:16px;padding:16px;box-shadow:var(--shadow)}
.stage{position:relative;width:100%;max-width:460px;aspect-ratio:1;margin:0 auto;border-radius:10px;overflow:hidden;
border:1px solid var(--line);touch-action:none;cursor:grab}
.stage.drag{cursor:grabbing}
canvas{display:block;width:100%;height:100%}
.framehint{position:absolute;inset:0;pointer-events:none;box-shadow:inset 0 0 0 1px #00000014}
.panel{background:var(--card);border-radius:16px;padding:18px;margin-bottom:16px;box-shadow:var(--shadow)}
.panel h3{margin:0 0 12px;font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--mut)}
.row{display:flex;align-items:center;gap:10px;margin:10px 0}
.row label{min-width:54px;color:var(--mut);font-size:12px}
input[type=range]{flex:1;accent-color:var(--acc2)}
button{font:inherit;color:var(--fg);background:#f1f2f7;border:1px solid var(--line);border-radius:8px;
padding:8px 12px;cursor:pointer;transition:.15s}
button:hover{border-color:var(--acc2);color:var(--acc2)}
button.acc{background:var(--grad);border:none;color:#fff;font-weight:600;width:100%;padding:13px;
box-shadow:0 4px 14px rgba(102,126,234,.35)}
button.acc:hover{transform:translateY(-2px);box-shadow:0 6px 18px rgba(102,126,234,.45);color:#fff}
button.acc:disabled{opacity:.55;cursor:not-allowed;transform:none;box-shadow:none}
button.ghost{flex:1}
#dlBtn{background:var(--acc);border:none;color:#fff;font-weight:600}
#dlBtn:hover{background:var(--acc-d);color:#fff}
.btns{display:flex;gap:8px}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{flex:1;border:0;border-radius:0;background:#f1f2f7}
.seg button:hover{color:var(--fg)}
.seg button.on{background:var(--acc2);color:#fff}
.swatch{width:18px;height:18px;border-radius:4px;border:1px solid #0002;display:inline-block;vertical-align:-3px}
.muted{color:var(--mut);font-size:12px}
.err{color:var(--danger);font-size:13px;margin-top:8px;min-height:1px}
.tl{position:relative;height:46px;background:#eef0f7;border:1px solid var(--line);border-radius:8px;margin:6px 0;touch-action:none}
.tl .sel{position:absolute;top:0;bottom:0;background:#667eea33;border-left:3px solid var(--acc2);border-right:3px solid var(--acc2)}
.tl .h{position:absolute;top:0;bottom:0;width:14px;margin-left:-7px;cursor:ew-resize}
.tl .play{position:absolute;top:0;bottom:0;width:2px;background:var(--acc);left:0;display:none}
.tcs{display:flex;justify-content:space-between;font-variant-numeric:tabular-nums;color:var(--mut);font-size:12px}
.result{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.result .chk{width:160px;height:160px;border:1px solid var(--line);border-radius:8px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;border:1px solid var(--line)}
.pill.ok{color:var(--acc);border-color:var(--acc)}.pill.bad{color:var(--danger);border-color:var(--danger)}
.chkbg{background-image:linear-gradient(45deg,#dcdce6 25%,transparent 25%),linear-gradient(-45deg,#dcdce6 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#dcdce6 75%),linear-gradient(-45deg,transparent 75%,#dcdce6 75%);background-size:18px 18px;background-position:0 0,0 9px,9px -9px,-9px 0px;background-color:#f4f4f8}
.spin{display:inline-block;width:13px;height:13px;border:2px solid #fff6;border-top-color:#fff;border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px;margin-right:6px}
@keyframes s{to{transform:rotate(360deg)}}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:4px}
.tile{border:1px solid var(--line);border-radius:10px;padding:8px;cursor:pointer;text-align:center;background:#f7f8fc;transition:.12s}
.tile:hover{border-color:var(--acc2);background:#eef1fb}
.tile.on{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc)}
.tile img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:6px;background:#eee;display:block}
.tile .lab{font-weight:600;margin-top:7px}
.tile .est{color:var(--mut);font-size:12px}
.tile.busy{opacity:.5;pointer-events:none}
@media(max-width:560px){.tiles{grid-template-columns:1fr}}
textarea#txt{width:100%;background:#fff;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px;font:inherit;resize:vertical;margin-bottom:4px}
textarea#txt:focus{outline:none;border-color:var(--acc2)}
select#font{background:#fff;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px}
input[type=color]{width:34px;height:26px;padding:0;border:1px solid var(--line);border-radius:6px;background:#fff;cursor:pointer}
#tiphelp{position:fixed;z-index:50;max-width:250px;background:#1f2330;color:#fff;font-size:12px;line-height:1.45;
padding:8px 11px;border-radius:8px;box-shadow:0 10px 28px rgba(0,0,0,.3);pointer-events:none;opacity:0;transition:opacity .12s}
#tiphelp.on{opacity:1}
</style></head><body>
<header><b>catsec · Signal Sticker Studio</b><span id=limnote></span>
  <span style="margin-left:auto;display:flex;gap:8px">
    <button id=restartBtn class="ghost hide" data-help="Reset framing, trim, animation and text back to defaults for THIS file (keeps the file loaded).">↻ Restart</button>
    <button id=newBtn class="ghost hide" data-help="Clear everything and start over with a different file.">＋ New conversion</button>
  </span></header>
<main>
  <div id=editor class=wrap>
    <div class=stagebox>
      <div id=stage class="stage chkbg" data-help="This is the 512×512 sticker frame. Drag the image to reposition it; scroll to zoom.">
        <canvas id=cv width=512 height=512></canvas>
        <div class=framehint></div>
        <div id=drop class=dropov data-help="Click to choose a photo or video, or drag a file straight onto this box.">
          <div>
            <p><b>Choose a photo or video</b></p>
            <p class=muted>or drop it here</p>
            <p class=muted style="margin-top:6px">gif · png · jpg · webp · mp4 · mov · webm</p>
            <p id=dropErr class=err></p>
          </div>
        </div>
      </div>
      <input id=file type=file accept="image/*,video/*" class=hide>
      <p id=stagehint class="muted hide" style="text-align:center;margin:10px 0 0">drag to move · scroll or slider to zoom</p>
    </div>
    <div id=controls class=hide>
      <div class=panel>
        <h3>Step 1 · Framing</h3>
        <div class=row><label>Zoom</label><input id=zoom type=range min=0 max=1000 value=500 data-help="Zoom the image in or out within the frame. You can also scroll over the preview."></div>
        <div class=btns><button class=ghost id=fit data-help="Fit the whole image inside the frame — nothing is cropped (may leave padded edges).">Fit</button><button class=ghost id=fill data-help="Scale the image to fill the whole frame — edges that overflow get cropped.">Fill</button><button class=ghost id=reset data-help="Reset zoom and position to the default framing.">Reset</button></div>
        <div class=row style="margin-top:14px"><label>Edges</label>
          <div class=seg style=flex:1>
            <button id=padDom class=on data-help="Fill any empty edges with the auto-detected dominant colour from the image."><span id=sw class=swatch></span> Dominant</button>
            <button id=padTrans data-help="Make any empty edges transparent instead of filled with colour.">Transparent</button>
          </div>
        </div>
        <p class=muted id=padnote></p>
      </div>
      <div id=trimPanel class="panel hide">
        <h3>Step 2 · Trim <span class=muted>(clip &gt; 3s)</span></h3>
        <div id=tl class=tl data-help="Drag the two handles to pick which part of the clip to keep — Signal stickers can be at most 3 seconds." ><div id=sel class=sel><div id=hL class=h style=left:0></div><div id=hR class=h style=right:0></div></div><div id=ph class=play></div></div>
        <div class=tcs><span id=tStart>0.00s</span><span id=tDur>0.00s window</span><span id=tEnd>0.00s</span></div>
        <div class=btns style=margin-top:8px><button id=playBtn class=ghost data-help="Play just the selected part on a loop so you can preview it.">▶ Preview loop</button></div>
      </div>
      <div id=animPanel class="panel hide">
        <h3>Step 3 · Animation</h3>
        <div class=row><label>Frame&nbsp;rate</label><input id=fps type=range min=3 max=15 value=15 data-help="Frames per second. Higher looks smoother but makes a bigger file — if the sticker won't fit under 300KB, lower this first.">
          <span id=fpsv class=muted style="min-width:48px;text-align:right;font-variant-numeric:tabular-nums">15 fps</span></div>
        <p class=muted>Higher is smoother but larger. If a clip won’t fit, lower this.</p>
        <div class=row style="margin-top:4px"><label style="min-width:auto;color:var(--fg)" data-help="Append the clip played in reverse so it loops back seamlessly. Needs the trimmed clip under 3s.">
          <input type=checkbox id=loop> Loop (boomerang)</label></div>
        <p class=muted id=loopnote>Plays forward then reverse for a seamless loop.</p>
      </div>
      <div id=textPanel class=panel>
        <h3>Text <span class=muted>(optional · sits 20px from the bottom)</span></h3>
        <textarea id=txt rows=2 placeholder="Caption — Enter for a new line" data-help="Optional caption baked into the sticker near the bottom. Press Enter for a new line. Leave empty for no text."></textarea>
        <div class=row><label>Font</label><select id=font data-help="Font for the caption (only server-installed fonts are offered)."></select></div>
        <div class=row><label>Size</label><input id=tsize type=range min=8 max=120 value=32 data-help="Caption text size, in output pixels."><span id=tsizev class=muted style="min-width:28px;text-align:right">32</span></div>
        <div class=row><label>Outline</label><input id=tout type=range min=0 max=20 value=2 data-help="Thickness of the outline drawn around the text for legibility (0 = no outline)."><span id=toutv class=muted style="min-width:28px;text-align:right">2</span></div>
        <div class=row><label>Align</label><div class=seg style=flex:1 data-help="Horizontal alignment of the caption.">
          <button id=alL>Left</button><button id=alC class=on>Center</button><button id=alR>Right</button></div></div>
        <div class=row><label>Colour</label>
          <input type=color id=tcol value="#ffffff" data-help="Fill colour of the caption text."><span class=muted>text</span>
          <input type=color id=tocol value="#000000" data-help="Colour of the text outline."><span class=muted>outline</span></div>
      </div>
      <div class=panel>
        <button id=go class=acc data-help="Render the Signal-ready sticker (APNG if animated, PNG if static) under the 300KB limit. If it can't fit at full quality you'll be offered quality options.">Convert to sticker</button>
        <div id=err class=err></div>
      </div>
      <div id=qualPanel class="panel hide">
        <h3>Choose quality</h3>
        <p id=qualMsg class=muted></p>
        <div id=tiles class=tiles data-help="The sticker didn't fit at full quality. Each tile is a different trade-off (softer detail, fewer colours, or balanced) that does fit — click one to render it."></div>
      </div>
      <div id=resPanel class="panel hide">
        <h3>Result</h3>
        <div class=result>
          <img id=resImg class="chk chkbg" alt="">
          <div><div id=resStat></div>
            <div class=btns style=margin-top:10px><button id=dlBtn class=ghost data-help="Download the finished sticker file to add to Signal.">⬇ Download</button><button id=again class=ghost data-help="Clear this result and start over with a new file.">New file</button></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</main>
<script>
const $=s=>document.querySelector(s);
const cv=$('#cv'),ctx=cv.getContext('2d');
let S=null,media=null,isVideo=false,curRound=1;
let CFG={max_upload_mb:100,max_pixels:40000000,max_edge:8192,max_seconds:3,
  fonts:['Sans'],default_font:'Sans',out_canvas:256,canvas:512,text_bottom:20,text_side:16};
const FONT_CSS={'Impact':'Impact, Haettenschweiler, sans-serif','Sans Bold':'bold sans-serif',
  'Sans':'sans-serif','Serif':'serif','Mono':'monospace','Comic':'"Comic Sans MS", cursive'};
(async()=>{try{CFG=await (await fetch('/api/config')).json();}catch(e){}
  $('#limnote').textContent=`256² animated · 512² static · ≤300KB · ≤${CFG.max_seconds}s · upload ≤${CFG.max_upload_mb}MB`;
  const fsel=$('#font');fsel.innerHTML='';(CFG.fonts||[]).forEach(f=>{const o=document.createElement('option');o.value=o.textContent=f;fsel.appendChild(o);});
  if(CFG.default_font)fsel.value=CFG.default_font;})();

function dropErr(m){$('#dropErr').textContent=m||''}
function err(m){$('#err').textContent=m||'';if(m)dropErr(m)}
function reset(){if(media&&media.tagName==='VIDEO')media.pause();S=null;media=null;
  $('#drop').classList.remove('hide');$('#controls').classList.add('hide');$('#editor').classList.remove('editing');
  $('#stagehint').classList.add('hide');$('#resPanel').classList.add('hide');
  $('#newBtn').classList.add('hide');$('#restartBtn').classList.add('hide');
  err('');dropErr('');ctx.clearRect(0,0,512,512);}
$('#newBtn').onclick=reset;
$('#restartBtn').onclick=()=>{if(S){$('#qualPanel').classList.add('hide');$('#resPanel').classList.add('hide');setupUI();}};

$('#drop').onclick=()=>$('#file').click();
$('#file').onchange=e=>{if(e.target.files[0])load(e.target.files[0]);e.target.value='';};
['dragover','dragenter'].forEach(ev=>$('#drop').addEventListener(ev,e=>{e.preventDefault();$('#drop').classList.add('hot')}));
['dragleave','drop'].forEach(ev=>$('#drop').addEventListener(ev,e=>{e.preventDefault();$('#drop').classList.remove('hot')}));
$('#drop').addEventListener('drop',e=>{if(e.dataTransfer.files[0])load(e.dataTransfer.files[0])});

async function load(file){
  dropErr('');err('');
  // client-side gate 1: type
  if(!/^(image|video)\//.test(file.type||'')){dropErr('Please choose an image or video file.');return;}
  // client-side gate 2: size (avoid uploading something the server will reject)
  if(file.size>CFG.max_upload_mb*1048576){
    dropErr(`That file is ${(file.size/1048576).toFixed(0)} MB — the limit is ${CFG.max_upload_mb} MB. Trim or compress it first.`);return;}
  const url=URL.createObjectURL(file);
  const v=(file.type||'').startsWith('video');
  const el=document.createElement(v?'video':'img');
  let dims;
  try{
    dims=await new Promise((res,rej)=>{
      const to=setTimeout(()=>rej(new Error('Could not read the media (timed out).')),15000);
      if(v){el.muted=true;el.playsInline=true;el.preload='metadata';
        el.onloadedmetadata=()=>{clearTimeout(to);res({w:el.videoWidth,h:el.videoHeight})};
        el.onerror=()=>{clearTimeout(to);rej(new Error('Unreadable or unsupported video.'))};}
      else{el.onload=()=>{clearTimeout(to);res({w:el.naturalWidth,h:el.naturalHeight})};
        el.onerror=()=>{clearTimeout(to);rej(new Error('Unreadable or unsupported image.'))};}
      el.src=url;});
  }catch(e){dropErr(e.message||(''+e));URL.revokeObjectURL(url);return;}
  // client-side gate 3: resolution
  if(dims.w>CFG.max_edge||dims.h>CFG.max_edge||dims.w*dims.h>CFG.max_pixels){
    dropErr(`Resolution ${dims.w}×${dims.h} is too large (limit ${CFG.max_edge}px edge / ${(CFG.max_pixels/1e6).toFixed(0)}MP).`);
    URL.revokeObjectURL(url);return;}
  // upload for authoritative server-side probe + validation
  dropErr('Uploading…');
  let meta;
  try{const fd=new FormData();fd.append('file',file);
    const r=await fetch('/api/open',{method:'POST',body:fd});meta=await r.json();
    if(!r.ok||meta.error){dropErr(meta.error||('Upload failed ('+r.status+').'));URL.revokeObjectURL(url);return;}}
  catch(e){dropErr(''+e);URL.revokeObjectURL(url);return;}
  dropErr('');
  media=el;isVideo=v;
  S={...meta,scale:1,ox:0,oy:0,pad:meta.pad,padMode:meta.pad==='transparent'?'trans':'dom',
     trimStart:0,trimDur:Math.min(CFG.max_seconds,meta.duration||0),fps:15,
     width:meta.width||dims.w,height:meta.height||dims.h};
  setupUI();
}

function fitScale(){return Math.min(512/S.width,512/S.height)}
function fillScale(){return Math.max(512/S.width,512/S.height)}
function centre(s){S.scale=s;S.ox=(512-S.width*s)/2;S.oy=(512-S.height*s)/2}
function clampScale(s){return Math.max(fitScale()*0.25,Math.min(s,fillScale()*8))}
function zoomToSlider(){const lo=Math.log(fitScale()*0.5),hi=Math.log(fillScale()*4);
  $('#zoom').value=Math.round((Math.log(S.scale)-lo)/(hi-lo)*1000)}
function sliderToZoom(v){const lo=Math.log(fitScale()*0.5),hi=Math.log(fillScale()*4);return Math.exp(lo+(v/1000)*(hi-lo))}

function setupUI(){
  $('#drop').classList.add('hide');$('#controls').classList.remove('hide');$('#editor').classList.add('editing');
  $('#stagehint').classList.remove('hide');$('#resPanel').classList.add('hide');err('');
  $('#newBtn').classList.remove('hide');$('#restartBtn').classList.remove('hide');
  const small=S.width<=512&&S.height<=512;centre(small?fitScale():fillScale());
  if(S.pad==='transparent'){$('#sw').style.background='transparent';$('#padnote').textContent='Edges look transparent.';}
  else{$('#sw').style.background=S.pad;$('#padnote').textContent='Auto-detected edge colour '+S.pad+'.';}
  setPad(S.padMode);
  const needTrim=S.animated&&S.duration>CFG.max_seconds+0.001;
  $('#trimPanel').classList.toggle('hide',!needTrim);
  if(needTrim){S.trimStart=0;S.trimDur=CFG.max_seconds;layoutTrim();}else{S.trimStart=0;S.trimDur=Math.min(CFG.max_seconds,S.duration||0);}
  if(isVideo){try{media.currentTime=S.trimStart||0;}catch(e){}}
  $('#animPanel').classList.toggle('hide',!S.animated);
  S.fps=15;$('#fps').value=15;$('#fpsv').textContent='15 fps';
  $('#loop').checked=false;updateLoopAvail();
  // text defaults
  S.text='';S.font=$('#font').value||CFG.default_font;S.size=32;S.outlineW=2;
  S.align='center';S.color='#ffffff';S.ocolor='#000000';
  $('#txt').value='';$('#tsize').value=32;$('#tsizev').textContent='32';
  $('#tout').value=2;$('#toutv').textContent='2';$('#tcol').value='#ffffff';$('#tocol').value='#000000';
  setAlign('center');
  $('#qualPanel').classList.add('hide');
  zoomToSlider();loop();
}
$('#txt').oninput=e=>{S.text=e.target.value;};
$('#font').onchange=e=>{S.font=e.target.value;};
$('#tsize').oninput=e=>{S.size=+e.target.value;$('#tsizev').textContent=S.size;};
$('#tout').oninput=e=>{S.outlineW=+e.target.value;$('#toutv').textContent=S.outlineW;};
$('#tcol').oninput=e=>{S.color=e.target.value;};
$('#tocol').oninput=e=>{S.ocolor=e.target.value;};
$('#alL').onclick=()=>setAlign('left');$('#alC').onclick=()=>setAlign('center');$('#alR').onclick=()=>setAlign('right');
function setAlign(a){S&&(S.align=a);$('#alL').classList.toggle('on',a==='left');
  $('#alC').classList.toggle('on',a==='center');$('#alR').classList.toggle('on',a==='right');}
function fwdDur(){return (S.animated&&S.duration>CFG.max_seconds+0.001)?S.trimDur:Math.min(S.duration||0,CFG.max_seconds);}
function updateLoopAvail(){
  const ok=S.animated&&fwdDur()<CFG.max_seconds-0.05;
  const c=$('#loop');c.disabled=!ok;if(!ok)c.checked=false;
  $('#loopnote').textContent=ok?'Plays forward then reverse for a seamless loop.'
    :'Trim under 3 s to enable a seamless boomerang loop.';
}

$('#padDom').onclick=()=>setPad('dom');$('#padTrans').onclick=()=>setPad('trans');
function setPad(m){if(m==='dom'&&S.pad==='transparent')m='trans';S.padMode=m;
  $('#padDom').classList.toggle('on',m==='dom');$('#padTrans').classList.toggle('on',m==='trans');
  $('#stage').classList.toggle('chkbg',m==='trans');$('#stage').style.background=m==='trans'?'':S.pad;}

$('#fit').onclick=()=>{centre(fitScale());zoomToSlider()};
$('#fill').onclick=()=>{centre(fillScale());zoomToSlider()};
$('#reset').onclick=()=>{const small=S.width<=512&&S.height<=512;centre(small?fitScale():fillScale());zoomToSlider()};
$('#zoom').oninput=e=>{const old=S.scale,ns=clampScale(sliderToZoom(+e.target.value));
  S.ox=256-(256-S.ox)*(ns/old);S.oy=256-(256-S.oy)*(ns/old);S.scale=ns;};
const stage=$('#stage');let dragging=false,lx=0,ly=0;
function toFrame(e){const r=stage.getBoundingClientRect();return{x:(e.clientX-r.left)/r.width*512,y:(e.clientY-r.top)/r.height*512}}
stage.addEventListener('pointerdown',e=>{dragging=true;stage.classList.add('drag');const p=toFrame(e);lx=p.x;ly=p.y;stage.setPointerCapture(e.pointerId)});
stage.addEventListener('pointermove',e=>{if(!dragging)return;const p=toFrame(e);S.ox+=p.x-lx;S.oy+=p.y-ly;lx=p.x;ly=p.y});
stage.addEventListener('pointerup',()=>{dragging=false;stage.classList.remove('drag')});
stage.addEventListener('wheel',e=>{e.preventDefault();const p=toFrame(e);const old=S.scale;
  const ns=clampScale(old*(e.deltaY<0?1.1:1/1.1));S.ox=p.x-(p.x-S.ox)*(ns/old);S.oy=p.y-(p.y-S.oy)*(ns/old);S.scale=ns;zoomToSlider();},{passive:false});

function loop(){if(!S)return;ctx.clearRect(0,0,512,512);
  if(S.padMode==='dom'&&S.pad!=='transparent'){ctx.fillStyle=S.pad;ctx.fillRect(0,0,512,512);}
  try{ctx.imageSmoothingQuality='high';ctx.drawImage(media,S.ox,S.oy,S.width*S.scale,S.height*S.scale);}catch(e){}
  drawTextPreview();
  requestAnimationFrame(loop);}
function drawTextPreview(){
  if(!S||!S.text||!S.text.trim())return;
  // canvas is 512; text is measured in output space (256 animated / 512 static),
  // so scale the preview by 512/outputSize to match the final sticker proportions.
  const outSize=S.animated?(CFG.out_canvas||256):(CFG.canvas||512), k=512/outSize;
  const px=S.size*k, lh=px*1.18, sw=S.outlineW*k;
  ctx.font=`${px}px ${FONT_CSS[S.font]||'sans-serif'}`;
  ctx.textBaseline='alphabetic';ctx.lineJoin='round';
  ctx.textAlign=S.align;
  const x=S.align==='left'?CFG.text_side*k:S.align==='right'?512-CFG.text_side*k:256;
  const lines=S.text.split('\n');
  let y=512-CFG.text_bottom*k-(lines.length-1)*lh;   // baseline of first line
  for(const ln of lines){
    if(sw>0){ctx.lineWidth=sw*2;ctx.strokeStyle=S.ocolor;ctx.strokeText(ln,x,y);}
    ctx.fillStyle=S.color;ctx.fillText(ln,x,y);y+=lh;}
}

const tl=$('#tl'),sel=$('#sel');
function fmt(t){return t.toFixed(2)+'s'}
function layoutTrim(){const D=S.duration||1,x0=S.trimStart/D*100,w=S.trimDur/D*100;
  sel.style.left=x0+'%';sel.style.width=w+'%';
  $('#tStart').textContent=fmt(S.trimStart);$('#tEnd').textContent=fmt(S.trimStart+S.trimDur);$('#tDur').textContent=fmt(S.trimDur)+' window';
  updateLoopAvail();}
let tdrag=null;
function tlx(e){const r=tl.getBoundingClientRect();return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))*(S.duration||1)}
$('#hL').addEventListener('pointerdown',e=>{e.stopPropagation();tdrag='L';tl.setPointerCapture(e.pointerId)});
$('#hR').addEventListener('pointerdown',e=>{e.stopPropagation();tdrag='R';tl.setPointerCapture(e.pointerId)});
sel.addEventListener('pointerdown',e=>{tdrag='M';tl.setPointerCapture(e.pointerId);S._g=tlx(e)-S.trimStart});
tl.addEventListener('pointermove',e=>{if(!tdrag)return;const t=tlx(e),D=S.duration,M=CFG.max_seconds;
  if(tdrag==='L'){const ne=S.trimStart+S.trimDur;let ns=Math.min(t,ne-0.2);S.trimStart=Math.max(0,Math.max(ns,ne-M));S.trimDur=ne-S.trimStart;}
  else if(tdrag==='R'){let ne=Math.max(t,S.trimStart+0.2);ne=Math.min(ne,S.trimStart+M,D);S.trimDur=ne-S.trimStart;}
  else{S.trimStart=Math.max(0,Math.min(t-(S._g||0),D-S.trimDur));}
  layoutTrim();if(isVideo){try{media.currentTime=S.trimStart;}catch(e){}}});
tl.addEventListener('pointerup',()=>{tdrag=null});
let looping=false;
$('#playBtn').onclick=()=>{if(!isVideo)return;looping=!looping;$('#playBtn').textContent=looping?'⏸ Stop':'▶ Preview loop';
  $('#ph').style.display=looping?'block':'none';if(looping){media.currentTime=S.trimStart;media.play();tick();}else{media.pause();}};
function tick(){if(!looping||!S)return;const D=S.duration||1;
  if(media.currentTime>=S.trimStart+S.trimDur||media.currentTime<S.trimStart-0.05)media.currentTime=S.trimStart;
  $('#ph').style.left=(media.currentTime/D*100)+'%';requestAnimationFrame(tick);}

$('#fps').oninput=e=>{S.fps=+e.target.value;$('#fpsv').textContent=S.fps+' fps';};

function jobBody(extra){return {id:S.id,scale:S.scale,ox:S.ox,oy:S.oy,
  pad:S.padMode==='trans'?'transparent':S.pad,animated:!!S.animated,fps:S.fps,
  loop:$('#loop').checked&&!$('#loop').disabled,
  text:(S.text||'').trim()?S.text:'',font:S.font,size:S.size,outline_w:S.outlineW,
  align:S.align,color:S.color,outline_color:S.ocolor,
  trim_start:S.trimStart,trim_dur:(S.animated&&S.duration>CFG.max_seconds+0.001)?S.trimDur:0,...(extra||{})};}
async function post(url,body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let j={};try{j=await r.json();}catch(e){}return {r,j};}

// Step 1: analyze. Fits -> done. Too dense -> choose a look. Floor unreachable -> lower fps.
$('#go').onclick=async()=>{
  err('');$('#qualPanel').classList.add('hide');$('#resPanel').classList.add('hide');
  const btn=$('#go');btn.disabled=true;btn.innerHTML='<span class=spin></span>Analyzing…';
  try{const {r,j}=await post('/api/analyze',jobBody());
    if(!r.ok||j.error){err(j.error||('Analysis failed ('+r.status+').'));}
    else if(j.fits){showResult(j);}
    else if(j.need_lower_fps){showLowerFps(j.predicted_kb);}
    else{curRound=1;showOptions(j.options,'Too detailed to fit at full quality — pick the look you prefer:');}}
  catch(e){err(''+e);}
  btn.disabled=false;btn.textContent='Convert to sticker';
};
function showLowerFps(kb){
  $('#qualPanel').classList.remove('hide');$('#tiles').innerHTML='';
  $('#qualMsg').innerHTML='At <b>'+S.fps+' fps</b> this clip can’t fit 300 KB even at the lowest quality'
    +(kb?' (~'+Math.round(kb)+' KB at full quality)':'')+'. Lower the frame rate above and convert again'
    +(S.fps<=3?' — or trim shorter / crop tighter':'')+'.';
}
function showOptions(opts,msg){
  $('#qualPanel').classList.remove('hide');$('#qualMsg').textContent=msg;
  const t=$('#tiles');t.innerHTML='';
  opts.forEach(o=>{const d=document.createElement('div');d.className='tile';
    d.innerHTML='<img src="'+o.url+'" alt=""><div class=lab>'+o.label+'</div><div class=est>~'+Math.round(o.est_kb)+' KB</div>';
    d.onclick=()=>chooseOption(o,d);t.appendChild(d);});
}
// Step 2: encode the chosen look. Over budget -> tighter options or lower-fps prompt.
async function chooseOption(o,el){
  err('');document.querySelectorAll('.tile').forEach(x=>x.classList.add('busy'));
  el.classList.remove('busy');el.classList.add('on');
  const lab=el.querySelector('.lab'),txt=lab.textContent;lab.innerHTML='<span class=spin></span>Encoding…';
  try{const {r,j}=await post('/api/convert',jobBody({detail:o.detail,colors:o.colors,round:curRound}));
    if(!r.ok||j.error){err(j.error||('Conversion failed ('+r.status+').'));}
    else if(j.ok){$('#qualPanel').classList.add('hide');showResult(j);return;}
    else if(j.need_lower_fps){showLowerFps(j.actual_kb);return;}
    else if(j.options){curRound=j.round||curRound+1;showOptions(j.options,'Still over 300 KB — tighter options:');return;}}
  catch(e){err(''+e);}
  lab.textContent=txt;document.querySelectorAll('.tile').forEach(x=>x.classList.remove('busy','on'));
}
function showResult(j){
  $('#qualPanel').classList.add('hide');
  $('#resPanel').classList.remove('hide');$('#resImg').src=j.url;
  const pill=j.over_budget?'<span class="pill bad">OVER 300KB</span>':'<span class="pill ok">'+j.kb+' KB</span>';
  $('#resStat').innerHTML=pill+'<br><span class=muted>'+(j.animated?(j.fps+' fps · '+j.frames+' frames · '):'')+j.colors+' colors · '+j.engine+'</span>'+
    (j.over_budget?'<div class=err>Couldn’t fit 300KB — trim shorter or crop tighter.</div>':'');
  $('#dlBtn').onclick=()=>{const a=document.createElement('a');a.href=j.url+'&dl=1';a.download='sticker.png';a.click();};
}
$('#again').onclick=reset;

// ---- hover help: after a 2s hover on any [data-help], show an explanatory frame ----
(function(){
  const tip=document.createElement('div');tip.id='tiphelp';document.body.appendChild(tip);
  let timer=null,tipFor=null;
  const clear=()=>{if(timer){clearTimeout(timer);timer=null;}};
  function place(el){
    const r=el.getBoundingClientRect(),t=tip.getBoundingClientRect();
    let x=Math.max(8,Math.min(r.left+r.width/2-t.width/2,innerWidth-t.width-8));
    let y=r.bottom+8; if(y+t.height>innerHeight-8) y=r.top-t.height-8; if(y<8) y=8;
    tip.style.left=x+'px';tip.style.top=y+'px';
  }
  function schedule(el){
    if(el===tipFor)return;
    clear();tipFor=el;tip.classList.remove('on');
    if(!el)return;
    timer=setTimeout(()=>{timer=null;if(tipFor!==el)return;
      tip.textContent=el.getAttribute('data-help')||'';
      tip.style.left='-9999px';tip.style.top='0';tip.classList.add('on');place(el);},2000);
  }
  document.addEventListener('pointerover',e=>{
    const el=e.target.closest&&e.target.closest('[data-help]');if(el)schedule(el);});
  document.addEventListener('pointerout',e=>{
    const el=e.target.closest&&e.target.closest('[data-help]');
    if(el&&tipFor===el&&!(e.relatedTarget&&el.contains(e.relatedTarget)))schedule(null);});
  ['pointerdown','wheel','keydown'].forEach(ev=>document.addEventListener(ev,()=>schedule(null),{passive:true}));
  document.addEventListener('scroll',()=>schedule(null),true);
})();
</script></body></html>"""


def main() -> None:
    port = _free_port()
    url = f"http://127.0.0.1:{port}/"
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    print(f"Signal Sticker Studio → {url}  (Ctrl-C to quit)")
    app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()

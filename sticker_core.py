"""
sticker_core.py — framing/trim-aware conversion core for the Signal sticker GUI.

Reuses the validated quantize/assemble pipeline from signal_sticker.py and adds:
  * arbitrary placement of the source inside a fixed 512x512 output frame
    (scale + offset), so the same model covers BOTH "pad a small image" and
    "crop/zoom a large one" — gaps are filled with the dominant edge colour or
    left transparent;
  * optional trim (start, duration) for clips longer than 3 s;
  * dominant-edge-colour detection (returns '#rrggbb' or 'transparent').

The browser renders an identical preview (same scale/offset math), then this
runs the real ffmpeg + pngquant + oxipng + apng pipeline at full resolution.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import signal_sticker as ss  # validated core: build_hq / build_fallback / flags

CANVAS = ss.CANVAS              # 512 — framing/preview space (client coords)
MAX_SECONDS = ss.MAX_SECONDS    # 3.0
# Animated output canvas. Signal stores APNG stickers AS-IS (verified in
# Signal-Desktop sticker-creator/processImage.ts: animated images are validated but
# not resized — must be square, 10..512 px, loop forever, <=300 KB), so a smaller
# canvas keeps ~4x the byte budget per pixel -> far more colours/fps fit under the
# 300 KB cap, and Signal upscales it to the sticker slot on display (softer edges,
# but smooth colour beats sharp-but-banded for photographic content). Framing stays
# in 512-space; the final composite is downscaled to OUT_CANVAS for animation.
# (STATIC stickers stay 512 — Signal re-renders static images up to 512 anyway.)
OUT_CANVAS = 256
HQ = ss.HAVE_PNGQUANT and ss.HAVE_APNG

# Bound every external process so a crafted/huge input can't hang a worker.
STEP_TIMEOUT = int(os.environ.get("STEP_TIMEOUT_S", "60"))
ss.RUN_TIMEOUT = STEP_TIMEOUT


# ------------------------------- text overlay -------------------------------
# Text is drawn with Pillow (proper multi-line align + outline), baked into the
# output-space frames AFTER extraction and BEFORE the boomerang — so it shows on
# every frame, forward and reversed. Font names are a server-side WHITELIST (the
# name never reaches a shell/filter); colours are validated #rrggbb upstream.
TEXT_BOTTOM_MARGIN = 20         # px from the bottom edge (output space), per spec
TEXT_SIDE_MARGIN = 16           # px inset for left/right alignment
MAX_TEXT_LEN = 500
MAX_TEXT_LINES = 12


def _build_fonts() -> dict:
    """Map friendly font names to the first available file (covers macOS dev +
    common Linux/Alpine paths). Add fonts to the image to widen this in Docker."""
    # macOS dev paths + Debian (/usr/share/fonts/truetype/...) + Alpine
    # (/usr/share/fonts/{dejavu,liberation}/...) so the same registry works in the
    # Docker image. Impact/Comic are macOS-only; Docker falls back to DejaVu/Liberation.
    candidates = {
        "Sans Bold": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                      "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                      "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                      "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
                      "/usr/share/fonts/ttf-dejavu/DejaVuSans-Bold.ttf"],
        "Sans": ["/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                 "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/dejavu/DejaVuSans.ttf"],
        "Impact": ["/System/Library/Fonts/Supplemental/Impact.ttf"],
        "Serif": ["/System/Library/Fonts/Supplemental/Times New Roman.ttf",
                  "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
                  "/usr/share/fonts/liberation/LiberationSerif-Regular.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
                  "/usr/share/fonts/dejavu/DejaVuSerif.ttf"],
        "Mono": ["/System/Library/Fonts/Supplemental/Courier New.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
                 "/usr/share/fonts/liberation/LiberationMono-Regular.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                 "/usr/share/fonts/dejavu/DejaVuSansMono.ttf"],
        "Comic": ["/System/Library/Fonts/Supplemental/Comic Sans MS.ttf"],
    }
    out = {}
    for name, paths in candidates.items():
        for p in paths:
            if Path(p).is_file():
                out[name] = p
                break
    return out


FONTS = _build_fonts()
DEFAULT_FONT = ("Impact" if "Impact" in FONTS else
                "Sans Bold" if "Sans Bold" in FONTS else
                (next(iter(FONTS)) if FONTS else None))


def text_signature(t: dict) -> str:
    raw = "|".join(str(t.get(k, "")) for k in
                   ("text", "font", "size", "outline_w", "align", "color", "outline_color"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _draw_text_frames(frames: list[Path], out_dir: Path, t: dict) -> list[Path]:
    """Bake `t` onto each frame at 20 px from the bottom, growing up. Geometry is
    measured once (all frames share the output size)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    font_path = FONTS.get(t["font"]) or (FONTS.get(DEFAULT_FONT) if DEFAULT_FONT else None)
    if not font_path:
        return frames                       # no fonts installed -> skip text
    font = ImageFont.truetype(font_path, int(t["size"]))
    txt, align, sw = t["text"], t["align"], int(t["outline_w"])
    with Image.open(frames[0]) as im0:
        W, H = im0.size
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bb = scratch.multiline_textbbox((0, 0), txt, font=font, stroke_width=sw, align=align)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    if align == "left":
        x = TEXT_SIDE_MARGIN - bb[0]
    elif align == "right":
        x = W - TEXT_SIDE_MARGIN - tw - bb[0]
    else:
        x = (W - tw) / 2 - bb[0]
    y = H - TEXT_BOTTOM_MARGIN - th - bb[1]
    out = []
    for p in frames:
        im = Image.open(p).convert("RGBA")
        ImageDraw.Draw(im).multiline_text(
            (x, y), txt, font=font, fill=t["color"], align=align,
            stroke_width=sw, stroke_fill=t["outline_color"])
        o = out_dir / p.name
        im.save(o)
        out.append(o)
    return out


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True,
                          timeout=STEP_TIMEOUT)


def probe(src: Path) -> dict:
    """Return width, height, duration (s), fps, frame count, animated flag."""
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries",
                "stream=width,height,r_frame_rate,nb_frames:format=duration",
                "-of", "json", str(src)]).stdout
    j = json.loads(out)
    st = (j.get("streams") or [{}])[0]
    w, h = int(st.get("width", 0)), int(st.get("height", 0))
    num, _, den = str(st.get("r_frame_rate", "0/1")).partition("/")
    fps = (float(num) / float(den or 1)) if float(den or 1) else 0.0
    try:
        dur = float(j.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        dur = 0.0
    try:
        nb = int(st.get("nb_frames"))
    except (TypeError, ValueError):
        nb = 0
    if not nb and fps and dur:
        nb = max(1, round(fps * dur))
    animated = (src.suffix.lower() != ".gif" and nb > 1) or \
               (src.suffix.lower() == ".gif" and _gif_animated(src)) or \
               (dur > 0.05 and nb > 1)
    return {"width": w, "height": h, "duration": round(dur, 3),
            "fps": round(fps, 3) if fps else 0.0, "frames": nb,
            "animated": bool(animated)}


def _gif_animated(src: Path) -> bool:
    try:
        im = Image.open(src)
        return getattr(im, "is_animated", False) and im.n_frames > 1
    except Exception:
        return False


def first_frame(src: Path, dst: Path, at: float = 0.0) -> Path:
    _run(["ffmpeg", "-v", "error", "-y", "-ss", str(at), "-i", str(src),
          "-frames:v", "1", str(dst)])
    return dst


def dominant_edge(src: Path) -> str:
    """Most common colour around a 1-px-thick border; 'transparent' if the
    border is mostly transparent."""
    tmp = src.with_suffix(".edge.png")
    try:
        first_frame(src, tmp)
        im = Image.open(tmp).convert("RGBA")
    except Exception:
        return "transparent"
    finally:
        pass
    w, h = im.size
    px = im.load()
    border, transp = [], 0
    coords = ([(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)] +
              [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)])
    for x, y in coords:
        r, g, b, a = px[x, y]
        if a < 32:
            transp += 1
        else:
            border.append((r // 16 * 16, g // 16 * 16, b // 16 * 16))
    try:
        tmp.unlink()
    except Exception:
        pass
    if transp > len(coords) * 0.5 or not border:
        return "transparent"
    (r, g, b), _ = Counter(border).most_common(1)[0]
    return f"#{r:02x}{g:02x}{b:02x}"


def _pad_filter(pad: str) -> str:
    if pad == "transparent" or not pad:
        return "color=c=black@0.0:s=%dx%d,format=rgba[bg]" % (CANVAS, CANVAS)
    hexc = pad if pad.startswith("#") else "#" + pad
    return "color=c=%s:s=%dx%d,format=rgba[bg]" % (hexc, CANVAS, CANVAS)


def _transform_fc(width: int, height: int, scale: float, ox: float, oy: float,
                  pad: str, detail: float, fps_part: str, out_size: int) -> str:
    """filter_complex placing the (optionally detail-softened) source onto a
    512x512 pad canvas. detail<1 downscales the source then scales it back up,
    discarding high-frequency detail to buy bytes while keeping framing identical
    so the browser preview still matches geometry."""
    sw = max(1, round(width * scale))
    sh = max(1, round(height * scale))
    ox_i, oy_i = round(ox), round(oy)
    soft = ""
    if detail < 0.999:
        dw = max(1, round(sw * detail))
        dh = max(1, round(sh * detail))
        soft = f"scale={dw}:{dh}:flags=lanczos,scale={sw}:{sh}:flags=lanczos,"
    # Downscale the finished 512 composite to the output canvas (256 for animation)
    # — framing/preview math stays in 512-space, only the emitted pixels shrink.
    down = f"scale={out_size}:{out_size}:flags=lanczos," if out_size != CANVAS else ""
    # setpts=PTS-STARTPTS rebases the source to t=0: without it the overlay emits
    # a background-only (blank) first frame, and a non-zero -ss trim desyncs the
    # source against the infinite [bg] colour source (-> blank frames). fps is
    # applied AFTER the overlay: [bg] runs at its own rate and overlay emits at the
    # background's rate, so resampling the composite is the only place that reliably
    # sets the frame cadence.
    return (f"{_pad_filter(pad)};"
            f"[0:v]setpts=PTS-STARTPTS,scale={sw}:{sh}:flags=lanczos,{soft}format=rgba[s];"
            f"[bg][s]overlay=x={ox_i}:y={oy_i}:shortest=1,"
            f"crop={CANVAS}:{CANVAS}:0:0,{down}{fps_part}setsar=1[o]")


def extract_transformed(src: Path, workdir: Path, *, width: int, height: int,
                        scale: float, ox: float, oy: float, pad: str,
                        animated: bool, sample_fps: int,
                        trim: tuple[float, float] | None,
                        detail: float = 1.0) -> list[Path]:
    """Place source (scaled by `scale`, top-left at (ox,oy) in 512-space) onto a
    512x512 pad canvas; emit RGBA PNG frames. Math mirrors the browser preview:
    output(X,Y) samples source((X-ox)/scale, (Y-oy)/scale). `detail` (0,1]
    softens the source to trade quality for bytes."""
    fps_part = f"fps={sample_fps}," if animated else ""
    out_size = OUT_CANVAS if animated else CANVAS   # animation 256, static 512
    fc = _transform_fc(width, height, scale, ox, oy, pad, detail, fps_part, out_size)
    cmd = ["ffmpeg", "-v", "error", "-y"]
    # -ss before -i = fast input seek; -t AFTER -i = reliable output-duration cap
    # (as an *input* option -t is ignored by the GIF demuxer -> whole clip leaks
    # through, which previously blew the frame count and the size budget).
    dur = None
    if trim:
        cmd += ["-ss", f"{trim[0]:.3f}"]
        dur = min(trim[1], MAX_SECONDS)
    elif animated:
        dur = MAX_SECONDS
    cmd += ["-i", str(src), "-filter_complex", fc, "-map", "[o]"]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    if not animated:
        cmd += ["-frames:v", "1"]
    cmd += [str(workdir / "f_%05d.png")]
    _run(cmd)
    frames = sorted(workdir.glob("f_*.png"))
    if not frames:
        raise RuntimeError("ffmpeg produced no frames (check transform/trim params)")
    return frames


# --------------------- interactive predict / preview search -----------------
#
# APNG has no inter-frame compression: every frame is an independent PNG, so the
# total size is ~ (mean bytes/frame) x (frame count). That makes size cheaply
# predictable from a small sample, and gives us three quality levers:
#   * fps      -> frame count   (chosen by the user via a slider, fixed per round)
#   * detail   -> spatial resolution softening (see _transform_fc)
#   * colors   -> palette size
# We predict from a sample, show the user one rendered frame per strategy, encode
# the full clip on their choice, and (if still over budget) re-predict harsher
# from the *actual* bytes. When even the floor won't fit, the caller asks the
# user to lower the fps.

DETAIL_LADDER: tuple[float, ...] = (1.0, 0.85, 0.72, 0.6, 0.5, 0.42,
                                    0.34, 0.27, 0.21, 0.16, 0.12)
COLOR_LADDER: tuple[int, ...] = (256, 224, 192, 160, 128, 96, 64, 48, 32, 24, 16)
DETAIL_FLOOR = DETAIL_LADDER[-1]
COLOR_FLOOR = COLOR_LADDER[-1]
PREDICT_SAMPLE = 12             # frames quantized to estimate full size
# The fast estimate sums PER-FRAME pngquant sizes; the real encode is ONE ffmpeg
# pass with a shared palette + inter-frame diffing, whose size relative to that sum
# is strongly content-dependent (~0.4 for a static-background clip, ~0.9 for full
# motion). So we don't use a fixed factor: the caller measures one real encode as an
# anchor and passes the per-clip ratio as `factor` (see GUI `_calibrate`).


def _sample_idx(n: int, k: int) -> list[int]:
    if n <= k:
        return list(range(n))
    return [round(i * (n - 1) / (k - 1)) for i in range(k)]


def _quant_est(src: Path, dst: Path, colors: int) -> int:
    """Fast quantize used only for size estimation. Uses pngquant (speed 5, no
    dither, matching the final encode) when present, else a Pillow fallback — so
    pngquant is OPTIONAL (the animated encoder is ffmpeg-only; per-clip calibration
    corrects any estimate offset regardless). ffmpeg is the only hard dependency."""
    if ss.HAVE_PNGQUANT:
        _run(["pngquant", str(colors), "--speed", "5", "--quality", "0-100",
              "--nofs", "--strip", "--force", "--output", str(dst), str(src)])
    else:
        Image.open(src).convert("RGBA").quantize(
            colors=max(2, min(256, colors)), method=Image.Quantize.FASTOCTREE).save(dst)
    return dst.stat().st_size


def predict_kb(frames: list[Path], workdir: Path, colors: int,
               factor: float = 1.0) -> tuple[float, Path]:
    """Estimate full-APNG KB at `colors` from a per-frame sample, scaled by the
    per-clip calibration `factor`; also return the heaviest sampled frame (the
    honest worst case to show as a preview tile)."""
    qd = workdir / "_est"; qd.mkdir(exist_ok=True)
    idx = _sample_idx(len(frames), PREDICT_SAMPLE)
    sizes = []
    heavy, heavy_b = frames[idx[0]], -1
    for i in idx:
        b = _quant_est(frames[i], qd / f"{colors}_{i}.png", colors)
        sizes.append(b)
        if b > heavy_b:
            heavy_b, heavy = b, frames[i]
    mean = sum(sizes) / len(sizes)
    est = mean * len(frames) * factor
    return est / 1024, heavy


def raw_sample_kb(frames: list[Path], workdir: Path, colors: int) -> float:
    """Uncalibrated per-frame-sample estimate (factor=1) — the denominator for
    computing the per-clip calibration ratio against one real encode."""
    return predict_kb(frames, workdir, colors, 1.0)[0]


def _detail_dir(workdir: Path, sample_fps: int, detail: float) -> Path:
    # cache key MUST include fps: changing the fps slider changes the frame set,
    # so a detail-only key would silently reuse a stale-cadence extraction.
    return workdir / f"f{int(sample_fps):02d}d{int(round(detail * 1000)):04d}"


def _loopify(frames: list[Path], sample_fps: int) -> list[Path]:
    """Boomerang: append a (speed-adjusted) reversed copy so the sticker loops
    seamlessly (last forward frame flows into the reverse, which ends back at the
    first frame). Forward keeps normal speed (T s); the reverse is time-compressed
    to fill up to MAX_SECONDS total — R = min(T, MAX_SECONDS - T), reverse speed
    T/R — so a 2 s clip gets ~1 s of 2x reverse, while a clip <=1.5 s reverses at
    normal speed. The turn/loop seam frames are dropped so neither seam stutters.
    Frames are reused (same files, reordered), so no extra extraction."""
    n = len(frames)
    if n < 3:
        return frames
    T = n / sample_fps
    R = min(T, MAX_SECONDS - T)
    if R <= 0:                                  # already ~MAX_SECONDS, no room
        return frames
    m = max(1, round(n * R / T))                # reverse frame count
    rev = list(range(n - 2, 0, -1))             # f(n-2)..f1 (drop both seam dups)
    if m < len(rev):
        rev = ([rev[round(i * (len(rev) - 1) / (m - 1))] for i in range(m)]
               if m > 1 else [rev[len(rev) // 2]])
    return frames + [frames[i] for i in rev]


def _extract_detail(src: Path, workdir: Path, *, width, height, scale, ox, oy,
                    pad, animated, sample_fps, trim, detail: float,
                    loop: bool = False, text: dict | None = None) -> list[Path]:
    """Extract a frame set at a given (fps, detail) into a memoised subdir (so the
    final encode can reuse the search's extraction). `text` bakes a caption into
    the frames (cached separately by text-hash, so changing text doesn't force a
    re-extract; the SAME pixels on every frame keep the text region static for the
    APNG inter-frame diff). `loop` appends a reversed boomerang tail. Order matters:
    text first (so the reverse frames carry it), then loop."""
    sub = _detail_dir(workdir, sample_fps, detail)
    if sub.exists() and any(sub.glob("f_*.png")):
        frames = sorted(sub.glob("f_*.png"))
    else:
        sub.mkdir(exist_ok=True)
        frames = extract_transformed(src, sub, width=width, height=height,
                                     scale=scale, ox=ox, oy=oy, pad=pad,
                                     animated=animated, sample_fps=sample_fps,
                                     trim=trim, detail=detail)
    if text:
        tdir = workdir / f"txt{text_signature(text)}_{int(sample_fps):02d}d{int(round(detail*1000)):04d}"
        if tdir.exists() and any(tdir.glob("f_*.png")):
            frames = sorted(tdir.glob("f_*.png"))
        else:
            frames = _draw_text_frames(frames, tdir, text)
    if loop and animated:
        frames = _loopify(frames, sample_fps)
    return frames


def search_options(src: Path, workdir: Path, base_frames: list[Path], *,
                   width, height, scale, ox, oy, pad, animated, sample_fps, trim,
                   target_kb: float, cal: float = 1.0, loop: bool = False,
                   text: dict | None = None) -> list[dict]:
    """Three strategies, each the lightest step on its ladder predicted to fit
    `target_kb`: soften resolution, cut colors, or split the difference. `cal` is
    the per-clip calibration factor applied to every prediction.
    Each dict: {key,label,detail,colors,est_kb,heavy}."""
    def at_detail(d):
        return _extract_detail(src, workdir, width=width, height=height,
                               scale=scale, ox=ox, oy=oy, pad=pad,
                               animated=animated, sample_fps=sample_fps,
                               trim=trim, detail=d, loop=loop, text=text)
    def qdir(d):
        return _detail_dir(workdir, sample_fps, d)

    def fewer():   # full detail, drop colours (dither hides it) — best for photos/skin
        for c in COLOR_LADDER[1:]:
            kb, hv = predict_kb(base_frames, workdir, c, cal)
            if kb <= target_kb or c == COLOR_FLOOR:
                return dict(key="fewer", label="Sharp", detail=1.0,
                            colors=c, est_kb=round(kb, 1), heavy=str(hv))
    def softer():  # keep colours, soften resolution — for flat/graphic content
        for d in DETAIL_LADDER[1:]:
            kb, hv = predict_kb(at_detail(d), qdir(d), 256, cal)
            if kb <= target_kb or d == DETAIL_FLOOR:
                return dict(key="softer", label="Soft", detail=d, colors=256,
                            est_kb=round(kb, 1), heavy=str(hv))
    def balanced():
        for d, c in list(zip(DETAIL_LADDER, COLOR_LADDER))[1:]:
            kb, hv = predict_kb(at_detail(d), qdir(d), c, cal)
            if kb <= target_kb or (d == DETAIL_FLOOR and c == COLOR_FLOOR):
                return dict(key="balanced", label="Balanced", detail=d, colors=c,
                            est_kb=round(kb, 1), heavy=str(hv))
    # Sharp first: for photographic content full-detail + dither beats blurring.
    opts = [fewer(), softer(), balanced()]
    return [o for o in opts if o]


def render_preview(heavy_frame: Path, out_png: Path, colors: int) -> None:
    """Quantize an already-extracted heavy frame at `colors` -> a preview tile,
    using the SAME palette + bayer dither as the animated encoder so the tile shows
    the real skin/banding the sticker will have (not a no-dither stand-in)."""
    ss.quantize_frame_bayer(heavy_frame, out_png, colors)


def encode_choice(src: Path, workdir: Path, out: Path, *, width, height, scale,
                  ox, oy, pad, animated, sample_fps, trim, detail: float,
                  colors: int, loop: bool = False, text: dict | None = None) -> dict:
    """Full encode at an explicit (detail, colors); fps is baked into sample_fps.
    Returns stats incl. exact size so the caller can decide on a harsher round."""
    frames = _extract_detail(src, workdir, width=width, height=height,
                             scale=scale, ox=ox, oy=oy, pad=pad, animated=animated,
                             sample_fps=sample_fps, trim=trim, detail=detail,
                             loop=loop, text=text)
    if not animated or len(frames) == 1:
        if HQ:
            ss.quantize_hq(frames[0], out, colors)
        else:
            Image.open(frames[0]).convert("RGBA").quantize(
                colors=colors, method=Image.Quantize.FASTOCTREE).save(out)
        data = out.read_bytes()
        return {"bytes": len(data), "kb": round(len(data) / 1024, 1), "fps": 0,
                "colors": colors, "frames": 1, "animated": False,
                "detail": detail, "engine": "pngquant" if HQ else "pillow",
                "ok": len(data) <= ss.BUDGET}
    # Animated: ONE shared palette across all frames (no per-frame flicker), no
    # dither. See ss.assemble_shared_palette.
    data = ss.assemble_shared_palette(frames, sample_fps, colors, workdir)
    out.write_bytes(data)
    return {"bytes": len(data), "kb": round(len(data) / 1024, 1),
            "fps": sample_fps, "colors": colors, "frames": len(frames),
            "animated": True, "detail": detail,
            "engine": "shared-palette+oxipng+apng",
            "ok": len(data) <= ss.BUDGET}


def convert(src: Path, workdir: Path, out: Path, *, width: int, height: int,
            scale: float, ox: float, oy: float, pad: str, animated: bool,
            trim: tuple[float, float] | None,
            want_fps: int | None = None, want_colors: int | None = None) -> dict:
    """Full conversion. Returns stats dict."""
    sample_fps = min(int(round(ss.probe_fps(src))), ss.SAMPLE_FPS_CAP) or ss.SAMPLE_FPS_CAP
    frames = extract_transformed(
        src, workdir, width=width, height=height, scale=scale, ox=ox, oy=oy,
        pad=pad, animated=animated, sample_fps=sample_fps, trim=trim)

    if not animated or len(frames) == 1:
        # static sticker -> single quantized 512x512 PNG (Signal accepts PNG)
        if HQ:
            ss.quantize_hq(frames[0], out, want_colors or 256)
        else:
            Image.open(frames[0]).convert("RGBA").quantize(
                colors=want_colors or 256, method=Image.Quantize.FASTOCTREE
            ).save(out)
        data = out.read_bytes()
        return {"bytes": len(data), "kb": round(len(data) / 1024, 1),
                "fps": 0, "colors": want_colors or 256, "frames": 1,
                "animated": False, "engine": "pngquant" if HQ else "pillow",
                "ok": len(data) <= ss.BUDGET}

    if HQ:
        data, fps, colors, nf = ss.build_hq(frames, sample_fps, workdir, want_fps, want_colors)
    else:
        data, fps, colors, nf = ss.build_fallback(frames, sample_fps, want_fps, want_colors)
    out.write_bytes(data)
    return {"bytes": len(data), "kb": round(len(data) / 1024, 1),
            "fps": fps, "colors": colors, "frames": nf, "animated": True,
            "engine": "pngquant+oxipng+apng" if HQ else "pillow-fallback",
            "ok": len(data) <= ss.BUDGET}

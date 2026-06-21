#!/usr/bin/env python3
"""
signal_sticker.py — convert an animated GIF or video into the highest-quality
Signal-compliant animated sticker (APNG) that fits Signal's limits.

Signal animated sticker constraints (per Signal support docs):
  * Format: APNG (not GIF)        * Canvas: 512x512 px, transparent background
  * Size:   <= 300 KB per sticker * Length: <= 3 seconds

Quality pipeline (default — no flags needed):
  ffmpeg  : decode once, Lanczos fit to 512x512, transparent square pad, <=3s
  pngquant: per-frame perceptual quantization (libimagequant), NO dithering
            (it shimmers across APNG frames) but SMOOTH 8-bit alpha (anti-aliased
            edges, not the hard binary alpha of naive palette conversion)
  oxipng  : lossless re-compression of every frame (level 6) to buy back bytes
  apng    : mux frames into APNG preserving pngquant/oxipng's exact bytes —
            no re-quantization, so nothing degrades during assembly

It then searches a quality ladder that protects colour depth first: it spends
frame rate (down to a 12 fps floor) before it ever reduces the palette, and
returns the best-looking config that lands under 300 KB.

If pngquant is not installed it transparently falls back to a Pillow-only path
(hard alpha, lower quality) so the tool still runs — but install pngquant for
the intended output:  macOS  `brew install pngquant`   Debian `apt install pngquant`

Python deps: pillow, apng, pyoxipng   (pip install pillow apng pyoxipng)
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image


def _add_bundled_bin_to_path() -> None:
    """Let a packaged build find ffmpeg/ffprobe/pngquant placed next to the
    executable (or under a sibling `bin/`), so the release binaries are
    self-contained when those tools are bundled. Pure PATH prepend — a no-op if
    nothing is there, so dev/Docker (tools on PATH) are unaffected."""
    cands = []
    if getattr(sys, "frozen", False):                  # PyInstaller bundle
        cands.append(Path(sys.executable).resolve().parent)
        mp = getattr(sys, "_MEIPASS", None)
        if mp:
            cands.append(Path(mp))
    cands.append(Path(__file__).resolve().parent)
    for base in cands:
        for d in (base, base / "bin"):
            if d.is_dir():
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


_add_bundled_bin_to_path()

CANVAS = 512
MAX_SECONDS = 3.0
BUDGET = 300 * 1024
SAFETY = int(BUDGET * 0.98)      # land just under; APNG overhead is tiny
# Animated stickers dither with ffmpeg's ORDERED (bayer) dither, which is
# position-deterministic -> it smooths skin/gradient banding WITHOUT shimmering
# across frames (error-diffusion would shimmer and ~2x the file). bayer_scale: lower
# = stronger dither / less banding; 1 is the strongest, best for skin.
BAYER_SCALE = 1
SAMPLE_FPS_CAP = 24

# Quality ladder, best -> acceptable. Colour depth is preserved as long as
# possible; frame rate is sacrificed down to 12 fps before the palette shrinks.
LADDER: tuple[tuple[int, int], ...] = (
    (24, 256), (20, 256), (15, 256), (12, 256),
    (20, 224), (15, 224), (12, 224),
    (20, 192), (15, 192), (12, 192),
    (15, 160), (12, 160),
    (15, 128), (12, 128),
    (12, 96), (10, 96),
    (12, 64), (10, 64), (8, 64),
    (10, 48), (8, 48),
    (8, 32),
)

try:
    import oxipng
    HAVE_OXIPNG = True
except Exception:
    HAVE_OXIPNG = False

try:
    from apng import APNG
    HAVE_APNG = True
except Exception:
    HAVE_APNG = False

HAVE_PNGQUANT = shutil.which("pngquant") is not None


RUN_TIMEOUT = None  # seconds; set by callers (e.g. the GUI) to bound subprocesses


# Restrict ffmpeg/ffprobe to local-file protocols. Without this a crafted upload
# (a playlist/concat/HLS-style container) can make ffmpeg follow references to URLs
# (SSRF) or other local files (LFI) during demux. `-nostdin` stops a malformed input
# from leaving ffmpeg blocked on stdin. Applied to EVERY ffmpeg/ffprobe call via the
# run()/_run() wrappers, so new call sites are covered automatically.
AV_PROTOCOLS = "file,crypto,data"


def av_safe(cmd: list[str]) -> list[str]:
    """Inject protocol/stdin guards for ffmpeg/ffprobe; pass other commands through."""
    if not cmd:
        return cmd
    tool = os.path.basename(str(cmd[0]))
    if tool == "ffmpeg":
        return [cmd[0], "-nostdin", "-protocol_whitelist", AV_PROTOCOLS, *cmd[1:]]
    if tool == "ffprobe":
        return [cmd[0], "-protocol_whitelist", AV_PROTOCOLS, *cmd[1:]]
    return cmd


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(av_safe(cmd), check=True, capture_output=True, text=True,
                          timeout=RUN_TIMEOUT)


def probe_fps(src: Path) -> float:
    try:
        out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=r_frame_rate",
                   "-of", "default=nw=1:nk=1", str(src)]).stdout.strip()
        num, _, den = out.partition("/")
        fps = float(num) / float(den or 1)
        return fps if 0 < fps <= 120 else 24.0
    except Exception:
        return 24.0


def extract_frames(src: Path, workdir: Path, sample_fps: int) -> list[Path]:
    """Decode once into RGBA PNGs fit & transparently padded to 512x512."""
    vf = (f"fps={sample_fps},"
          f"scale={CANVAS}:{CANVAS}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"format=rgba,"
          f"pad={CANVAS}:{CANVAS}:(ow-iw)/2:(oh-ih)/2:color=#00000000,"
          f"setsar=1")
    run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-t", str(MAX_SECONDS),
         "-vf", vf, "-f", "image2", str(workdir / "f_%05d.png")])
    frames = sorted(workdir.glob("f_*.png"))
    if not frames:
        sys.exit("error: no frames decoded — is the input a valid gif/video?")
    return frames


def subsample(frames: list[Path], src_fps: int, target_fps: int) -> list[Path]:
    if target_fps >= src_fps:
        return frames
    step = src_fps / target_fps
    out, i = [], 0.0
    while int(i) < len(frames):
        out.append(frames[int(i)]); i += step
    return out or frames[:1]


# --- high-quality path: pngquant + oxipng + apng ---------------------------

def quantize_hq(src: Path, dst: Path, colors: int) -> None:
    run(["pngquant", str(colors), "--speed", "1", "--quality", "0-100",
         "--nofs", "--strip", "--force", "--output", str(dst), str(src)])
    if HAVE_OXIPNG:
        try:
            oxipng.optimize(str(dst), level=6)
        except Exception:
            pass


def assemble_hq(frame_paths: list[Path], fps: int) -> bytes:
    delay = max(1, round(1000 / fps))
    a = APNG(num_plays=0)  # loop forever
    for p in frame_paths:
        a.append_file(str(p), delay=delay, delay_den=1000)
    return a.to_bytes()


def quantize_frame_bayer(src: Path, dst: Path, colors: int) -> None:
    """Single-frame palette + bayer-dither quantize, matching the animated encoder's
    look — so preview tiles show the SAME skin/banding the final sticker will have
    (the pngquant --nofs path made previews look worse than the dithered result)."""
    fc = (f"split[a][b];"
          f"[a]palettegen=max_colors={max(2, colors)}:reserve_transparent=1:"
          f"stats_mode=full[p];"
          f"[b][p]paletteuse=dither=bayer:bayer_scale={BAYER_SCALE}:"
          f"alpha_threshold=128")
    run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter_complex", fc,
         "-frames:v", "1", str(dst)])
    if HAVE_OXIPNG:
        try:
            oxipng.optimize(str(dst), level=6)
        except Exception:
            pass


def assemble_shared_palette(frame_paths: list[Path], fps: int, colors: int,
                            tmp: Path) -> bytes:
    """Encode ALL frames to ONE shared palette and emit the APNG in a single ffmpeg
    pass (split -> palettegen -> paletteuse -> `-plays 0`), then oxipng the whole
    file. Two reasons this beats per-frame pngquant + apng-lib mux:
      * one GLOBAL palette (no per-frame palettes -> no colour shimmer on static
        regions, and the palette is stored once, not per frame);
      * ffmpeg's APNG encoder does real inter-frame diffing (unchanged regions are
        not re-sent), so a clip with a static background is far smaller.
    Net on real clips: ~half the bytes of the per-frame path -> many more colours
    fit the budget. `reserve_transparent` keeps the pad/letterbox transparent;
    `alpha_threshold` makes alpha binary (smooth alpha can't survive a shared
    indexed palette — fine for full-frame video; stills keep smooth alpha via the
    pngquant path)."""
    work = tmp / "sp"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for i, p in enumerate(frame_paths, 1):              # stage a contiguous sequence
        (work / f"s_{i:05d}.png").write_bytes(Path(p).read_bytes())
    out = work / "out.png"
    fc = (f"[0:v]split[a][b];"
          f"[a]palettegen=max_colors={max(2, colors)}:reserve_transparent=1:"
          f"stats_mode=full[p];"
          f"[b][p]paletteuse=dither=bayer:bayer_scale={BAYER_SCALE}:"
          f"alpha_threshold=128")
    run(["ffmpeg", "-v", "error", "-y", "-framerate", str(fps),
         "-i", str(work / "s_%05d.png"), "-filter_complex", fc,
         "-plays", "0", "-f", "apng", str(out)])
    if HAVE_OXIPNG:
        try:
            oxipng.optimize(str(out), level=6)           # oxipng preserves APNG anim
        except Exception:
            pass
    return out.read_bytes()


def build_hq(frames: list[Path], src_fps: int, tmp: Path,
             want_fps: int | None, want_colors: int | None):
    qdir = tmp / "q"; qdir.mkdir(exist_ok=True)
    cache: dict[tuple[str, int], Path] = {}

    def quant(p: Path, colors: int) -> Path:
        key = (p.name, colors)
        if key not in cache:
            out = qdir / f"{colors}_{p.name}"
            quantize_hq(p, out, colors)
            cache[key] = out
        return cache[key]

    ladder = [(want_fps or f, want_colors or c) for (f, c) in LADDER]
    if want_fps or want_colors:
        ladder = [(want_fps or LADDER[0][0], want_colors or LADDER[0][1])]

    tried: set[tuple[int, int]] = set()
    last = None
    for fps, colors in ladder:
        sub = subsample(frames, src_fps, fps)
        sig = (len(sub), colors)
        if sig in tried:
            continue
        tried.add(sig)
        qframes = [quant(p, colors) for p in sub]
        data = assemble_hq(qframes, fps)
        eff_fps = min(fps, src_fps)
        last = (data, eff_fps, colors, len(sub))
        if len(data) <= SAFETY:
            return last
    return last


# --- fallback path: Pillow only (hard alpha) -------------------------------

def build_fallback(frame_paths: list[Path], src_fps: int,
                   want_fps: int | None, want_colors: int | None):
    frames = [Image.open(p).convert("RGBA").copy() for p in frame_paths]
    fps_opts = (want_fps,) if want_fps else (24, 20, 15, 12, 10, 8, 6, 5)
    col_opts = (want_colors,) if want_colors else (256, 224, 192, 160, 128, 96, 64, 48, 32)

    def sub(fr, tf):
        if tf >= src_fps: return fr
        s = src_fps / tf; o, i = [], 0.0
        while int(i) < len(fr): o.append(fr[int(i)]); i += s
        return o or fr[:1]

    last = None
    for fps in fps_opts:
        s = sub(frames, fps)
        for colors in col_opts:
            q = [f.quantize(colors=colors, method=Image.Quantize.FASTOCTREE,
                            dither=Image.Dither.NONE) for f in s]
            buf = io.BytesIO()
            q[0].save(buf, format="PNG", save_all=True, append_images=q[1:],
                      duration=max(1, round(1000 / fps)), loop=0,
                      disposal=0, blend=0, optimize=True)
            data = buf.getvalue()
            last = (data, min(fps, src_fps), colors, len(s))
            if len(data) <= SAFETY:
                return last
    return last


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert gif/video to a high-quality Signal APNG sticker (<=300KB, 512x512, <=3s).")
    ap.add_argument("input", type=Path, help="source .gif / .mp4 / .webm / .mov ...")
    ap.add_argument("-o", "--output", type=Path, help="output .png (APNG). default: <input>.sticker.png")
    ap.add_argument("--fps", type=int, help="force frame rate instead of auto-search")
    ap.add_argument("--colors", type=int, help="force palette size 2-256 instead of auto-search")
    ap.add_argument("--sample-fps", type=int, help="source extraction fps (default min(source,24))")
    args = ap.parse_args()

    if not args.input.exists():
        sys.exit(f"error: {args.input} not found")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"error: {tool} not found on PATH")

    hq = HAVE_PNGQUANT and HAVE_APNG
    if not hq:
        missing = [n for n, ok in (("pngquant", HAVE_PNGQUANT), ("apng (pip)", HAVE_APNG)) if not ok]
        print(f"warning: {', '.join(missing)} missing -> falling back to lower-quality "
              f"Pillow path (hard alpha). Install for best output.", file=sys.stderr)

    out = args.output or args.input.with_suffix(".sticker.png")
    src_fps = args.sample_fps or min(int(round(probe_fps(args.input))), SAMPLE_FPS_CAP) or SAMPLE_FPS_CAP

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        frames = extract_frames(args.input, tmpd, src_fps)
        if hq:
            data, fps, colors, nf = build_hq(frames, src_fps, tmpd, args.fps, args.colors)
        else:
            data, fps, colors, nf = build_fallback(frames, src_fps, args.fps, args.colors)

    out.write_bytes(data)
    kb = len(data) / 1024
    engine = "pngquant+oxipng+apng" if hq else "pillow-fallback"
    status = "OK" if len(data) <= BUDGET else "OVER BUDGET"
    print(f"{out}  |  {kb:.1f} KB  |  {fps} fps  |  {colors} colors  |  {nf} frames  |  {engine}  |  {status}")
    if len(data) > BUDGET:
        print("warning: could not fit 300KB. Shorten the clip, crop tighter, or lower --fps.", file=sys.stderr)


if __name__ == "__main__":
    main()

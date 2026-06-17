# Signal Sticker Studio

Turn a photo, GIF, or video into a **Signal-compliant sticker** — with a browser GUI for
framing, trimming, captions, and a seamless boomerang loop, or a one-shot CLI.

Output always satisfies Signal's hard limits:

- **Animated → APNG** (256×256) · **static → PNG** (512×512)
- **≤ 300 KB** per sticker · **≤ 3 s** · transparent background supported · loops forever

> Why 256² for animation? Signal stores animated stickers **as‑is** (it only re‑renders
> *static* images up to 512). A smaller canvas keeps ~4× the byte budget per pixel, so far
> more colour/frames fit under 300 KB; Signal upscales it to the sticker slot on display.

---

## Features

- **Smart quality search** — predicts size from a sample, then lets you pick a look
  (Sharp / Soft / Balanced) when a clip can't fit at full quality; asks you to lower the
  fps only when nothing fits.
- **One shared palette + inter‑frame diffing** (ffmpeg APNG) — no per‑frame colour
  shimmer, static backgrounds cost almost nothing.
- **Ordered (Bayer) dithering** — smooth skin/gradients with no frame‑to‑frame flicker.
- **Framing** — drag/zoom/pan onto the 512 canvas, dominant‑edge or transparent padding.
- **Trim** clips longer than 3 s.
- **Boomerang loop** — append a speed‑matched reverse for a seamless cycle.
- **Text overlay** — multi‑line caption with font, size, outline, alignment, and colours,
  baked into the frames (so it never shimmers and compresses essentially free).

---

## Quick start

### Docker (recommended — bundles ffmpeg + pngquant + fonts)

Runs **standalone — no tunnel required**:

```bash
docker compose up -d --build      # -> http://127.0.0.1:8765  (localhost only by default)
```

Exposure is your choice (the app has no auth, so the port defaults to localhost):

```bash
STICKER_BIND=0.0.0.0 docker compose up -d     # LAN access (no auth — trust your network)
STICKER_PORT=9000     docker compose up -d     # different host port
```

Or pull the published multi-arch image directly:

```bash
docker run -d --name sticker -p 127.0.0.1:8765:8000 -v sticker_work:/work \
  ghcr.io/catsec/signal-animated-sticker-maker:latest      # -> http://127.0.0.1:8765
```

**Behind a tunnel / reverse proxy is optional.** To use one (e.g. Cloudflare Access),
put the container on the proxy's Docker network and point the proxy at
`http://sticker:8000` — no published port needed. See the optional `networks` blocks in
`docker-compose.yml`.

### Desktop binary

Download the build for your platform from the [Releases](#releases--ci) page
(**macOS arm64**, **Linux x64**, **Linux arm64**), unzip, and run
`signal-sticker-studio` — it starts a local server and opens your browser.

**`ffmpeg` is bundled in the download**, so it runs as‑is. `pngquant` is optional (only
improves *static* PNG quality); install it (`brew install pngquant` /
`apt install pngquant`) or drop it next to the executable if you want it — the app adds
its own folder to PATH automatically.

> Windows isn't shipped as a binary — use **Docker** or run **from source** (below).
> macOS binaries are unsigned, so first launch needs right‑click → Open (or
> `xattr -dr com.apple.quarantine <folder>`).

### From source (Python ≥ 3.10)

```bash
pip install -r requirements.txt        # + ffmpeg & (optional) pngquant on PATH
python signal_sticker_gui.py           # GUI: binds 127.0.0.1:<random>, opens browser
```

---

## Usage

### GUI

`python signal_sticker_gui.py` (or the desktop binary). Drop a file, frame it, optionally
trim / loop / add a caption, click **Convert**, download the sticker. Use **New
conversion** to start a new file or **Restart** to reset the current one.

### CLI

```bash
python signal_sticker.py input.gif                       # -> input.sticker.png
python signal_sticker.py clip.mp4 -o out.png --fps 24 --colors 128
```

---

## Configuration (server / Docker env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `WORK_DIR` | tempdir (`/work` in compose) | Session storage (must be writable). |
| `MAX_UPLOAD_MB` | 100 | Upload cap. |
| `MAX_PIXELS` / `MAX_EDGE` | 40 MP / 8192 | Source resolution caps. |
| `MAX_CONCURRENT` | 2 | Simultaneous conversions. |
| `QUEUE_TIMEOUT_S` | 20 | Wait for a slot before 429. |
| `SESSION_TTL_MIN` | 20 | Session reaper age. |
| `STEP_TIMEOUT_S` | 60 | Per‑subprocess timeout. |
| `MIN_FREE_MB` | 512 | Reject uploads below this free disk. |

The server has **no app‑level auth** — run it behind an authenticating reverse proxy
(e.g. Cloudflare Access) if exposed. All input is validated server‑side; uploaded text is
rendered with Pillow and never reaches the ffmpeg filtergraph.

---

## Releases & CI

GitHub Actions:

- **`docker.yml`** — builds a multi‑arch (`linux/amd64` + `linux/arm64`) image and pushes
  to GHCR on pushes to `main` and version tags.
- **`release.yml`** — on a `v*` tag, builds standalone desktop binaries for
  **macOS (arm64)** and **Linux (x64, arm64)**, **with a static ffmpeg bundled in**, and
  attaches them to the GitHub Release. (Windows is intentionally omitted — use Docker.)

Cut a release:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

---

## License

No license is set yet — add a `LICENSE` file before publishing if you intend others to use
it. Note that ffmpeg (a runtime dependency) is GPL/LGPL; bundling it in binaries carries
its own license obligations.

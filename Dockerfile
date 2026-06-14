# syntax=docker/dockerfile:1
###############################################################################
# Signal Sticker Studio — hardened container
# Base: official python:3.12 on Alpine (musl). All Python deps install from
# prebuilt musllinux wheels, so NO compiler/toolchain is ever in the image.
# For production pin by digest:  FROM python:3.12-alpine@sha256:<digest>
###############################################################################

# ---- build stage: resolve deps into an isolated venv -----------------------
FROM python:3.12-alpine AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
# --require-hashes-ready; fails loudly if any dep would need a source build
RUN pip install --only-binary=:all: -r requirements.txt

# ---- runtime stage ---------------------------------------------------------
FROM python:3.12-alpine

# ffmpeg (decode/scale/pad) + pngquant (quantize) + fonts for the text overlay
# (font-dejavu -> Sans/Serif/Mono, ttf-liberation -> Liberation Sans/Serif/Mono)
RUN apk add --no-cache ffmpeg pngquant font-dejavu ttf-liberation \
 && addgroup -g 1000 app \
 && adduser -D -u 1000 -G app -h /home/app app \
 && mkdir -p /work && chown app:app /work

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY signal_sticker.py sticker_core.py signal_sticker_gui.py ./

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    TMPDIR=/tmp

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD wget -qO- http://127.0.0.1:8000/health >/dev/null 2>&1 || exit 1

# One worker + threads is the default. Sessions are now disk-backed (WORK_DIR),
# so multiple workers also work; note the conversion semaphore (MAX_CONCURRENT) is
# per-process, so total concurrency = workers x MAX_CONCURRENT — size them together.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "300", \
     "--worker-tmp-dir", "/tmp", "-b", "0.0.0.0:8000", \
     "signal_sticker_gui:app"]

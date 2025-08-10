# Universal Encoder

Self‑hosted, in‑place transcoder with a simple web UI.

- Crawls a directory tree for “non‑standard” or non‑streaming‑friendly formats.
- Remuxes when possible (h264 + aac) or transcodes to **H.264 + AAC**.
- Replaces the original atomically using a temporary `.<name>.transcoding.mp4`.
- Live UI: queue, logs, per‑item **Top / Now**, **Pause/Resume**, **Stop**, ETA & speed.
- Auto‑scan with intervals (1m / 10m / 1h / daily) and optional auto‑start.
- Multiple concurrent workers.
- Settings drawer: **CRF**, **preset**, **audio bitrate**, **NVENC**, keep original, preserve timestamps, dry‑run.
- Robust pause/resume (signals both **process group** and **pid**) and path‑normalization (case + Unicode).

> Creator: **code-418.com** — MIT licensed. Use it freely.

---

## Quick start

```bash
git clone https://github.com/code-418dotcom/universal-encoder.git
cd universal-encoder
docker compose up --build -d
# open http://localhost:11223
```

Mount your media to `./data` (mapped to `/data` in the container).

### Environment

Use `.env` (copy from `.env.sample`) or set env vars in compose:

- `TARGET_DIR` – path to watch/encode inside the container (default `/data`).
- `CRF` – x264 CRF (lower = better quality, larger files). Default `22`.
- `PRESET` – x264 or NVENC preset (e.g. `veryfast`, `fast`, `medium`). Default `veryfast`.
- `AUDIO_BITRATE` – AAC bitrate (e.g. `160k`).
- `USE_NVENC` – `true` to use NVIDIA NVENC if available; otherwise libx264.
- `KEEP_ORIGINAL` – `true` to keep the source file after success.
- `PRESERVE_TIMESTAMPS` – `true` to copy atime/mtime to output.
- `DRY_RUN` – `true` to simulate (no output written).

### Features recap

- **Queue** shows upcoming files (two‑line names, directory shown), with per‑item:
  - **Top** – move to the head of the queue.
  - **Now** – start immediately (pauses current file if needed).
- **Jobs** shows current encodes with progress %, speed, ETA and controls:
  - **Pause / Resume** (single toggle).
  - **Stop** (removes current ffmpeg and requeues the file).
- **Logs** streams full ffmpeg logs.
- **Auto‑scan** toggle + interval select; **Workers** (1–4).

### API (FYI)

- `POST /scan` – rescan target dir, refill queue.
- `POST /start` / `POST /stop` – start/stop worker pool.
- `GET /queue` – queued items.
- `GET /state` – running + paused + queue length.
- `POST /queue/top` – move file to front.
- `POST /transcode_now` – start file immediately.
- `POST /job/toggle` – pause/resume current job for that file.
- `POST /job/stop` – stop current job for that file.
- `GET /config`, `GET/POST /options`, `GET/POST /transcode_options`.

### How it picks formats

A file is remuxed if **video = h264** and **audio = aac**. Everything else is transcoded to H.264 + AAC MP4 with `-movflags +faststart`. Temp files end with `.transcoding.mp4` and are swapped in atomically on success.

### Notes

- Don’t expose this container directly to the internet. No auth is included.
- On Linux with NVIDIA, set `USE_NVENC=true` and run the container with proper GPU access (e.g., `--gpus all`).

---

## Dev / Contributing

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 11223
```

---

## License

MIT © code-418.com

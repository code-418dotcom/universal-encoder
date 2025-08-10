# Universal In‑Place Transcoder + Debug UI

**Creator:** [code-418.com](https://code-418.com)  
**License:** Public Domain (The Unlicense)

A tiny Dockerized service that recursively scans a directory, queues all videos,
and converts non‑standard formats **in place** to streaming‑friendly **MP4 (H.264 + AAC)**.
If a file is already H.264/AAC, it **remuxes** (container swap only) instead of
re‑encoding — so it's lossless and finishes in seconds.

The **debug UI** (port `11223`) shows logs, a live queue, progress, ETA, and lets you:
- **Scan** (populate queue only)
- **Start** / **Stop** (strong stop: SIGINT → SIGTERM → SIGKILL, re‑queues current job)
- Move an item to the **Top** of the queue
- **Transcode Now** (pauses current job with `SIGSTOP`, runs the selected file, then `SIGCONT` to resume)

---

## Features

- **Auto remux vs transcode** using ffprobe (H.264/AAC → remux; otherwise → H.264 + AAC).
- **In‑place outputs** with atomic swap via `.<name>.transcoding.mp4` temp file.
- **Fast‑start MP4** for web streaming.
- **Queue controls** (Top, Now), **progress + ETA** via ffmpeg `-progress` pipe.
- **Timestamps preserved**, optional keep original.
- **Start/Stop decoupled** from scanning; queue persists across stops.

---

## Quick start

```bash
docker compose up --build
# open http://localhost:11223
# Click Scan → Start
```

By default, `./sample_data` is mounted to `/data`. Change the volume in `docker-compose.yml`
to point at your media root when ready.

---

## Environment variables

| Variable               | Default    | Description                                                                  |
|------------------------|------------|------------------------------------------------------------------------------|
| `TARGET_DIR`           | `/data`    | Root folder to scan recursively                                              |
| `CRF`                  | `22`       | x264 quality (lower = better). Ignored when NVENC is used                    |
| `PRESET`               | `veryfast` | x264/NVENC speed preset                                                      |
| `AUDIO_BITRATE`        | `160k`     | AAC bitrate                                                                  |
| `USE_NVENC`            | `false`    | Use `h264_nvenc` if your FFmpeg supports it                                  |
| `KEEP_ORIGINAL`        | `false`    | Keep original file after success                                             |
| `PRESERVE_TIMESTAMPS`  | `true`     | Copy atime/mtime from source to output                                       |
| `DRY_RUN`              | `false`    | Don’t write any outputs; log only                                           |

> The base image uses Debian ffmpeg which usually **does not** include NVENC. For GPU
> encoding, switch to an FFmpeg build with NVENC and run with the NVIDIA container runtime.

---

## Project layout

```
universal-encoder/
├─ app/
│  ├─ main.py                # FastAPI backend + worker
│  └─ static/
│     └─ index.html          # Minimal live UI
├─ Dockerfile
├─ docker-compose.yml
├─ requirements.txt
├─ LICENSE
├─ .gitignore
└─ sample_data/.gitkeep
```

---

## Changelog

### v1.0.0
- Start/Stop separated from Scan
- Visible queue with **Top** and **Now** actions
- Priority **Now** job pauses current ffmpeg and resumes after
- Strong Stop kills ffmpeg process group and re‑queues current/paused file
- Auto **remux** when already H.264/AAC; otherwise **transcode** to H.264 + AAC
- Progress + ETA + logs in UI

---

## Dev tips

- If the UI looks stale (placeholders like `${it.file}`), hard‑refresh (Ctrl/Cmd + F5).
- Use `DRY_RUN=true` to validate the scan/queue without writing files.

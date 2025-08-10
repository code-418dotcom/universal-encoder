# Transcoder Debug UI

Small FastAPI web UI to **scan**, **transcode/remux**, and **monitor progress** for a media library.
It converts legacy/non-standard formats to **MP4 (H.264 + AAC)**, and remuxes already-compatible
streams with `+faststart` for instant streaming.

- Live logs and per-file progress (percent, speed, ETA) via WebSockets
- One-click scan of the configured target directory (recursive)
- In-place replacement with safe temp files
- Timestamp stabilization to reduce DTS/PTS warnings
- Works on CPU (x264) or optionally NVENC (if your FFmpeg build + GPU support it)

**Created by [code-418.com](https://code-418.com)** — released into the public domain (see License).

## Quick start

```bash
# Docker
docker compose build
docker compose up
# open http://localhost:11223/
```

Map your library root to `/data` in `docker-compose.yml`.

## Configuration

All via environment variables (see `docker-compose.yml`):

| Var | Default | Description |
|-----|---------|-------------|
| `TARGET_DIR` | `/data` | Root to scan recursively |
| `CRF` | `22` | x264 quality (lower = higher quality/larger) |
| `PRESET` | `veryfast` | x264 or NVENC preset (`ultrafast..medium` or `p1..p7` for NVENC) |
| `AUDIO_BITRATE` | `160k` | AAC audio bitrate |
| `USE_NVENC` | `false` | `true` to use NVIDIA NVENC (requires GPU + NVENC-enabled ffmpeg) |
| `KEEP_ORIGINAL` | `false` | Keep original files alongside the MP4 outputs |
| `PRESERVE_TIMESTAMPS` | `true` | Apply source mtime/atime to output |
| `DRY_RUN` | `false` | Show what would happen without writing files |
| `PORT` | `11223` | Web UI port |

Supported extensions scanned: `avi, wmv, mov, mkv, flv, ts, m2ts, mts, m2t, mpg, mpeg, vob, mxf, webm, 3gp, 3g2, ogv, rm, rmvb, divx, xvid, f4v, m4v, mp4`.

## Notes

- Temp files are named like `.<stem>.transcoding.mp4` to keep `.mp4` as the last extension for muxer detection.
- We apply `-fflags +genpts`, `-vsync vfr`, audio resampling, and `-avoid_negative_ts make_zero` to stabilize timestamps.
- NVENC may not be available in Alpine’s default FFmpeg builds. Use a custom FFmpeg image if you need NVENC.

## Contributing

PRs welcome. By contributing, you agree to dedicate your changes to the public domain (The Unlicense).

## License

This is free and unencumbered software released into the public domain by **code-418.com**.
See `LICENSE` (The Unlicense) for full text.

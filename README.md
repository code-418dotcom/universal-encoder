# Universal Encoder

Self‑hosted, in‑place transcoder with a web UI. Remux when possible (h264+aac), else transcode to H.264 + AAC MP4 with `+faststart`. Live logs, queue controls, auto‑scan, and multi‑worker support.

**Creator:** code-418.com • **License:** The Unlicense (public domain).

## Quick start
```bash
docker compose up --build -d
# open http://localhost:11223
```

## Release & tags
- Project version lives in `VERSION` (also baked into the image & API).
- Tag a release: `git tag -a v2.2.0 -m "v2.2.0" && git push --tags`

## License
This project is dedicated to the public domain under **The Unlicense**.  
See `LICENSE` or <https://unlicense.org>.

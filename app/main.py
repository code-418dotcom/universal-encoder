import os
import asyncio
import json
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

TARGET_DIR = os.getenv("TARGET_DIR", "/data")
CRF = os.getenv("CRF", "22")
PRESET = os.getenv("PRESET", "veryfast")
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "160k")
USE_NVENC = os.getenv("USE_NVENC", "false").lower() == "true"
KEEP_ORIGINAL = os.getenv("KEEP_ORIGINAL", "false").lower() == "true"
PRESERVE_TIMESTAMPS = os.getenv("PRESERVE_TIMESTAMPS", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

EXTENSIONS = ["avi","wmv","mov","mkv","flv","ts","m2ts","mts","m2t","mpg","mpeg","vob","mxf","webm","3gp","3g2","ogv","rm","rmvb","divx","xvid","f4v","m4v","mp4"]

app = FastAPI(title="Transcoder Debug UI", version="1.0")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


class WSManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, msg: Dict[str, Any]):
        stale = []
        for ws in list(self.active):
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)


ws_manager = WSManager()
RUNNING = False
CANCEL = False


def log(message: str):
    return {"type": "log", "ts": time.time(), "message": message}


async def ws_log(message: str):
    await ws_manager.broadcast(log(message))


async def ffprobe_duration(path: str) -> Optional[float]:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        val = float(out.decode().strip())
        return val if val > 0 else None
    except Exception:
        return None


async def ffprobe_codecs(path: str) -> Dict[str, Optional[str]]:
    async def codec(stream: str):
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", f"{stream}:0",
            "-show_entries", "stream=codec_name", "-of", "csv=p=0", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        t = out.decode().strip()
        return t if t else None

    v = await codec("v")
    a = await codec("a")
    return {"v": v, "a": a}


def build_ffmpeg_cmd(src: str, dst_tmp: str, transcode: bool) -> list[str]:
    base = ["ffmpeg", "-nostdin", "-fflags", "+genpts", "-y", "-i", src]
    if transcode:
        common = [
            "-vsync", "vfr", "-vf", "setpts=PTS-STARTPTS",
            "-af", "aresample=async=1000:min_hard_comp=0.100:first_pts=0,asetpts=PTS-STARTPTS",
            "-map", "0:v:0", "-map", "0:a?:0",
        ]
        if USE_NVENC:
            video = ["-c:v", "h264_nvenc", "-preset", PRESET, "-rc", "vbr", "-cq", "23", "-pix_fmt", "yuv420p"]
        else:
            video = ["-c:v", "libx264", "-preset", PRESET, "-tune", "fastdecode", "-pix_fmt", "yuv420p", "-crf", CRF]
        audio = ["-c:a", "aac", "-ar", "48000", "-b:a", AUDIO_BITRATE, "-ac", "2"]
        tail = ["-movflags", "+faststart", "-avoid_negative_ts", "make_zero", "-progress", "pipe:1", dst_tmp]
        return base + common + video + audio + tail
    else:
        return base + ["-c", "copy", "-movflags", "+faststart", "-avoid_negative_ts", "make_zero", "-progress", "pipe:1", dst_tmp]


async def run_ffmpeg(src: str, dst_tmp: str, duration: Optional[float], transcode: bool):
    cmd = build_ffmpeg_cmd(src, dst_tmp, transcode)
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def read_stdout():
        nonlocal duration
        buf = {}
        last_send = 0.0
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            s = line.decode(errors="ignore").strip()
            if "=" in s:
                k, v = s.split("=", 1)
                buf[k] = v
                if k == "progress" and v in ("continue", "end"):
                    out_ms = float(buf.get("out_time_ms", "0") or 0.0)
                    speed = buf.get("speed", "")
                    try:
                        sfloat = float(speed.rstrip("x")) if speed.endswith("x") else None
                    except Exception:
                        sfloat = None
                    percent = None
                    eta = None
                    if duration and duration > 0:
                        percent = min(99.9, (out_ms / 1e6) / duration * 100.0)
                        if sfloat and sfloat > 0:
                            remaining = max(0.0, duration - (out_ms / 1e6))
                            eta = remaining / sfloat
                    now = time.time()
                    if now - last_send >= 0.2 or v == "end":
                        await ws_manager.broadcast({
                            "type": "progress", "file": src, "percent": percent,
                            "speed": sfloat, "eta": eta, "stage": v
                        })
                        last_send = now
                    if v == "end":
                        break

    async def read_stderr():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            await ws_manager.broadcast({"type": "log", "ts": time.time(), "message": line.decode(errors="ignore").rstrip()})

    await asyncio.gather(read_stdout(), read_stderr())
    rc = await proc.wait()
    return rc


async def process_file(src: str):
    p = Path(src)
    dst = str(p.with_suffix(".mp4"))
    dst_tmp = str(p.with_name("." + p.stem + ".transcoding.mp4"))

    dur = await ffprobe_duration(src)
    codecs = await ffprobe_codecs(src)
    transcode = not (codecs.get("v") == "h264" and codecs.get("a") == "aac")

    await ws_log(f"Processing: {src} (duration={dur if dur else 'unknown'}s) transcode={transcode}")
    if DRY_RUN:
        await ws_log(f"[DRY_RUN] Would {'transcode' if transcode else 'remux'} -> {dst}")
        return True

    rc = await run_ffmpeg(src, dst_tmp, dur, transcode)
    if rc == 0:
        if PRESERVE_TIMESTAMPS:
            try:
                os.utime(dst_tmp, (os.path.getatime(src), os.path.getmtime(src)))
            except Exception:
                pass
        os.replace(dst_tmp, dst)
        if not KEEP_ORIGINAL and os.path.abspath(dst) != os.path.abspath(src):
            try:
                os.remove(src)
            except Exception as e:
                await ws_log(f"[warn] Could not remove original: {e}")
        await ws_log(f"[ok] {src} -> {dst}")
        await ws_manager.broadcast({"type": "done", "file": src, "ok": True})
        return True
    else:
        await ws_log(f"[error] ffmpeg rc={rc} for {src}")
        await ws_manager.broadcast({"type": "done", "file": src, "ok": False})
        try:
            if os.path.exists(dst_tmp):
                os.remove(dst_tmp)
        except Exception:
            pass
        return False


def should_pick(path: Path) -> bool:
    return path.suffix.lower().lstrip(".") in EXTENSIONS


async def scan_tree(root: str) -> list[str]:
    found: list[str] = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            if should_pick(p):
                found.append(str(p))
    return found


async def worker_loop():
    global RUNNING, CANCEL
    RUNNING = True
    CANCEL = False
    try:
        files = await scan_tree(TARGET_DIR)
        await ws_log(f"Queued {len(files)} files under {TARGET_DIR}")
        for f in files:
            if CANCEL:
                break
            await process_file(f)
    finally:
        RUNNING = False
        await ws_log("Worker finished.")


from fastapi import APIRouter
router = APIRouter()


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/config")
async def get_config():
    return {
        "target_dir": TARGET_DIR,
        "crf": CRF,
        "preset": PRESET,
        "audio_bitrate": AUDIO_BITRATE,
        "use_nvenc": USE_NVENC,
        "keep_original": KEEP_ORIGINAL,
        "preserve_timestamps": PRESERVE_TIMESTAMPS,
        "dry_run": DRY_RUN,
    }


@app.post("/scan")
async def start_scan():
    global RUNNING
    if RUNNING:
        return JSONResponse({"status": "already-running"})
    asyncio.create_task(worker_loop())
    return {"status": "started"}


@app.post("/stop")
async def stop_scan():
    global CANCEL
    CANCEL = True
    return {"status": "stopping"}


@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        await ws.send_text(json.dumps({"type": "hello", "message": "connected"}))
        while True:
            _ = await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)

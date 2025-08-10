import os
import asyncio
import json
import time
import signal
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Body
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

app = FastAPI(title="Transcoder Debug UI", version="1.6.1")
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
PRIORITY_RUNNING = False

QUEUE: List[str] = []

CURRENT_PROC: Optional[asyncio.subprocess.Process] = None
CURRENT_FILE: Optional[str] = None
PAUSED_PROC: Optional[asyncio.subprocess.Process] = None
PAUSED_FILE: Optional[str] = None


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
    global CURRENT_PROC
    proc = await asyncio.create_subprocess_exec(
        *build_ffmpeg_cmd(src, dst_tmp, transcode),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True
    )
    CURRENT_PROC = proc

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
                    percent, eta = None, None
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
    CURRENT_PROC = None
    return rc


async def process_file(src: str):
    global CURRENT_FILE
    CURRENT_FILE = src
    p = Path(src)
    if p.name.endswith(".transcoding.mp4"):
        await ws_log(f"[skip] temp file: {src}")
        CURRENT_FILE = None
        return True

    dst = str(p.with_suffix(".mp4"))
    dst_tmp = str(p.with_name("." + p.stem + ".transcoding.mp4"))
    dur = await ffprobe_duration(src)
    codecs = await ffprobe_codecs(src)
    transcode = not (codecs.get("v") == "h264" and codecs.get("a") == "aac")

    await ws_manager.broadcast({"type": "queue_pop", "file": src})
    await ws_log(f"Processing: {src} (duration={dur if dur else 'unknown'}s) transcode={transcode}")
    if DRY_RUN:
        await ws_log(f"[DRY_RUN] Would {'transcode' if transcode else 'remux'} -> {dst}")
        CURRENT_FILE = None
        return True

    rc = await run_ffmpeg(src, dst_tmp, dur, transcode)
    ok = (rc == 0)
    if ok:
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
    else:
        try:
            if os.path.exists(dst_tmp):
                os.remove(dst_tmp)
        except Exception:
            pass
        await ws_log(f"[error] ffmpeg rc={rc} for {src}")
        await ws_manager.broadcast({"type": "done", "file": src, "ok": False})
    CURRENT_FILE = None
    return ok


def should_pick(path: Path) -> bool:
    n = path.name
    if n.endswith(".transcoding.mp4"):
        return False
    return path.suffix.lower().lstrip(".") in EXTENSIONS


async def scan_only(root: str) -> None:
    global QUEUE
    QUEUE = []
    await ws_manager.broadcast({"type": "queue_reset"})
    batch = []
    BATCH = 200
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            if should_pick(p):
                fp = str(p)
                QUEUE.append(fp)
                batch.append({"file": fp, "dir": str(p.parent)})
                if len(batch) >= BATCH:
                    await ws_manager.broadcast({"type": "queue_append", "items": batch, "total": len(QUEUE)})
                    batch = []
    if batch:
        await ws_manager.broadcast({"type": "queue_append", "items": batch, "total": len(QUEUE)})
    await ws_log(f"Queued {len(QUEUE)} files under {root} (scan only)")


async def worker_loop():
    global RUNNING, CANCEL
    RUNNING = True
    CANCEL = False
    try:
        while not CANCEL and QUEUE:
            f = QUEUE.pop(0)
            await process_file(f)
        await ws_log("Stopped or queue is empty.")
    finally:
        RUNNING = False


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
        "running": RUNNING,
        "queue_len": len(QUEUE),
        "current": CURRENT_FILE,
    }


@app.get("/queue")
async def get_queue(limit: int = Query(500, ge=1, le=5000)):
    items = [{"file": f, "dir": str(Path(f).parent)} for f in QUEUE[:limit]]
    return {"total": len(QUEUE), "items": items}


@app.post("/scan")
async def api_scan():
    await scan_only(TARGET_DIR)
    return {"status": "scanned", "total": len(QUEUE)}


@app.post("/start")
async def api_start():
    global RUNNING
    if RUNNING:
        return JSONResponse({"status": "already-running"})
    if not QUEUE:
        return JSONResponse({"status": "empty"}, status_code=400)
    asyncio.create_task(worker_loop())
    return {"status": "started"}


@app.post("/stop")
async def api_stop():
    global CANCEL, CURRENT_PROC, CURRENT_FILE, PAUSED_PROC, PAUSED_FILE
    CANCEL = True
    await ws_log("Stop requested: halting current job and leaving queue intact.")
    await ws_manager.broadcast({"type": "stopping", "file": CURRENT_FILE})

    if CURRENT_FILE and (CURRENT_FILE not in QUEUE):
        QUEUE.insert(0, CURRENT_FILE)
    if PAUSED_FILE and (PAUSED_FILE not in QUEUE):
        QUEUE.insert(0, PAUSED_FILE)

    for proc in (CURRENT_PROC, PAUSED_PROC):
        if proc and (proc.returncode is None):
            try: os.killpg(proc.pid, signal.SIGINT)
            except Exception: pass
            await asyncio.sleep(0.5)
            if proc.returncode is None:
                try: os.killpg(proc.pid, signal.SIGTERM)
                except Exception: pass
            await asyncio.sleep(0.8)
            if proc.returncode is None:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except Exception: pass

    PAUSED_PROC = None
    PAUSED_FILE = None
    return {"status": "stopping"}


@app.post("/queue/top")
async def queue_top(payload: Dict[str, str] = Body(...)):
    file = payload.get("file")
    if not file:
        return JSONResponse({"error": "missing file"}, status_code=400)
    try:
        idx = QUEUE.index(file)
    except ValueError:
        return JSONResponse({"error": "file not in queue"}, status_code=404)
    QUEUE.pop(idx)
    QUEUE.insert(0, file)
    await ws_manager.broadcast({"type": "queue_move", "file": file, "to": 0})
    await ws_log(f"[queue] Moved to top: {file}")
    return {"status": "ok"}


@app.post("/transcode_now")
async def transcode_now(payload: Dict[str, str] = Body(...)):
    global PRIORITY_RUNNING, PAUSED_PROC, PAUSED_FILE
    file = payload.get("file")
    if not file:
        return JSONResponse({"error": "missing file"}, status_code=400)

    if CURRENT_FILE and os.path.abspath(CURRENT_FILE) == os.path.abspath(file):
        return {"status": "already-current"}

    try:
        idx = QUEUE.index(file)
        QUEUE.pop(idx)
        await ws_manager.broadcast({"type": "queue_pop", "file": file})
    except ValueError:
        pass

    if PRIORITY_RUNNING:
        return JSONResponse({"status": "busy", "message": "priority job running"}, status_code=409)

    PRIORITY_RUNNING = True
    await ws_log(f"[priority] NOW: {file}")

    async def do_now():
        nonlocal file
        global PRIORITY_RUNNING, PAUSED_PROC, PAUSED_FILE
        if CURRENT_PROC and (CURRENT_PROC.returncode is None):
            try:
                CURRENT_PROC.send_signal(signal.SIGSTOP)
                PAUSED_PROC = CURRENT_PROC
                PAUSED_FILE = CURRENT_FILE
                await ws_manager.broadcast({"type": "paused", "file": PAUSED_FILE})
                await ws_log(f"[priority] Paused current: {PAUSED_FILE}")
            except ProcessLookupError:
                PAUSED_PROC = None
                PAUSED_FILE = None

        await process_file(file)

        if PAUSED_PROC and (PAUSED_PROC.returncode is None):
            try:
                PAUSED_PROC.send_signal(signal.SIGCONT)
                await ws_manager.broadcast({"type": "resumed", "file": PAUSED_FILE})
                await ws_log(f"[priority] Resumed: {PAUSED_FILE}")
            except ProcessLookupError:
                await ws_log("[priority] Paused process already exited.")
        PAUSED_PROC = None
        PAUSED_FILE = None
        PRIORITY_RUNNING = False

    asyncio.create_task(do_now())
    return {"status": "started"}


@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        await ws.send_text(json.dumps({"type": "hello", "message": "connected"}))
        while True:
            _ = await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)

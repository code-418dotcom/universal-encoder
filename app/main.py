import os, asyncio, json, time, signal, unicodedata
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------- Configuration ----------
TARGET_DIR = os.getenv("TARGET_DIR", "/data")
RUNTIME = {
    "crf": os.getenv("CRF", "22"),
    "preset": os.getenv("PRESET", "veryfast"),
    "audio_bitrate": os.getenv("AUDIO_BITRATE", "160k"),
    "use_nvenc": os.getenv("USE_NVENC", "false").lower() == "true",
    "keep_original": os.getenv("KEEP_ORIGINAL", "false").lower() == "true",
    "preserve_timestamps": os.getenv("PRESERVE_TIMESTAMPS", "true").lower() == "true",
    "dry_run": os.getenv("DRY_RUN", "false").lower() == "true",
}
EXTENSIONS = [
    "avi","wmv","mov","mkv","flv","ts","m2ts","mts","m2t","mpg","mpeg","vob","mxf",
    "webm","3gp","3g2","ogv","rm","rmvb","divx","xvid","f4v","m4v","mp4"
]

app = FastAPI(title="Universal Encoder", version="2.2.0")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent/"static")), name="static")

# ---------- WebSocket bus ----------
class WS:
    def __init__(self): self.active:List[WebSocket] = []
    async def connect(self,ws:WebSocket): await ws.accept(); self.active.append(ws)
    def disconnect(self,ws:WebSocket): 
        if ws in self.active: self.active.remove(ws)
    async def send(self,msg:Dict[str,Any]):
        for ws in list(self.active):
            try: await ws.send_text(json.dumps(msg))
            except Exception: self.disconnect(ws)
ws = WS()

# ---------- Runtime state ----------
RUNNING=False; CANCEL=False
QUEUE:List[str]=[]; QUEUE_LOCK=asyncio.Lock()
CURRENT_PROCS:Dict[str,asyncio.subprocess.Process]={}   # keyed by normalized path
ORIG_NAME:Dict[str,str]={}                               # normalized -> original path string
PAUSED_SET:set[str]=set()
OPTIONS={"continuous_scan": False, "scan_interval": 60, "concurrency": 1, "auto_start": True}

# ---------- Helpers ----------
def nkey(p:str)->str:
    """Normalize for stable, case-insensitive, unicode-safe matching."""
    try:
        rp = os.path.realpath(p)
    except Exception:
        rp = os.path.abspath(p)
    return unicodedata.normalize("NFC", rp).casefold()

async def wlog(m:str): await ws.send({"type":"log","ts":time.time(),"message":m})

def proc_for(file_path:str)->Optional[asyncio.subprocess.Process]:
    """Find running process by normalized full path, with basename fallback."""
    k=nkey(file_path)
    if k in CURRENT_PROCS: return CURRENT_PROCS[k]
    b=unicodedata.normalize("NFC", Path(file_path).name).casefold()
    for key,proc in CURRENT_PROCS.items():
        if unicodedata.normalize("NFC", Path(ORIG_NAME.get(key,key)).name).casefold()==b:
            return proc
    return None

def should_pick(p:Path)->bool:
    return (not p.name.endswith(".transcoding.mp4")) and p.suffix.lower().lstrip(".") in EXTENSIONS

async def ffprobe_duration(path:str)->Optional[float]:
    proc=await asyncio.create_subprocess_exec(
        "ffprobe","-v","error","-select_streams","v:0","-show_entries","format=duration",
        "-of","default=nw=1:nk=1",path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out,_=await proc.communicate()
    try:
        v=float(out.decode().strip())
        return v if v>0 else None
    except Exception:
        return None

async def ffprobe_codecs(path:str)->Dict[str,Optional[str]]:
    async def one(sel):
        p=await asyncio.create_subprocess_exec(
            "ffprobe","-v","error","-select_streams",f"{sel}:0","-show_entries","stream=codec_name",
            "-of","csv=p=0",path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out,_=await p.communicate()
        t=out.decode().strip()
        return t or None
    return {"v":await one("v"), "a":await one("a")}

def build_ffmpeg_cmd(src:str, dst_tmp:str, transcode:bool)->list[str]:
    base=["ffmpeg","-nostdin","-fflags","+genpts","-y","-i",src]
    if transcode:
        common=["-vsync","vfr","-vf","setpts=PTS-STARTPTS",
                "-af","aresample=async=1000:min_hard_comp=0.100:first_pts=0,asetpts=PTS-STARTPTS",
                "-map","0:v:0","-map","0:a?:0"]
        if RUNTIME["use_nvenc"]:
            video=["-c:v","h264_nvenc","-preset",RUNTIME["preset"],"-rc","vbr","-cq","23","-pix_fmt","yuv420p"]
        else:
            video=["-c:v","libx264","-preset",RUNTIME["preset"],"-tune","fastdecode","-pix_fmt","yuv420p","-crf",RUNTIME["crf"]]
        audio=["-c:a","aac","-ar","48000","-b:a",RUNTIME["audio_bitrate"],"-ac","2"]
        return base + common + video + audio + ["-movflags","+faststart","-avoid_negative_ts","make_zero","-progress","pipe:1",dst_tmp]
    else:
        return base + ["-c","copy","-movflags","+faststart","-avoid_negative_ts","make_zero","-progress","pipe:1",dst_tmp]

async def run_ffmpeg(src:str, dst_tmp:str, duration:Optional[float], transcode:bool):
    proc=await asyncio.create_subprocess_exec(
        *build_ffmpeg_cmd(src,dst_tmp,transcode),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True
    )
    key=nkey(src); CURRENT_PROCS[key]=proc; ORIG_NAME[key]=src

    async def read_out():
        buf={}; last=0.0
        while True:
            line=await proc.stdout.readline()
            if not line: break
            s=line.decode(errors="ignore").strip()
            if "=" in s:
                k,v=s.split("=",1); buf[k]=v
                if k=="progress" and v in ("continue","end"):
                    out_ms=float(buf.get("out_time_ms","0") or 0.0)
                    speed=buf.get("speed","")
                    try: sfloat=float(speed.rstrip("x")) if speed.endswith("x") else None
                    except Exception: sfloat=None
                    percent=eta=None
                    if duration and duration>0:
                        percent=min(99.9,(out_ms/1e6)/duration*100.0)
                        if sfloat and sfloat>0:
                            eta=max(0.0,duration-(out_ms/1e6))/sfloat
                    now=time.time()
                    if now-last>=0.2 or v=="end":
                        await ws.send({"type":"progress","file":ORIG_NAME.get(key,src),"percent":percent,"speed":sfloat,"eta":eta,"stage":v}); last=now
                    if v=="end": break

    async def read_err():
        while True:
            line=await proc.stderr.readline()
            if not line: break
            await wlog(line.decode(errors="ignore").rstrip())

    await asyncio.gather(read_out(), read_err())
    rc=await proc.wait()
    CURRENT_PROCS.pop(key,None); ORIG_NAME.pop(key,None); PAUSED_SET.discard(key)
    return rc

async def process_file(src:str):
    p=Path(src)
    if p.name.endswith(".transcoding.mp4"): return True
    dst=str(p.with_suffix(".mp4")); tmp=str(p.with_name("."+p.stem+".transcoding.mp4"))
    dur=await ffprobe_duration(src); c=await ffprobe_codecs(src)
    trans=not (c.get("v")=="h264" and c.get("a")=="aac")

    await ws.send({"type":"queue_pop","file":src})
    await wlog(f"Processing: {src} (duration={dur if dur else 'unknown'}s) transcode={trans}")
    if RUNTIME["dry_run"]: await wlog(f"[DRY_RUN] Would {'transcode' if trans else 'remux'} -> {dst}"); return True

    rc=await run_ffmpeg(src,tmp,dur,trans)
    if rc==0:
        if RUNTIME["preserve_timestamps"]:
            try: os.utime(tmp,(os.path.getatime(src),os.path.getmtime(src)))
            except Exception: pass
        os.replace(tmp,dst)
        if not RUNTIME["keep_original"] and os.path.abspath(dst)!=os.path.abspath(src):
            try: os.remove(src)
            except Exception as e: await wlog(f"[warn] Could not remove original: {e}")
        await wlog(f"[ok] {src} -> {dst}"); await ws.send({"type":"done","file":src,"ok":True})
    else:
        try: 
            if os.path.exists(tmp): os.remove(tmp)
        except Exception: pass
        await wlog(f"[error] ffmpeg rc={rc} for {src}"); await ws.send({"type":"done","file":src,"ok":False})
    return rc==0

# ---------- REST ----------
@app.get("/")
async def index(): return FileResponse(Path(__file__).parent/"static"/"index.html")

@app.get("/config")
async def get_config():
    async with QUEUE_LOCK: qlen=len(QUEUE)
    return {"target_dir": TARGET_DIR, "running": RUNNING, "queue_len": qlen, "options": OPTIONS, "xcode": RUNTIME}

@app.get("/options")
async def options_get(): return OPTIONS

@app.post("/options")
async def options_set(payload:Dict[str,Any]=Body(...)):
    loop = asyncio.get_event_loop()
    changed=False
    if "continuous_scan" in payload: OPTIONS["continuous_scan"]=bool(payload["continuous_scan"]); changed=True
    if "scan_interval" in payload:
        try: OPTIONS["scan_interval"]=max(5,int(payload["scan_interval"])); changed=True
        except Exception: pass
    if "concurrency" in payload:
        try: OPTIONS["concurrency"]=max(1,min(8,int(payload["concurrency"]))); changed=True
        except Exception: pass
    if "auto_start" in payload: OPTIONS["auto_start"]=bool(payload["auto_start"]); changed=True
    if changed: await ws.send({"type":"options","options":OPTIONS})
    return {"status":"ok","options":OPTIONS}

@app.get("/queue")
async def get_queue():
    async with QUEUE_LOCK: items=[{"file":f,"dir":str(Path(f).parent)} for f in QUEUE]; total=len(QUEUE)
    return {"total":total,"items":items}

@app.post("/scan")
async def api_scan():
    global QUEUE
    async with QUEUE_LOCK: QUEUE=[]
    await ws.send({"type":"queue_reset"})
    running_keys=set(CURRENT_PROCS.keys())|set(PAUSED_SET)
    added=[]
    for d,_,files in os.walk(TARGET_DIR):
        for name in files:
            p=Path(d)/name
            if not should_pick(p): continue
            k=nkey(str(p))
            if k in running_keys: continue
            async with QUEUE_LOCK: QUEUE.append(str(p))
            added.append({"file":str(p),"dir":str(p.parent)})
    if added: await ws.send({"type":"queue_append","items":added,"total":len(QUEUE)})
    await wlog(f"Queued {len(QUEUE)} files under {TARGET_DIR} (scan only)")
    return {"status":"ok","queued":len(QUEUE)}

async def pop_next()->Optional[str]:
    async with QUEUE_LOCK:
        if QUEUE: return QUEUE.pop(0)
        return None

WORKERS:List[asyncio.Task]=[]
async def worker(i:int):
    await wlog(f"[worker {i}] started")
    try:
        while not CANCEL:
            f=await pop_next()
            if not f: break
            await process_file(f)
    finally:
        await wlog(f"[worker {i}] stopped")

@app.post("/start")
async def start_pool():
    global RUNNING,CANCEL,WORKERS
    if RUNNING: return {"status":"already-running"}
    async with QUEUE_LOCK:
        if not QUEUE: return {"status":"empty"}
    RUNNING=True; CANCEL=False; WORKERS=[]
    n=max(1,min(8,int(OPTIONS.get("concurrency",1))))
    for i in range(n): WORKERS.append(asyncio.create_task(worker(i+1)))
    async def waiter():
        global RUNNING,WORKERS
        await asyncio.gather(*WORKERS,return_exceptions=True)
        RUNNING=False; WORKERS=[]
        await wlog("Worker pool finished.")
    asyncio.create_task(waiter())
    return {"status":"started","workers":n}

@app.post("/stop")
async def stop_pool():
    global CANCEL,RUNNING
    CANCEL=True
    # return paused/running to queue front
    for key in list(PAUSED_SET)+list(CURRENT_PROCS.keys()):
        f=ORIG_NAME.get(key,key)
        async with QUEUE_LOCK:
            if f not in QUEUE: QUEUE.insert(0,f)
    for key,proc in list(CURRENT_PROCS.items()):
        try:
            pgid=os.getpgid(proc.pid)
        except Exception:
            pgid=None
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGKILL):
            try:
                if pgid is not None: os.killpg(pgid,sig)
                os.kill(proc.pid,sig)
            except Exception: pass
            await asyncio.sleep(0.25)
            if proc.returncode is not None: break
    RUNNING=False
    await ws.send({"type":"jobs_clear"})
    return {"status":"stopping"}

@app.get("/state")
async def state():
    return {"running_files":[ORIG_NAME.get(k,k) for k in CURRENT_PROCS.keys()],
            "paused":[ORIG_NAME.get(k,k) for k in PAUSED_SET], "queue_len": len(QUEUE)}

@app.post("/job/toggle")
async def toggle(payload:Dict[str,str]=Body(...)):
    f=payload.get("file")
    if not f: return JSONResponse({"error":"missing file"},status_code=400)
    proc=proc_for(f)
    if not proc or (proc.returncode is not None):
        return JSONResponse({"error":"not-running"},status_code=404)
    key=nkey(f)
    try: pgid=os.getpgid(proc.pid)
    except Exception: pgid=None
    if key in PAUSED_SET:
        if pgid is not None:
            try: os.killpg(pgid, signal.SIGCONT)
            except Exception: pass
        try: os.kill(proc.pid, signal.SIGCONT)
        except Exception: pass
        await asyncio.sleep(0.05)
        PAUSED_SET.discard(key)
        await ws.send({"type":"resumed","file":ORIG_NAME.get(key,f)})
        await wlog(f"[job] Resumed: {ORIG_NAME.get(key,f)}")
        return {"status":"resumed"}
    else:
        if pgid is not None:
            try: os.killpg(pgid, signal.SIGSTOP)
            except Exception: pass
        try: os.kill(proc.pid, signal.SIGSTOP)
        except Exception: pass
        PAUSED_SET.add(key)
        await ws.send({"type":"paused","file":ORIG_NAME.get(key,f)})
        await wlog(f"[job] Paused: {ORIG_NAME.get(key,f)}")
        return {"status":"paused"}

@app.post("/job/stop")
async def job_stop(payload:Dict[str,Any]=Body(...)):
    f=payload.get("file")
    if not f: return JSONResponse({"error":"missing file"},status_code=400)
    proc=proc_for(f)
    if not proc or (proc.returncode is not None):
        return JSONResponse({"error":"not-running"},status_code=404)
    try: pgid=os.getpgid(proc.pid)
    except Exception: pgid=None
    await ws.send({"type":"stopping","file":f})
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGKILL):
        try:
            if pgid is not None: os.killpg(pgid,sig)
            os.kill(proc.pid,sig)
        except Exception: pass
        await asyncio.sleep(0.3)
        if proc.returncode is not None: break
    await ws.send({"type":"job_stopped","file":f})
    return {"status":"stopped"}

@app.post("/queue/top")
async def queue_top(payload:Dict[str,str]=Body(...)):
    f=payload.get("file")
    if not f: return {"status":"ok"}
    async with QUEUE_LOCK:
        if f in QUEUE:
            QUEUE.remove(f); QUEUE.insert(0,f)
    await ws.send({"type":"queue_move","file":f,"to":0})
    return {"status":"ok"}

@app.post("/transcode_now")
async def transcode_now(payload:Dict[str,str]=Body(...)):
    f=payload.get("file")
    if not f: return {"status":"ok"}
    async with QUEUE_LOCK:
        if f in QUEUE: QUEUE.remove(f)
    await ws.send({"type":"queue_pop","file":f})
    asyncio.create_task(process_file(f))
    return {"status":"queued-or-started"}

# ---------- WebSocket ----------
@app.websocket("/ws")
async def websocket(ws_conn:WebSocket):
    await ws.connect(ws_conn)
    try:
        await ws_conn.send_text(json.dumps({"type":"hello","message":"connected"}))
        while True:
            _ = await ws_conn.receive_text()
    except WebSocketDisconnect:
        ws.disconnect(ws_conn)

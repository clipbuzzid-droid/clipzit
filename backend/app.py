"""Clipzit server — FastAPI: upload/YouTube -> job AI clipping -> klip 9:16 + subtitle."""
from __future__ import annotations
import os, json, copy, time, uuid, shutil, threading, traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from clipper import probe, transcribe, find_clips, write_srt, write_ass, render_clip, make_thumbnail
import publisher

ROOT = Path(__file__).resolve().parent.parent
# CLIPZIT_STORE_DIR mengarahkan SEMUA state (jobs/creds/uploads/clips) ke folder
# lain. Wajib dipakai tes/E2E supaya tidak menyentuh storage produksi.
STORE = Path(os.getenv("CLIPZIT_STORE_DIR") or (ROOT/"storage"))
UPLOADS = STORE/"uploads"; CLIPS = STORE/"clips"
UPLOADS.mkdir(parents=True, exist_ok=True); CLIPS.mkdir(parents=True, exist_ok=True)
JOBS_FILE = STORE/"jobs.json"
FRONTEND = ROOT/"frontend"

app = FastAPI(title="Clipzit — AI Clipper")
pool = ThreadPoolExecutor(max_workers=2)
# RLock: _save() butuh lock yang sama tapi sering dipanggil dari dalam blok
# `with _lock` (re-entrant), kalau pakai Lock biasa -> deadlock.
_lock = threading.RLock()
_jobs: dict = {}
if JOBS_FILE.exists():
    try:
        _loaded = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        if isinstance(_loaded, dict):
            _jobs = _loaded
    except Exception:
        _jobs = {}

def _save():
    """Tulis state job atomik. Wajib di dalam lock: json.dumps atas dict yang
    sedang dimutasi worker lain -> RuntimeError 'dictionary changed size'."""
    with _lock:
        tmp = JOBS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_jobs, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, JOBS_FILE)

def _set(jid, **kw):
    with _lock:
        if jid not in _jobs:
            return
        _jobs[jid].update(kw); _save()

class YTRequest(BaseModel):
    url: str
    num_clips: int = 5
    min_dur: int = 25
    max_dur: int = 50
    ratio: str = "9:16"
    with_subs: bool = True
    lang: str = "auto"

def _new_job(source: str, title: str, opts: dict) -> str:
    jid = uuid.uuid4().hex[:10]
    with _lock:
        _jobs[jid] = {"id":jid,"title":title,"source":source,"status":"queued",
                      "created":time.time(),
                      "progress":"Antre…","progress_pct":5,"clips":[],**opts}
        _save()
    return jid

def _process(jid: str, video_path: str, opts: dict):
    try:
        _set(jid, status="transcribing",
             progress="Menganalisis video…", progress_pct=12)
        info = probe(video_path)
        dur = info["duration"]
        if dur < 5: raise RuntimeError("Video terlalu pendek (<5 detik).")
        # clamp opsi dari form: num_clips=0 -> pembagian nol di find_clips,
        # min>maks bikin window kosong.
        try:
            n_clips = max(1, min(30, int(opts.get("num_clips", 5) or 5)))
            min_d = max(5, min(120, int(opts.get("min_dur", 25) or 25)))
            max_d = max(min_d, min(180, int(opts.get("max_dur", 50) or 50)))
        except (TypeError, ValueError):
            n_clips, min_d, max_d = 5, 25, 50
        _set(jid, duration=round(dur,1), progress="Transkripsi AI (faster-whisper)…", progress_pct=25)
        try:
            segs = transcribe(video_path, model_size=os.getenv("CLIPZIT_MODEL","tiny"),
                              language=opts.get("lang","auto"))
        except Exception as e:
            segs = []
            _set(jid, progress=f"Transkrip gagal ({e}), pakai potongan merata…")
        _set(jid, progress="Mencari momen viral…", progress_pct=55,
             transcript=" ".join(s["text"] for s in segs)[:4000])
        clips = find_clips(segs, dur, n_clips, min_d, max_d)
        _set(jid, status="rendering", progress=f"Merender {len(clips)} klip…",
             progress_pct=65, segments=len(segs))
        out_clips = []
        for i, c in enumerate(clips, 1):
            _set(jid, progress=f"Merender klip {i}/{len(clips)}…",
                 progress_pct=65+int(30*i/len(clips)))
            base = f"{jid}_clip{i}"
            srt = str(CLIPS/f"{base}.srt")
            write_srt(c.get("words",[]), c["start"], srt)
            ass = str(CLIPS/f"{base}.ass")
            write_ass(c.get("words",[]), c["start"], ass)
            (CLIPS/f"{base}.words.json").write_text(
                json.dumps(c.get("words",[]), ensure_ascii=False), encoding="utf-8")
            mp4 = str(CLIPS/f"{base}.mp4")
            render_clip(video_path, c["start"], c["end"], srt, mp4,
                        ratio=opts.get("ratio","9:16"), with_subs=opts.get("with_subs",True),
                        ass_path=ass)
            thumb = str(CLIPS/f"{base}.jpg")
            make_thumbnail(mp4, 1.0, thumb)
            out_clips.append({
                "n":i, "start":c["start"], "end":c["end"],
                "dur":round(c["end"]-c["start"],1), "score":c["score"],
                "text":c["text"][:500], "reasons":c.get("reasons",[]),
                "video":f"/clips/{base}.mp4", "thumb":f"/clips/{base}.jpg",
                "srt":f"/clips/{base}.srt", "ass":f"/clips/{base}.ass"})
        _set(jid, status="done", progress="Selesai ✓", progress_pct=100, clips=out_clips)
    except Exception as e:
        traceback.print_exc()
        _set(jid, status="error", progress=f"Gagal: {e}", progress_pct=100)

@app.post("/api/jobs/upload")
async def upload_job(file: UploadFile = File(...),
                     num_clips: int = Form(5), min_dur: int = Form(25),
                     max_dur: int = Form(50), ratio: str = Form("9:16"),
                     with_subs: bool = Form(True), lang: str = Form("auto")):
    ext = os.path.splitext(file.filename or "video.mp4")[1].lower() or ".mp4"
    if ext not in (".mp4",".mov",".mkv",".webm",".m4v",".avi"):
        raise HTTPException(400, "Format harus video (mp4/mov/mkv/webm).")
    jid = _new_job("upload", file.filename or "video",
                   {"num_clips":num_clips,"min_dur":min_dur,"max_dur":max_dur,
                    "ratio":ratio,"with_subs":with_subs,"lang":lang})
    dest = UPLOADS/f"{jid}{ext}"
    try:
        # baca per-chunk + await: copyfileobj sinkron memblokir event loop,
        # jadi /api/jobs ikut macet selama upload besar.
        with open(dest, "wb") as f:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        with _lock:
            _jobs.pop(jid, None)
            _save()
        raise HTTPException(500, "Gagal menyimpan file upload.")
    _set(jid, file=str(dest))
    pool.submit(_process, jid, str(dest),
                {"num_clips":num_clips,"min_dur":min_dur,"max_dur":max_dur,
                 "ratio":ratio,"with_subs":with_subs,"lang":lang})
    return {"id": jid}

@app.post("/api/jobs/youtube")
async def youtube_job(req: YTRequest):
    import yt_dlp
    jid = _new_job("youtube", req.url, req.model_dump())
    def _dl():
        try:
            _set(jid, status="downloading", progress="Mengunduh dari YouTube…", progress_pct=8)
            # %(ext)s: ekstensi nyata bisa .mp4/.webm/.mkv — template ".mp4" bikin
            # path yang dipakai _process tidak ada di disk.
            outtmpl = str(UPLOADS/f"{jid}.%(ext)s")
            with yt_dlp.YoutubeDL({"format":"bv*[height<=720]+ba/b[height<=720]/b",
                                    "outtmpl":outtmpl, "quiet":True, "noplaylist":True,
                                    "merge_output_format":"mp4"}) as ydl:
                meta = ydl.extract_info(req.url, download=True)
                title = (meta or {}).get("title") or req.url
                path = ydl.prepare_filename(meta) if meta else ""
            if not path or not os.path.exists(path):
                raise RuntimeError(f"hasil unduhan tidak ditemukan (path: {path or 'kosong'})")
            _set(jid, title=title, file=path)
            pool.submit(_process, jid, path, req.model_dump())
        except Exception as e:
            _set(jid, status="error", progress=f"Unduhan gagal: {e}")
    threading.Thread(target=_dl, daemon=True).start()
    return {"id": jid}

@app.get("/api/jobs")
def list_jobs():
    with _lock:
        items = copy.deepcopy(list(_jobs.values()))
    # urut terbaru dulu (id acak -> urutan tidak deterministik)
    return sorted(items, key=lambda j:(j.get("created") or 0, j.get("id","")),
                  reverse=True)[:30]

@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    with _lock:
        job = _jobs.get(jid)
        if job is None:
            raise HTTPException(404, "Job tidak ada")
        # deepcopy: serialisasi dict hidup sambil worker menulis -> RuntimeError
        return copy.deepcopy(job)

@app.delete("/api/jobs/{jid}")
def del_job(jid: str):
    with _lock:
        job = _jobs.get(jid)
        if job is None:
            raise HTTPException(404, "Job tidak ada")
        clips = copy.deepcopy(job.get("clips", []))
        f = job.get("file")
        _jobs.pop(jid, None)
        _save()
    for c in clips:
        for k in ("video", "thumb", "srt", "ass"):
            p = CLIPS / (os.path.basename(c.get(k, "") or "") or ".")
            if p.name != "." and p.exists():
                try: p.unlink()
                except OSError: pass
        w = CLIPS / (f"{jid}_clip{c.get('n','')}.words.json")
        if w.exists():
            try: w.unlink()
            except OSError: pass
    if f and os.path.exists(f):
        try: os.remove(f)
        except OSError: pass
    return {"ok": True}

@app.get("/api/health")
def health(): return {"ok": True, "jobs": len(_jobs)}

# ====================== Auto-upload (publisher) ======================

class CredsRequest(BaseModel):
    platform: str
    creds: dict = {}

class PublishRequest(BaseModel):
    jid: str
    n: int                  # nomor klip (1-based)
    platform: str
    title: str = ""
    description: str = ""
    tags: str = ""
    privacy: str = "private"

@app.get("/api/creds")
def get_creds_status():
    return publisher.creds_status()

@app.post("/api/creds")
def put_creds(req: CredsRequest):
    if req.platform not in publisher.PLATFORMS:
        raise HTTPException(400, f"Platform tidak dikenal: {req.platform}")
    publisher.save_creds(req.platform, req.creds)
    return {"ok": True, **publisher.creds_status()}

@app.delete("/api/creds/{platform}")
def del_creds(platform: str):
    if platform not in publisher.PLATFORMS:
        raise HTTPException(400, f"Platform tidak dikenal: {platform}")
    publisher.delete_creds(platform)
    return {"ok": True, **publisher.creds_status()}

# ---- YouTube one-click OAuth connect ----

class YtConnectRequest(BaseModel):
    client_id: str
    client_secret: str

@app.post("/api/oauth/youtube/start")
def yt_oauth_start(req: YtConnectRequest):
    """Simpan client_id/secret dulu, lalu balikin URL consent Google."""
    cid, secret = req.client_id.strip(), req.client_secret.strip()
    if not cid or not secret:
        raise HTTPException(400, "Client ID dan Client Secret wajib diisi.")
    if not cid.endswith(".apps.googleusercontent.com"):
        raise HTTPException(400, "Client ID tidak valid — harus diakhiri .apps.googleusercontent.com")
    if len(secret) < 10:
        raise HTTPException(400, "Client Secret tidak valid (terlalu pendek).")
    # merge, jangan replace: token lama tetap ada kalau user reconnect & gagal
    existing_creds = publisher.get_creds("youtube") or {}
    publisher.save_creds("youtube", {**existing_creds, "client_id": cid, "client_secret": secret})
    url = publisher.yt_auth_url(cid)
    return {"ok": True, "auth_url": url}

@app.get("/api/oauth/youtube/callback")
def yt_oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Google mengarahkan ke sini setelah user Allow. Tukar code -> token."""
    html_ok = ("<html><body style='font-family:sans-serif;text-align:center;padding-top:3em'>"
               "<h2>✅ YouTube terhubung ke Clipzit</h2>"
               "<p>Boleh tutup tab ini dan kembali ke aplikasi.</p></body></html>")
    html_err = ("<html><body style='font-family:sans-serif;text-align:center;padding-top:3em'>"
                "<h2>❌ {msg}</h2><p>Kembali ke Clipzit dan coba lagi.</p></body></html>")
    if error:
        return HTMLResponse(html_err.format(msg=error), status_code=400)
    if not code or not state or not publisher.yt_state_take(state):
        return HTMLResponse(html_err.format(msg="State OAuth tidak valid (kedaluwarsa / dipakai dua kali)"), status_code=400)
    existing = publisher.get_creds("youtube") or {}
    try:
        tok = publisher.yt_exchange(code, existing)
    except publisher.PublishError as e:
        return HTMLResponse(html_err.format(msg=str(e)[:300]), status_code=400)
    publisher.save_creds("youtube", tok)
    return HTMLResponse(html_ok)

@app.get("/api/oauth/youtube/client")
def yt_oauth_client_status():
    """Cek apakah client_id/secret sudah tersimpan (untuk tombol Connect di UI)."""
    c = publisher.get_creds("youtube") or {}
    return {"has_client": bool(c.get("client_id") and c.get("client_secret")),
            "connected": bool(c.get("access_token"))}

@app.post("/api/publish")
def publish_clip(req: PublishRequest):
    if req.platform not in publisher.PLATFORMS:
        raise HTTPException(400, "Platform tidak dikenal")
    creds = publisher.get_creds(req.platform)
    if not creds:
        name = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}.get(req.platform, req.platform)
        raise HTTPException(400, f"{name}: kredensial belum disimpan")
    # temukan path video klip + siapkan slot status publish (satu blok lock:
    # menulis dict job di luar lock bikin json.dumps/_save() race)
    with _lock:
        job = _jobs.get(req.jid)
        if job is None:
            raise HTTPException(404, "Job tidak ada")
        clip = next((c for c in job.get("clips", []) if c.get("n") == req.n), None)
        if not clip:
            raise HTTPException(404, f"Klip #{req.n} tidak ada di job ini")
        video_url = clip.get("video") or ""
        status = clip.setdefault("publish", {})
        meta = {"title": req.title, "description": req.description,
                "tags": req.tags, "privacy": req.privacy}
        status[req.platform] = {"status": "uploading", "platform": req.platform,
                                "progress": "Mulai upload…", "error": None, "result": None}
        _save()
    vname = os.path.basename(video_url)
    vpath = CLIPS / vname
    if not vpath.exists():
        raise HTTPException(404, f"File klip tidak ada: {vname}")

    def _run():
        try:
            res = publisher.publish(req.platform, str(vpath), meta, dict(creds))
            with _lock:
                st = _clip_slot(req.jid, req.n)
                if st is not None:
                    st[req.platform] = {"status": "done", "progress": "Selesai ✓",
                                        "result": res, "error": None}
                    _save()
        except Exception as e:
            with _lock:
                st = _clip_slot(req.jid, req.n)
                if st is not None:
                    st[req.platform] = {"status": "error", "progress": "Gagal",
                                        "error": str(e)}
                    _save()
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "status": status[req.platform]}


def _clip_slot(jid: str, n: int) -> dict | None:
    """Slot status publish klip n (dipanggil HANYA saat _lock sudah dipegang).
    None bila job/klip sudah dihapus user -> worker tidak boleh KeyError."""
    job = _jobs.get(jid)
    if not job:
        return None
    for c in job.get("clips", []):
        if c.get("n") == n:
            return c.setdefault("publish", {})
    return None

app.mount("/clips", StaticFiles(directory=str(CLIPS)), name="clips")

@app.get("/")
def index(): return FileResponse(str(FRONTEND/"index.html"))

if FRONTEND.exists():
    # mount "/style.css" TIDAK menyajikan file (StaticFiles melayani PATH di bawah
    # mount point, jadi "/style.css" -> 404). Pakai satu mount direktori.
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=int(os.getenv("PORT","8787")), reload=False)

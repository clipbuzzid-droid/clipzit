"""Clipzit core: transcribe (faster-whisper) -> score viral clips -> render 9:16 + subtitle (ffmpeg)."""
from __future__ import annotations
import os, re, json, math, shutil, subprocess, tempfile
from dataclasses import dataclass
from pathlib import Path

HOOK_WORDS = [
    # ID
    "rahasia","gratis","cara","jangan","bukti","viral","uang","sukses","gagal","bahaya",
    "penting","terbukti","cuan","bisnis","modal","untung","rugi","tips","trik","hack",
    "pertama","terbesar","termurah","terbaik","wajib","stop","dilarang","sebenarnya",
    "fakta","mitos","kesalahan","solusi","strategi","peluang","miliarder","gaji",
    "kerja","kuliah","skripsi","beasiswa","investasi","saham","kripto","politik",
    # EN
    "secret","free","how","never","always","proof","money","success","fail","mistake",
    "warning","truth","myth","hack","strategy","million","billion","viral","insane",
    "crazy","guaranteed","proven","stop","why",
]

QUESTION_RE = re.compile(r"\?")
NUMBER_RE = re.compile(r"\d+")


def _find_ffmpeg_tool(tool: str) -> str:
    """Cari ffmpeg/ffprobe yang layak: build dengan libass (filter subtitles)
    diprioritaskan; build Octave yang menutupi PATH tidak dipakai."""
    cands = shutil.which(tool) or ""
    hits = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip().strip('"')
        if not d:
            continue
        p = os.path.join(d, tool + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(p) and p not in hits:
            hits.append(p)
    if cands and cands not in hits:
        hits.append(cands)

    def ok(path: str) -> bool:
        try:
            r = subprocess.run([path, "-hide_banner", "-filters"],
                               capture_output=True, text=True, timeout=10)
            return "subtitles" in r.stdout
        except Exception:
            return False

    for p in hits:
        if ok(p):
            return p
    return cands  # tidak ada yang punya libass -> biarkan default (fallback jalan)


_FFMPEG = _find_ffmpeg_tool("ffmpeg")
_FFPROBE = _find_ffmpeg_tool("ffprobe")


def _num(v, default: float = 0.0) -> float:
    """Koersi aman untuk output tool eksternal: None/''/'N/A' -> default."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def probe(path: str) -> dict:
    """ffprobe: duration, width, height."""
    out = subprocess.run(
        [_FFPROBE, "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True)
    try:
        info = json.loads(out.stdout or "{}")
    except ValueError:
        info = {}
    if not isinstance(info, dict):
        info = {}
    fmt = info.get("format") or {}
    if not isinstance(fmt, dict):
        fmt = {}
    dur = _num(fmt.get("duration"))
    streams = info.get("streams") or []
    w = h = 0
    for s in streams:
        if not isinstance(s, dict):
            continue
        if s.get("codec_type") == "video" and not w:
            w, h = int(_num(s.get("width"))), int(_num(s.get("height")))
    return {"duration":dur,"width":w,"height":h}


def transcribe(video_path: str, model_size: str = "tiny", language: str | None = None) -> list[dict]:
    """Transkrip dengan faster-whisper. Return segmen [{start,end,text,words[]}]."""
    from faster_whisper import WhisperModel
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    lang = None if (not language or language=="auto") else language
    segments, _ = model.transcribe(video_path, language=lang, word_timestamps=True,
                                   vad_filter=True,
                                   vad_parameters={"min_silence_duration_ms":500})
    out = []
    for s in segments:
        words = [{"start":w.start,"end":w.end,"word":w.word.strip()}
                 for w in (s.words or []) if w.word and w.word.strip()]
        out.append({"start":float(s.start),"end":float(s.end),"text":s.text.strip(),"words":words})
    return out


def _window_text(segs: list[dict], t0: float, t1: float) -> tuple[str,list[dict]]:
    inside = [s for s in segs if s["end"]>t0 and s["start"]<t1]
    text = " ".join(s["text"] for s in inside)
    words = []
    for s in inside:
        for w in s.get("words",[]):
            if w["end"]>t0 and w["start"]<t1:
                words.append(w)
    if not words:  # fallback tanpa word timestamp
        words = [{"start":t0,"end":t1,"word":text.strip()}]
    return text, words


def _score_window(text: str, dur: float) -> tuple[float,list[str]]:
    score = 50.0
    reasons: list[str] = []
    low = text.lower()
    hooks = sum(1 for h in HOOK_WORDS if h in low)
    if hooks:
        add = min(22, hooks*7); score += add; reasons.append(f"{hooks} kata hook (+{add})")
    q = len(QUESTION_RE.findall(text))
    if q:
        add = min(12, q*6); score += add; reasons.append(f"{q} pertanyaan (+{add})")
    if NUMBER_RE.search(text):
        score += 6; reasons.append("Ada angka/data (+6)")
    if "!" in text:
        score += 5; reasons.append("Emfatik (+5)")
    n_words = len(text.split())
    wpm = (n_words/dur*60) if dur>0 else 0
    if 130 <= wpm <= 200:
        score += 8; reasons.append(f"Tempo bicara ideal {wpm:.0f} wpm (+8)")
    elif wpm > 60:
        score += 3; reasons.append(f"Tempo {wpm:.0f} wpm (+3)")
    if 28 <= dur <= 45:
        score += 10; reasons.append("Durasi sweet-spot 28–45 dtk (+10)")
    elif 20 <= dur <= 60:
        score += 5; reasons.append("Durasi ideal Shorts/Reels (+5)")
    if n_words >= 40:
        score += 5; reasons.append(f"Padat isi {n_words} kata (+5)")
    if any(k in low for k in ["kamu","anda","kalian","guys","teman","bro"]):
        score += 4; reasons.append("Menyapa penonton (+4)")
    return round(min(99,max(35,score)),1), reasons


def find_clips(segments: list[dict], duration: float, num_clips: int = 5,
               min_dur: int = 25, max_dur: int = 50) -> list[dict]:
    """Sliding-window di atas transkrip. Fallback: potongan merata bila sepi bicara."""
    min_dur = max(5, int(min_dur))
    max_dur = max(min_dur, int(max_dur))
    num_clips = max(1, int(num_clips))  # normalisasi: nol/negatif dari form UI
    cands: list[dict] = []
    if segments and duration > 10:
        step = 8
        wlen = min(max_dur, max(min_dur, 35))
        t = 0.0
        while t + min_dur <= duration:
            t1 = min(duration, t + wlen)
            if t1 - t < min_dur:
                break
            text, words = _window_text(segments, t, t1)
            if len(text.split()) >= 8:
                s, reasons = _score_window(text, t1-t)
                # bonus hook di 3 detik pertama (retensi pembuka)
                head, _ = _window_text(segments, t, t+3)
                if any(h in head.lower() for h in HOOK_WORDS):
                    s = min(99, s+5); reasons.append("Hook di 3 detik pertama (+5)")
                cands.append({"start":round(t,2),"end":round(t1,2),"text":text,
                              "words":words,"score":s,"reasons":reasons})
            t += step
    if not cands:  # fallback video tanpa/minim bicara
        n = max(1, min(num_clips, int(duration // min_dur) or 1))
        chunk = duration / n
        bounds = [round(i * chunk, 2) for i in range(n)] + [round(duration, 2)]
        for i in range(n):
            t0, t1 = bounds[i], bounds[i + 1]
            cands.append({"start": t0, "end": t1,
                          "text": "(tanpa transkrip — potongan merata)",
                          "words": [], "score": 70.0 - i,
                          "reasons": ["Fallback: audio minim bicara"]})
        return cands[:num_clips]
    # NMS: urut skor, buang yang overlap >40% dengan yang sudah dipilih
    cands.sort(key=lambda c:-c["score"])
    picked: list[dict] = []
    for c in cands:
        ok = True
        for p in picked:
            inter = max(0, min(c["end"],p["end"])-max(c["start"],p["start"]))
            span = min(c["end"]-c["start"], p["end"]-p["start"])
            if span>0 and inter/span > 0.4:
                ok = False; break
        if ok:
            picked.append(c)
        if len(picked)>=num_clips:
            break
    picked.sort(key=lambda c:-c["score"])
    # normalisasi skor ke rentang 78–97 agar terasa seperti Vizard
    if picked:
        mn, mx = min(c["score"] for c in picked), max(c["score"] for c in picked)
        for c in picked:
            if mx>mn:
                c["score"] = round(78+19*(c["score"]-mn)/(mx-mn),1)
            else:
                c["score"] = round(min(97,c["score"]+18),1)
    return picked


def _fmt_srt_time(t: float) -> str:
    ms = int(round(t*1000))
    h, ms = divmod(ms,3600000); m, ms = divmod(ms,60000); s, ms = divmod(ms,1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(words: list[dict], t0: float, path: str, max_chars: int = 42):
    """Kelompokkan kata jadi baris SRT 1–2 baris gaya Shorts."""
    groups: list[list[dict]] = []
    cur: list[dict] = []
    chars = 0
    for idx, w in enumerate(words):
        txt = w.get("word", "").strip()
        if not txt:
            continue
        cur.append(w); chars += len(txt) + 1
        gap = (words[idx + 1]["start"] - w["end"]) if idx + 1 < len(words) else 99
        if chars >= max_chars or w["end"] - cur[0]["start"] >= 2.4 or gap > 0.6:
            groups.append(cur); cur = []; chars = 0
    if cur:
        groups.append(cur)
    if not groups:
        groups = [[{"start": t0, "end": t0 + 2, "word": "..."}]]
    with open(path, "w", encoding="utf-8") as f:
        for i, g in enumerate(groups, 1):
            s = max(0.0, g[0]["start"] - t0)
            e = max(s, g[-1]["end"] - t0)
            f.write(f"{i}\n{_fmt_srt_time(s)} --> {_fmt_srt_time(e)}\n"
                    f"{' '.join(x['word'] for x in g).strip()}\n\n")


def _ass_time(t: float) -> str:
    cs = int(round(max(0, t) * 100))
    h, cs = divmod(cs, 360000); m, cs = divmod(cs, 6000); s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_esc(t: str) -> str:
    return t.replace("{", "(").replace("}", ")").replace("\n", " ").strip()


NEG_WORDS = {"tidak","jangan","bukan","enggak","nggak","gak","never","no","not",
             "don't","dont","can't","cant","won't","wont","stop","dilarang"}


def _word_color(w: str) -> str:
    low = w.lower().strip(".,!?;:\"'()")
    if NUMBER_RE.search(low):
        return "WGreen"
    if low in NEG_WORDS:
        return "WRed"
    if low in HOOK_WORDS:
        return "WYellow"
    return "WWhite"


def write_ass(words: list[dict], t0: float, path: str, hold: float = 0.30):
    """Subtitle pop-word ala Shorts viral: SATU kata muncul tepat saat diucapkan,
    besar di tengah layar. Warna: angka=hijau, negasi=merah, hook=kuning,
    lainnya=putih."""
    clean = []
    for w in words:
        txt = _ass_esc(str(w.get("word", "")).upper()).strip()
        if txt:
            clean.append({"start": float(w["start"]), "end": float(w["end"]), "word": txt})
    if not clean:
        clean = [{"start": t0, "end": t0 + 2, "word": "..."}]
    # Font pop-word: Impact bila tersedia (bawaan Windows); kalau tidak, libass
    # akan diam-diam memakai font default — lebih baik minta Arial eksplisit.
    font = "Impact"
    try:
        if os.name == "nt":
            windir = os.environ.get("WINDIR", r"C:\Windows")
            if not os.path.exists(os.path.join(windir, "Fonts", "impact.ttf")):
                font = "Arial"
    except Exception:
        pass
    base = (f"{{style}},{font},88,&H00XXXXXX,&H00FFFFFF,&H00000000,&H00000000,"
            "-1,0,0,0,100,100,1.5,0,1,4,1,5,60,60,0,1")
    styles = {
        "WYellow": base.replace("&H00XXXXXX", "&H0000E6FF"),
        "WWhite": base.replace("&H00XXXXXX", "&H00FFFFFF"),
        "WGreen": base.replace("&H00XXXXXX", "&H0076E600"),
        "WRed": base.replace("&H00XXXXXX", "&H003B3BFF"),
    }
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
                "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
                "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                "MarginL, MarginR, MarginV, Encoding\n")
        for name, s in styles.items():
            f.write(f"Style: {s}\n".replace("{style}", name))
        f.write("\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
                "MarginR, MarginV, Effect, Text\n")
        for i, w in enumerate(clean):
            nxt = clean[i + 1]["start"] if i + 1 < len(clean) else w["end"] + hold
            end = min(w["end"] + hold, nxt + 0.02)
            if end <= w["start"]:
                end = w["start"] + 0.15
            color = _word_color(w["word"])
            f.write(f"Dialogue: 0,{_ass_time(w['start'] - t0)},"
                    f"{_ass_time(end - t0)},{color},,0,0,0,,"
                    f"{{\\fad(60,60)}}{w['word']}\n")


def render_clip(src: str, start: float, end: float, srt_path: str | None,
                out_path: str, ratio: str = "9:16", with_subs: bool = True,
                ass_path: str | None = None):
    """Render 1 klip: potong + reframe vertikal (blur-bg ala Vizard) + bakar subtitle."""
    dur = max(1, end-start)
    # Path absolut: cwd ffmpeg dipindah ke folder subtitle (agar filter subtitles
    # menerima basename tanpa escaping), jadi src/out relatif akan salah resolve
    # -> "Error opening input" lalu fallback tanpa subtitle (klip bisu teks).
    src = os.path.abspath(src)
    out_path = os.path.abspath(out_path)
    filters: list[str] = []
    if ratio == "9:16":
        # foreground crop tengah + background blur penuh (efek Vizard/Opus)
        filters.append(
            "[0:v]split[a][b];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=40[bg];"
            "[b]scale=1080:-2:flags=lanczos[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
        )
    else:
        filters.append("[0:v]scale=1280:720:force_original_aspect_ratio=decrease,"
                       "pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p[v]")
    vmap = "[v]"
    ass = ass_path if (ass_path and os.path.exists(ass_path)) else None
    srt = srt_path if (srt_path and os.path.exists(srt_path)) else None
    if with_subs and (ass or srt):
        if ass:  # karaoke CapCut, gaya sudah di dalam file .ass
            filters.append(f"[v]subtitles={os.path.basename(ass)}[vs]")
        else:
            style = ("FontName=Arial,FontSize=13,PrimaryColour=&H00FFFFFF,"
                     "OutlineColour=&H80000000,BorderStyle=1,Outline=2,Shadow=0,"
                     "Alignment=2,MarginV=70")
            filters.append(f"[v]subtitles={os.path.basename(srt)}:force_style='{style}'[vs]")
        vmap = "[vs]"
    fc = ";".join(filters)
    sub_used = ass or srt  # path lengkap file subtitle yang dipakai (untuk cwd ffmpeg)
    common = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
    cmd = [_FFMPEG, "-y", "-ss", str(start), "-i", src, "-t", str(dur),
           "-filter_complex", fc, "-map", vmap, "-map", "0:a?",
           *common, "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
           "-shortest", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       cwd=(os.path.dirname(sub_used) or None) if sub_used else None)
    if r.returncode != 0 or not os.path.exists(out_path):
        # fallback 1: tanpa subtitle bila filter subtitles/font gagal
        cmd2 = [_FFMPEG, "-y", "-ss", str(start), "-i", src, "-t", str(dur),
                "-filter_complex", filters[0], "-map", "[v]", "-map", "0:a?",
                *common, "-c:a", "aac", "-movflags", "+faststart",
                "-shortest", out_path]
        r2 = subprocess.run(cmd2, capture_output=True, text=True)
        if r2.returncode != 0:
            # fallback 2: format input tidak didukung decoder build ini
            # (mis. AV1/opus pada ffmpeg lama) -> transcode dulu ke h264/aac
            tmp = tempfile.mktemp(suffix=".mp4", dir=os.path.dirname(out_path))
            tr = subprocess.run(
                [_FFMPEG, "-y", "-i", src, "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "20", "-c:a", "aac", "-b:a", "128k", tmp],
                capture_output=True, text=True)
            if tr.returncode == 0 and os.path.exists(tmp):
                # input WAJIB diganti ke hasil transcode: cmd2 = [ff,-y,-ss,start,-i,src,-t,dur,...]
                # -> [ff,-y,-ss,start] + [-i,tmp] + [-t,dur, ...]
                cmd3 = cmd2[:4] + ["-i", tmp] + cmd2[6:]
                r3 = subprocess.run(cmd3, capture_output=True, text=True)
                try: os.remove(tmp)
                except OSError: pass
                if r3.returncode == 0 and os.path.exists(out_path):
                    return out_path
            raise RuntimeError("ffmpeg gagal: " + (r.stderr or r2.stderr)[-1500:])
    return out_path


def make_thumbnail(src: str, at: float, out_path: str):
    """Ambil 1 frame sebagai thumbnail. Return path bila berhasil, None bila gagal
    (frame di luar durasi / decode error) — jangan biarkan file 0-byte dipakai UI."""
    try:
        r = subprocess.run([_FFMPEG,"-y","-ss",str(at),"-i",src,"-frames:v","1",
                            "-vf","scale=540:-2",out_path],
                           capture_output=True)
    except OSError:
        return None
    if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    try:
        os.remove(out_path)
    except OSError:
        pass
    return None

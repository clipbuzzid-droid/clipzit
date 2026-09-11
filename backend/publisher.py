"""Clipzit publisher — auto-upload klip ke TikTok / YouTube / Instagram via API resmi.

Kredensial disimpan di storage/creds.json (jangan commit, jangan bagikan).
Tiap platform butuh token akses sendiri-sendiri (lihat README). Publish berjalan
async lewat threading; status disimpan per-klip di job.

Alur tiap platform (API resmi, 2026):
- TikTok : POST /v2/post/publish/inbox/video/init/ (FILE_UPLOAD) -> upload_url
           -> PUT video ke upload_url (start/end Content-Range) -> publish_id.
           Ini meng-upload ke inbox draf creator (tanpa app-teraudit langsung publish).
- YouTube: POST /upload/youtube/v3/videos?uploadType=resumable -> Location
           -> PUT video binary + Content-Range -> video id (langsung publish sesuai privacy).
- Instagram: POST /<ig-id>/media (REELS, upload_type=resumable) -> container id
           -> POST https://rupload.facebook.com/ig-api-upload/<id> (binary)
           -> POST /<ig-id>/media_publish?creation_id=<id> -> media id.
"""
from __future__ import annotations
import os, math, json, re, threading, mimetypes
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
# CLIPZIT_STORE_DIR: lihat app.py — tes/E2E WAJIB memakai store terpisah.
STORE = Path(os.getenv("CLIPZIT_STORE_DIR") or (ROOT / "storage"))
CREDS_FILE = STORE / "creds.json"
_lock = threading.Lock()

PLATFORMS = ("tiktok", "youtube", "instagram")

# TikTok: chunk harus banyak-nya kelipatan 1024 byte per docs lama; gunakan 10MB.
TIKTOK_CHUNK = 10 * 1024 * 1024
GRAPH_VERSION = "v25.0"

# Redirect OAuth HARUS sama dengan port server yang benar-benar jalan (app.py pakai
# env PORT) — kalau hardcode 8787 sementara server di port lain, callback tidak nyampe.
OAUTH_PORT = os.getenv("PORT", "8787")
YT_REDIRECT_URI = f"http://127.0.0.1:{OAUTH_PORT}/api/oauth/youtube/callback"


class PublishError(RuntimeError):
    """Kesalahan publikasi yang ramah ditampilkan di UI."""


# ---------------- Credential store ----------------

def _load() -> dict:
    if CREDS_FILE.exists():
        try:
            return json.loads(CREDS_FILE.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
    return {}


def _save(c: dict):
    CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CREDS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CREDS_FILE)


def save_creds(platform: str, creds: dict) -> None:
    """Simpan/mutakhirkan kredensial satu platform.

    MERGE, bukan replace: UI hanya mengirim field yang benar-benar diisi (input
    password selalu kosong saat panel dibuka), jadi replace akan menghapus token
    lama hanya karena user tidak mengetik ulang. Pakai delete_creds() untuk
    menghapus kredensial platform."""
    if platform not in PLATFORMS:
        raise PublishError(f"Platform tidak dikenal: {platform}")
    clean = {k: (v.strip() if isinstance(v, str) else v) for k, v in (creds or {}).items()}
    clean = {k: v for k, v in clean.items() if v not in ("", None)}
    with _lock:
        store = _load()
        merged = dict(store.get(platform) or {})
        merged.update(clean)
        store[platform] = merged
        _save(store)


def delete_creds(platform: str) -> None:
    with _lock:
        store = _load()
        store.pop(platform, None)
        _save(store)


def get_creds(platform: str) -> dict:
    with _lock:
        return _load().get(platform) or {}


def creds_status() -> dict:
    """Info status (connected / field apa yang kurang) tanpa membuka rahasia."""
    out = {}
    with _lock:
        store = _load()
        for p in PLATFORMS:
            c = store.get(p, {})
            missing = _missing_fields(p, c)
            out[p] = {
                "connected": not missing,
                "missing": missing,
                "fields": _field_labels(p),
                "has_any": bool(c),
            }
    return out


def _missing_fields(p: str, c: dict) -> list[str]:
    required = {
        "tiktok": ["client_key", "client_secret", "access_token"],
        "youtube": ["access_token"],
        "instagram": ["access_token", "ig_user_id"],
    }[p]
    return [f for f in required if not c.get(f)]


def _field_labels(p: str) -> dict[str, str]:
    return {
        "tiktok": {
            "client_key": "Client Key",
            "client_secret": "Client Secret",
            "access_token": "Access Token (video.upload)",
        },
        "youtube": {
            "access_token": "Access Token (youtube.upload)",
            "refresh_token": "Refresh Token (opsional, biar token auto-renew)",
            "client_id": "Client ID (opsional, pasangan refresh token)",
            "client_secret": "Client Secret (opsional, pasangan refresh token)",
        },
        "instagram": {
            "access_token": "Access Token (instagram_content_publish)",
            "ig_user_id": "IG User ID",
        },
    }[p]


# ---------------- YouTube OAuth connect (one-click) ----------------

YT_SCOPE = "https://www.googleapis.com/auth/youtube.upload"

def _state_key(state: str) -> Path | None:
    """Path file state OAuth. None bila state tidak berbentuk token (anti path-traversal)."""
    if not state or not re.fullmatch(r"[A-Za-z0-9_\-]{8,128}", state):
        return None
    return STORE / f"oauth_state_{state}.json"

def yt_auth_url(client_id: str) -> str:
    """Buat state acak + URL consent. State disimpan utk verifikasi callback."""
    import secrets, time
    state = secrets.token_urlsafe(16)
    path = _state_key(state)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"created": time.time()}), encoding="utf-8")
    os.replace(tmp, path)
    from urllib.parse import urlencode
    qs = urlencode({
        "client_id": client_id,
        "redirect_uri": YT_REDIRECT_URI,
        "response_type": "code",
        "scope": YT_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    return "https://accounts.google.com/o/oauth2/v2/auth?" + qs

def yt_exchange(code: str, creds_existing: dict) -> dict:
    """Tukar authorization code -> token, gabung dengan client_id/secret tersimpan."""
    client = creds_existing or {}
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": client.get("client_id", ""),
        "client_secret": client.get("client_secret", ""),
        "redirect_uri": YT_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=60)
    if r.status_code != 200:
        raise PublishError(f"YouTube OAuth gagal: {r.text[:300]}")
    tok = r.json() or {}
    return {
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token") or client.get("refresh_token", ""),
        "client_id": client.get("client_id", ""),
        "client_secret": client.get("client_secret", ""),
    }

def yt_state_take(state: str) -> bool:
    """Verifikasi + hapus state (one-time, anti-CSRF)."""
    path = _state_key(state)
    if path is None or not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        pass
    return True


# ---------------- Providers ----------------

def _mime(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    return mt or "video/mp4"


def _put_stat(resp: requests.Response, what: str):
    if not (200 <= resp.status_code < 300):
        raise PublishError(f"{what} gagal (HTTP {resp.status_code}): {resp.text[:500]}")


def publish(platform: str, video_path: str, meta: dict, creds: dict) -> dict:
    """Jalankan publish satu platform. Return dict hasil (urlid/track)."""
    fn = {
        "tiktok": _publish_tiktok,
        "youtube": _publish_youtube,
        "instagram": _publish_instagram,
    }.get(platform)
    if fn is None:
        raise PublishError(f"Platform tidak dikenal: {platform}")
    return fn(video_path, meta or {}, creds or {})


# --- TikTok ---

def _publish_tiktok(video_path: str, meta: dict, creds: dict) -> dict:
    for f in _missing_fields("tiktok", creds):
        raise PublishError(f"TikTok: isi dulu {f}")
    size = os.path.getsize(video_path)
    chunks = max(1, math.ceil(size / TIKTOK_CHUNK))
    r = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
        headers={"Authorization": f"Bearer {creds['access_token']}",
                 "Content-Type": "application/json; charset=UTF-8"},
        json={"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                              "chunk_size": TIKTOK_CHUNK, "total_chunk_count": chunks}},
        timeout=60,
    )
    _put_stat(r, "Init TikTok")
    data = (r.json().get("data") or {})
    upload_url = data.get("upload_url")
    publish_id = data.get("publish_id")
    if not upload_url:
        raise PublishError("TikTok: tidak dapat upload_url di respons init: " + r.text[:400])

    with open(video_path, "rb") as fh:
        for i in range(chunks):
            start = i * TIKTOK_CHUNK
            end = min(start + TIKTOK_CHUNK, size) - 1
            fh.seek(start)
            body = fh.read(end - start + 1)
            up = requests.put(
                upload_url,
                data=body,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Content-Type": _mime(video_path),
                    "Content-Length": str(len(body)),
                },
                timeout=300,
            )
            if up.status_code != 206:
                # 206 = chunk diterima sebagian; selain itu gagal.
                # (dicek di luar blok 2xx karena 206 sudah termasuk 2xx)
                raise PublishError(f"TikTok upload chunk {i+1} gagal (HTTP {up.status_code}): {up.text[:400]}")

    return {"platform": "tiktok", "ok": True, "publish_id": publish_id,
            "note": "Ter-upload ke inbox TikTok (klik notifikasi untuk menyelesaikan posting)"}


# --- YouTube ---

def _youtube_token(creds: dict) -> str:
    """Access token YouTube; refresh otomatis bila refresh_token + client tersedia
    (token playground cuma hidup ~1 jam — tanpa refresh, upload kedua selalu 401)."""
    if creds.get("refresh_token") and creds.get("client_id") and creds.get("client_secret"):
        try:
            r = requests.post(
                "https://oauth2.googleapis.com/token",
                data={"client_id": creds["client_id"],
                      "client_secret": creds["client_secret"],
                      "refresh_token": creds["refresh_token"],
                      "grant_type": "refresh_token"},
                timeout=60)
            if r.status_code == 200:
                tok = (r.json() or {}).get("access_token")
                if tok:
                    return tok
        except Exception:
            pass  # gagal refresh -> pakai token lama, biarkan error asli muncul
    return creds["access_token"]


def _publish_youtube(video_path: str, meta: dict, creds: dict) -> dict:
    for f in _missing_fields("youtube", creds):
        raise PublishError(f"YouTube: isi dulu {f}")
    token = _youtube_token(creds)
    title = (meta.get("title") or os.path.splitext(os.path.basename(video_path))[0])[:100]
    desc = (meta.get("description") or "")[:5000]
    tags = [t.strip() for t in (meta.get("tags") or "").split(",") if t.strip()][:500]
    privacy = (meta.get("privacy") or "private").strip().lower()
    if privacy not in ("public", "private", "unlisted"):
        privacy = "private"
    size = os.path.getsize(video_path)
    body = {
        "snippet": {"title": title, "description": desc, "tags": tags or None},
        "status": {"privacyStatus": privacy},
    }

    r = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": _mime(video_path),
        },
        json=body,
        timeout=60,
    )
    _put_stat(r, "Init YouTube")
    session_uri = r.headers.get("Location")
    if not session_uri:
        raise PublishError("YouTube: tidak ada Location (session upload) di respons: " + r.text[:400])

    with open(video_path, "rb") as fh:
        data = fh.read()
    r2 = requests.put(
        session_uri,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Length": str(size),
            "Content-Type": _mime(video_path),
        },
        timeout=900,
    )
    if not (200 <= r2.status_code < 300):
        raise PublishError(f"YouTube upload gagal (HTTP {r2.status_code}): {r2.text[:500]}")
    video_id = None
    try:
        video_id = r2.json().get("id")
    except Exception:
        pass
    return {"platform": "youtube", "ok": True, "video_id": video_id,
            "url": f"https://youtu.be/{video_id}" if video_id else None}


# --- Instagram ---

def _publish_instagram(video_path: str, meta: dict, creds: dict) -> dict:
    for f in _missing_fields("instagram", creds):
        raise PublishError(f"Instagram: isi dulu {f}")
    token = creds["access_token"]
    ig_id = creds["ig_user_id"].strip()
    caption = (meta.get("description") or meta.get("caption") or "").strip()[:2200]

    # 1) buat container REELS (resumable upload untuk file lokal)
    pr = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption,
        "share_to_feed": "true",
        "access_token": token,
    }
    r = requests.post(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{ig_id}/media",
        data=pr, timeout=60,
    )
    _put_stat(r, "Buat container Instagram")
    try:
        cj = r.json() or {}
    except ValueError:
        raise PublishError("Instagram: respons bukan JSON: " + (r.text or "")[:400])
    if not isinstance(cj, dict):
        cj = {}
    container_id = str(cj.get("id") or "").strip()
    if not container_id:
        raise PublishError("Instagram: tidak ada id container di respons: " + r.text[:400])
    # cek status_code apakah video siap (opsional; langsung lanjut)
    # container_id boleh berbentuk {id}; gunakan id polos

    # 2) upload binary via rupload
    size = os.path.getsize(video_path)
    with open(video_path, "rb") as fh:
        data = fh.read()
    up = requests.post(
        f"https://rupload.facebook.com/ig-api-upload/{container_id}",
        data=data,
        headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(size),
            "Content-Type": _mime(video_path),
        },
        timeout=900,
    )
    _put_stat(up, "Upload Instagram")

    # 3) publish container
    pub = requests.post(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{ig_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=120,
    )
    _put_stat(pub, "Publish Instagram")
    media_id = None
    try:
        media_id = pub.json().get("id")
    except Exception:
        pass
    return {"platform": "instagram", "ok": True, "media_id": media_id,
            "url": f"https://instagram.com/p/{media_id}" if media_id else None}
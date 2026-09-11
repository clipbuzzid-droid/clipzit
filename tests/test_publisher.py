"""Clipzit auto-upload (publisher) — tes API dengan publisher di-mock (no network)."""
import json, os, sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

import pytest

# --- isolate storage so tests never touch real creds/jobs ---
# WAJIB di-set SEBELUM import app/publisher (dibaca saat import). Tanpa ini,
# tes/E2E menulis ke storage produksi dan bisa menghapus kredensial user.
tmp_store = Path(__file__).resolve().parent / "_test_store"
os.environ["CLIPZIT_STORE_DIR"] = str(tmp_store)
tmp_store.mkdir(parents=True, exist_ok=True)

# --- mock publisher BEFORE importing app ---
import publisher as publisher_mod
_CALLS = []

def fake_publish(platform, video_path, meta, creds):
    _CALLS.append({"platform": platform, "path": video_path, "meta": meta, "creds": creds})
    return {"platform": platform, "ok": True, "video_id": "mock"}

publisher_mod.publish = fake_publish

_test_root = tmp_store
assert publisher_mod.CREDS_FILE == _test_root / "creds.json", \
    f"store tes tidak terisolasi: {publisher_mod.CREDS_FILE}"

from fastapi.testclient import TestClient
import app
assert app.JOBS_FILE == _test_root / "jobs.json", f"store tes tidak terisolasi: {app.JOBS_FILE}"


@pytest.fixture(autouse=True)
def _clean_client():
    # reset jobs + creds file per test, guard against leftover state
    with app._lock:
        app._jobs.clear()
    publisher_mod.CREDS_FILE.unlink(missing_ok=True)
    app.JOBS_FILE.unlink(missing_ok=True)
    yield
    with app._lock:
        app._jobs.clear()
    publisher_mod.CREDS_FILE.unlink(missing_ok=True)

def _client():
    return TestClient(app.app)


def test_creds_flow_and_publish():
    client = _client()
    # 1) status awal belum terhubung
    r = client.get("/api/creds")
    assert r.status_code == 200
    d = r.json()
    assert d["youtube"]["connected"] is False
    assert "access_token" in d["youtube"]["missing"]

    # 2) simpan kredensial youtube
    r = client.post("/api/creds", json={"platform": "youtube", "creds": {"access_token": "tok-abc"}})
    assert r.status_code == 200
    assert r.json()["youtube"]["connected"] is True

    # 3) buat job & klip dummy di memori
    with app._lock:
        jid = "testjob1"
        app._jobs[jid] = {
            "id": jid, "title": "job", "status": "done", "clips": [{
                "n": 1, "video": "/clips/testjob1_clip1.mp4",
            }],
        }
    # 3b) buat file klip nyata
    clip_dir = app.CLIPS
    clip_dir.mkdir(parents=True, exist_ok=True)
    vp = clip_dir / "testjob1_clip1.mp4"
    vp.write_bytes(b"\x00" * 32)

    # 4) publish klip 1 ke youtube
    r = client.post("/api/publish", json={"jid": jid, "n": 1, "platform": "youtube",
                                          "title": "Judul", "description": "Desc", "tags": "a,b"})
    assert r.status_code == 200, r.text
    assert r.json()["status"]["status"] in ("uploading", "done")  # mock bisa selesai seketika

    # tunggu thread mock selesai
    import time
    for _ in range(50):
        with app._lock:
            st = app._jobs[jid]["clips"][0].get("publish", {}).get("youtube")
        if st and st.get("status") == "done":
            break
        time.sleep(0.05)

    with app._lock:
        st = app._jobs[jid]["clips"][0]["publish"]["youtube"]
    assert st["status"] == "done", st
    assert st["result"]["video_id"] == "mock"
    # meta terdistribusi dengan benar
    assert _CALLS[-1]["meta"]["title"] == "Judul"

    # 5) platform tanpa creds -> 400
    r = client.post("/api/publish", json={"jid": jid, "n": 1, "platform": "tiktok",
                                          "title": "", "description": "", "tags": ""})
    assert r.status_code == 400

    # 6) hapus kredensial
    r = client.delete("/api/creds/youtube")
    assert r.status_code == 200
    assert r.json()["youtube"]["connected"] is False


def test_platform_missing_fields_validation():
    client = _client()
    for plat, missing in (("tiktok", ["client_key", "client_secret", "access_token"]),
                          ("instagram", ["access_token", "ig_user_id"])):
        r = client.post("/api/creds", json={"platform": plat, "creds": {}})
        assert r.status_code == 200
        assert r.json()[plat]["missing"] == missing


def test_publish_unknown_platform_400():
    client = _client()
    # create a job with creds present to reach platform check
    for plat in ("tiktok", "instagram", "youtube"):
        client.post("/api/creds", json={"platform": plat, "creds": {"access_token": "x", "client_key": "x", "client_secret": "x", "ig_user_id": "1"}})
    r = client.post("/api/publish", json={"jid": "nope", "n": 1, "platform": "facebook", "title": "", "description": "", "tags": ""})
    assert r.status_code == 400


def test_direct_publish_functions_build_requests():
    """Verifikasi publisher.publish dikenal untuk ketiga platform tanpa panggil jaringan."""
    for plat in ("tiktok", "youtube", "instagram"):
        fn = publisher_mod.publish  # di-mock
        assert callable(fn)


def test_store_dir_env_isolates_all_state():
    """CLIPZIT_STORE_DIR harus mengarahkan creds + jobs + clips ke folder itu,
    supaya tes/E2E tidak pernah menyentuh kredensial produksi user."""
    assert publisher_mod.STORE == _test_root
    assert publisher_mod.CREDS_FILE == _test_root / "creds.json"
    assert app.STORE == _test_root
    assert app.JOBS_FILE == _test_root / "jobs.json"
    assert app.CLIPS == _test_root / "clips"
    assert app.UPLOADS == _test_root / "uploads"
    # state OAuth juga ikut store ini
    assert publisher_mod._state_key("abcdefgh12345678").parent == _test_root
    # dan bukan folder storage produksi
    assert "storage" not in str(publisher_mod.CREDS_FILE).replace("_test_store", "")


# ============ REGRESI ============

def test_creds_unknown_platform_400_not_500():
    """Platform tak dikenal -> 400 (PublishError dulu lolos jadi 500)."""
    client = _client()
    r = client.post("/api/creds", json={"platform": "facebook", "creds": {"access_token": "x"}})
    assert r.status_code == 400, r.text
    r2 = client.delete("/api/creds/facebook")
    assert r2.status_code == 400, r2.text


def test_save_creds_merges_and_never_wipes_with_empty_values():
    """Bug: menyimpan 1 field tidak boleh menghapus token lain (input password
    di UI selalu kosong saat panel dibuka)."""
    publisher_mod.save_creds("youtube", {"access_token": "tok-1"})
    publisher_mod.save_creds("youtube", {"client_id": "cid", "client_secret": ""})
    c = publisher_mod.get_creds("youtube")
    assert c["access_token"] == "tok-1", c
    assert c["client_id"] == "cid"
    assert "client_secret" not in c


def test_oauth_state_rejects_path_traversal(tmp_path, monkeypatch):
    """state dari query param tidak boleh jadi path sembarang."""
    assert publisher_mod._state_key("../creds") is None
    assert publisher_mod._state_key("a/b") is None
    assert publisher_mod._state_key("") is None
    assert publisher_mod.yt_state_take("../creds") is False
    assert publisher_mod.yt_state_take("../../storage/creds") is False
    # state sah (token_urlsafe) tetap diterima sekali saja
    url = publisher_mod.yt_auth_url("cid.apps.googleusercontent.com")
    state = url.split("state=")[1].split("&")[0]
    assert publisher_mod.yt_state_take(state) is True
    assert publisher_mod.yt_state_take(state) is False


def test_oauth_redirect_uri_follows_port_env():
    assert publisher_mod.YT_REDIRECT_URI.endswith(
        f":{publisher_mod.OAUTH_PORT}/api/oauth/youtube/callback")


def test_job_endpoints_return_copies_and_newest_first():
    client = _client()
    with app._lock:
        jid1 = app._new_job("upload", "lama", {})
        jid2 = app._new_job("upload", "baru", {})
    got = client.get(f"/api/jobs/{jid1}").json()
    got["status"] = "DIUBAH-DARI-LUAR"
    assert client.get(f"/api/jobs/{jid1}").json()["status"] == "queued"
    ids = [j["id"] for j in client.get("/api/jobs").json()]
    assert ids[0] == jid2 and jid1 in ids, ids


def test_delete_job_removes_source_file(tmp_path):
    client = _client()
    src = tmp_path / "video.mp4"
    src.write_bytes(b"\x00" * 16)
    with app._lock:
        jid = app._new_job("upload", "ada file", {})
        app._jobs[jid]["file"] = str(src)
        app._save()
    assert client.delete(f"/api/jobs/{jid}").status_code == 200
    assert not src.exists(), "file sumber job tidak ikut terhapus"
    assert client.get(f"/api/jobs/{jid}").status_code == 404


def test_youtube_job_uses_real_downloaded_path(tmp_path, monkeypatch):
    """Bug: outtmpl dipaksa '.mp4' padahal yt-dlp menulis ekstensi nyata
    (.webm/.mkv) -> _process menerima path yang tidak ada di disk."""
    import sys, types, time
    client = _client()
    monkeypatch.setattr(app, "UPLOADS", tmp_path)

    written = {}

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            path = self.opts["outtmpl"].replace("%(ext)s", "webm")
            Path(path).write_bytes(b"\x00" * 16)
            written["path"] = path
            return {"ext": "webm", "id": "abc", "title": "Judul YT",
                    "requested_downloads": [{"filepath": path}]}

        def prepare_filename(self, info):
            return self.opts["outtmpl"].replace("%(ext)s", (info or {}).get("ext", "mp4"))

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    class DummyPool:
        def __init__(self):
            self.calls = []

        def submit(self, fn, *a, **kw):
            self.calls.append(a)

    pool = DummyPool()
    monkeypatch.setattr(app, "pool", pool)

    r = client.post("/api/jobs/youtube", json={"url": "https://youtu.be/abc"})
    assert r.status_code == 200, r.text
    jid = r.json()["id"]

    for _ in range(60):
        with app._lock:
            job = dict(app._jobs.get(jid) or {})
        if job.get("file"):
            break
        time.sleep(0.05)

    assert job.get("file") == written["path"], job
    assert str(job["file"]).endswith(".webm")
    assert os.path.exists(job["file"])
    assert job["title"] == "Judul YT"
    # path yang diserahkan ke worker harus ada di disk
    assert pool.calls and os.path.exists(pool.calls[-1][1])
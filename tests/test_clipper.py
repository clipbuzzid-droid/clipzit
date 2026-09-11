"""Clipzit test suite — jalankan: pytest tests/ -q (dari root project)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

import clipper  # noqa: E402

FFPROBE_OK = "subtitles" in subprocess.run(
    [clipper._FFPROBE, "-hide_banner", "-filters"],
    capture_output=True, text=True).stdout


# ============ UNIT: find_clips ============

def test_find_clips_min_bigger_than_max():
    """Bug swap min/max (kemarin): window tidak boleh nol/negatif."""
    clips = clipper.find_clips([], 120.0, num_clips=3, min_dur=50, max_dur=25)
    assert clips, "harus tetap menghasilkan klip"
    assert all(c["end"] - c["start"] >= 50 - 0.01 for c in clips)


def test_find_clips_fallback_contiguous_covers_duration():
    """Fallback tanpa transkrip: chunk contiguous & menutup seluruh durasi."""
    clips = clipper.find_clips([], 37.0, num_clips=4, min_dur=25)
    assert clips[0]["start"] == 0.0
    assert abs(clips[-1]["end"] - 37.0) < 0.01
    for a, b in zip(clips, clips[1:]):
        assert a["end"] == b["start"], "chunk harus nyambung"


def test_find_clips_no_zero_clips_on_short_video():
    clips = clipper.find_clips([], 8.0, num_clips=5, min_dur=25, max_dur=50)
    assert len(clips) >= 1


# ============ UNIT: probe ============

def test_probe_corrupt_file_no_crash():
    """File tanpa metadata valid → durasi 0, tidak ValueError."""
    jobs_json = BACKEND.parent / "storage" / "jobs.json"
    info = clipper.probe(str(jobs_json))
    assert info == {"duration": 0.0, "width": 0, "height": 0}


# ============ UNIT: subtitle writers ============

WORDS = [
    {"start": 0.0, "end": 0.5, "word": "RAHASIA"},
    {"start": 0.6, "end": 1.0, "word": "123"},
    {"start": 1.1, "end": 1.5, "word": "JANGAN"},
    {"start": 1.6, "end": 2.0, "word": "kamu"},
]


def test_write_ass_colors_and_font(tmp_path):
    """Angka=hijau, negasi=merah, hook=kuning; font terdeteksi (Impact/Arial)."""
    p = tmp_path / "t.ass"
    clipper.write_ass(WORDS, 0.0, str(p))
    txt = p.read_text(encoding="utf-8-sig")
    assert "Impact,88" in txt or "Arial,88" in txt
    lines = txt.splitlines()
    # mapping warna per kata (urutan dialogue = urutan kata)
    seq = [l.split(",")[3] for l in lines if l.startswith("Dialogue:")]
    assert seq == ["WYellow", "WGreen", "WRed", "WWhite"]


def test_write_ass_word_before_clip_start(tmp_path):
    """Word mulai sebelum t0 → timestamp tidak negatif (pitfall whisper)."""
    p = tmp_path / "t.ass"
    clipper.write_ass([{"start": -0.2, "end": 0.3, "word": "HALO"}], 0.0, str(p))
    txt = p.read_text(encoding="utf-8-sig")
    dlg = [l for l in txt.splitlines() if l.startswith("Dialogue:")][0]
    assert not dlg.split(",")[1].startswith("-")


def test_write_srt_groups_and_format(tmp_path):
    p = tmp_path / "t.srt"
    clipper.write_srt(WORDS, 0.0, str(p))
    txt = p.read_text(encoding="utf-8")
    assert "00:00:00,000 --> " in txt
    assert "RAHASIA" in txt
    # format cue bernomor urut
    assert txt.lstrip().startswith("1\n")


# ============ UNIT: scoring ============

def test_score_window_bounds():
    """Skor selalu di rentang 35–99 walau teks ekstrem."""
    s, _ = clipper._score_window("rahasia gratis uang viral " * 50, 30.0)
    assert 35 <= s <= 99
    s2, _ = clipper._score_window("", 30.0)
    assert 35 <= s2 <= 99


# ============ INTEGRATION: ffmpeg binary ============

def test_ffmpeg_binary_has_libass():
    """ffmpeg terpilih WAJIB punya filter subtitles (bug Octave ffmpeg)."""
    assert FFPROBE_OK, f"ffmpeg terpilih tidak punya libass: {clipper._FFMPEG}"
    assert Path(clipper._FFMPEG).exists()


def test_probe_real_video(tmp_path):
    """Probe video sintetis: durasi & dimensi benar (butuh ffmpeg)."""
    v = tmp_path / "v.mp4"
    subprocess.run([clipper._FFMPEG, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc=size=1280x720:duration=3",
                    str(v)], check=True)
    info = clipper.probe(str(v))
    assert abs(info["duration"] - 3.0) < 0.5
    assert info["width"] == 1280 and info["height"] == 720


def test_render_clip_burns_subtitle(tmp_path):
    """Render 2 dtk dengan subtitle → output ada; jika libass: teks terbakar
    (pixel-diff vs render tanpa sub di band tengah)."""
    v = tmp_path / "in.mp4"
    subprocess.run([clipper._FFMPEG, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc=size=1280x720:duration=3",
                    "-c:v", "libx264", str(v)], check=True)
    ass = tmp_path / "t.ass"
    clipper.write_ass([{"start": 0.5, "end": 2.5, "word": "TEST"}], 0.0, str(ass))
    out = tmp_path / "out.mp4"
    clipper.render_clip(str(v), 0.5, 2.5, None, str(out), ass_path=str(ass))
    assert out.exists() and out.stat().st_size > 10000

    # pixel-diff: frame with-sub vs no-sub pada t=1.0 klip
    def frame(mp4, t, name):
        subprocess.run([clipper._FFMPEG, "-y", "-v", "error", "-ss", str(t),
                        "-i", str(mp4), "-frames:v", "1", str(tmp_path / name)],
                       check=True)
        return (tmp_path / name).read_bytes()

    out2 = tmp_path / "out_nosub.mp4"
    clipper.render_clip(str(v), 0.5, 2.5, None, str(out2), ass_path=None)
    f_sub = frame(out, 0.6, "f_sub.png")
    f_no = frame(out2, 0.6, "f_no.png")
    # libass hadir → wajib ada beda; kalau fallback no-sub aktif keduanya identik
    if FFPROBE_OK:
        assert f_sub != f_no, "subtitle tidak terbakar (frame identik dengan no-sub)"


def test_render_fallback_transcode_uses_transcoded_input(tmp_path, monkeypatch):
    """Fallback transcode (input tak didukung decoder) WAJIB pakai file hasil
    transcode sebagai input. Bug lama: slice cmd2 menyisakan `-i <src asli>`,
    jadi fallback membaca ulang file yang sama dan selalu gagal."""
    src = tmp_path / "src.av1"
    src.write_bytes(b"\x00" * 32)
    out = tmp_path / "out.mp4"
    tmp_conv = tmp_path / "conv.mp4"
    tmp_conv.write_bytes(b"\x00" * 32)  # hasil transcode "berhasil"

    monkeypatch.setattr(clipper.tempfile, "mktemp", lambda *a, **k: str(tmp_conv))
    calls = []

    class R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        r = R()
        # panggilan ke-3 = transcode (harus "berhasil"), sisanya gagal
        r.returncode = 0 if len(calls) == 3 else 1
        return r

    monkeypatch.setattr(clipper.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        clipper.render_clip(str(src), 0.0, 3.0, None, str(out))

    assert len(calls) == 4, calls
    assert calls[2][calls[2].index("-i") + 1] == str(src), "panggilan ke-3 = transcode sumber"
    cmd3 = calls[3]
    assert cmd3[cmd3.index("-i") + 1] == str(tmp_conv), \
        "fallback transcode masih membaca file sumber asli"
    assert str(src) not in cmd3
    # graph filter memakai [0:v] -> input 0 harus file transcode
    assert cmd3[cmd3.index("-filter_complex") + 1].startswith("[0:v]")


def test_make_thumbnail_failure_returns_none_and_no_file(tmp_path):
    """Frame di luar durasi -> None + tidak meninggalkan file 0-byte."""
    v = tmp_path / "v.mp4"
    subprocess.run([clipper._FFMPEG, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:duration=1",
                    "-c:v", "libx264", str(v)], check=True)
    thumb = tmp_path / "t.jpg"
    assert clipper.make_thumbnail(str(v), 99.0, str(thumb)) is None
    assert not thumb.exists()


def test_find_clips_zero_num_clips_no_crash():
    """num_clips=0 dari form UI tidak boleh bikin pembagian nol di fallback."""
    clips = clipper.find_clips([], 60.0, num_clips=0, min_dur=25, max_dur=50)
    assert len(clips) >= 1

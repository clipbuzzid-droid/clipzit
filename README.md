# ✂ Clipzit — AI Clipper ala Vizard

Upload video panjang / tempel link YouTube → AI membuat klip vertikal siap Shorts/Reels/TikTok.

- 🧠 Transkrip lokal (faster-whisper) + skor viralitas (hook, pertanyaan, angka, tempo, hook 3-detik)
- 📱 Reframe 9:16 ala Vizard (foreground tajam + background blur) + subtitle terbakar gaya Shorts
- ⬆ **Auto-upload ke TikTok / YouTube / Instagram** langsung dari UI (API resmi)
- 🔒 100% lokal & gratis (ffmpeg + yt-dlp, tanpa cloud)

## Cara jalan (Windows)

```bat
jalankan.bat
```

atau manual:

```bash
pip install -r requirements.txt
cd backend && python app.py
# buka http://127.0.0.1:8787
```

Model whisper default `tiny` (cepat, ~75 MB, unduh otomatis saat job pertama).
Untuk akurasi lebih baik: `set CLIPZIT_MODEL=base` sebelum jalan (atau `small` bila CPU kuat).

## Auto-upload ke sosial media

Klik **⬆ Upload** pada klip mana pun → pilih platform → klip dikirim ke API resmi platform.

| Platform | Yang perlu disiapkan | Cara kredensial |
|---|---|---|
| TikTok | Aplikasi di [developers.tiktok.com](https://developers.tiktok.com) dengan scope `video.upload` + token user | Panel ⚙ Konfigurasi di UI |
| YouTube | OAuth token dengan scope `youtube.upload` (mis. lewat OAuth Playground) | Panel ⚙ Konfigurasi di UI |
| Instagram | Akun IG **Professional/Business** + token dengan `instagram_content_publish` + IG User ID | Panel ⚙ Konfigurasi di UI |

Kredensial disimpan lokal di `storage/creds.json` (tidak dikirim ke mana pun).
Upload TikTok lewat API resmi masuk ke **inbox draf** creator (klik notifikasi TikTok untuk menyelesaikan posting);
YouTube & Instagram ter-publish langsung sesuai `privacy` yang dipilih.

> Catatan: TikTok & Instagram mensyaratkan aplikasi ter-audit agar bisa publish langsung;
> tanpa audit, konten biasanya di-set private/draf oleh platform.Ini batasan platform, bukan bug Clipzit.

## API

| Method | Endpoint | Fungsi |
|---|---|---|
| POST | /api/jobs/upload | upload video (multipart) |
| POST | /api/jobs/youtube | unduh + proses link YouTube |
| GET | /api/jobs | daftar job |
| GET | /api/jobs/{id} | status + klip (poll tiap 2 dtk) |
| DELETE | /api/jobs/{id} | hapus job + file |
| GET | /api/creds | status kredensial tiap platform |
| POST | /api/creds | simpan kredensial platform |
| DELETE | /api/creds/{platform} | hapus kredensial platform |
| POST | /api/publish | upload 1 klip ke platform (async) |
| GET | /api/health | cek server |

## Testing

```bat
test.bat
```

26 tes otomatis (pytest): unit (`find_clips`, `probe`, subtitle writers, scoring, kredensial)
+ integrasi (ffmpeg libass, render burn-in via pixel-diff, alur publish API dengan publisher
di-mock, fallback transcode, path unduhan YouTube). Semua bug yang pernah ditemukan kini punya
tes permanen — regresi tertangkap sebelum deploy.

> Tes memakai `CLIPZIT_STORE_DIR` (lihat `tests/_test_store/`) supaya **tidak pernah**
> menyentuh `storage/` produksi. Pakai env yang sama saat menjalankan server uji manual:
> `CLIPZIT_STORE_DIR=/tmp/store-uji PORT=8791 python backend/app.py`.
> Tanpa isolasi itu, tes/E2E bisa menulis atau menghapus kredensial platform asli.

## Struktur

```
clipzit/
  backend/app.py        # FastAPI + job manager + endpoint publish/creds
  backend/clipper.py    # transkrip → skor → render ffmpeg
  backend/publisher.py  # klien API resmi TikTok / YouTube / Instagram
  frontend/index.html   # UI (dark+light glass, Space Grotesk)
  storage/uploads|clips
  storage/creds.json    # kredensial platform (JANGAN di-commit)
```

Env yang dibaca: `CLIPZIT_MODEL` (ukuran model whisper), `PORT` (port server + redirect
OAuth YouTube), `CLIPZIT_STORE_DIR` (folder state — wajib dipakai untuk tes/E2E).

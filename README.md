# 🎌 Animeku.id

Website nonton anime sub indo gratis, dibangun dengan Flask + API Sanka Vollerei.

## 📁 Struktur Project

```
animeku/
├── app.py              ← Flask main app
├── requirements.txt    ← Dependencies
├── vercel.json         ← Konfigurasi deploy Vercel
├── static/
│   ├── css/main.css
│   ├── js/main.js
│   └── img/placeholder.svg
└── templates/
    ├── base.html       ← Layout utama
    ├── index.html      ← Homepage
    ├── detail.html     ← Detail anime + list episode
    ├── episode.html    ← Player streaming
    ├── list.html       ← Halaman list (ongoing/completed/movie/popular)
    ├── genre.html      ← Anime per genre
    ├── genres.html     ← Semua genre
    ├── schedule.html   ← Jadwal tayang
    ├── animelist.html  ← Daftar anime A-Z
    └── search.html     ← Halaman pencarian
```

## 🚀 Cara Run Lokal

```bash
pip install -r requirements.txt
python app.py
```
Buka `http://localhost:5000`

## ☁️ Deploy ke Vercel

1. Install Vercel CLI:
   ```bash
   npm i -g vercel
   ```

2. Login:
   ```bash
   vercel login
   ```

3. Deploy:
   ```bash
   cd animeku
   vercel
   ```

4. Ikuti instruksi, pilih framework: **Other**

## ✨ Fitur

- 🏠 **Homepage** — Ongoing, Populer, Terbaru + jadwal strip
- 🎬 **Player** — Streaming per episode, multi-server (480p/720p/1080p)
- 📋 **Detail Anime** — Sinopsis, info, genre, list episode
- 🔍 **Search** — Live search + halaman pencarian
- 🏷️ **Genre** — Browse per genre
- 🗓️ **Jadwal** — Jadwal tayang per hari
- 📺 **Ongoing/Completed/Movie/Popular** — List dengan pagination
- 🔤 **Animelist** — Daftar A-Z

## 🔧 Konfigurasi

Edit `API_BASE` di `app.py`:
```python
API_BASE = "https://www.sankavollerei.com"
```

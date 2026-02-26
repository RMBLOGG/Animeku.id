from flask import Flask, render_template, request, jsonify, session, redirect, send_from_directory
import requests
import json
import os
import hashlib
import hmac
from upstash_redis import Redis
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "animeku-secret-2026")
API_BASE = "https://www.sankavollerei.com"

SUPABASE_URL      = "https://mafnnqttvkdgqqxczqyt.supabase.co"
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1hZm5ucXR0dmtkZ3FxeGN6cXl0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzE4NzQyMDEsImV4cCI6MjA4NzQ1MDIwMX0.YRh1oWVKnn4tyQNRbcPhlSyvr7V_1LseWN7VjcImb-Y")
# Gunakan service_role key untuk operasi admin (bypass RLS)
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Secret key untuk akses admin — set di Vercel Environment Variables
# Contoh: ADMIN_SECRET_KEY=rahasia-super-kuat-2026
ADMIN_SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY", "ganti-ini-sebelum-deploy")

# Info rekening transfer untuk ditampilkan ke user
PAYMENT_INFO = {
    "bank":    os.environ.get("PAYMENT_BANK", "BCA"),
    "number":  os.environ.get("PAYMENT_NUMBER", "1234567890"),
    "name":    os.environ.get("PAYMENT_NAME", "Nama Pemilik"),
}

# Cloudinary config untuk upload bukti transfer
CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "dzfkklsza")
CLOUDINARY_API_KEY    = os.environ.get("CLOUDINARY_API_KEY", "588474134734416")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "9c12YJe5rZSYSg7zROQuvmVZ7mg")

def supabase_headers(access_token=None, use_service_key=False):
    key = SUPABASE_SERVICE_KEY if use_service_key else SUPABASE_ANON_KEY
    h = {"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"}
    if use_service_key and SUPABASE_SERVICE_KEY:
        h["Authorization"] = f"Bearer {SUPABASE_SERVICE_KEY}"
    elif access_token:
        h["Authorization"] = f"Bearer {access_token}"
    else:
        h["Authorization"] = f"Bearer {SUPABASE_ANON_KEY}"
    return h

# ── Upstash Redis Cache ────────────────────────────────────────────────────────
redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
)

CACHE_TTL = {
    "home":300, "popular":600, "movies":600, "ongoing":300,
    "completed":600, "recent":300, "search":120, "genres":3600,
    "genre":300, "schedule":120, "list":3600,
    "anime":600, "episode":180, "server":60, "default":300,
}

import random, time

def _ttl(path):
    base = CACHE_TTL["default"]
    for k, v in CACHE_TTL.items():
        if k in path:
            base = v
            break
    return base + int(base * random.uniform(-0.1, 0.1))

def fetch(path, params=None):
    key   = "samehadaku:" + path + str(sorted(params.items()) if params else "")
    lock_key = key + ":lock"
    try:
        cached = redis.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        print(f"Redis get error: {e}")

    lock_acquired = False
    try:
        lock_acquired = redis.set(lock_key, "1", nx=True, ex=10)
    except Exception as e:
        print(f"Redis lock error: {e}")

    if lock_acquired:
        try:
            r = requests.get(f"{API_BASE}{path}", params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            try:
                redis.set(key, json.dumps(data), ex=_ttl(path))
            except Exception as e:
                print(f"Redis set error: {e}")
            return data
        except Exception as e:
            print(f"API error [{path}]: {e}")
            return None
        finally:
            try:
                redis.delete(lock_key)
            except Exception:
                pass
    else:
        for _ in range(6):
            time.sleep(0.5)
            try:
                cached = redis.get(key)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass
        try:
            r = requests.get(f"{API_BASE}{path}", params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"API fallback error [{path}]: {e}")
            return None


# ── Helper normalisasi ─────────────────────────────────────────────────────────

def norm_anime(anime):
    if not anime:
        return anime
    return {
        "slug":          anime.get("animeId", anime.get("slug", "")),
        "title":         anime.get("title", ""),
        "poster":        anime.get("poster", ""),
        "episode":       str(anime.get("episodes", anime.get("episode", ""))),
        "status_or_day": anime.get("releasedOn", anime.get("status", anime.get("status_or_day", ""))),
        "type":          anime.get("type", ""),
        "score":         anime.get("score", ""),
        "rank":          anime.get("rank", None),
    }

def norm_list(animes):
    return [norm_anime(a) for a in (animes or [])]

def norm_genre(g):
    return {
        "name": g.get("title", g.get("name", "")),
        "slug": g.get("genreId", g.get("slug", "")),
    }

def norm_genres(genres):
    return [norm_genre(g) for g in (genres or [])]

def norm_episode_item(ep):
    return {
        "name": str(ep.get("title", ep.get("name", ""))),
        "slug": ep.get("episodeId", ep.get("slug", "")),
    }

DAY_ID = {
    "Monday": "Senin", "Tuesday": "Selasa", "Wednesday": "Rabu",
    "Thursday": "Kamis", "Friday": "Jumat", "Saturday": "Sabtu", "Sunday": "Minggu"
}

def norm_schedule(raw):
    if not raw or not raw.get("data") or not raw["data"].get("days"):
        return None
    sched_dict = {}
    for day_obj in raw["data"]["days"]:
        day_name = DAY_ID.get(day_obj.get("day", ""), day_obj.get("day", ""))
        items = []
        for a in day_obj.get("animeList", []):
            items.append({
                "slug":          a.get("animeId", ""),
                "title":         a.get("title", ""),
                "poster":        a.get("poster", ""),
                "episode":       str(a.get("episodes", "")),
                "time":          a.get("estimation", ""),
                "status_or_day": a.get("estimation", ""),
                "type":          a.get("type", ""),
            })
        sched_dict[day_name] = items
    return {"schedule": sched_dict}

def _norm_paginated(raw, page):
    if not raw or not raw.get("data"):
        return None
    animes = norm_list(raw["data"].get("animeList", []))
    pagination = raw.get("pagination")
    pag_norm = None
    if pagination:
        pag_norm = {
            "hasNext":    pagination.get("hasNextPage", False),
            "hasPrev":    pagination.get("hasPrevPage", False),
            "currentPage": pagination.get("currentPage", page),
        }
    return {"animes": animes, "pagination": pag_norm}


# ── Subscription Helper ────────────────────────────────────────────────────────

def check_episode_access(user_id, anime_slug, episode_number):
    """
    Cek apakah user boleh menonton episode tertentu.
    - Episode 1-2: GRATIS untuk semua
    - Episode 3+: butuh monthly subscription ATAU per-anime subscription
    """
    if episode_number <= 2:
        return True

    if not user_id:
        return False

    now_iso = datetime.utcnow().isoformat()

    # Cek monthly subscription aktif
    try:
        monthly = requests.get(
            f"{SUPABASE_URL}/rest/v1/user_subscriptions",
            headers=supabase_headers(use_service_key=True),
            params={
                "user_id":    f"eq.{user_id}",
                "plan_id":    "eq.monthly",
                "is_active":  "eq.true",
                "expired_at": f"gte.{now_iso}",
                "select":     "id",
                "limit":      "1",
            }
        ).json()
        if monthly:
            return True
    except Exception as e:
        print(f"Subscription check error (monthly): {e}")

    # Cek per-anime subscription
    try:
        per_anime = requests.get(
            f"{SUPABASE_URL}/rest/v1/user_subscriptions",
            headers=supabase_headers(use_service_key=True),
            params={
                "user_id":   f"eq.{user_id}",
                "anime_slug": f"eq.{anime_slug}",
                "is_active":  "eq.true",
                "select":     "id",
                "limit":      "1",
            }
        ).json()
        if per_anime:
            return True
    except Exception as e:
        print(f"Subscription check error (per_anime): {e}")

    return False


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.route("/manifest.json")
def manifest():
    return send_from_directory(app.static_folder, "manifest.json", mimetype="application/manifest+json")

@app.route("/")
def home():
    raw      = fetch("/anime/samehadaku/home")
    popular  = fetch("/anime/samehadaku/popular")
    schedule = fetch("/anime/samehadaku/schedule")

    data = None
    if raw and raw.get("data"):
        d = raw["data"]
        recent_list = d.get("recent", {}).get("animeList", [])
        top10_list  = d.get("top10",  {}).get("animeList", [])
        data = {
            "ongoing": norm_list(recent_list),
            "recent":  norm_list(top10_list),
        }

    pop_norm = None
    if popular and popular.get("data"):
        pop_norm = {"animes": norm_list(popular["data"].get("animeList", []))}

    return render_template("index.html", data=data, popular=pop_norm,
                           schedule=norm_schedule(schedule))


@app.route("/anime/<slug>")
def detail(slug):
    raw = fetch(f"/anime/samehadaku/anime/{slug}")
    data = None
    if raw and raw.get("data"):
        d = raw["data"]
        eps    = [norm_episode_item(e) for e in d.get("episodeList", [])]
        genres = norm_genres(d.get("genreList", []))
        score_val = ""
        if isinstance(d.get("score"), dict):
            score_val = d["score"].get("value", "")
        else:
            score_val = str(d.get("score", ""))
        data = {
            "detail": {
                "title":    d.get("title", ""),
                "poster":   d.get("poster", ""),
                "synopsis": " ".join(d.get("synopsis", {}).get("paragraphs", [])),
                "trailer":  d.get("trailer", ""),
                "genres":   genres,
                "episodes": eps,
                "info": {
                    "japanese":      d.get("japanese", ""),
                    "status":        d.get("status", ""),
                    "type":          d.get("type", ""),
                    "score":         score_val,
                    "total_episode": str(d.get("episodes", "")),
                    "duration":      d.get("duration", ""),
                    "released":      d.get("aired", ""),
                    "studio":        d.get("studios", ""),
                    "season":        d.get("season", ""),
                }
            }
        }
    return render_template("detail.html", data=data, slug=slug)


@app.route("/episode/<slug>")
def episode(slug):
    raw        = fetch(f"/anime/samehadaku/episode/{slug}")
    anime_slug = request.args.get("anime", "")

    data = None
    if raw and raw.get("data"):
        d = raw["data"]
        streams = []
        for quality in d.get("server", {}).get("qualities", []):
            q_title = quality.get("title", "")
            for srv in quality.get("serverList", []):
                srv_name = srv.get("title", "")
                if q_title and q_title.lower() not in srv_name.lower():
                    label = f"{srv_name} {q_title}".strip()
                else:
                    label = srv_name
                streams.append({
                    "name":     label,
                    "serverId": srv.get("serverId", ""),
                    "url":      "",
                })
        default_url = d.get("defaultStreamingUrl", "")
        if default_url:
            streams.insert(0, {"name": "Default Auto", "serverId": "", "url": default_url})
        ep_anime_id = d.get("animeId", "")
        data = {
            "title":    d.get("title", ""),
            "anime_id": ep_anime_id,
            "streams":  streams,
            "downloads": [],
        }

    if not anime_slug and data and data.get("anime_id"):
        anime_slug = data["anime_id"]

    anime_raw  = fetch(f"/anime/samehadaku/anime/{anime_slug}") if anime_slug else None
    anime_data = None
    if anime_raw and anime_raw.get("data"):
        d2     = anime_raw["data"]
        eps    = [norm_episode_item(e) for e in d2.get("episodeList", [])]
        genres = norm_genres(d2.get("genreList", []))
        anime_data = {
            "detail": {
                "title":    d2.get("title", ""),
                "poster":   d2.get("poster", ""),
                "genres":   genres,
                "episodes": eps,
            }
        }

    # Hitung nomor episode dari daftar episode anime
    # (API mengurutkan terbaru ke terlama, index 0 = terbaru)
    episode_number = 1
    user = session.get("user")
    user_id = user["id"] if user else None

    if anime_data and anime_data["detail"]["episodes"]:
        eps_list = anime_data["detail"]["episodes"]
        total    = len(eps_list)
        for i, ep in enumerate(eps_list):
            if ep["slug"] == slug:
                # index 0 = episode terbaru = episode terbesar
                episode_number = total - i
                break

    # Cek apakah user punya akses ke episode ini
    has_access = check_episode_access(user_id, anime_slug, episode_number)

    return render_template(
        "episode.html",
        data=data,
        slug=slug,
        anime_slug=anime_slug,
        anime_data=anime_data,
        episode_number=episode_number,
        has_access=has_access,
        payment_info=PAYMENT_INFO,
        anime_title=(anime_data["detail"]["title"] if anime_data and anime_data["detail"] else ""),
    )


@app.route("/api/server/<server_id>")
def api_server(server_id):
    raw = fetch(f"/anime/samehadaku/server/{server_id}")
    if raw and raw.get("data"):
        return jsonify({"url": raw["data"].get("url", "")})
    return jsonify({"url": ""}), 404


@app.route("/genre/<slug>")
def genre(slug):
    page       = request.args.get("page", 1)
    raw        = fetch(f"/anime/samehadaku/genres/{slug}", {"page": page})
    genres_raw = fetch("/anime/samehadaku/genres")

    data = None
    if raw and raw.get("data"):
        animes   = norm_list(raw["data"].get("animeList", []))
        pagination = raw.get("pagination")
        pag_norm = None
        if pagination:
            pag_norm = {
                "hasNext":    pagination.get("hasNextPage", False),
                "hasPrev":    pagination.get("hasPrevPage", False),
                "currentPage": pagination.get("currentPage", 1),
            }
        data = {"animes": animes, "pagination": pag_norm}

    genres = None
    if genres_raw and genres_raw.get("data"):
        genres = {"genres": norm_genres(genres_raw["data"].get("genreList", []))}

    return render_template("genre.html", data=data, slug=slug, genres=genres, page=int(page))


@app.route("/genres")
def genres():
    raw = fetch("/anime/samehadaku/genres")
    data = None
    if raw and raw.get("data"):
        data = {"genres": norm_genres(raw["data"].get("genreList", []))}
    return render_template("genres.html", data=data)


@app.route("/jadwal")
def schedule():
    raw = fetch("/anime/samehadaku/schedule")
    return render_template("schedule.html", data=norm_schedule(raw))


@app.route("/movies")
def movies():
    page = request.args.get("page", 1)
    data = _norm_paginated(fetch("/anime/samehadaku/movies", {"page": page}), int(page))
    return render_template("list.html", data=data, title="Movie", page=int(page), base_url="/movies")


@app.route("/ongoing")
def ongoing():
    page = request.args.get("page", 1)
    data = _norm_paginated(fetch("/anime/samehadaku/ongoing", {"page": page}), int(page))
    return render_template("list.html", data=data, title="Ongoing", page=int(page), base_url="/ongoing")


@app.route("/completed")
def completed():
    page = request.args.get("page", 1)
    data = _norm_paginated(fetch("/anime/samehadaku/completed", {"page": page}), int(page))
    return render_template("list.html", data=data, title="Completed", page=int(page), base_url="/completed")


@app.route("/popular")
def popular():
    page = request.args.get("page", 1)
    data = _norm_paginated(fetch("/anime/samehadaku/popular", {"page": page}), int(page))
    return render_template("list.html", data=data, title="Populer", page=int(page), base_url="/popular")


@app.route("/animelist")
def animelist():
    raw = fetch("/anime/samehadaku/list")
    data = None
    if raw and raw.get("data"):
        animes = raw["data"].get("animeList", [])
        groups = {}
        for a in animes:
            title  = a.get("title", "")
            letter = title[0].upper() if title else "#"
            if letter not in groups:
                groups[letter] = []
            groups[letter].append({"title": title, "slug": a.get("animeId", "")})
        anime_list = [{"letter": k, "animes": v} for k, v in sorted(groups.items())]
        data = {"anime_list": anime_list}
    return render_template("animelist.html", data=data)


@app.route("/search")
def search():
    q   = request.args.get("q", "")
    raw = fetch("/anime/samehadaku/search", {"q": q}) if q else None
    data = None
    if raw and raw.get("data"):
        data = {"animes": norm_list(raw["data"].get("animeList", []))}
    return render_template("search.html", data=data, query=q)


@app.route("/koleksi")
def koleksi():
    return render_template("koleksi.html")


# ── API Proxy ──────────────────────────────────────────────────────────────────

@app.route("/api/search/<keyword>")
def api_search(keyword):
    raw = fetch("/anime/samehadaku/search", {"q": keyword})
    if raw and raw.get("data"):
        return jsonify({"animes": norm_list(raw["data"].get("animeList", []))})
    return jsonify({"animes": []})  # fix: jangan return None


# ── Subscription API ───────────────────────────────────────────────────────────

@app.route("/api/access/<anime_slug>/<int:episode_number>")
def api_check_access(anime_slug, episode_number):
    """Frontend memanggil ini untuk cek apakah user boleh nonton."""
    user = session.get("user")
    user_id = user["id"] if user else None
    has_access = check_episode_access(user_id, anime_slug, episode_number)
    return jsonify({
        "access":         has_access,
        "episode_number": episode_number,
        "logged_in":      user_id is not None,
    })


@app.route("/api/payment/submit", methods=["POST"])
def payment_submit():
    """User submit bukti transfer, status jadi 'pending' menunggu admin."""
    user = session.get("user")
    if not user:
        return jsonify({"error": "Login dulu ya!"}), 401

    data        = request.get_json() or {}
    plan_id     = data.get("plan_id", "")
    anime_slug  = data.get("anime_slug") or None
    anime_title = data.get("anime_title", "")
    proof_url   = data.get("payment_proof", "")

    if plan_id not in ["monthly", "per_anime"]:
        return jsonify({"error": "Plan tidak valid"}), 400
    if plan_id == "per_anime" and not anime_slug:
        return jsonify({"error": "Pilih anime terlebih dahulu"}), 400
    if not proof_url:
        return jsonify({"error": "Bukti transfer wajib diupload"}), 400

    # Cek apakah user sudah punya pembayaran pending untuk plan yang sama
    existing = requests.get(
        f"{SUPABASE_URL}/rest/v1/payments",
        headers=supabase_headers(use_service_key=True),
        params={
            "user_id": f"eq.{user['id']}",
            "plan_id": f"eq.{plan_id}",
            "status":  "eq.pending",
            **({"anime_slug": f"eq.{anime_slug}"} if anime_slug else {}),
            "select":  "id",
            "limit":   "1",
        }
    ).json()
    if existing:
        return jsonify({"error": "Kamu sudah punya pembayaran pending. Tunggu konfirmasi admin ya!"}), 409

    amount = 25000 if plan_id == "monthly" else 5000

    payload = {
        "user_id":       user["id"],
        "user_name":     user.get("name", ""),
        "user_email":    user.get("email", ""),
        "plan_id":       plan_id,
        "anime_slug":    anime_slug,
        "anime_title":   anime_title,
        "amount":        amount,
        "payment_proof": proof_url,
        "status":        "pending",
    }

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/payments",
        headers={**supabase_headers(use_service_key=True), "Prefer": "return=representation"},
        json=payload
    )

    if r.ok:
        return jsonify({"ok": True, "message": "Pembayaran berhasil dikirim! Verifikasi admin 1×24 jam."})
    return jsonify({"error": "Gagal submit pembayaran", "detail": r.text}), 500


@app.route("/api/my/subscriptions")
def my_subscriptions():
    """Daftar langganan aktif milik user yang sedang login."""
    user = session.get("user")
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    subs = requests.get(
        f"{SUPABASE_URL}/rest/v1/user_subscriptions",
        headers=supabase_headers(use_service_key=True),
        params={
            "user_id":  f"eq.{user['id']}",
            "is_active": "eq.true",
            "select":    "*",
            "order":     "started_at.desc",
        }
    ).json()

    payments = requests.get(
        f"{SUPABASE_URL}/rest/v1/payments",
        headers=supabase_headers(use_service_key=True),
        params={
            "user_id": f"eq.{user['id']}",
            "status":  "eq.pending",
            "select":  "*",
            "order":   "created_at.desc",
        }
    ).json()

    return jsonify({"subscriptions": subs or [], "pending_payments": payments or []})


@app.route("/api/payment/upload-proof", methods=["POST"])
def payment_upload_proof():
    """Upload bukti transfer langsung ke Cloudinary dari server."""
    # Cek auth: bisa dari Flask session ATAU Authorization header (localStorage token)
    user = session.get("user")
    if not user:
        auth_header  = request.headers.get("Authorization", "")
        access_token = auth_header.replace("Bearer ", "").strip()
        if access_token:
            user_resp = requests.get(f"{SUPABASE_URL}/auth/v1/user", headers=supabase_headers(access_token))
            if user_resp.ok:
                ud = user_resp.json()
                user = {
                    "id":    ud.get("id"),
                    "name":  ud.get("user_metadata", {}).get("full_name", "User"),
                    "email": ud.get("email", ""),
                }
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    if "file" not in request.files:
        return jsonify({"error": "File tidak ditemukan"}), 400

    file = request.files["file"]
    if file.content_length and file.content_length > 5 * 1024 * 1024:
        return jsonify({"error": "File terlalu besar, maks 5MB"}), 400

    # Buat signature Cloudinary
    timestamp  = int(time.time())
    folder     = "payment-proofs"
    public_id  = f"{folder}/{user['id']}_{timestamp}"
    params_str = f"folder={folder}&public_id={user['id']}_{timestamp}&timestamp={timestamp}{CLOUDINARY_API_SECRET}"
    signature  = hashlib.sha1(params_str.encode()).hexdigest()

    try:
        upload_resp = requests.post(
            f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload",
            data={
                "api_key":   CLOUDINARY_API_KEY,
                "timestamp": timestamp,
                "signature": signature,
                "folder":    folder,
                "public_id": f"{user['id']}_{timestamp}",
            },
            files={"file": (file.filename, file.stream, file.content_type)},
            timeout=30,
        )
        if not upload_resp.ok:
            return jsonify({"error": "Upload ke Cloudinary gagal", "detail": upload_resp.text}), 500

        result   = upload_resp.json()
        img_url  = result.get("secure_url", "")
        return jsonify({"ok": True, "url": img_url})

    except Exception as e:
        return jsonify({"error": f"Upload error: {str(e)}"}), 500


# ── Admin API ──────────────────────────────────────────────────────────────────

def require_admin():
    """
    Cek admin via header X-Admin-Key.
    Frontend kirim: headers: { 'X-Admin-Key': key_dari_localStorage }
    """
    key = request.headers.get("X-Admin-Key", "")
    if not key or key != ADMIN_SECRET_KEY:
        return False
    return True


@app.route("/admin")
def admin_page():
    """Halaman admin — auth dicek di frontend via localStorage key."""
    return render_template("admin.html")


@app.route("/api/admin/payments")
def admin_payments():
    """Daftar pembayaran untuk admin, bisa filter by status."""
    if not require_admin():
        return jsonify({"error": "Forbidden"}), 403

    status = request.args.get("status", "pending")
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/payments",
        headers=supabase_headers(use_service_key=True),
        params={
            "status": f"eq.{status}",
            "order":  "created_at.desc",
            "select": "*",
        }
    )
    return jsonify(r.json() if r.ok else [])


@app.route("/api/admin/payment/<payment_id>/review", methods=["POST"])
def admin_review_payment(payment_id):
    """Admin approve atau reject pembayaran."""
    if not require_admin():
        return jsonify({"error": "Forbidden"}), 403

    data   = request.get_json() or {}
    action = data.get("action")   # 'approved' | 'rejected'
    note   = data.get("note", "")

    if action not in ["approved", "rejected"]:
        return jsonify({"error": "Action tidak valid"}), 400

    # Ambil data payment
    payment_resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/payments",
        headers=supabase_headers(use_service_key=True),
        params={"id": f"eq.{payment_id}", "select": "*"}
    ).json()

    if not payment_resp:
        return jsonify({"error": "Payment tidak ditemukan"}), 404

    payment = payment_resp[0]

    # Guard: jangan approve ulang
    if payment["status"] != "pending":
        return jsonify({"error": f"Payment sudah di-{payment['status']}"}), 409

    # Update status payment
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/payments",
        headers=supabase_headers(use_service_key=True),
        params={"id": f"eq.{payment_id}"},
        json={
            "status":      action,
            "admin_note":  note,
            "reviewed_at": datetime.utcnow().isoformat(),
            "reviewed_by": "admin",
        }
    )

    # Kalau approved → buat subscription aktif
    if action == "approved":
        sub_payload = {
            "user_id":    payment["user_id"],
            "plan_id":    payment["plan_id"],
            "anime_slug": payment.get("anime_slug"),
            "payment_id": payment_id,
            "is_active":  True,
        }
        if payment["plan_id"] == "monthly":
            sub_payload["expired_at"] = (
                datetime.utcnow() + timedelta(days=30)
            ).isoformat()
        # per_anime: expired_at NULL = selamanya

        requests.post(
            f"{SUPABASE_URL}/rest/v1/user_subscriptions",
            headers={**supabase_headers(use_service_key=True), "Prefer": "return=representation"},
            json=sub_payload
        )

    return jsonify({"ok": True, "action": action, "payment_id": payment_id})


@app.route("/api/admin/subscriptions")
def admin_subscriptions():
    """Daftar semua subscription aktif."""
    if not require_admin():
        return jsonify({"error": "Forbidden"}), 403

    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/user_subscriptions",
        headers=supabase_headers(use_service_key=True),
        params={
            "is_active": "eq.true",
            "order":     "started_at.desc",
            "select":    "*",
        }
    )
    return jsonify(r.json() if r.ok else [])


@app.route("/api/admin/proof-url", methods=["POST"])
def admin_proof_url():
    """Return URL bukti transfer — sekarang pakai Cloudinary, URL sudah langsung bisa diakses."""
    if not require_admin():
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json() or {}
    path = data.get("path", "")
    # Cloudinary URL sudah berupa https:// langsung, kembalikan apa adanya
    return jsonify({"url": path})


@app.route("/api/admin/subscription/<sub_id>/revoke", methods=["POST"])
def admin_revoke_subscription(sub_id):
    """Admin bisa cabut subscription user."""
    if not require_admin():
        return jsonify({"error": "Forbidden"}), 403

    requests.patch(
        f"{SUPABASE_URL}/rest/v1/user_subscriptions",
        headers=supabase_headers(use_service_key=True),
        params={"id": f"eq.{sub_id}"},
        json={"is_active": False}
    )
    return jsonify({"ok": True})


# ── Auth ───────────────────────────────────────────────────────────────────────

@app.route("/auth/login")
def auth_login():
    redirect_to = f"{SUPABASE_URL}/auth/v1/authorize?provider=google&redirect_to={request.host_url}auth/callback"
    return redirect(redirect_to)

@app.route("/auth/callback")
def auth_callback():
    return render_template("auth_callback.html")

@app.route("/auth/session", methods=["POST"])
def auth_session():
    data = request.get_json()
    if data and data.get("access_token"):
        session["access_token"] = data["access_token"]
        session["user"] = {
            "id":     data.get("user", {}).get("id"),
            "name":   data.get("user", {}).get("user_metadata", {}).get("full_name", "User"),
            "avatar": data.get("user", {}).get("user_metadata", {}).get("avatar_url", ""),
            "email":  data.get("user", {}).get("email", ""),
        }
    return jsonify({"ok": True})

@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/me")
def api_me():
    return jsonify({"user": session.get("user")})


# ── Comments ───────────────────────────────────────────────────────────────────

@app.route("/api/comments/<anime_slug>")
def get_comments(anime_slug):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/anime_comments",
        headers=supabase_headers(),
        params={"anime_slug": f"eq.{anime_slug}", "order": "created_at.desc", "select": "*"}
    )
    return jsonify(r.json() if r.ok else [])

@app.route("/api/comments", methods=["POST"])
def post_comment():
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not access_token:
        return jsonify({"error": "Login dulu ya!"}), 401
    user_resp = requests.get(f"{SUPABASE_URL}/auth/v1/user", headers=supabase_headers(access_token))
    if not user_resp.ok:
        return jsonify({"error": "Login dulu ya!"}), 401
    user_data = user_resp.json()
    user = {
        "id":     user_data.get("id"),
        "name":   user_data.get("user_metadata", {}).get("full_name", "User"),
        "avatar": user_data.get("user_metadata", {}).get("avatar_url", ""),
    }
    data       = request.get_json()
    content    = (data.get("content") or "").strip()
    anime_slug = data.get("anime_slug", "")
    if not content or len(content) < 2:
        return jsonify({"error": "Komentar terlalu pendek"}), 400
    payload = {"anime_slug": anime_slug, "user_id": user["id"],
               "user_name": user["name"], "user_avatar": user["avatar"], "content": content}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/anime_comments",
                      headers={**supabase_headers(access_token), "Prefer": "return=representation"},
                      json=payload)
    if r.ok:
        return jsonify(r.json()[0] if r.json() else {})
    return jsonify({"error": "Gagal kirim komentar", "detail": r.text}), 500

@app.route("/api/comments/<comment_id>", methods=["DELETE"])
def delete_comment(comment_id):
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not access_token:
        return jsonify({"error": "Unauthorized"}), 401
    user_resp = requests.get(f"{SUPABASE_URL}/auth/v1/user", headers=supabase_headers(access_token))
    if not user_resp.ok:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = user_resp.json().get("id")
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/anime_comments",
                        headers=supabase_headers(access_token),
                        params={"id": f"eq.{comment_id}", "user_id": f"eq.{user_id}"})
    return jsonify({"ok": r.ok})


if __name__ == "__main__":
    app.run(debug=True)

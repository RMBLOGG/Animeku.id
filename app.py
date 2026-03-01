from flask import Flask, render_template, request, jsonify, session, redirect, send_from_directory
import requests
import json
import os
from upstash_redis import Redis

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "animeku-secret-2026")
API_BASE = "https://www.sankavollerei.com"

SUPABASE_URL = "https://mafnnqttvkdgqqxczqyt.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1hZm5ucXR0dmtkZ3FxeGN6cXl0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzE4NzQyMDEsImV4cCI6MjA4NzQ1MDIwMX0.YRh1oWVKnn4tyQNRbcPhlSyvr7V_1LseWN7VjcImb-Y"

# ── Premium Config ─────────────────────────────────────────────────────────────
FREE_EPISODE_LIMIT = 2  # 2 episode pertama gratis (index 0 & 1, diurutkan terbaru)

PREMIUM_PAKET = {
    15000:  {"plan_id": "monthly",   "days": 30,  "label": "1 Bulan"},
    35000:  {"plan_id": "quarterly", "days": 90,  "label": "3 Bulan"},
    100000: {"plan_id": "yearly",    "days": 365, "label": "1 Tahun"},
}

def supabase_headers(access_token=None):
    h = {"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"}
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    else:
        h["Authorization"] = f"Bearer {SUPABASE_ANON_KEY}"
    return h

# ── Premium Helpers ────────────────────────────────────────────────────────────

def is_premium(user_id):
    from datetime import datetime, timezone
    if not user_id:
        return False
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/premium_users",
            headers=supabase_headers(),
            params={"user_id": f"eq.{user_id}", "expired_at": f"gt.{now_iso}", "select": "id", "limit": "1"},
            timeout=5,
        )
        return r.ok and len(r.json()) > 0
    except Exception as e:
        print(f"[Premium] is_premium error: {e}")
        return False

def get_premium_info(user_id):
    from datetime import datetime, timezone
    if not user_id:
        return None
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/premium_users",
            headers=supabase_headers(),
            params={"user_id": f"eq.{user_id}", "expired_at": f"gt.{now_iso}", "select": "*", "order": "expired_at.desc", "limit": "1"},
            timeout=5,
        )
        rows = r.json() if r.ok else []
        return rows[0] if rows else None
    except Exception as e:
        print(f"[Premium] get_premium_info error: {e}")
        return None

def extract_email(text):
    import re
    text = (text or "").strip()
    match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    return match.group(0).lower() if match else None

def find_user_by_email(email):
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not service_key:
        print("[Premium] SUPABASE_SERVICE_ROLE_KEY tidak di-set")
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/auth/v1/admin/users",
            headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            params={"filter": f"email.eq.{email}"},
            timeout=8,
        )
        if r.ok:
            users = r.json().get("users", [])
            return users[0] if users else None
    except Exception as e:
        print(f"[Premium] find_user_by_email error: {e}")
    return None

def grant_premium_by_user_id(user_id, plan_id, days):
    from datetime import datetime, timezone, timedelta
    expired_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/premium_users",
        headers={**supabase_headers(), "Prefer": "return=representation"},
        json={"user_id": user_id, "plan_id": plan_id, "expired_at": expired_at},
        timeout=8,
    )
    return r.ok, expired_at

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
    # Jitter ±10% supaya cache tidak expired serentak
    return base + int(base * random.uniform(-0.1, 0.1))

def fetch(path, params=None):
    key   = "samehadaku:" + path + str(sorted(params.items()) if params else "")
    lock_key = key + ":lock"

    # 1. Fast path — cache hit
    try:
        cached = redis.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        print(f"Redis get error: {e}")

    # 2. Coba acquire distributed lock (SET NX EX 10)
    #    Hanya 1 worker/instance yang berhasil, sisanya dapat None
    lock_acquired = False
    try:
        lock_acquired = redis.set(lock_key, "1", nx=True, ex=10)
    except Exception as e:
        print(f"Redis lock error: {e}")

    if lock_acquired:
        # Worker yang dapat lock → fetch ke API
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
        # Worker lain sedang fetch → tunggu max 3 detik sampai cache terisi
        for _ in range(6):
            time.sleep(0.5)
            try:
                cached = redis.get(key)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass
        # Timeout — fallback fetch langsung (last resort)
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

# ── Pages ──────────────────────────────────────────────────────────────────────

@app.route("/manifest.json")
def manifest():
    return send_from_directory(app.static_folder, "manifest.json", mimetype="application/manifest+json")

@app.route("/")
def landing():
    return render_template("landing.html")

@app.route("/home")
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

    # Fallback anime_slug dari animeId di response episode
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

    # ── Cek premium ───────────────────────────────────────────────────────────
    locked = False
    if anime_data and anime_data.get("detail") and anime_data["detail"].get("episodes"):
        episodes = anime_data["detail"]["episodes"]
        ep_index = next((i for i, e in enumerate(episodes) if e["slug"] == slug), None)
        if ep_index is not None and ep_index >= FREE_EPISODE_LIMIT:
            user = session.get("user")
            if not user or not is_premium(user.get("id")):
                locked = True
                if data:
                    data["streams"] = []

    return render_template("episode.html", data=data, slug=slug,
                           anime_slug=anime_slug, anime_data=anime_data,
                           locked=locked, free_limit=FREE_EPISODE_LIMIT)


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
        list_data = raw["data"].get("list", [])
        anime_list = []
        for group in list_data:
            letter = group.get("startWith", "#")
            animes = [{"title": a.get("title", ""), "slug": a.get("animeId", "")}
                      for a in group.get("animeList", [])]
            if animes:
                anime_list.append({"letter": letter, "animes": animes})
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


@app.route("/chat")
def chat():
    return render_template("chat.html")


@app.route("/admin")
def admin():
    return render_template("admin.html")

@app.route("/premium")
def premium_page():
    user = session.get("user")
    premium_info = get_premium_info(user["id"]) if user else None
    plans = [
        {"id": "monthly",   "name": "1 Bulan",  "price": 15000,  "days": 30,  "badge": "🔥 Populer"},
        {"id": "quarterly", "name": "3 Bulan",  "price": 35000,  "days": 90,  "badge": "💎 Hemat"},
        {"id": "yearly",    "name": "1 Tahun",  "price": 100000, "days": 365, "badge": "👑 Terbaik"},
    ]
    return render_template("premium.html", plans=plans, user=user, premium_info=premium_info)

# ── Premium API ────────────────────────────────────────────────────────────────

@app.route("/api/premium/status")
def api_premium_status():
    user = session.get("user")
    if not user:
        return jsonify({"premium": False, "user": None})
    info = get_premium_info(user["id"])
    return jsonify({"premium": info is not None, "expired_at": info["expired_at"] if info else None, "user": user})

@app.route("/api/premium/grant-by-email", methods=["POST"])
def api_premium_grant_by_email():
    from datetime import datetime, timezone, timedelta
    admin_secret = request.headers.get("X-Admin-Secret", "")
    expected     = os.environ.get("ADMIN_SECRET", "")
    if not expected or admin_secret != expected:
        return jsonify({"error": "Unauthorized"}), 401
    data       = request.get_json(silent=True) or {}
    email      = (data.get("email") or "").strip().lower()
    days       = int(data.get("days", 30))
    pending_id = data.get("pending_id")
    if not email:
        return jsonify({"error": "Email wajib diisi"}), 400
    user = find_user_by_email(email)
    if not user:
        return jsonify({"error": f"User '{email}' tidak ditemukan"}), 404
    plan_map = {30: "monthly", 90: "quarterly", 365: "yearly"}
    ok, expired_at = grant_premium_by_user_id(user["id"], plan_map.get(days, "manual"), days)
    if not ok:
        return jsonify({"error": "Gagal insert ke premium_users"}), 500
    if pending_id:
        try:
            requests.patch(
                f"{SUPABASE_URL}/rest/v1/pending_premium",
                headers={**supabase_headers(), "Prefer": "return=representation"},
                params={"id": f"eq.{pending_id}"},
                json={"status": "granted", "granted_to_email": email, "granted_at": datetime.now(timezone.utc).isoformat()},
                timeout=8,
            )
        except Exception as e:
            print(f"[Premium] Gagal update pending: {e}")
    return jsonify({"ok": True, "expired_at": expired_at, "email": email})

@app.route("/api/premium/check/<user_id>")
def api_premium_check(user_id):
    return jsonify({"premium": is_premium(user_id), "user_id": user_id})


@app.route("/api/premium/grant-by-email", methods=["POST"])
def api_premium_grant_by_email():
    """Grant premium by email — dipanggil dari panel admin."""
    from datetime import datetime, timezone, timedelta

    admin_secret = request.headers.get("X-Admin-Secret", "")
    expected     = os.environ.get("ADMIN_SECRET", "")
    if not expected or admin_secret != expected:
        return jsonify({"error": "Unauthorized"}), 401

    data       = request.get_json(silent=True) or {}
    email      = (data.get("email") or "").strip().lower()
    days       = int(data.get("days", 30))
    pending_id = data.get("pending_id")

    if not email:
        return jsonify({"error": "Email wajib diisi"}), 400

    # Cari user by email
    user = find_user_by_email(email)
    if not user:
        return jsonify({"error": f"User dengan email '{email}' tidak ditemukan di Supabase"}), 404

    user_id   = user.get("id")
    plan_map  = {30: "monthly", 90: "quarterly", 365: "yearly"}
    plan_id   = plan_map.get(days, "manual")

    ok, expired_at = grant_premium_by_user_id(user_id, plan_id, days)
    if not ok:
        return jsonify({"error": "Gagal insert ke premium_users"}), 500

    print(f"[Premium] ✅ Manual grant oleh admin → {email} | {days} hari | s/d {expired_at[:10]}")

    # Update status pending jika ada pending_id
    if pending_id:
        try:
            requests.patch(
                f"{SUPABASE_URL}/rest/v1/pending_premium",
                headers={**supabase_headers(), "Prefer": "return=representation"},
                params={"id": f"eq.{pending_id}"},
                json={"status": "granted", "granted_to_email": email, "granted_at": datetime.now(timezone.utc).isoformat()},
                timeout=8,
            )
        except Exception as e:
            print(f"[Premium] Gagal update pending status: {e}")

    return jsonify({"ok": True, "expired_at": expired_at, "email": email})


@app.route("/api/premium/pending")
def api_premium_pending():
    """Ambil daftar donasi pending (untuk admin)."""
    admin_secret = request.headers.get("X-Admin-Secret", "")
    expected     = os.environ.get("ADMIN_SECRET", "")
    if not expected or admin_secret != expected:
        return jsonify({"error": "Unauthorized"}), 401

    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/pending_premium",
        headers=supabase_headers(),
        params={"order": "created_at.desc", "limit": "100", "select": "*"},
    )
    return jsonify(r.json() if r.ok else [])


# ── API Proxy ──────────────────────────────────────────────────────────────────

@app.route("/api/search/<keyword>")
def api_search(keyword):
    raw = fetch("/anime/samehadaku/search", {"q": keyword})
    data = None
    if raw and raw.get("data"):
        data = {"animes": norm_list(raw["data"].get("animeList", []))}
    return jsonify(data)


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


# ── Sociabuzz Webhook ──────────────────────────────────────────────────────────

# Mapping nominal → paket premium
PREMIUM_PAKET = {
    15000:  {"plan_id": "monthly",   "days": 30,  "label": "1 Bulan"},
    35000:  {"plan_id": "quarterly", "days": 90,  "label": "3 Bulan"},
    100000: {"plan_id": "yearly",    "days": 365, "label": "1 Tahun"},
}

def extract_email(text):
    """Ekstrak alamat email dari string pesan."""
    import re
    text = (text or "").strip()
    match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    return match.group(0).lower() if match else None

def find_user_by_email(email):
    """Cari user di Supabase Auth berdasarkan email (butuh service role key)."""
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not service_key:
        print("[Premium] SUPABASE_SERVICE_ROLE_KEY tidak di-set, tidak bisa cari user by email")
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/auth/v1/admin/users",
            headers={
                "apikey":        service_key,
                "Authorization": f"Bearer {service_key}",
            },
            params={"filter": f"email.eq.{email}"},
            timeout=8,
        )
        if r.ok:
            users = r.json().get("users", [])
            return users[0] if users else None
    except Exception as e:
        print(f"[Premium] find_user_by_email error: {e}")
    return None

def grant_premium_by_user_id(user_id, plan_id, days):
    """Insert row premium_users ke Supabase."""
    from datetime import datetime, timezone, timedelta
    expired_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    payload = {"user_id": user_id, "plan_id": plan_id, "expired_at": expired_at}
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/premium_users",
        headers={**supabase_headers(), "Prefer": "return=representation"},
        json=payload,
        timeout=8,
    )
    return r.ok, expired_at

@app.route("/api/sociabuzz/webhook", methods=["POST"])
def sociabuzz_webhook():
    from datetime import datetime, timezone, timedelta

    data = request.get_json(silent=True) or {}

    donor_name   = data.get("donatur_name", data.get("name", "Anonymous"))
    amount       = int(data.get("amount", 0))
    message      = data.get("message", "")
    supporter_id = str(data.get("order_id", data.get("id", "")))

    print(f"[Sociabuzz] Donasi dari {donor_name}: Rp{amount} - '{message}' (ID: {supporter_id})")

    # ── 1. Simpan ke tabel donations ─────────────────────────────────────────
    premium_granted  = False
    premium_email    = None
    premium_plan     = None
    premium_note     = ""

    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/donations",
            headers={**supabase_headers(), "Prefer": "return=representation"},
            json={
                "donor_name":   donor_name,
                "amount":       amount,
                "message":      message,
                "supporter_id": supporter_id,
            },
        )
        print(f"[Sociabuzz] Donasi tersimpan: {r.ok}")
    except Exception as e:
        print(f"[Sociabuzz] Simpan donasi error: {e}")

    # ── 2. Auto-grant premium berdasarkan nominal & email di pesan ───────────
    paket = PREMIUM_PAKET.get(amount)
    if paket:
        premium_plan = paket
        email = extract_email(message)

        if email:
            premium_email = email
            print(f"[Premium] Email ditemukan: {email} | Paket: {paket['label']}")

            user = find_user_by_email(email)
            if user:
                user_id = user.get("id")
                ok, expired_at = grant_premium_by_user_id(user_id, paket["plan_id"], paket["days"])
                if ok:
                    premium_granted = True
                    premium_note = f"Premium {paket['label']} granted ke {email} s/d {expired_at[:10]}"
                    print(f"[Premium] ✅ {premium_note}")
                else:
                    premium_note = f"Gagal insert premium_users untuk {email}"
                    print(f"[Premium] ❌ {premium_note}")
            else:
                premium_note = f"User dengan email '{email}' tidak ditemukan di Supabase"
                print(f"[Premium] ⚠️ {premium_note}")
        else:
            # ── Tidak ada email → masuk antrian pending ──────────────────
            premium_note = "Tidak ada email di pesan — masuk antrian pending"
            print(f"[Premium] ⚠️ {premium_note}")
            try:
                requests.post(
                    f"{SUPABASE_URL}/rest/v1/pending_premium",
                    headers={**supabase_headers(), "Prefer": "return=representation"},
                    json={
                        "donor_name":   donor_name,
                        "amount":       amount,
                        "message":      message,
                        "supporter_id": supporter_id,
                        "plan_id":      paket["plan_id"],
                        "status":       "pending",
                    },
                    timeout=8,
                )
                print(f"[Premium] Tersimpan ke pending_premium ✅")
            except Exception as e:
                print(f"[Premium] Gagal simpan pending: {e}")
    else:
        premium_note = f"Nominal Rp{amount} tidak cocok dengan paket manapun"
        print(f"[Premium] ℹ️ {premium_note}")

    # ── 3. Simpan log premium ke tabel premium_logs ───────────────────────────
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/premium_logs",
            headers={**supabase_headers(), "Prefer": "return=representation"},
            json={
                "supporter_id":    supporter_id,
                "donor_name":      donor_name,
                "amount":          amount,
                "email_dari_pesan": premium_email,
                "plan_id":         paket["plan_id"] if paket else None,
                "granted":         premium_granted,
                "note":            premium_note,
            },
        )
    except Exception as e:
        print(f"[Premium] Log error: {e}")

    # ── 4. Notifikasi Live Chat ───────────────────────────────────────────────
    try:
        rp_fmt = f"Rp {amount:,}".replace(",", ".")
        if premium_granted:
            chat_content = (
                f"🎉 SPECIAL THANKS kepada {donor_name} yang berlangganan Premium {paket['label']} ({rp_fmt})!"
                f" Selamat menikmati streaming tanpa batas! 🚀"
            )
        else:
            chat_content = f"🎉 SPECIAL THANKS kepada {donor_name} yang telah berdonasi {rp_fmt}!"
            if message:
                chat_content += f' 💬 "{message}"'

        requests.post(
            f"{SUPABASE_URL}/rest/v1/chat_messages",
            headers={**supabase_headers(), "Prefer": "return=representation"},
            json={
                "room_id":     "global",
                "user_id":     "system-donation",
                "user_name":   "💖 Donasi Alert",
                "user_avatar": "",
                "content":     chat_content,
                "is_donation": True,
                "donor_name":  donor_name,
                "amount":      amount,
                "reactions":   {},
            },
        )
        print(f"[Sociabuzz] Notifikasi chat terkirim ✅")
    except Exception as e:
        print(f"[Sociabuzz] Chat error: {e}")

    return jsonify({
        "ok":              True,
        "received":        supporter_id,
        "premium_granted": premium_granted,
        "note":            premium_note,
    }), 200


@app.route("/api/donations")
def api_donations():
    from datetime import datetime, timezone

    # Ambil donation goal dari site_config
    goal = 300000
    try:
        cfg = requests.get(
            f"{SUPABASE_URL}/rest/v1/site_config",
            headers=supabase_headers(),
            params={"key": "eq.donation_goal", "select": "value"}
        )
        if cfg.ok and cfg.json():
            goal = cfg.json()[0]["value"].get("monthly_target", 300000)
    except Exception:
        pass

    # Ambil 50 donasi terbaru
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/donations",
        headers=supabase_headers(),
        params={"order": "created_at.desc", "limit": "50", "select": "*"}
    )
    donations = r.json() if r.ok else []

    # Hitung total donasi bulan ini
    now       = datetime.now(timezone.utc)
    month_str = now.strftime("%Y-%m")
    total     = sum(d["amount"] for d in donations if d.get("created_at", "").startswith(month_str))

    # Leaderboard: top 5 donor berdasarkan total donasi
    lb_dict = {}
    for d in donations:
        name = d.get("donor_name", "Anonymous")
        lb_dict[name] = lb_dict.get(name, 0) + d["amount"]
    leaderboard = sorted(
        [{"name": k, "total": v} for k, v in lb_dict.items()],
        key=lambda x: x["total"], reverse=True
    )[:5]

    return jsonify({
        "donations":      donations[:20],
        "monthly_total":  total,
        "monthly_target": goal,
        "leaderboard":    leaderboard,
    })


if __name__ == "__main__":
    app.run(debug=True)
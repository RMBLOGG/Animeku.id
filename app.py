from flask import Flask, render_template, request, jsonify, session, redirect
import requests
import json
import os
from upstash_redis import Redis

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "animeku-secret-2026")
API_BASE = "https://www.sankavollerei.com"

SUPABASE_URL = "https://mafnnqttvkdgqqxczqyt.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1hZm5ucXR0dmtkZ3FxeGN6cXl0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzE4NzQyMDEsImV4cCI6MjA4NzQ1MDIwMX0.YRh1oWVKnn4tyQNRbcPhlSyvr7V_1LseWN7VjcImb-Y"

def supabase_headers(access_token=None):
    h = {"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"}
    if access_token:
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
    "completed":600, "latest":300, "search":120, "genres":3600,
    "genre":300, "schedule":1800, "animelist":3600,
    "detail":600, "episode":180, "default":300,
}

def _ttl(path):
    for k, v in CACHE_TTL.items():
        if k in path: return v
    return CACHE_TTL["default"]

def fetch(path, params=None):
    key = "animasu:" + path + str(sorted(params.items()) if params else "")

    # Cek cache Redis dulu
    try:
        cached = redis.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        print(f"Redis get error: {e}")

    # Kalau tidak ada, fetch dari API
    try:
        r = requests.get(f"{API_BASE}{path}", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        # Simpan ke Redis dengan TTL otomatis
        try:
            redis.set(key, json.dumps(data), ex=_ttl(path))
        except Exception as e:
            print(f"Redis set error: {e}")
        return data
    except Exception as e:
        print(f"API error [{path}]: {e}")
        return None

# ── Pages ──────────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    data     = fetch("/anime/animasu/home")
    popular  = fetch("/anime/animasu/popular")
    schedule = fetch("/anime/animasu/schedule")
    return render_template("index.html", data=data, popular=popular, schedule=schedule)

@app.route("/anime/<slug>")
def detail(slug):
    data = fetch(f"/anime/animasu/detail/{slug}")
    return render_template("detail.html", data=data, slug=slug)

@app.route("/episode/<slug>")
def episode(slug):
    data       = fetch(f"/anime/animasu/episode/{slug}")
    anime_slug = request.args.get("anime", "")
    anime_data = fetch(f"/anime/animasu/detail/{anime_slug}") if anime_slug else None
    return render_template("episode.html", data=data, slug=slug,
                           anime_slug=anime_slug, anime_data=anime_data)

@app.route("/genre/<slug>")
def genre(slug):
    page   = request.args.get("page", 1)
    data   = fetch(f"/anime/animasu/genre/{slug}", {"page": page})
    genres = fetch("/anime/animasu/genres")
    return render_template("genre.html", data=data, slug=slug, genres=genres, page=int(page))

@app.route("/genres")
def genres():
    return render_template("genres.html", data=fetch("/anime/animasu/genres"))

@app.route("/jadwal")
def schedule():
    return render_template("schedule.html", data=fetch("/anime/animasu/schedule"))

@app.route("/movies")
def movies():
    page = request.args.get("page", 1)
    data = fetch("/anime/animasu/movies", {"page": page})
    return render_template("list.html", data=data, title="Movie", page=int(page), base_url="/movies")

@app.route("/ongoing")
def ongoing():
    page = request.args.get("page", 1)
    data = fetch("/anime/animasu/ongoing", {"page": page})
    return render_template("list.html", data=data, title="Ongoing", page=int(page), base_url="/ongoing")

@app.route("/completed")
def completed():
    page = request.args.get("page", 1)
    data = fetch("/anime/animasu/completed", {"page": page})
    return render_template("list.html", data=data, title="Completed", page=int(page), base_url="/completed")

@app.route("/popular")
def popular():
    page = request.args.get("page", 1)
    data = fetch("/anime/animasu/popular", {"page": page})
    return render_template("list.html", data=data, title="Populer", page=int(page), base_url="/popular")

@app.route("/animelist")
def animelist():
    return render_template("animelist.html", data=fetch("/anime/animasu/animelist"))

@app.route("/search")
def search():
    q    = request.args.get("q", "")
    data = fetch(f"/anime/animasu/search/{q}") if q else None
    return render_template("search.html", data=data, query=q)

@app.route("/koleksi")
def koleksi():
    return render_template("koleksi.html")

# ── API Proxy ──────────────────────────────────────────────────────────────────

@app.route("/api/search/<keyword>")
def api_search(keyword):
    return jsonify(fetch(f"/anime/animasu/search/{keyword}"))

# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.route("/auth/login")
def auth_login():
    redirect_to = f"{SUPABASE_URL}/auth/v1/authorize?provider=google&redirect_to={request.host_url}auth/callback"
    return redirect(redirect_to)

@app.route("/auth/callback")
def auth_callback():
    # Token dikirim via hash fragment, ditangkap JS di client
    return render_template("auth_callback.html")

@app.route("/auth/session", methods=["POST"])
def auth_session():
    data = request.get_json()
    if data and data.get("access_token"):
        session["access_token"] = data["access_token"]
        session["user"] = {
            "id": data.get("user", {}).get("id"),
            "name": data.get("user", {}).get("user_metadata", {}).get("full_name", "User"),
            "avatar": data.get("user", {}).get("user_metadata", {}).get("avatar_url", ""),
            "email": data.get("user", {}).get("email", ""),
        }
    return jsonify({"ok": True})

@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/me")
def api_me():
    user = session.get("user")
    return jsonify({"user": user})

# ── Comment Routes ─────────────────────────────────────────────────────────────

@app.route("/api/comments/<anime_slug>")
def get_comments(anime_slug):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/anime_comments",
        headers=supabase_headers(),
        params={
            "anime_slug": f"eq.{anime_slug}",
            "order": "created_at.desc",
            "select": "*"
        }
    )
    return jsonify(r.json() if r.ok else [])

@app.route("/api/comments", methods=["POST"])
def post_comment():
    # Ambil token dari Authorization header (bukan session, karena Vercel serverless)
    auth_header = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not access_token:
        return jsonify({"error": "Login dulu ya!"}), 401

    # Verifikasi user dari Supabase
    user_resp = requests.get(
        f"{SUPABASE_URL}/auth/v1/user",
        headers=supabase_headers(access_token)
    )
    if not user_resp.ok:
        return jsonify({"error": "Login dulu ya!"}), 401

    user_data = user_resp.json()
    user = {
        "id": user_data.get("id"),
        "name": user_data.get("user_metadata", {}).get("full_name", "User"),
        "avatar": user_data.get("user_metadata", {}).get("avatar_url", ""),
    }

    data = request.get_json()
    content = (data.get("content") or "").strip()
    anime_slug = data.get("anime_slug", "")
    if not content or len(content) < 2:
        return jsonify({"error": "Komentar terlalu pendek"}), 400

    payload = {
        "anime_slug": anime_slug,
        "user_id": user["id"],
        "user_name": user["name"],
        "user_avatar": user["avatar"],
        "content": content
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/anime_comments",
        headers={**supabase_headers(access_token), "Prefer": "return=representation"},
        json=payload
    )
    if r.ok:
        return jsonify(r.json()[0] if r.json() else {})
    return jsonify({"error": "Gagal kirim komentar", "detail": r.text}), 500

@app.route("/api/comments/<comment_id>", methods=["DELETE"])
def delete_comment(comment_id):
    auth_header = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not access_token:
        return jsonify({"error": "Unauthorized"}), 401

    user_resp = requests.get(
        f"{SUPABASE_URL}/auth/v1/user",
        headers=supabase_headers(access_token)
    )
    if not user_resp.ok:
        return jsonify({"error": "Unauthorized"}), 401

    user_id = user_resp.json().get("id")
    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/anime_comments",
        headers=supabase_headers(access_token),
        params={"id": f"eq.{comment_id}", "user_id": f"eq.{user_id}"}
    )
    return jsonify({"ok": r.ok})

if __name__ == "__main__":
    app.run(debug=True)

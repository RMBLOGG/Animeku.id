from flask import Flask, render_template, request, jsonify
import requests
import time

app = Flask(__name__)
API_BASE = "https://www.sankavollerei.com"

# ── In-Memory Cache ────────────────────────────────────────────────────────────
CACHE_TTL = {
    "home":300, "popular":600, "movies":600, "ongoing":300,
    "completed":600, "latest":300, "search":120, "genres":3600,
    "genre":300, "schedule":1800, "animelist":3600,
    "detail":600, "episode":180, "default":300,
}
_cache = {}

def _ttl(path):
    for k, v in CACHE_TTL.items():
        if k in path: return v
    return CACHE_TTL["default"]

def fetch(path, params=None):
    key = path + str(sorted(params.items()) if params else "")
    now = time.time()
    if key in _cache:
        data, exp = _cache[key]
        if now < exp: return data
    try:
        r = requests.get(f"{API_BASE}{path}", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        _cache[key] = (data, now + _ttl(path))
        return data
    except Exception as e:
        print(f"API error [{path}]: {e}")
        return _cache[key][0] if key in _cache else None

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

if __name__ == "__main__":
    app.run(debug=True)

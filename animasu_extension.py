"""
animasu_extension.py
====================
Extension untuk provider Animasu. Daftarkan ke app.py dengan:

    from animasu_extension import animasu_bp
    app.register_blueprint(animasu_bp)

URL prefix semua route di sini: /animasu/...
Contoh:
    /animasu/home
    /animasu/anime/<slug>
    /animasu/episode/<slug>
    /animasu/ongoing
    /animasu/completed
    /animasu/genre/<slug>
    /animasu/genres
    /animasu/jadwal
    /animasu/animelist
    /animasu/search
    /animasu/api/search/<keyword>
"""

from flask import Blueprint, render_template, request, jsonify
import json, os, time, random
import requests
from upstash_redis import Redis

# ── Config ─────────────────────────────────────────────────────────────────────
ANIMASU_BASE = "https://www.sankavollerei.com"   # ganti jika base URL beda
ANIMASU_PREFIX = "/anime/animasu"

# Variable yang di-inject ke semua template agar link /episode/ dan /anime/ benar
BASE_VARS = {
    "episode_base": "/animasu/episode",
    "anime_base":   "/animasu/anime",
}

animasu_bp = Blueprint("animasu", __name__, url_prefix="/animasu")

# ── Redis (shared, reuse dari env yang sama) ───────────────────────────────────
_redis = None

def get_redis():
    global _redis
    if _redis is None:
        _redis = Redis(
            url=os.environ["UPSTASH_REDIS_REST_URL"],
            token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
        )
    return _redis

CACHE_TTL = {
    "home": 300, "ongoing": 300, "completed": 600, "latest": 300,
    "search": 120, "genres": 3600, "genre": 300, "schedule": 120,
    "animelist": 3600, "anime": 600, "episode": 180,
    "default": 300,
}

def _ttl(path):
    base = CACHE_TTL["default"]
    for k, v in CACHE_TTL.items():
        if k in path:
            base = v
            break
    return base + int(base * random.uniform(-0.1, 0.1))


def fetch_animasu(path, params=None):
    """Fetch dari endpoint Animasu dengan Redis cache + distributed lock."""
    redis = get_redis()
    key      = f"animasu:{path}{str(sorted(params.items()) if params else '')}"
    lock_key = key + ":lock"

    # 1. Cache hit
    try:
        cached = redis.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        print(f"[animasu] Redis get error: {e}")

    # 2. Acquire lock
    lock_acquired = False
    try:
        lock_acquired = redis.set(lock_key, "1", nx=True, ex=10)
    except Exception as e:
        print(f"[animasu] Redis lock error: {e}")

    if lock_acquired:
        try:
            r = requests.get(f"{ANIMASU_BASE}{path}", params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            try:
                redis.set(key, json.dumps(data), ex=_ttl(path))
            except Exception as e:
                print(f"[animasu] Redis set error: {e}")
            return data
        except Exception as e:
            print(f"[animasu] API error [{path}]: {e}")
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
            r = requests.get(f"{ANIMASU_BASE}{path}", params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[animasu] Fallback error [{path}]: {e}")
            return None


# ── Normalisasi ────────────────────────────────────────────────────────────────
# Animasu sudah return field yang sama (slug, title, poster, episode,
# status_or_day, type) jadi tidak perlu mapping banyak.

def _norm_anime(a):
    """Normalisasi item anime Animasu → format standar template."""
    if not a:
        return a
    return {
        "slug":          a.get("slug", ""),
        "title":         a.get("title", ""),
        "poster":        a.get("poster", ""),
        "episode":       a.get("episode", ""),
        "status_or_day": a.get("status_or_day", ""),
        "type":          a.get("type", ""),
        "score":         a.get("score", ""),
        "rank":          a.get("rank", None),
    }

def _norm_list(animes):
    return [_norm_anime(a) for a in (animes or [])]

def _norm_pagination(raw):
    p = raw.get("pagination") if raw else None
    if not p:
        return None
    return {
        "hasNext":     p.get("hasNext", False),
        "hasPrev":     p.get("hasPrev", False),
        "currentPage": p.get("currentPage", 1),
    }

def _norm_genres(genres):
    return [{"name": g.get("name", ""), "slug": g.get("slug", "")} for g in (genres or [])]

def _norm_schedule(raw):
    """
    Animasu schedule sudah dalam format:
      { "schedule": { "senin": [...], "selasa": [...], ... } }
    Tiap item: { slug, title, poster, episode, status_or_day, type }
    """
    if not raw or not raw.get("schedule"):
        return None
    sched = {}
    for day, items in raw["schedule"].items():
        sched[day.capitalize()] = [_norm_anime(a) for a in items]
    return {"schedule": sched}


# ── Halaman ────────────────────────────────────────────────────────────────────

@animasu_bp.route("/home")
def home():
    raw = fetch_animasu(f"{ANIMASU_PREFIX}/home")
    data = None
    if raw and raw.get("status") == "success":
        data = {
            "ongoing": _norm_list(raw.get("ongoing", [])),
            "recent":  _norm_list(raw.get("recent", [])),
        }
    # Reuse template index.html yang sudah ada
    # popular & schedule optional – kirim None agar template tidak error
    return render_template("index.html", data=data, popular=None, schedule=None, **BASE_VARS)


@animasu_bp.route("/anime/<slug>")
def detail(slug):
    raw = fetch_animasu(f"{ANIMASU_PREFIX}/anime/{slug}")
    data = None
    if raw and raw.get("status") == "success":
        d = raw.get("detail", {})
        eps    = [{"name": e.get("name", ""), "slug": e.get("slug", "")}
                  for e in d.get("episodes", [])]
        genres = _norm_genres(d.get("genres", []))
        data = {
            "detail": {
                "title":    d.get("title", ""),
                "poster":   d.get("poster", ""),
                "synopsis": d.get("synopsis", ""),
                "trailer":  d.get("trailer", ""),
                "genres":   genres,
                "episodes": eps,
                "info": {
                    "japanese":      d.get("synonym", ""),
                    "status":        d.get("status", ""),
                    "type":          d.get("type", ""),
                    "score":         d.get("rating", ""),
                    "total_episode": "",
                    "duration":      d.get("duration", ""),
                    "released":      d.get("aired", ""),
                    "studio":        d.get("studio", ""),
                    "season":        d.get("season", ""),
                }
            }
        }
    # Pakai template detail.html yang sama, tapi slug episode harus diawali
    # dengan prefix provider agar route episode animasu kepanggil.
    # Template perlu tahu base URL episode → kirim via extra context
    return render_template("detail.html", data=data, slug=slug, **BASE_VARS)


@animasu_bp.route("/episode/<slug>")
def episode(slug):
    raw        = fetch_animasu(f"{ANIMASU_PREFIX}/episode/{slug}")
    anime_slug = request.args.get("anime", "")

    data = None
    if raw and raw.get("status") == "success":
        # Animasu episode response: { streams: [{name, url}], downloads: [] }
        streams = [{"name": s.get("name", ""), "serverId": "", "url": s.get("url", "")}
                   for s in raw.get("streams", [])]
        data = {
            "title":     raw.get("title", ""),
            "anime_id":  anime_slug,
            "streams":   streams,
            "downloads": raw.get("downloads", []),
        }

    # Ambil data anime untuk sidebar episode list
    anime_data = None
    if anime_slug:
        anime_raw = fetch_animasu(f"{ANIMASU_PREFIX}/anime/{anime_slug}")
        if anime_raw and anime_raw.get("status") == "success":
            d2  = anime_raw.get("detail", {})
            eps = [{"name": e.get("name", ""), "slug": e.get("slug", "")}
                   for e in d2.get("episodes", [])]
            anime_data = {
                "detail": {
                    "title":    d2.get("title", ""),
                    "poster":   d2.get("poster", ""),
                    "genres":   _norm_genres(d2.get("genres", [])),
                    "episodes": eps,
                }
            }

    return render_template("episode.html", data=data, slug=slug,
                           anime_slug=anime_slug, anime_data=anime_data, **BASE_VARS)


@animasu_bp.route("/ongoing")
def ongoing():
    page = request.args.get("page", 1, type=int)
    raw  = fetch_animasu(f"{ANIMASU_PREFIX}/ongoing", {"page": page})
    data = None
    if raw and raw.get("status") == "success":
        data = {"animes": _norm_list(raw.get("animes", [])),
                "pagination": _norm_pagination(raw)}
    return render_template("list.html", data=data, title="Ongoing (Animasu)",
                           page=page, base_url="/animasu/ongoing", **BASE_VARS)


@animasu_bp.route("/completed")
def completed():
    page = request.args.get("page", 1, type=int)
    raw  = fetch_animasu(f"{ANIMASU_PREFIX}/completed", {"page": page})
    data = None
    if raw and raw.get("status") == "success":
        data = {"animes": _norm_list(raw.get("animes", [])),
                "pagination": _norm_pagination(raw)}
    return render_template("list.html", data=data, title="Completed (Animasu)",
                           page=page, base_url="/animasu/completed", **BASE_VARS)


@animasu_bp.route("/latest")
def latest():
    page = request.args.get("page", 1, type=int)
    raw  = fetch_animasu(f"{ANIMASU_PREFIX}/latest", {"page": page})
    data = None
    if raw and raw.get("status") == "success":
        data = {"animes": _norm_list(raw.get("animes", [])),
                "pagination": _norm_pagination(raw)}
    return render_template("list.html", data=data, title="Terbaru (Animasu)",
                           page=page, base_url="/animasu/latest", **BASE_VARS)


@animasu_bp.route("/animelist")
def animelist():
    raw = fetch_animasu(f"{ANIMASU_PREFIX}/animelist")
    data = None
    if raw and raw.get("status") == "success":
        # Animasu animelist: { animes: [{title, slug, poster, genres, release, status, type, episode_count}] }
        # Kelompokkan per huruf awal
        from collections import defaultdict
        grouped = defaultdict(list)
        for a in raw.get("animes", []):
            letter = a.get("title", "#")[0].upper()
            grouped[letter].append({"title": a.get("title", ""), "slug": a.get("slug", "")})
        anime_list = [{"letter": k, "animes": v} for k, v in sorted(grouped.items())]
        data = {"anime_list": anime_list}
    return render_template("animelist.html", data=data, **BASE_VARS)


@animasu_bp.route("/genres")
def genres():
    raw = fetch_animasu(f"{ANIMASU_PREFIX}/genres")
    data = None
    if raw and raw.get("status") == "success":
        data = {"genres": _norm_genres(raw.get("genres", []))}
    return render_template("genres.html", data=data, **BASE_VARS)


@animasu_bp.route("/genre/<slug>")
def genre(slug):
    page       = request.args.get("page", 1, type=int)
    raw        = fetch_animasu(f"{ANIMASU_PREFIX}/genre/{slug}", {"page": page})
    genres_raw = fetch_animasu(f"{ANIMASU_PREFIX}/genres")

    data = None
    if raw and raw.get("status") == "success":
        data = {"animes": _norm_list(raw.get("animes", [])),
                "pagination": _norm_pagination(raw)}

    genres = None
    if genres_raw and genres_raw.get("status") == "success":
        genres = {"genres": _norm_genres(genres_raw.get("genres", []))}

    return render_template("genre.html", data=data, slug=slug,
                           genres=genres, page=page, **BASE_VARS)


@animasu_bp.route("/jadwal")
def schedule():
    raw = fetch_animasu(f"{ANIMASU_PREFIX}/schedule")
    return render_template("schedule.html", data=_norm_schedule(raw), **BASE_VARS)


@animasu_bp.route("/search")
def search():
    q   = request.args.get("q", "")
    raw = fetch_animasu(f"{ANIMASU_PREFIX}/search/{q}") if q else None
    data = None
    if raw and raw.get("status") == "success":
        data = {"animes": _norm_list(raw.get("animes", []))}
    return render_template("search.html", data=data, query=q, **BASE_VARS)


# ── API ────────────────────────────────────────────────────────────────────────

@animasu_bp.route("/api/search/<keyword>")
def api_search(keyword):
    raw = fetch_animasu(f"{ANIMASU_PREFIX}/search/{keyword}")
    data = None
    if raw and raw.get("status") == "success":
        data = {"animes": _norm_list(raw.get("animes", []))}
    return jsonify(data)

from flask import Flask, render_template, request, jsonify, session, redirect, send_from_directory
import requests
import json
import os
from upstash_redis import Redis

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "animeku-secret-2026")

BYPASS_PATHS = ['/static/', '/api/']

@app.before_request
def bypass_static():
    for bp in BYPASS_PATHS:
        if request.path.startswith(bp):
            return None
API_BASE = "https://www.sankavollerei.com"

# ── Endpoint Sources ───────────────────────────────────────────────────────────
SOURCES = {
    "samehadaku": {
        "label": "Dayynime-v1",
        "prefix": "/anime/samehadaku",
        "type": "samehadaku",
    },
    "animasu": {
        "label": "Dayynime-v2",
        "prefix": "/anime/animasu",
        "type": "animasu",
    },
    "otakudesu": {
        "label": "Dayynime-v3",
        "prefix": "/anime",
        "type": "otakudesu",
    },
}
DEFAULT_SOURCE = "samehadaku"

def get_active_source():
    """Baca source aktif: cookie user → Redis cache → site_config Supabase → default."""
    # 1. Cookie browser user (pilihan per-user, 30 hari)
    try:
        user_src = request.cookies.get("active_source")
        if user_src and user_src in SOURCES:
            return user_src
    except Exception:
        pass
    # 2. Redis cache (cepat, di-set oleh admin saat switch)
    try:
        val = redis.get("animeku:active_source")
        if val and val in SOURCES:
            return val
    except Exception:
        pass
    # 3. Supabase site_config (source of truth untuk admin)
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/site_config",
            headers=supabase_service_headers(),
            params={"key": "eq.active_source", "select": "value"},
            timeout=3
        )
        if r.ok and r.json():
            val = r.json()[0].get("value")
            if isinstance(val, str) and val in SOURCES:
                # Cache ke Redis supaya request berikutnya tidak perlu ke Supabase
                try:
                    redis.set("animeku:active_source", val, ex=3600)
                except Exception:
                    pass
                return val
    except Exception:
        pass
    return DEFAULT_SOURCE

def src_prefix():
    return SOURCES[get_active_source()]["prefix"]

def src_type():
    return SOURCES[get_active_source()]["type"]

# Inject active_source ke semua template otomatis
@app.context_processor
def inject_active_source():
    src = get_active_source()
    return {
        "active_source": src,
        "active_source_label": SOURCES[src]["label"],
        "all_sources": SOURCES,
    }

SUPABASE_URL = "https://mafnnqttvkdgqqxczqyt.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1hZm5ucXR0dmtkZ3FxeGN6cXl0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzE4NzQyMDEsImV4cCI6MjA4NzQ1MDIwMX0.YRh1oWVKnn4tyQNRbcPhlSyvr7V_1LseWN7VjcImb-Y"
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

def supabase_headers(access_token=None):
    h = {"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"}
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    else:
        h["Authorization"] = f"Bearer {SUPABASE_ANON_KEY}"
    return h

def supabase_service_headers():
    """Headers pakai service role key - bypass RLS, hanya untuk admin."""
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }

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
    source   = get_active_source()          # "samehadaku" atau "animasu"
    key      = f"animeku:{source}:" + path + str(sorted(params.items()) if params else "")
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

# ── Animasu Normalizers ────────────────────────────────────────────────────────
# Animasu response memakai field berbeda dari samehadaku

def animasu_norm_anime(a):
    """Normalize satu item anime dari animasu ke format internal."""
    if not a:
        return a
    return {
        "slug":          a.get("slug", ""),
        "title":         a.get("title", ""),
        "poster":        a.get("poster", ""),
        "episode":       str(a.get("episode", "")),
        "status_or_day": a.get("status_or_day", ""),
        "type":          a.get("type", ""),
        "score":         a.get("score", ""),
        "rank":          a.get("rank", None),
    }

def animasu_norm_list(animes):
    return [animasu_norm_anime(a) for a in (animes or [])]

def animasu_norm_home(raw):
    """Parse animasu /home response."""
    if not raw or raw.get("status") != "success":
        return None
    return {
        "ongoing": animasu_norm_list(raw.get("ongoing", [])),
        "recent":  animasu_norm_list(raw.get("recent", [])),
    }

def animasu_norm_paginated(raw, page):
    """Parse animasu paginated list (ongoing/completed/latest/genre)."""
    if not raw or raw.get("status") != "success":
        return None
    animes = animasu_norm_list(raw.get("animes", []))
    pag = raw.get("pagination", {})
    return {
        "animes": animes,
        "pagination": {
            "hasNext":     pag.get("hasNext", False),
            "hasPrev":     pag.get("hasPrev", False),
            "currentPage": pag.get("currentPage", page),
        }
    }

def animasu_norm_genres(raw):
    """Parse animasu genres list."""
    if not raw or raw.get("status") != "success":
        return None
    return [{"name": g.get("name", ""), "slug": g.get("slug", "")} for g in raw.get("genres", [])]

def animasu_norm_schedule(raw):
    """Parse animasu schedule (sama formatnya: dict hari -> list anime)."""
    if not raw or raw.get("status") != "success" or not raw.get("schedule"):
        return None
    sched_dict = {}
    for day_key, items in raw["schedule"].items():
        normalized = []
        for a in items:
            normalized.append({
                "slug":          a.get("slug", ""),
                "title":         a.get("title", ""),
                "poster":        a.get("poster", ""),
                "episode":       str(a.get("episode", "Sudah Rilis!")),
                "status_or_day": str(a.get("status_or_day", "")),
                "time":          str(a.get("status_or_day", "")),
                "type":          a.get("type", ""),
            })
        sched_dict[day_key] = normalized
    return {"schedule": sched_dict}

def animasu_norm_detail(raw, slug):
    """Parse animasu /detail/:slug response."""
    if not raw or raw.get("status") != "success" or not raw.get("detail"):
        return None
    d = raw["detail"]
    genres = [{"name": g.get("name", ""), "slug": g.get("slug", "")} for g in d.get("genres", [])]
    eps = [{"name": e.get("name", ""), "slug": e.get("slug", "")} for e in d.get("episodes", [])]
    return {
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
                "score":         str(d.get("rating", "")),
                "total_episode": "",
                "duration":      d.get("duration", ""),
                "released":      d.get("aired", ""),
                "studio":        d.get("studio", ""),
                "season":        d.get("season", ""),
            }
        }
    }

def animasu_norm_episode(raw):
    """Parse animasu /episode/:slug response."""
    if not raw or raw.get("status") != "success":
        return None
    streams = [{"name": s.get("name", ""), "serverId": "", "url": s.get("url", "")} for s in raw.get("streams", [])]
    return {
        "title":    raw.get("title", ""),
        "anime_id": "",
        "streams":  streams,
        "downloads": raw.get("downloads", []),
    }

def animasu_norm_animelist(raw):
    """Parse animasu /animelist - tidak ada grouping huruf, buat flat."""
    if not raw or raw.get("status") != "success":
        return None
    animes = raw.get("animes", [])
    # Group by huruf pertama
    groups = {}
    for a in animes:
        letter = a.get("title", "#")[0].upper()
        if letter not in groups:
            groups[letter] = []
        groups[letter].append({"title": a.get("title", ""), "slug": a.get("slug", "")})
    anime_list = [{"letter": k, "animes": v} for k, v in sorted(groups.items())]
    return {"anime_list": anime_list}

def animasu_norm_search(raw):
    """Parse animasu search response."""
    if not raw or raw.get("status") != "success":
        return None
    return {"animes": animasu_norm_list(raw.get("animes", []))}

# ── Otakudesu Normalizers ─────────────────────────────────────────────────────
# Endpoint: GET /anime/home
# Response: { status, data: { ongoing: { animeList: [...] }, completed: { animeList: [...] } } }
# Tiap item: { animeId, title, poster, episodes, releaseDay, latestReleaseDate, score, type }

def otakudesu_norm_anime(a):
    if not a:
        return a
    return {
        "slug":          a.get("animeId", ""),
        "title":         a.get("title", ""),
        "poster":        a.get("poster", ""),
        "episode":       str(a.get("episodes", "")),
        "status_or_day": a.get("releaseDay", a.get("status", "")),
        "type":          a.get("type", ""),
        "score":         str(a.get("score", "")),
        "rank":          a.get("rank", None),
    }

def otakudesu_norm_list(animes):
    return [otakudesu_norm_anime(a) for a in (animes or [])]

def otakudesu_norm_home(raw):
    """GET /anime/home → data.ongoing.animeList & data.completed.animeList"""
    if not raw or raw.get("status") != "success" or not raw.get("data"):
        return None
    d = raw["data"]
    return {
        "ongoing": otakudesu_norm_list(d.get("ongoing",   {}).get("animeList", [])),
        "recent":  otakudesu_norm_list(d.get("completed", {}).get("animeList", [])),
    }

def otakudesu_norm_paginated(raw, page):
    if not raw or raw.get("status") != "success":
        return None
    anime_list = raw.get("data", {}).get("animeList", [])
    pag        = raw.get("pagination") or {}
    pag_norm   = {
        "hasNext":     pag.get("hasNextPage", False),
        "hasPrev":     pag.get("hasPrevPage", False),
        "currentPage": pag.get("currentPage", page),
    } if pag else None
    return {"animes": otakudesu_norm_list(anime_list), "pagination": pag_norm}

def otakudesu_norm_genres(raw):
    """GET /anime/genre → data.genreList: [{title, genreId}]"""
    if not raw or raw.get("status") != "success" or not raw.get("data"):
        return None
    return [
        {"name": g.get("title", ""), "slug": g.get("genreId", "")}
        for g in raw["data"].get("genreList", [])
    ]

def otakudesu_norm_schedule(raw):
    """GET /anime/schedule → data: [{day, anime_list:[{title,slug,poster}]}]"""
    if not raw or raw.get("status") != "success" or not raw.get("data"):
        return None
    data = raw["data"]
    days = data if isinstance(data, list) else data.get("days", [])
    if not days:
        return None
    sched_dict = {}
    for day_obj in days:
        day_name = DAY_ID.get(day_obj.get("day", ""), day_obj.get("day", ""))
        # API pakai "anime_list" dan "slug" (bukan animeList/animeId)
        anime_list = day_obj.get("anime_list", day_obj.get("animeList", []))
        items = [{
            "slug":          a.get("slug", a.get("animeId", "")),
            "title":         a.get("title", ""),
            "poster":        a.get("poster", ""),
            "episode":       str(a.get("episodes", "")),
            "time":          a.get("time", ""),
            "status_or_day": a.get("time", ""),
            "type":          a.get("type", ""),
        } for a in anime_list]
        sched_dict[day_name] = items
    return {"schedule": sched_dict}

def otakudesu_norm_detail(raw, slug):
    """GET /anime/anime/:slug"""
    if not raw or raw.get("status") != "success" or not raw.get("data"):
        return None
    d = raw["data"]
    eps    = [{"name": str(e.get("title", "")), "slug": e.get("episodeId", "")}
              for e in d.get("episodeList", [])]
    genres = [{"name": g.get("title", ""), "slug": g.get("genreId", "")}
              for g in d.get("genreList", [])]
    score  = d["score"].get("value", "") if isinstance(d.get("score"), dict) else str(d.get("score", ""))
    syn    = d.get("synopsis", "")
    if isinstance(syn, dict):
        syn = " ".join(syn.get("paragraphs", []))
    return {
        "detail": {
            "title":    d.get("title", ""),
            "poster":   d.get("poster", ""),
            "synopsis": syn,
            "trailer":  d.get("trailer", ""),
            "genres":   genres,
            "episodes": eps,
            "info": {
                "japanese":      d.get("japanese", ""),
                "status":        d.get("status", ""),
                "type":          d.get("type", ""),
                "score":         score,
                "total_episode": str(d.get("episodes", "")),
                "duration":      d.get("duration", ""),
                "released":      d.get("aired", ""),
                "studio":        d.get("studios", ""),
                "season":        d.get("season", ""),
            }
        }
    }

def otakudesu_norm_episode(raw):
    """GET /anime/episode/:slug"""
    if not raw or raw.get("status") != "success" or not raw.get("data"):
        return None
    d       = raw["data"]
    streams = []
    for quality in d.get("server", {}).get("qualities", []):
        q_title = quality.get("title", "")
        for srv in quality.get("serverList", []):
            srv_name = srv.get("title", "")
            label    = f"{srv_name} {q_title}".strip() if q_title and q_title.lower() not in srv_name.lower() else srv_name
            streams.append({"name": label, "serverId": srv.get("serverId", ""), "url": ""})
    default_url = d.get("defaultStreamingUrl", "")
    if default_url:
        streams.insert(0, {"name": "Default Auto", "serverId": "", "url": default_url})
    return {
        "title":     d.get("title", ""),
        "anime_id":  d.get("animeId", ""),
        "streams":   streams,
        "downloads": [],
    }

def otakudesu_norm_animelist(raw):
    """GET /anime/list → data.list[{startWith, animeList}]"""
    if not raw or raw.get("status") != "success" or not raw.get("data"):
        return None
    anime_list = []
    for group in raw["data"].get("list", []):
        animes = [{"title": a.get("title", ""), "slug": a.get("animeId", "")}
                  for a in group.get("animeList", [])]
        if animes:
            anime_list.append({"letter": group.get("startWith", "#"), "animes": animes})
    return {"anime_list": anime_list}

def otakudesu_norm_search(raw):
    """GET /anime/search/:keyword → data.animeList"""
    if not raw or raw.get("status") != "success" or not raw.get("data"):
        return None
    return {"animes": otakudesu_norm_list(raw["data"].get("animeList", []))}

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
    source = get_active_source()
    pfx    = SOURCES[source]["prefix"]

    if source == "animasu":
        raw      = fetch(f"{pfx}/home")
        pop_raw  = fetch(f"{pfx}/popular")
        schedule = fetch(f"{pfx}/schedule")
        data     = animasu_norm_home(raw)
        # populer dari endpoint /popular animasu (response: {animes: [...]})
        pop_norm = {"animes": animasu_norm_list(pop_raw.get("animes", []))} if pop_raw and pop_raw.get("animes") else None
        sched    = animasu_norm_schedule(schedule)
    elif source == "otakudesu":
        raw      = fetch(f"{pfx}/home")
        schedule = fetch(f"{pfx}/schedule")
        data     = otakudesu_norm_home(raw)
        # populer dari ongoing (otakudesu tidak punya endpoint popular terpisah)
        pop_norm = {"animes": data["ongoing"][:10]} if data and data.get("ongoing") else None
        sched    = otakudesu_norm_schedule(schedule)
    else:
        raw      = fetch(f"{pfx}/home")
        popular  = fetch(f"{pfx}/popular")
        schedule = fetch(f"{pfx}/schedule")
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
        sched = norm_schedule(schedule)

    return render_template("index.html", data=data, popular=pop_norm,
                           schedule=sched)


@app.route("/anime/<slug>")
def detail(slug):
    source = get_active_source()
    pfx    = SOURCES[source]["prefix"]

    if source == "animasu":
        raw  = fetch(f"{pfx}/detail/{slug}")
        data = animasu_norm_detail(raw, slug)
    elif source == "otakudesu":
        raw  = fetch(f"{pfx}/anime/{slug}")
        data = otakudesu_norm_detail(raw, slug)
    else:
        raw = fetch(f"{pfx}/anime/{slug}")
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
    source     = get_active_source()
    pfx        = SOURCES[source]["prefix"]
    anime_slug = request.args.get("anime", "")

    data = None
    if source == "animasu":
        raw  = fetch(f"{pfx}/episode/{slug}")
        data = animasu_norm_episode(raw)
        anime_data = None
        if anime_slug:
            araw = fetch(f"{pfx}/detail/{anime_slug}")
            adat = animasu_norm_detail(araw, anime_slug)
            if adat:
                anime_data = adat
    elif source == "otakudesu":
        raw  = fetch(f"{pfx}/episode/{slug}")
        data = otakudesu_norm_episode(raw)
        if not anime_slug and data and data.get("anime_id"):
            anime_slug = data["anime_id"]
        anime_raw  = fetch(f"{pfx}/anime/{anime_slug}") if anime_slug else None
        anime_data = None
        if anime_raw:
            adat = otakudesu_norm_detail(anime_raw, anime_slug)
            if adat:
                anime_data = {"detail": {
                    "title":    adat["detail"].get("title", ""),
                    "poster":   adat["detail"].get("poster", ""),
                    "genres":   adat["detail"].get("genres", []),
                    "episodes": adat["detail"].get("episodes", []),
                }}
    else:
        raw        = fetch(f"{pfx}/episode/{slug}")
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

        anime_raw  = fetch(f"{pfx}/anime/{anime_slug}") if anime_slug else None
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

    return render_template("episode.html", data=data, slug=slug,
                           anime_slug=anime_slug, anime_data=anime_data)


@app.route("/api/server/<server_id>")
def api_server(server_id):
    source = get_active_source()
    if source == "samehadaku":
        raw = fetch(f"/anime/samehadaku/server/{server_id}")
        if raw and raw.get("data"):
            return jsonify({"url": raw["data"].get("url", "")})
    elif source == "otakudesu":
        raw = fetch(f"/anime/server/{server_id}")
        if raw and raw.get("data"):
            return jsonify({"url": raw["data"].get("url", "")})
    return jsonify({"url": ""}), 404


@app.route("/genre/<slug>")
def genre(slug):
    page   = request.args.get("page", 1)
    source = get_active_source()
    pfx    = SOURCES[source]["prefix"]

    if source == "animasu":
        raw        = fetch(f"{pfx}/genre/{slug}", {"page": page})
        genres_raw = fetch(f"{pfx}/genres")
        data       = animasu_norm_paginated(raw, int(page)) if raw else None
        genres     = {"genres": animasu_norm_genres(genres_raw)} if genres_raw else None
    elif source == "otakudesu":
        raw        = fetch(f"{pfx}/genre/{slug}", {"page": page})
        genres_raw = fetch(f"{pfx}/genre")
        data = None
        if raw and raw.get("data"):
            pag      = raw.get("pagination") or {}
            pag_norm = {"hasNext": pag.get("hasNextPage", False), "hasPrev": pag.get("hasPrevPage", False), "currentPage": pag.get("currentPage", 1)} if pag else None
            data     = {"animes": otakudesu_norm_list(raw["data"].get("animeList", [])), "pagination": pag_norm}
        genres = {"genres": otakudesu_norm_genres(genres_raw)} if genres_raw else None
    else:
        raw        = fetch(f"{pfx}/genres/{slug}", {"page": page})
        genres_raw = fetch(f"{pfx}/genres")
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
    source = get_active_source()
    pfx    = SOURCES[source]["prefix"]
    # otakudesu pakai /anime/genre (tanpa s)
    if source == "otakudesu":
        raw = fetch(f"{pfx}/genre")
    else:
        raw = fetch(f"{pfx}/genres")
    data   = None
    if source == "animasu":
        if raw:
            data = {"genres": animasu_norm_genres(raw)}
    elif source == "otakudesu":
        if raw:
            data = {"genres": otakudesu_norm_genres(raw)}
    else:
        if raw and raw.get("data"):
            data = {"genres": norm_genres(raw["data"].get("genreList", []))}
    return render_template("genres.html", data=data)


@app.route("/jadwal")
def schedule():
    source = get_active_source()
    pfx    = SOURCES[source]["prefix"]
    raw    = fetch(f"{pfx}/schedule")
    if source == "animasu":
        sched = animasu_norm_schedule(raw)
    elif source == "otakudesu":
        sched = otakudesu_norm_schedule(raw)
    else:
        sched = norm_schedule(raw)
    return render_template("schedule.html", data=sched)


@app.route("/movies")
def movies():
    page   = request.args.get("page", 1)
    source = get_active_source()
    pfx    = SOURCES[source]["prefix"]
    if source == "animasu":
        data = animasu_norm_paginated(fetch(f"{pfx}/movies", {"page": page}), int(page))
    elif source == "otakudesu":
        # otakudesu tidak punya endpoint /movies, fallback ke complete-anime
        data = otakudesu_norm_paginated(fetch(f"{pfx}/complete-anime", {"page": page}), int(page))
    else:
        data = _norm_paginated(fetch(f"{pfx}/movies", {"page": page}), int(page))
    return render_template("list.html", data=data, title="Movie", page=int(page), base_url="/movies")


@app.route("/ongoing")
def ongoing():
    page   = request.args.get("page", 1)
    source = get_active_source()
    pfx    = SOURCES[source]["prefix"]
    if source == "animasu":
        data = animasu_norm_paginated(fetch(f"{pfx}/ongoing", {"page": page}), int(page))
    elif source == "otakudesu":
        data = otakudesu_norm_paginated(fetch(f"{pfx}/ongoing-anime", {"page": page}), int(page))
    else:
        data = _norm_paginated(fetch(f"{pfx}/ongoing", {"page": page}), int(page))
    return render_template("list.html", data=data, title="Ongoing", page=int(page), base_url="/ongoing")


@app.route("/completed")
def completed():
    page   = request.args.get("page", 1)
    source = get_active_source()
    pfx    = SOURCES[source]["prefix"]
    if source == "animasu":
        data = animasu_norm_paginated(fetch(f"{pfx}/completed", {"page": page}), int(page))
    elif source == "otakudesu":
        data = otakudesu_norm_paginated(fetch(f"{pfx}/complete-anime", {"page": page}), int(page))
    else:
        data = _norm_paginated(fetch(f"{pfx}/completed", {"page": page}), int(page))
    return render_template("list.html", data=data, title="Completed", page=int(page), base_url="/completed")


@app.route("/popular")
def popular():
    page   = request.args.get("page", 1)
    source = get_active_source()
    pfx    = SOURCES[source]["prefix"]
    if source == "animasu":
        data = animasu_norm_paginated(fetch(f"{pfx}/latest", {"page": page}), int(page))
    elif source == "otakudesu":
        # otakudesu tidak punya endpoint /popular, fallback ke ongoing-anime
        data = otakudesu_norm_paginated(fetch(f"{pfx}/ongoing-anime", {"page": page}), int(page))
    else:
        data = _norm_paginated(fetch(f"{pfx}/popular", {"page": page}), int(page))
    return render_template("list.html", data=data, title="Populer", page=int(page), base_url="/popular")


@app.route("/animelist")
def animelist():
    source = get_active_source()
    pfx    = SOURCES[source]["prefix"]
    if source == "animasu":
        raw  = fetch(f"{pfx}/animelist")
        data = animasu_norm_animelist(raw)
    elif source == "otakudesu":
        # otakudesu animelist ada di /anime/unlimited
        raw  = fetch(f"{pfx}/unlimited")
        data = otakudesu_norm_animelist(raw)
    else:
        raw  = fetch(f"{pfx}/list")
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
    q      = request.args.get("q", "")
    source = get_active_source()
    pfx    = SOURCES[source]["prefix"]
    data   = None
    if q:
        if source == "animasu":
            raw  = fetch(f"{pfx}/search/{q}")
            data = animasu_norm_search(raw)
        elif source == "otakudesu":
            raw  = fetch(f"{pfx}/search/{q}")
            data = otakudesu_norm_search(raw)
        else:
            raw  = fetch(f"{pfx}/search", {"q": q})
            if raw and raw.get("data"):
                data = {"animes": norm_list(raw["data"].get("animeList", []))}
    return render_template("search.html", data=data, query=q)


@app.route("/koleksi")
def koleksi():
    return render_template("koleksi.html")


@app.route("/rekomendasi")
def rekomendasi():
    return render_template("rekomendasi.html")

@app.route("/rekomendasi/anime/<int:mal_id>")
def rekomendasi_detail(mal_id):
    return render_template("rekomendasi_detail.html", mal_id=mal_id)

# ── MyList Anime ───────────────────────────────────────────────────────────────

@app.route("/mylist")
def mylist():
    return render_template("mylist.html")

@app.route("/mylist/jelajahi")
def mylist_explore():
    return render_template("mylist_explore.html")

@app.route("/mylist/list/<list_id>")
def mylist_view(list_id):
    return render_template("mylist_view.html", list_id=list_id)

@app.route("/mylist/drafts")
def mylist_drafts():
    return render_template("mylist_drafts.html")

# ── MyList API ─────────────────────────────────────────────────────────────────

@app.route("/api/mylist/save", methods=["POST"])
def mylist_save():
    body = request.json
    username   = (body.get("username") or "").strip()
    anime_list = body.get("anime_list", [])
    if not username:
        return jsonify({"error": "Username wajib diisi"}), 400
    if not anime_list:
        return jsonify({"error": "Pilih minimal 1 anime"}), 400
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/anime_lists",
        headers={**supabase_headers(), "Prefer": "return=representation"},
        json={"username": username, "anime_list": anime_list},
        timeout=10
    )
    if r.status_code in (200, 201):
        return jsonify({"success": True, "id": r.json()[0]["id"]})
    return jsonify({"error": r.text}), 500

@app.route("/api/mylist/lists")
def mylist_get_lists():
    page   = int(request.args.get("page", 1))
    limit  = 12
    offset = (page - 1) * limit
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/anime_lists?select=*&order=created_at.desc&limit={limit}&offset={offset}",
        headers=supabase_headers(), timeout=10
    )
    return jsonify(r.json())

@app.route("/api/mylist/list/<list_id>")
def mylist_get_list(list_id):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/anime_lists?id=eq.{list_id}&select=*",
        headers=supabase_headers(), timeout=10
    )
    data = r.json()
    if data:
        return jsonify(data[0])
    return jsonify({"error": "Not found"}), 404

@app.route("/api/mylist/draft/create", methods=["POST"])
def mylist_draft_create():
    import random, string
    body = request.json
    username   = (body.get("username") or "").strip()
    anime_list = body.get("anime_list", [])
    title      = (body.get("title") or "Draft baru").strip()
    if not username:
        return jsonify({"error": "Username wajib"}), 400
    pin = ""
    for _ in range(5):
        pin = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        chk = requests.get(
            f"{SUPABASE_URL}/rest/v1/anime_drafts?pin=eq.{pin}&select=id",
            headers=supabase_headers(), timeout=10
        )
        if not chk.json():
            break
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/anime_drafts",
        headers={**supabase_headers(), "Prefer": "return=representation"},
        json={"username": username, "anime_list": anime_list, "title": title, "pin": pin},
        timeout=10
    )
    if r.status_code in (200, 201):
        return jsonify({"success": True, "id": r.json()[0]["id"], "pin": pin})
    return jsonify({"error": r.text}), 500

@app.route("/api/mylist/draft/list")
def mylist_draft_list():
    pin = request.args.get("pin", "").strip().upper()
    if not pin:
        return jsonify({"error": "PIN wajib"}), 400
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/anime_drafts?pin=eq.{pin}&select=*&order=updated_at.desc",
        headers=supabase_headers(), timeout=10
    )
    data = r.json()
    return jsonify(data if isinstance(data, list) else [])

@app.route("/api/mylist/draft/update/<draft_id>", methods=["POST"])
def mylist_draft_update(draft_id):
    body = request.json
    pin  = (body.get("pin") or "").strip().upper()
    if not pin:
        return jsonify({"error": "PIN wajib"}), 400
    chk = requests.get(
        f"{SUPABASE_URL}/rest/v1/anime_drafts?id=eq.{draft_id}&pin=eq.{pin}&select=id",
        headers=supabase_headers(), timeout=10
    )
    if not chk.json():
        return jsonify({"error": "PIN salah"}), 403
    update_data = {k: body[k] for k in ["anime_list", "username", "title"] if k in body}
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/anime_drafts?id=eq.{draft_id}",
        headers=supabase_headers(), json=update_data, timeout=10
    )
    return jsonify({"success": True}) if r.status_code in (200, 201, 204) else (jsonify({"error": r.text}), 500)

@app.route("/api/mylist/draft/delete/<draft_id>", methods=["DELETE"])
def mylist_draft_delete(draft_id):
    pin = request.args.get("pin", "").strip().upper()
    if not pin:
        return jsonify({"error": "PIN wajib"}), 400
    chk = requests.get(
        f"{SUPABASE_URL}/rest/v1/anime_drafts?id=eq.{draft_id}&pin=eq.{pin}&select=id",
        headers=supabase_headers(), timeout=10
    )
    if not chk.json():
        return jsonify({"error": "PIN salah"}), 403
    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/anime_drafts?id=eq.{draft_id}",
        headers=supabase_headers(), timeout=10
    )
    return jsonify({"success": True}) if r.status_code in (200, 204) else (jsonify({"error": r.text}), 500)

@app.route("/api/mylist/draft/publish/<draft_id>", methods=["POST"])
def mylist_draft_publish(draft_id):
    body = request.json
    pin  = (body.get("pin") or "").strip().upper()
    if not pin:
        return jsonify({"error": "PIN wajib"}), 400
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/anime_drafts?id=eq.{draft_id}&pin=eq.{pin}&select=*",
        headers=supabase_headers(), timeout=10
    )
    drafts = r.json()
    if not drafts:
        return jsonify({"error": "PIN salah"}), 403
    d  = drafts[0]
    r2 = requests.post(
        f"{SUPABASE_URL}/rest/v1/anime_lists",
        headers={**supabase_headers(), "Prefer": "return=representation"},
        json={"username": d["username"], "anime_list": d["anime_list"]},
        timeout=10
    )
    if r2.status_code in (200, 201):
        return jsonify({"success": True, "id": r2.json()[0]["id"]})
    return jsonify({"error": r2.text}), 500


@app.route("/chat")
def chat():
    return render_template("chat.html")


@app.route("/admin")
def admin():
    return render_template("admin.html")

@app.route("/api/admin/cache/flush", methods=["POST"])
def admin_flush_cache():
    """Hapus semua cache Redis animeku. Admin only."""
    auth_header = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not _is_admin(access_token):
        return jsonify({"error": "Forbidden"}), 403
    try:
        keys = redis.keys("animeku:*")
        if keys:
            for k in keys:
                redis.delete(k)
        return jsonify({"ok": True, "deleted": len(keys) if keys else 0})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/premium")
def premium():
    return render_template("premium.html")

@app.route("/profile")
def profile():
    return render_template("profile.html")


# ── API Proxy ──────────────────────────────────────────────────────────────────

@app.route("/api/search/<keyword>")
def api_search(keyword):
    source = get_active_source()
    pfx    = SOURCES[source]["prefix"]
    data   = None
    if source == "animasu":
        raw  = fetch(f"{pfx}/search/{keyword}")
        data = animasu_norm_search(raw)
    elif source == "otakudesu":
        raw  = fetch(f"{pfx}/search/{keyword}")
        data = otakudesu_norm_search(raw)
    else:
        raw  = fetch(f"{pfx}/search", {"q": keyword})
        if raw and raw.get("data"):
            data = {"animes": norm_list(raw["data"].get("animeList", []))}
    return jsonify(data)


# ── Endpoint Switcher API ──────────────────────────────────────────────────────

@app.route("/api/source", methods=["GET"])
def api_get_source():
    """Ambil source yang sedang aktif (public)."""
    active = get_active_source()
    return jsonify({
        "active": active,
        "label":  SOURCES[active]["label"],
        "sources": [{"key": k, "label": v["label"]} for k, v in SOURCES.items()]
    })

@app.route("/api/source/switch", methods=["POST"])
def api_switch_source():
    """Ganti source aktif untuk user ini (cookie, works di Vercel serverless)."""
    data   = request.get_json()
    source = data.get("source", "")
    if source not in SOURCES:
        return jsonify({"error": f"Source tidak valid. Pilih: {list(SOURCES.keys())}"}), 400

    # Simpan ke cookie browser user (30 hari) — tidak butuh session/Redis
    resp = jsonify({"ok": True, "active": source, "label": SOURCES[source]["label"]})
    resp.set_cookie("active_source", source, max_age=30*24*3600, samesite="Lax")

    # Kalau admin → juga update Redis global sebagai default semua user
    user = session.get("user")
    ADMIN_IDS = ["1a2c72de-e85c-4430-8e27-8c1c1fd0b8f1"]
    try:
        r_cfg = requests.get(
            f"{SUPABASE_URL}/rest/v1/site_config",
            headers=supabase_service_headers(),
            params={"key": "eq.admin_ids", "select": "value"}
        )
        if r_cfg.ok and r_cfg.json():
            val = r_cfg.json()[0].get("value")
            if val:
                ADMIN_IDS = val if isinstance(val, list) else val.get("ids", ADMIN_IDS)
    except Exception:
        pass

    if user and user.get("id") in ADMIN_IDS:
        # Admin: update Supabase site_config sebagai default global semua user
        try:
            requests.post(
                f"{SUPABASE_URL}/rest/v1/site_config",
                headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates"},
                json={"key": "active_source", "value": source},
                timeout=5
            )
        except Exception:
            pass
        # Update Redis cache juga
        try:
            redis.set("animeku:active_source", source, ex=86400 * 365)
        except Exception:
            pass

    return resp


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


# ── Premium ────────────────────────────────────────────────────────────────────

FREE_EPISODE_COUNT = 2  # Episode 1 & 2 gratis

@app.route("/api/premium/status")
def premium_status():
    """Cek apakah user yang sedang login punya akses premium."""
    from datetime import datetime, timezone

    # Ambil user_id: coba dari Authorization header dulu, fallback ke session
    user_id = None
    auth_header = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip() if auth_header else ""

    if access_token:
        # Verifikasi token ke Supabase untuk dapat user_id
        r_user = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers=supabase_headers(access_token)
        )
        if r_user.ok:
            user_id = r_user.json().get("id")

    # Fallback ke session (untuk login server-side)
    if not user_id:
        user = session.get("user")
        if not user:
            return jsonify({"premium": False, "reason": "not_logged_in"})
        user_id = user.get("id")

    if not user_id:
        return jsonify({"premium": False, "reason": "not_logged_in"})

    # Pakai service key agar tidak kena RLS
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/user_premium",
        headers=supabase_service_headers(),
        params={"user_id": f"eq.{user_id}", "select": "is_active,expires_at"}
    )
    if r.ok and r.json():
        row = r.json()[0]
        expires_at = row.get("expires_at")
        if row.get("is_active"):
            if not expires_at:
                return jsonify({"premium": True})
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp > datetime.now(timezone.utc):
                    return jsonify({"premium": True, "expires_at": expires_at})
                else:
                    return jsonify({"premium": False, "reason": "expired"})
            except Exception:
                return jsonify({"premium": True})
    return jsonify({"premium": False, "reason": "no_subscription"})

@app.route("/api/premium/grant", methods=["POST"])
def premium_grant():
    """Admin grant/revoke premium untuk user tertentu."""
    user = session.get("user")
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    target_user_id = data.get("user_id")
    action = data.get("action", "grant")  # grant / revoke
    expires_at = data.get("expires_at")  # optional ISO string

    # Cek admin
    ADMIN_IDS = [
        "1a2c72de-e85c-4430-8e27-8c1c1fd0b8f1",
    ]
    r_user = requests.get(f"{SUPABASE_URL}/auth/v1/user",
                          headers=supabase_headers(session.get("access_token")))
    if not r_user.ok:
        return jsonify({"error": "Unauthorized"}), 401

    # Cek admin dari site_config
    cfg = requests.get(f"{SUPABASE_URL}/rest/v1/site_config",
                       headers=supabase_headers(),
                       params={"key": "eq.admin_ids", "select": "value"})
    if cfg.ok and cfg.json():
        try:
            val = cfg.json()[0]["value"]
            ADMIN_IDS = val if isinstance(val, list) else val.get("ids", ADMIN_IDS)
        except Exception:
            pass
    if user.get("id") not in ADMIN_IDS:
        return jsonify({"error": "Forbidden"}), 403

    if action == "grant":
        payload = {"user_id": target_user_id, "is_active": True}
        if expires_at:
            payload["expires_at"] = expires_at
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/user_premium",
            headers={**supabase_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
            json=payload
        )
    else:  # revoke
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/user_premium",
            headers={**supabase_headers(), "Prefer": "return=representation"},
            params={"user_id": f"eq.{target_user_id}"},
            json={"is_active": False}
        )
    return jsonify({"ok": r.ok, "detail": r.text})

@app.route("/api/premium/list")
def premium_list():
    """Daftar semua user premium (admin only)."""
    user = session.get("user")
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/user_premium",
        headers=supabase_headers(),
        params={"select": "*", "order": "created_at.desc"}
    )
    return jsonify(r.json() if r.ok else [])


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

@app.route("/api/sociabuzz/webhook", methods=["POST"])
def sociabuzz_webhook():
    # Validasi webhook secret dari Sociabuzz (opsional tapi disarankan)
    webhook_secret = os.environ.get("SOCIABUZZ_WEBHOOK_SECRET", "")
    if webhook_secret:
        req_secret = request.headers.get("X-Webhook-Secret", "") or request.args.get("secret", "")
        if req_secret != webhook_secret:
            print(f"[Sociabuzz] ❌ Invalid webhook secret")
            return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    donor_name   = data.get("donatur_name", data.get("name", "Anonymous"))
    amount       = int(data.get("amount", 0))
    message      = data.get("message", "")
    supporter_id = str(data.get("order_id", data.get("id", "")))

    print(f"[Sociabuzz] Donasi dari {donor_name}: Rp{amount} - {message} (ID: {supporter_id})")

    # Simpan ke tabel donations di Supabase
    try:
        payload = {
            "donor_name":   donor_name,
            "amount":       amount,
            "message":      message,
            "supporter_id": supporter_id,
        }
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/donations",
            headers={**supabase_headers(), "Prefer": "return=representation"},
            json=payload,
        )
        if not r.ok:
            print(f"[Sociabuzz] Supabase error: {r.text}")
        else:
            print(f"[Sociabuzz] Tersimpan ke Supabase")
    except Exception as e:
        print(f"[Sociabuzz] Exception: {e}")

    # ── AUTO GRANT PREMIUM ─────────────────────────────────────────
    # Syarat: amount >= 15000 DAN pesan mengandung "PREMIUM:<user_id>"
    PREMIUM_PRICE = 15000
    premium_granted = False
    premium_user_id = None

    if amount >= PREMIUM_PRICE:
        import re
        # Cari pola PREMIUM:<uuid> di pesan
        match = re.search(r'PREMIUM:([a-f0-9\-]{36})', message, re.IGNORECASE)
        if match:
            premium_user_id = match.group(1)
            try:
                from datetime import datetime, timezone, timedelta
                expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

                # Upsert ke tabel user_premium
                prem_payload = {
                    "user_id":    premium_user_id,
                    "is_active":  True,
                    "expires_at": expires_at,
                }
                rp = requests.post(
                    f"{SUPABASE_URL}/rest/v1/user_premium",
                    headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
                    json=prem_payload,
                )
                if rp.ok:
                    premium_granted = True
                    print(f"[Sociabuzz] ✅ Premium granted untuk user {premium_user_id} hingga {expires_at}")
                else:
                    print(f"[Sociabuzz] ❌ Gagal grant premium: {rp.text}")
            except Exception as e:
                print(f"[Sociabuzz] Exception grant premium: {e}")
        else:
            print(f"[Sociabuzz] ⚠️ Amount cukup tapi User ID tidak ditemukan di pesan: '{message}'")

    # ── Kirim notifikasi ke Live Chat ──────────────────────────────
    try:
        rp_fmt = f"Rp {amount:,}".replace(",", ".")
        if premium_granted:
            chat_content = f"✦ PREMIUM AKTIF! {donor_name} baru saja berlangganan Premium dengan donasi {rp_fmt}! 🎉"
        else:
            chat_content = f"🎉 SPECIAL THANKS kepada {donor_name} yang telah berdonasi {rp_fmt}!"
        if message and not premium_granted:
            chat_content += f' 💬 "{message}"'

        r2 = requests.post(
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
        if not r2.ok:
            print(f"[Sociabuzz] Chat error: {r2.text}")
        else:
            print(f"[Sociabuzz] Notifikasi chat terkirim ✅")
    except Exception as e:
        print(f"[Sociabuzz] Exception chat: {e}")

    return jsonify({
        "ok": True,
        "received": supporter_id,
        "premium_granted": premium_granted,
        "premium_user_id": premium_user_id,
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



# ── Admin: User Monitoring ─────────────────────────────────────────────────────

def _is_admin(access_token):
    """Cek apakah token milik admin."""
    if not access_token:
        return False
    ADMIN_IDS = ["c5ec3983-dbec-4e23-b6f6-2196fb4d5265"]
    # Cek dari site_config dulu
    try:
        cfg = requests.get(f"{SUPABASE_URL}/rest/v1/site_config",
                           headers=supabase_headers(),
                           params={"key": "eq.admin_ids", "select": "value"})
        if cfg.ok and cfg.json():
            val = cfg.json()[0]["value"]
            ADMIN_IDS = val if isinstance(val, list) else val.get("ids", ADMIN_IDS)
    except Exception:
        pass
    user_resp = requests.get(f"{SUPABASE_URL}/auth/v1/user", headers=supabase_headers(access_token))
    if not user_resp.ok:
        return False
    return user_resp.json().get("id") in ADMIN_IDS

@app.route("/api/admin/users")
def admin_users():
    """Daftar semua user Google + status premium. Admin only."""
    auth_header = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not _is_admin(access_token):
        return jsonify({"error": "Forbidden"}), 403

    if not SUPABASE_SERVICE_KEY:
        return jsonify({"error": "Service key tidak dikonfigurasi"}), 500

    # Ambil semua user dari Supabase Auth
    users_resp = requests.get(
        f"{SUPABASE_URL}/auth/v1/admin/users",
        headers=supabase_service_headers(),
        params={"per_page": 200}
    )
    if not users_resp.ok:
        return jsonify({"error": "Gagal ambil data user", "detail": users_resp.text}), 500

    users_data = users_resp.json().get("users", [])

    # Ambil semua data premium
    prem_resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/user_premium",
        headers=supabase_service_headers(),
        params={"select": "user_id,is_active,expires_at"}
    )
    prem_map = {}
    if prem_resp.ok:
        for row in prem_resp.json():
            prem_map[row["user_id"]] = row

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    result = []
    for u in users_data:
        uid = u.get("id")
        meta = u.get("user_metadata", {})
        prem = prem_map.get(uid)

        premium_status = "none"
        expires_at = None
        if prem and prem.get("is_active"):
            exp = prem.get("expires_at")
            if not exp:
                premium_status = "lifetime"
            else:
                try:
                    exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                    if exp_dt > now:
                        premium_status = "active"
                        expires_at = exp
                    else:
                        premium_status = "expired"
                        expires_at = exp
                except Exception:
                    premium_status = "active"
                    expires_at = exp

        result.append({
            "id":            uid,
            "name":          meta.get("full_name", u.get("email", "")),
            "email":         u.get("email", ""),
            "avatar":        meta.get("avatar_url", ""),
            "provider":      (u.get("app_metadata", {}).get("provider", "email")),
            "created_at":    u.get("created_at", ""),
            "last_sign_in":  u.get("last_sign_in_at", ""),
            "premium":       premium_status,
            "expires_at":    expires_at,
        })

    # Urutkan: premium aktif dulu, lalu by last_sign_in
    result.sort(key=lambda x: (x["premium"] != "active", x["last_sign_in"] or ""), reverse=False)
    return jsonify({"users": result, "total": len(result)})


@app.route("/api/admin/premium", methods=["POST"])
def admin_toggle_premium():
    """Grant/revoke premium langsung dari admin panel."""
    auth_header = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not _is_admin(access_token):
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json()
    target_id = data.get("user_id")
    action = data.get("action", "grant")  # grant / revoke

    from datetime import datetime, timezone, timedelta

    if action == "grant":
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        payload = {"user_id": target_id, "is_active": True, "expires_at": expires_at}
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/user_premium",
            headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
            json=payload
        )
    else:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/user_premium",
            headers={**supabase_service_headers(), "Prefer": "return=representation"},
            params={"user_id": f"eq.{target_id}"},
            json={"is_active": False}
        )

    return jsonify({"ok": r.ok, "detail": r.text})


@app.route("/api/admin/premium/extend", methods=["POST"])
def admin_extend_premium():
    """Extend atau set custom durasi premium."""
    auth_header = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not _is_admin(access_token):
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json()
    target_id = data.get("user_id")
    days = int(data.get("days", 30))  # default 30 hari

    from datetime import datetime, timezone, timedelta

    # Cek apakah sudah punya premium aktif — kalau iya, extend dari expires_at
    existing = requests.get(
        f"{SUPABASE_URL}/rest/v1/user_premium",
        headers=supabase_service_headers(),
        params={"user_id": f"eq.{target_id}", "select": "is_active,expires_at"}
    )
    now = datetime.now(timezone.utc)
    base = now
    if existing.ok and existing.json():
        row = existing.json()[0]
        if row.get("is_active") and row.get("expires_at"):
            try:
                exp = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
                if exp > now:
                    base = exp  # extend dari tanggal expired, bukan dari sekarang
            except Exception:
                pass

    expires_at = (base + timedelta(days=days)).isoformat()
    payload = {"user_id": target_id, "is_active": True, "expires_at": expires_at}
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/user_premium",
        headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
        json=payload
    )
    return jsonify({"ok": r.ok, "expires_at": expires_at, "days_added": days})


@app.route("/api/cron/premium-reminder", methods=["GET", "POST"])
def cron_premium_reminder():
    """
    Endpoint untuk cron-job.org — kirim notifikasi ke live chat
    untuk user yang premiumnya akan expired dalam 3 hari.
    Amankan dengan CRON_SECRET di env var.
    """
    # Validasi secret key
    cron_secret = os.environ.get("CRON_SECRET", "")
    req_secret = request.headers.get("X-Cron-Secret", "") or request.args.get("secret", "")
    if cron_secret and req_secret != cron_secret:
        return jsonify({"error": "Unauthorized"}), 401

    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    in_3_days = now + timedelta(days=3)

    # Ambil semua premium yang aktif dan expired dalam 3 hari
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/user_premium",
        headers=supabase_service_headers(),
        params={
            "is_active": "eq.true",
            "expires_at": f"lte.{in_3_days.isoformat()}",
            "select": "user_id,expires_at"
        }
    )
    if not r.ok:
        return jsonify({"error": "Gagal ambil data premium"}), 500

    rows = r.json()
    notified = []

    for row in rows:
        uid = row["user_id"]
        exp_str = row["expires_at"]
        try:
            exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
            if exp_dt < now:
                continue  # sudah expired, skip

            days_left = (exp_dt - now).days
            hours_left = int((exp_dt - now).total_seconds() / 3600)

            if days_left == 0:
                time_label = f"{hours_left} jam lagi"
            else:
                time_label = f"{days_left} hari lagi"

            # Cek apakah sudah pernah dinotif hari ini (simpan di notif_sent table)
            notif_key = f"{uid}:{exp_dt.strftime('%Y-%m-%d')}"
            check = requests.get(
                f"{SUPABASE_URL}/rest/v1/notif_sent",
                headers=supabase_service_headers(),
                params={"key": f"eq.{notif_key}", "select": "key"}
            )
            if check.ok and check.json():
                continue  # sudah dinotif, skip

            # Ambil info user
            user_resp = requests.get(
                f"{SUPABASE_URL}/auth/v1/admin/users/{uid}",
                headers=supabase_service_headers()
            )
            user_name = "Member"
            if user_resp.ok:
                meta = user_resp.json().get("user_metadata", {})
                user_name = meta.get("full_name", user_resp.json().get("email", "Member"))

            # Kirim notif ke live chat
            msg = f"⏰ Reminder: Premium @{user_name} akan berakhir dalam {time_label}! Perpanjang sebelum akses terkunci."
            requests.post(
                f"{SUPABASE_URL}/rest/v1/chat_messages",
                headers={**supabase_service_headers(), "Prefer": "return=representation"},
                json={
                    "room_id": "global",
                    "user_id": "system-reminder",
                    "user_name": "⏰ Premium Reminder",
                    "user_avatar": "",
                    "content": msg,
                    "is_donation": False,
                    "reactions": {},
                }
            )

            # Catat sudah dinotif
            requests.post(
                f"{SUPABASE_URL}/rest/v1/notif_sent",
                headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates"},
                json={"key": notif_key, "sent_at": now.isoformat()}
            )

            notified.append({"user_id": uid, "name": user_name, "expires_in": time_label})

        except Exception as e:
            print(f"[Cron] Error untuk {uid}: {e}")
            continue

    return jsonify({
        "ok": True,
        "checked": len(rows),
        "notified": len(notified),
        "users": notified
    })



@app.route("/debug2")
def debug2():
    import traceback
    results = {}
    # Test berbagai path alternatif untuk otakudesu
    endpoints = {
        "ongoing_v1":    "/anime/ongoing-anime",
        "ongoing_v2":    "/anime/ongoing",
        "completed_v1":  "/anime/complete-anime",
        "completed_v2":  "/anime/completed",
        "movies_v1":     "/anime/movies",
        "movies_v2":     "/anime/movie",
        "popular_v1":    "/anime/popular",
        "popular_v2":    "/anime/populer",
        "genres_v1":     "/anime/genres",
        "genres_v2":     "/anime/genre",
        "animelist_v1":  "/anime/list",
        "animelist_v2":  "/anime/anime-list",
        "search_v1":     "/anime/search?q=naruto",
    }
    for key, path in endpoints.items():
        try:
            raw = fetch(path)
            if raw is None:
                results[key] = "NULL"
            elif not isinstance(raw, dict):
                results[key] = f"type={type(raw).__name__}"
            else:
                data = raw.get("data")
                info = {
                    "status": raw.get("status"),
                    "top_keys": list(raw.keys()),
                }
                if isinstance(data, dict):
                    info["data_keys"] = list(data.keys())
                    info["animeList_count"] = len(data.get("animeList", []))
                elif isinstance(data, list):
                    info["data_type"] = f"list[{len(data)}]"
                    if data and isinstance(data[0], dict):
                        info["first_item_keys"] = list(data[0].keys())
                results[key] = info
        except Exception as e:
            results[key] = {"error": str(e)[:100]}
    
    # Juga cek ongoing item pertama untuk lihat struktur field
    try:
        raw = fetch("/anime/ongoing-anime")
        if raw and raw.get("data") and raw["data"].get("animeList"):
            first = raw["data"]["animeList"][0]
            results["_ongoing_first_item"] = first
    except Exception as e:
        results["_ongoing_first_item"] = str(e)
    
    return jsonify(results)

@app.route("/debug")
def debug():
    import traceback
    try:
        source = get_active_source()
        pfx    = SOURCES[source]["prefix"]
        raw    = fetch(f"{pfx}/home")
        return jsonify({
            "source":    source,
            "pfx":       pfx,
            "raw_ok":    raw is not None,
            "status":    raw.get("status") if raw else None,
            "data_keys": list(raw.get("data", {}).keys()) if raw else None,
            "ongoing_count": len(raw["data"].get("ongoing", {}).get("animeList", [])) if raw and raw.get("data") else 0,
        })
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

# ═══════════════════════════════════════════════════════
# TRAKTEER API
# ═══════════════════════════════════════════════════════
TRAKTEER_API_KEY = os.environ.get("TRAKTEER_API_KEY", "")
TRAKTEER_BASE    = "https://api.trakteer.id/v1/public"

def fetch_trakteer(endpoint, params=None):
    """Fetch dari Trakteer API dengan cache Redis 5 menit."""
    cache_key = f"animeku:trakteer:{endpoint}"
    try:
        cached = redis.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    try:
        headers = {
            "key": TRAKTEER_API_KEY,
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        r = requests.get(f"{TRAKTEER_BASE}/{endpoint}", headers=headers, params=params, timeout=8)
        data = r.json()
        try:
            redis.set(cache_key, json.dumps(data), ex=300)  # cache 5 menit
        except Exception:
            pass
        return data
    except Exception:
        return None

@app.route("/api/trakteer/debug")
def trakteer_debug():
    """Debug: coba semua kemungkinan endpoint Trakteer."""
    import traceback
    try:
        key = os.environ.get("TRAKTEER_API_KEY", "")
        if not key:
            return jsonify({"error": "TRAKTEER_API_KEY tidak diset"})
        headers = {
            "key": key,
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        # Coba semua endpoint yang mungkin
        # Coba transactions dengan berbagai parameter
        results = {}
        test_params = [
            {},
            {"limit": 10},
            {"per_page": 10},
            {"page": 1, "limit": 10},
            {"status": "success"},
            {"status": "paid"},
            {"type": "tip"},
        ]
        for i, params in enumerate(test_params):
            try:
                r = requests.get(f"{TRAKTEER_BASE}/transactions", headers=headers, params=params, timeout=5)
                body = r.json()
                results[f"try_{i}_{params}"] = {
                    "status": r.status_code,
                    "data_count": len(body.get("result",{}).get("data",[])),
                    "result_keys": list(body.get("result",{}).keys()) if body.get("result") else [],
                    "preview": str(r.text[:300]),
                }
            except Exception as ex:
                results[f"try_{i}"] = {"error": str(ex)}
        return jsonify({"key_prefix": key[:6]+"...", "results": results})
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()})

@app.route("/api/trakteer/supporters")
def trakteer_supporters():
    """Return 10 supporter terbaru untuk ditampilkan di home."""
    import traceback
    try:
        data = fetch_trakteer("transactions", {"limit": 10})
        if not data:
            return jsonify({"ok": False, "supporters": [], "reason": "fetch returned None"})

        items = data.get("result", {}).get("data", []) or data.get("data", []) or []
        supporters = []
        for item in items[:10]:
            supporters.append({
                "name":    item.get("supporter_name") or item.get("name") or item.get("supporter") or "Anonim",
                "amount":  item.get("amount_raw") or item.get("amount") or item.get("total_amount") or 0,
                "unit":    item.get("unit") or item.get("quantity") or 1,
                "message": item.get("supporter_message") or item.get("message") or item.get("note") or "",
                "time":    item.get("created_at") or item.get("transaction_time") or item.get("paid_at") or "",
            })
        return jsonify({"ok": True, "supporters": supporters})
    except Exception as e:
        return jsonify({"ok": False, "supporters": [], "error": str(e), "trace": traceback.format_exc()})

@app.route("/api/trakteer/latest")
def trakteer_latest():
    """Return supporter terbaru saja (untuk polling notifikasi)."""
    import traceback
    try:
        data = fetch_trakteer("transactions", {"limit": 1})
        if not data:
            return jsonify({"ok": False})

        items = data.get("result", {}).get("data", []) or data.get("data", []) or []
        if not items:
            return jsonify({"ok": True, "latest": None})

        item = items[0]
        return jsonify({
            "ok": True,
            "latest": {
                "id":      item.get("id") or item.get("transaction_id") or "",
                "name":    item.get("supporter_name") or item.get("name") or "Anonim",
                "amount":  item.get("amount_raw") or item.get("amount") or 0,
                "unit":    item.get("unit") or item.get("quantity") or 1,
                "message": item.get("supporter_message") or item.get("message") or "",
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route('/quiz')
def quiz():
    return render_template('quiz.html')

@app.route('/gacha')
def gacha():
    return render_template('gacha.html')

# ═══════════════════════════════════════════════════════
# SISTEM KOIN
# ═══════════════════════════════════════════════════════

COIN_PRICE_PER_ANIME = 5   # 5 koin untuk akses 1 anime penuh
COIN_RATE            = 1   # 1 koin = Rp 1.000
FREE_EPISODES        = 2   # episode 1-2 gratis tanpa koin

def _get_user_id_from_token(access_token):
    """Verifikasi token Supabase dan return user_id."""
    if not access_token:
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers=supabase_headers(access_token),
            timeout=5
        )
        if r.ok:
            return r.json().get("id")
    except Exception:
        pass
    return None

def _get_coin_balance(user_id):
    """Ambil saldo koin user dari Supabase."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/user_coins",
            headers=supabase_service_headers(),
            params={"user_id": f"eq.{user_id}", "select": "balance"}
        )
        if r.ok and r.json():
            return r.json()[0]["balance"]
    except Exception:
        pass
    return 0

def _has_anime_access(user_id, anime_slug):
    """Cek apakah user sudah punya akses ke anime ini."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/anime_access",
            headers=supabase_service_headers(),
            params={"user_id": f"eq.{user_id}", "anime_slug": f"eq.{anime_slug}", "select": "id"}
        )
        if r.ok and r.json():
            return True
    except Exception:
        pass
    return False

@app.route("/api/coins/balance")
def api_coin_balance():
    """Ambil saldo koin user yang sedang login."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    user_id = _get_user_id_from_token(access_token)
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    balance = _get_coin_balance(user_id)
    return jsonify({"balance": balance, "coin_price": COIN_PRICE_PER_ANIME})

@app.route("/api/coins/check-access")
def api_check_access():
    """Cek apakah user bisa akses anime tertentu (punya koin / sudah beli)."""
    anime_slug   = request.args.get("anime", "")
    ep_index     = int(request.args.get("ep_index", 0))  # index episode (0-based dari terbaru)
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()

    if not anime_slug:
        return jsonify({"error": "anime param required"}), 400

    # Episode 1 & 2 (index terakhir = ep terlama karena list terbalik) gratis
    # Kita pakai ep_index dari ujung: ep_index >= (total-FREE_EPISODES) = gratis
    # Tapi karena kita tidak tahu total di sini, kita cek dari frontend
    # Untuk API ini: kalau ep_index < FREE_EPISODES → gratis
    # Catatan: API mengurutkan terbaru dulu, jadi index 0 = terbaru
    # "Episode 1 & 2" = index terbesar (terlama). Kita handle di frontend.

    user_id = _get_user_id_from_token(access_token)
    if not user_id:
        return jsonify({
            "access": False,
            "reason": "not_logged_in",
            "balance": 0,
            "coin_price": COIN_PRICE_PER_ANIME,
            "free_episodes": FREE_EPISODES
        })

    # Cek apakah sudah beli anime ini
    if _has_anime_access(user_id, anime_slug):
        balance = _get_coin_balance(user_id)
        return jsonify({"access": True, "balance": balance, "already_purchased": True})

    balance = _get_coin_balance(user_id)
    return jsonify({
        "access":           False,
        "reason":           "not_purchased",
        "balance":          balance,
        "coin_price":       COIN_PRICE_PER_ANIME,
        "free_episodes":    FREE_EPISODES,
        "can_afford":       balance >= COIN_PRICE_PER_ANIME,
    })

@app.route("/api/coins/buy-access", methods=["POST"])
def api_buy_access():
    """User beli akses anime dengan koin."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    user_id = _get_user_id_from_token(access_token)
    if not user_id:
        return jsonify({"error": "Login dulu ya!"}), 401

    data       = request.get_json() or {}
    anime_slug = data.get("anime_slug", "").strip()
    anime_title = data.get("anime_title", anime_slug)
    if not anime_slug:
        return jsonify({"error": "anime_slug required"}), 400

    # Cek sudah punya akses
    if _has_anime_access(user_id, anime_slug):
        return jsonify({"success": True, "already_owned": True, "balance": _get_coin_balance(user_id)})

    # Cek saldo
    balance = _get_coin_balance(user_id)
    if balance < COIN_PRICE_PER_ANIME:
        return jsonify({
            "error": f"Koin tidak cukup. Kamu punya {balance} koin, butuh {COIN_PRICE_PER_ANIME} koin.",
            "balance": balance,
            "needed": COIN_PRICE_PER_ANIME
        }), 400

    # Kurangi saldo koin
    new_balance = balance - COIN_PRICE_PER_ANIME
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/user_coins",
            headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
            json={"user_id": user_id, "balance": new_balance}
        )
        if not r.ok:
            return jsonify({"error": "Gagal update saldo", "detail": r.text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Catat transaksi
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/coin_transactions",
            headers={**supabase_service_headers(), "Prefer": "return=representation"},
            json={
                "user_id":     user_id,
                "type":        "spend",
                "amount":      -COIN_PRICE_PER_ANIME,
                "description": f"Akses anime: {anime_title}",
                "anime_slug":  anime_slug,
            }
        )
    except Exception:
        pass

    # Beri akses anime
    try:
        r2 = requests.post(
            f"{SUPABASE_URL}/rest/v1/anime_access",
            headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
            json={"user_id": user_id, "anime_slug": anime_slug}
        )
        if not r2.ok:
            return jsonify({"error": "Gagal beri akses", "detail": r2.text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "success":     True,
        "new_balance": new_balance,
        "anime_slug":  anime_slug,
        "message":     f"Berhasil! Semua episode {anime_title} sekarang bisa ditonton."
    })

@app.route("/api/coins/transactions")
def api_coin_transactions():
    """Riwayat transaksi koin user."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    user_id = _get_user_id_from_token(access_token)
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/coin_transactions",
            headers=supabase_service_headers(),
            params={"user_id": f"eq.{user_id}", "order": "created_at.desc", "limit": "30", "select": "*"}
        )
        return jsonify(r.json() if r.ok else [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/coins/my-access")
def api_my_access():
    """Daftar anime yang sudah dibeli user."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    user_id = _get_user_id_from_token(access_token)
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/anime_access",
            headers=supabase_service_headers(),
            params={"user_id": f"eq.{user_id}", "order": "purchased_at.desc", "select": "*"}
        )
        return jsonify(r.json() if r.ok else [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Admin: Topup koin manual ───────────────────────────

@app.route("/api/admin/coins/topup", methods=["POST"])
def admin_coin_topup():
    """Admin topup koin ke user tertentu."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not _is_admin(access_token):
        return jsonify({"error": "Forbidden"}), 403

    data        = request.get_json() or {}
    target_id   = data.get("user_id", "").strip()
    amount      = int(data.get("amount", 0))
    description = data.get("description", "Topup manual oleh admin")
    if not target_id or amount <= 0:
        return jsonify({"error": "user_id dan amount (> 0) wajib diisi"}), 400

    # Ambil saldo sekarang
    current = _get_coin_balance(target_id)
    new_balance = current + amount

    # Update saldo
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/user_coins",
        headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
        json={"user_id": target_id, "balance": new_balance}
    )
    if not r.ok:
        return jsonify({"error": "Gagal update saldo", "detail": r.text}), 500

    # Catat transaksi
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/coin_transactions",
            headers={**supabase_service_headers(), "Prefer": "return=representation"},
            json={
                "user_id":     target_id,
                "type":        "topup",
                "amount":      amount,
                "description": description,
                "anime_slug":  None,
            }
        )
    except Exception:
        pass

    return jsonify({
        "ok":          True,
        "user_id":     target_id,
        "added":       amount,
        "new_balance": new_balance,
        "rp_value":    f"Rp {amount * COIN_RATE:,}".replace(",", ".")
    })

@app.route("/api/admin/coins/adjust", methods=["POST"])
def admin_coin_adjust():
    """Admin set saldo koin user ke nilai tertentu (override)."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not _is_admin(access_token):
        return jsonify({"error": "Forbidden"}), 403

    data      = request.get_json() or {}
    target_id = data.get("user_id", "").strip()
    balance   = int(data.get("balance", 0))
    if not target_id:
        return jsonify({"error": "user_id wajib diisi"}), 400

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/user_coins",
        headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
        json={"user_id": target_id, "balance": balance}
    )
    if not r.ok:
        return jsonify({"error": r.text}), 500

    return jsonify({"ok": True, "user_id": target_id, "balance": balance})

@app.route("/api/admin/coins/revoke-access", methods=["POST"])
def admin_revoke_access():
    """Admin cabut akses anime user."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not _is_admin(access_token):
        return jsonify({"error": "Forbidden"}), 403

    data       = request.get_json() or {}
    target_id  = data.get("user_id", "").strip()
    anime_slug = data.get("anime_slug", "").strip()
    if not target_id:
        return jsonify({"error": "user_id wajib diisi"}), 400

    params = {"user_id": f"eq.{target_id}"}
    if anime_slug:
        params["anime_slug"] = f"eq.{anime_slug}"

    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/anime_access",
        headers=supabase_service_headers(),
        params=params
    )
    return jsonify({"ok": r.ok})

# ═══════════════════════════════════════════════════════
# TOPUP REQUEST SYSTEM
# ═══════════════════════════════════════════════════════

CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "dzfkklsza")
CLOUDINARY_API_KEY    = os.environ.get("CLOUDINARY_API_KEY",    "588474134734416")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "9c12YJe5rZSYSg7zROQuvmVZ7mg")

PAYMENT_INFO = {
    "dana":  {"name": "DANA",  "number": "082320781747"},
    "ovo":   {"name": "OVO",   "number": "082320781747"},
    "gopay": {"name": "GoPay", "number": "082320781747"},
}

@app.route("/reward")
def reward_page():
    return render_template("reward.html")

# ═══════════════════════════════════════════════════════
# SISTEM REWARD HARIAN + NONTON IKLAN
# ═══════════════════════════════════════════════════════

DAILY_REWARD_COINS = 5     # koin per login harian
AD_REWARD_COINS    = 3     # koin per nonton iklan
AD_MAX_PER_DAY     = 5     # max nonton iklan per hari
NEW_USER_BONUS     = 10    # koin gratis user baru

def _add_coins(user_id, amount, tx_type, description):
    """Helper: tambah koin ke user dan catat transaksi."""
    balance = _get_coin_balance(user_id)
    new_balance = balance + amount
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/user_coins",
            headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
            json={"user_id": user_id, "balance": new_balance},
            timeout=8
        )
        requests.post(
            f"{SUPABASE_URL}/rest/v1/coin_transactions",
            headers={**supabase_service_headers(), "Prefer": "return=representation"},
            json={
                "user_id":     user_id,
                "type":        tx_type,
                "amount":      amount,
                "description": description,
            },
            timeout=8
        )
        return new_balance
    except Exception:
        return balance

def _get_today_str():
    """Tanggal hari ini format YYYY-MM-DD (WIB UTC+7)."""
    from datetime import datetime, timezone, timedelta
    wib = timezone(timedelta(hours=7))
    return datetime.now(wib).strftime("%Y-%m-%d")

def _get_daily_record(user_id):
    """Ambil record reward harian user hari ini."""
    today = _get_today_str()
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/user_daily_reward",
            headers=supabase_service_headers(),
            params={"user_id": f"eq.{user_id}", "reward_date": f"eq.{today}", "select": "*"},
            timeout=8
        )
        if r.ok and r.json():
            return r.json()[0]
    except Exception:
        pass
    return None

def _get_streak(user_id):
    """Hitung streak login harian user."""
    from datetime import datetime, timezone, timedelta
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/user_daily_reward",
            headers=supabase_service_headers(),
            params={"user_id": f"eq.{user_id}", "select": "reward_date", "order": "reward_date.desc", "limit": 30},
            timeout=8
        )
        if not r.ok or not r.json():
            return 1
        dates = sorted([d["reward_date"] for d in r.json()], reverse=True)
        streak = 0
        wib    = timezone(timedelta(hours=7))
        check  = datetime.now(wib).date()
        for d in dates:
            from datetime import date as date_type
            rec_date = datetime.strptime(d, "%Y-%m-%d").date()
            if rec_date == check:
                streak += 1
                check = check - timedelta(days=1)
            else:
                break
        return max(streak, 1)
    except Exception:
        return 1

@app.route("/api/reward/status")
def api_reward_status():
    """Cek status reward harian dan ad count hari ini."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    user_id = _get_user_id_from_token(access_token)
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    today  = _get_today_str()
    record = _get_daily_record(user_id)

    daily_claimed = bool(record and record.get("daily_claimed"))
    ad_count      = record.get("ad_count", 0) if record else 0
    streak        = _get_streak(user_id)

    return jsonify({
        "daily_claimed": daily_claimed,
        "ad_count":      ad_count,
        "ad_max":        AD_MAX_PER_DAY,
        "streak":        streak,
        "today":         today,
    })

@app.route("/api/reward/daily", methods=["POST"])
def api_reward_daily():
    """Klaim reward login harian."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    user_id = _get_user_id_from_token(access_token)
    if not user_id:
        return jsonify({"error": "Login dulu ya!"}), 401

    today  = _get_today_str()
    record = _get_daily_record(user_id)

    # Sudah klaim hari ini?
    if record and record.get("daily_claimed"):
        return jsonify({"error": "Sudah diklaim hari ini. Kembali besok!"}), 400

    # Tambah koin
    new_balance = _add_coins(user_id, DAILY_REWARD_COINS, "daily", "Login harian")

    # Upsert record harian
    ad_count = record.get("ad_count", 0) if record else 0
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/user_daily_reward",
            headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
            json={
                "user_id":       user_id,
                "reward_date":   today,
                "daily_claimed": True,
                "ad_count":      ad_count,
            },
            timeout=8
        )
    except Exception:
        pass

    streak = _get_streak(user_id)
    return jsonify({
        "success":     True,
        "coins_added": DAILY_REWARD_COINS,
        "new_balance": new_balance,
        "streak":      streak,
    })

@app.route("/api/reward/watch-ad", methods=["POST"])
def api_reward_watch_ad():
    """Klaim reward setelah nonton iklan."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    user_id = _get_user_id_from_token(access_token)
    if not user_id:
        return jsonify({"error": "Login dulu ya!"}), 401

    today  = _get_today_str()
    record = _get_daily_record(user_id)
    ad_count = record.get("ad_count", 0) if record else 0

    # Sudah max hari ini?
    if ad_count >= AD_MAX_PER_DAY:
        return jsonify({"error": f"Batas harian tercapai ({AD_MAX_PER_DAY}x). Kembali besok!"}), 400

    # Tambah koin
    new_balance = _add_coins(user_id, AD_REWARD_COINS, "ad", f"Nonton iklan #{ad_count + 1}")

    # Update record harian
    new_ad_count = ad_count + 1
    daily_claimed = record.get("daily_claimed", False) if record else False
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/user_daily_reward",
            headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
            json={
                "user_id":       user_id,
                "reward_date":   today,
                "daily_claimed": daily_claimed,
                "ad_count":      new_ad_count,
            },
            timeout=8
        )
    except Exception:
        pass

    return jsonify({
        "success":     True,
        "coins_added": AD_REWARD_COINS,
        "new_balance": new_balance,
        "ad_count":    new_ad_count,
        "ad_max":      AD_MAX_PER_DAY,
    })

@app.route("/topup")
def topup_page():
    return render_template("topup.html")

@app.route("/api/topup/upload-proof", methods=["POST"])
def topup_upload_proof():
    """Upload bukti transfer ke Cloudinary dan buat topup request."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    user_id = _get_user_id_from_token(access_token)
    if not user_id:
        return jsonify({"error": "Login dulu ya!"}), 401

    amount_rp   = int(request.form.get("amount_rp", 0))
    payment_via = request.form.get("payment_via", "").strip()
    note        = request.form.get("note", "").strip()
    proof_file  = request.files.get("proof")

    if amount_rp < 5000:
        return jsonify({"error": "Minimal topup Rp 5.000 (5 koin)"}), 400
    if not payment_via or payment_via not in PAYMENT_INFO:
        return jsonify({"error": "Pilih metode pembayaran"}), 400
    if not proof_file:
        return jsonify({"error": "Bukti transfer wajib diupload"}), 400

    # Upload ke Cloudinary
    import hashlib, time as _time, base64 as b64
    try:
        timestamp = int(_time.time())
        file_data = proof_file.read()
        data_uri  = f"data:{proof_file.mimetype};base64,{b64.b64encode(file_data).decode()}"
        sig_str   = f"folder=animeku_topup&timestamp={timestamp}{CLOUDINARY_API_SECRET}"
        signature = hashlib.sha256(sig_str.encode()).hexdigest()
        upload_resp = requests.post(
            f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload",
            data={
                "file":      data_uri,
                "api_key":   CLOUDINARY_API_KEY,
                "timestamp": timestamp,
                "folder":    "animeku_topup",
                "signature": signature,
            },
            timeout=20
        )
        if not upload_resp.ok:
            return jsonify({"error": "Gagal upload bukti", "detail": upload_resp.text}), 500
        proof_url = upload_resp.json().get("secure_url", "")
    except Exception as e:
        return jsonify({"error": f"Upload error: {str(e)}"}), 500

    amount_coins = amount_rp // 1000

    # Ambil nama user
    user_name = ""
    try:
        ur = requests.get(f"{SUPABASE_URL}/auth/v1/user", headers=supabase_headers(access_token))
        if ur.ok:
            meta = ur.json().get("user_metadata", {})
            user_name = meta.get("full_name", ur.json().get("email", ""))
    except Exception:
        pass

    # Simpan request ke Supabase
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/topup_requests",
        headers={**supabase_service_headers(), "Prefer": "return=representation"},
        json={
            "user_id":      user_id,
            "user_name":    user_name,
            "amount_rp":    amount_rp,
            "amount_coins": amount_coins,
            "payment_via":  payment_via,
            "proof_url":    proof_url,
            "note":         note,
            "status":       "pending",
        },
        timeout=10
    )
    if not r.ok:
        return jsonify({"error": "Gagal simpan request", "detail": r.text}), 500

    req_id = r.json()[0]["id"] if r.json() else ""
    return jsonify({
        "success":      True,
        "request_id":   req_id,
        "amount_coins": amount_coins,
        "proof_url":    proof_url,
        "message":      f"Request {amount_coins} koin dikirim! Tunggu konfirmasi admin."
    })

@app.route("/api/topup/my-requests")
def topup_my_requests():
    """Riwayat request topup milik user."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    user_id = _get_user_id_from_token(access_token)
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/topup_requests",
        headers=supabase_service_headers(),
        params={"user_id": f"eq.{user_id}", "order": "created_at.desc", "limit": "20", "select": "*"}
    )
    return jsonify(r.json() if r.ok else [])

# ── Admin: kelola topup requests ──────────────────────

@app.route("/api/admin/topup/requests")
def admin_topup_requests():
    """Ambil semua topup request (admin only)."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not _is_admin(access_token):
        return jsonify({"error": "Forbidden"}), 403
    status = request.args.get("status", "")
    params = {"order": "created_at.desc", "limit": "50", "select": "*"}
    if status:
        params["status"] = f"eq.{status}"
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/topup_requests",
        headers=supabase_service_headers(),
        params=params
    )
    return jsonify(r.json() if r.ok else [])

@app.route("/api/admin/topup/approve/<req_id>", methods=["POST"])
def admin_topup_approve(req_id):
    """Admin approve → otomatis tambah koin ke user."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not _is_admin(access_token):
        return jsonify({"error": "Forbidden"}), 403

    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/topup_requests",
        headers=supabase_service_headers(),
        params={"id": f"eq.{req_id}", "select": "*"}
    )
    if not r.ok or not r.json():
        return jsonify({"error": "Request tidak ditemukan"}), 404
    req_data = r.json()[0]
    if req_data["status"] != "pending":
        return jsonify({"error": f"Request sudah {req_data['status']}"}), 400

    user_id      = req_data["user_id"]
    amount_coins = req_data["amount_coins"]

    # Tambah koin
    current = _get_coin_balance(user_id)
    new_bal = current + amount_coins
    rb = requests.post(
        f"{SUPABASE_URL}/rest/v1/user_coins",
        headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
        json={"user_id": user_id, "balance": new_bal}
    )
    if not rb.ok:
        return jsonify({"error": "Gagal tambah koin"}), 500

    # Catat transaksi
    requests.post(
        f"{SUPABASE_URL}/rest/v1/coin_transactions",
        headers={**supabase_service_headers(), "Prefer": "return=representation"},
        json={
            "user_id":     user_id,
            "type":        "topup",
            "amount":      amount_coins,
            "description": f"Topup via {req_data.get('payment_via','').upper()} — disetujui admin",
            "anime_slug":  None,
        }
    )

    # Update status → approved
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/topup_requests",
        headers=supabase_service_headers(),
        params={"id": f"eq.{req_id}"},
        json={"status": "approved"}
    )

    return jsonify({"ok": True, "user_id": user_id, "added_coins": amount_coins, "new_balance": new_bal})

@app.route("/api/admin/topup/reject/<req_id>", methods=["POST"])
def admin_topup_reject(req_id):
    """Admin reject topup request."""
    auth_header  = request.headers.get("Authorization", "")
    access_token = auth_header.replace("Bearer ", "").strip()
    if not _is_admin(access_token):
        return jsonify({"error": "Forbidden"}), 403
    data   = request.get_json() or {}
    reason = data.get("reason", "Ditolak admin")
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/topup_requests",
        headers=supabase_service_headers(),
        params={"id": f"eq.{req_id}"},
        json={"status": "rejected", "note": reason}
    )
    return jsonify({"ok": r.ok})

if __name__ == "__main__":
    app.run(debug=True)

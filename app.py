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
    "completed":600, "recent":300, "search":120, "genres":3600,
    "genre":300, "schedule":1800, "list":3600,
    "anime":600, "episode":180, "server":60, "default":300,
}

def _ttl(path):
    for k, v in CACHE_TTL.items():
        if k in path: return v
    return CACHE_TTL["default"]

def fetch(path, params=None):
    key = "samehadaku:" + path + str(sorted(params.items()) if params else "")
    try:
        cached = redis.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        print(f"Redis get error: {e}")
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

    return render_template("episode.html", data=data, slug=slug,
                           anime_slug=anime_slug, anime_data=anime_data)


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


if __name__ == "__main__":
    app.run(debug=True)

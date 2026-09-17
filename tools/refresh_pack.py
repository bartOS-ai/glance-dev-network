#!/usr/bin/env python3
"""Refresh a Basement Marquee poster pack from TMDB.

GDN draws *bundled* PNGs and never fetches an image at render time (see the
`now-playing` app), so the poster pack is a snapshot refreshed offline by this
tool rather than a live lookup. Given a public Letterboxd user, it pulls their
recent films, finds each on TMDB, downloads the poster, crops it to the panel's
native 21x32, and rewrites the pack data (CANON / CANON_ORDER / BY_TITLE /
POSTERS between the `# >>> PACK` markers in app.star) plus the manifest
`assets:` list to match.

Usage:
    export TMDB_API_KEY=...          # free at themoviedb.org/settings/api
    python3 tools/refresh_pack.py apps/home-cinema-marquee --user abartos27
    gdn validate apps/home-cinema-marquee

  --user LB       public Letterboxd username; its recent films seed the pack.
                  Omit to only re-download posters for the films already in the
                  canon (keeps the exact same set, refreshes the art).
  --list SLUG     bake a public list (needs --user) in rank order instead of the
                  diary; the ranked films land at the front of CANON_ORDER, so
                  the app's Canon top 4 becomes the list's top 4. Pass a comma-
                  separated set (--list a,b,c) or --list all to bake the union of
                  every public list the user has, deduped in list order.
  --limit N       max films to pull from Letterboxd (default 30, so a full list
                  scrolls without being cut off).
  --region R      unused today; reserved for regional release lookups.
  --replace       rebuild the canon *purely* from recent watches instead of
                  adding to the curated set.
  --no-overwrite  keep any poster PNG that already exists on disk.
  --dry-run       report what would change; write nothing.

The TMDB key is read from the environment only and is never written to disk,
so it cannot leak into the repo. This product uses the TMDB API but is not
endorsed or certified by TMDB.

Run it inside the SDK venv (where Pillow + requests already live):
    source .venv/bin/activate
"""

import argparse
import ast
import html
import os
import re
import sys
import time

try:
    import requests
    from PIL import Image, ImageOps, ImageEnhance
except ImportError as e:  # pragma: no cover - environment guidance
    sys.exit(
        "error: missing dependency (%s). Run inside the SDK venv, which has "
        "Pillow + requests:\n    source .venv/bin/activate" % e.name
    )

TMDB = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w342"
POSTER_W, POSTER_H = 21, 32
PACK_BEGIN = "# >>> PACK:BEGIN"
PACK_END = "# >>> PACK:END"

# Letterboxd 429s a bare python-requests User-Agent after a few quick hits, so a
# list bake (one page fetch per film) needs a browser UA + backoff on 429.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
_LB = requests.Session()
_LB.headers.update({"User-Agent": _UA})


def lb_get(url):
    """GET a Letterboxd page, retrying through rate-limit (429) with backoff."""
    for attempt in range(5):
        r = _LB.get(url, timeout=20)
        if r.status_code != 429:
            return r
        time.sleep(1.5 * (attempt + 1))
    return r  # last 429; caller decides


# --------------------------------------------------------------------- TMDB
class TmdbError(Exception):
    """A per-film TMDB failure: skip that film, keep the run going."""


def tmdb_get(path, key, **params):
    params["api_key"] = key
    try:
        r = requests.get(TMDB + path, params=params, timeout=20)
    except requests.exceptions.RequestException as e:
        raise TmdbError("network error: %s" % e)
    if r.status_code == 401:
        # a rejected key is fatal for the whole run, not just this film
        sys.exit("error: TMDB rejected the key (401 Unauthorized). TMDB_API_KEY "
                 "must be a real key from themoviedb.org/settings/api, not a "
                 "placeholder.")
    if r.status_code != 200:
        raise TmdbError("HTTP %d from %s" % (r.status_code, path))
    return r.json()


def _us_cert(d):
    """The US certificate from an appended release_dates payload.

    TMDB returns a release list per country, each entry typed: 1 premiere,
    2 limited theatrical, 3 theatrical, 4 digital, 5 physical, 6 TV. The
    theatrical entry carries the MPAA rating, so prefer type 3 and fall back to
    the first non-empty. Films with no US release simply have none, which is a
    blank field rather than an error."""
    for country in d.get("release_dates", {}).get("results", []):
        if country.get("iso_3166_1") != "US":
            continue
        fallback = ""
        for rel in country.get("release_dates", []):
            cert = (rel.get("certification") or "").strip()
            if not cert:
                continue
            if rel.get("type") == 3:
                return cert
            if not fallback:
                fallback = cert
        return fallback
    return ""


def _surname(name):
    """The panel shows a director by surname, but the last space-separated token
    is not always it: Gus Van Sant becomes SANT, Brian De Palma becomes PALMA.
    Nobiliary particles are pulled back in so the name still reads as itself."""
    if not name:
        return ""
    parts = name.strip().split()
    if not parts:
        return ""
    PARTICLES = {"van", "von", "de", "del", "della", "di", "da", "dos", "du",
                 "la", "le", "ter", "ten", "den", "der", "st", "st.", "saint",
                 "mac", "mc", "o'"}
    i = len(parts) - 1
    while i > 0 and parts[i - 1].lower().strip(".") in PARTICLES:
        i -= 1
    return " ".join(parts[i:]).upper()


def _details(d):
    """Shape a TMDB /movie payload into our canon fields."""
    director = ""
    for crew in d.get("credits", {}).get("crew", []):
        if crew.get("job") == "Director":
            director = crew.get("name", "")
            break
    return {
        "title": (d.get("title") or "").upper(),
        "year": (d.get("release_date") or "")[:4],
        "runtime": str(d.get("runtime") or "").strip(),
        "director": _surname(director),
        "cert": _us_cert(d),
        "poster_path": d.get("poster_path"),
    }


def tmdb_details(mid, key):
    """Runtime + director + certificate + poster for a known TMDB movie id.

    credits and release_dates are appended to the details call rather than
    fetched separately: three requests per film against a render budget of
    eight would not fit, and even offline it triples the run time."""
    return _details(tmdb_get("/movie/%s" % mid, key,
                             append_to_response="credits,release_dates"))


def tmdb_lookup(title, year, key):
    """Best TMDB match for a title (+year) when there's no id to go on."""
    params = {"query": title, "language": "en-US"}
    if year:
        params["year"] = year
    hits = tmdb_get("/search/movie", key, **params).get("results", [])
    if not hits:
        return None
    return tmdb_details(hits[0]["id"], key)


def fetch_poster(url, dest):
    """Download a poster and render it to the panel's native 21x32 so it still
    reads as a poster at LED size rather than a muddy thumbnail: a stepped
    LANCZOS downscale (halving before the final fit keeps more edge detail than
    one 40x+ jump), then autocontrast + a saturation/contrast/sharpness lift."""
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    import io

    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    while img.width > POSTER_W * 4:
        img = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
    img = ImageOps.fit(img, (POSTER_W, POSTER_H), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Color(img).enhance(1.30)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Sharpness(img).enhance(1.8)
    img.save(dest, format="PNG", optimize=True)


# ---------------------------------------------------------------- Letterboxd
def _rating10(s):
    """Letterboxd's <memberRating> is "4.0"/"4.5"; fold to a 0-10 half-star
    scale (0 = unrated). Mirrors rating10() in app.star."""
    s = (s or "").strip()
    if not s:
        return 0
    parts = s.split(".")
    v = (int(parts[0]) * 2) if parts[0].isdigit() else 0
    if len(parts) > 1 and parts[1].startswith("5"):
        v += 1
    return min(v, 10)


def letterboxd_recent(user, limit):
    """Recent films from a public Letterboxd diary RSS. Each item carries the
    poster image URL, TMDB movie id, and the owner's star rating inline, so the
    pack is keyless: slug, title, year, poster_url, tmdb_id, rating."""
    r = lb_get("https://letterboxd.com/%s/rss/" % user)
    if r.status_code == 404:
        sys.exit("error: Letterboxd user '%s' not found (404)." % user)
    r.raise_for_status()
    body = r.text
    films, seen = [], set()
    for item in body.split("<item>")[1:]:
        title = html.unescape(_between(item, "<letterboxd:filmTitle>", "</letterboxd:filmTitle>"))
        year = _between(item, "<letterboxd:filmYear>", "</letterboxd:filmYear>")
        link = _between(item, "<link>", "</link>")
        poster_url = html.unescape(_between(item, 'src="', '"'))  # the description's poster <img>
        tmdb_id = _between(item, "<tmdb:movieId>", "</tmdb:movieId>")
        rating = _rating10(_between(item, "<letterboxd:memberRating>", "</letterboxd:memberRating>"))
        slug = ""
        i = link.find("/film/")
        if i >= 0:
            slug = link[i + 6:].strip("/").split("/")[0]
        if not slug or slug in seen:
            continue
        seen.add(slug)
        films.append({"slug": slug, "title": title, "year": year,
                      "poster_url": poster_url, "tmdb_id": tmdb_id, "rating": rating})
        if len(films) >= limit:
            break
    return films


def _between(s, a, b):
    i = s.find(a)
    if i < 0:
        return ""
    i += len(a)
    j = s.find(b, i)
    return s[i:j].strip() if j >= 0 else ""


def _ogp(body, prop):
    """An OpenGraph <meta property=...> content value (single or double quotes)."""
    for q in ('"', "'"):
        marker = "<meta property=%s%s%s content=%s" % (q, prop, q, q)
        i = body.find(marker)
        if i >= 0:
            j = i + len(marker)
            k = body.find(q, j)
            return body[j:k] if k >= 0 else ""
    return ""


def _poster_url(body):
    """The portrait POSTER image, not the landscape og:image share card. The
    page's JSON-LD block carries the poster as its "image" (a 2:3 crop; the path
    varies -- /resized/film-poster/ or /resized/sm/upload/), whereas og:image is
    a 1200x675 scene grab -- baking THAT is what made posters look like
    screenshots. The crop size is encoded as -0-W-0-H-crop, so bump it to a big
    source and let fetch_poster downscale from something sharp."""
    block = body
    i = body.find("application/ld+json")
    if i >= 0:
        j = body.find("</script>", i)
        block = body[i:j] if j >= 0 else body[i:]
    m = re.search(r'"image"\s*:\s*"([^"]+)"', block)
    if not m:
        return ""
    url = html.unescape(m.group(1))
    return re.sub(r"-0-\d+-0-\d+-crop", "-0-1000-0-1500-crop", url)


def letterboxd_film(slug):
    """Poster URL + title + year for a single film, from its public film page
    (keyless). The portrait poster comes from the page's film-poster image;
    og:title is 'Title (YYYY)'. The TMDB id is on the page too, for enrichment."""
    r = lb_get("https://letterboxd.com/film/%s/" % slug)
    if r.status_code != 200:
        return None
    body = r.text
    poster_url = _poster_url(body)
    title = html.unescape(_ogp(body, "og:title"))
    year = ""
    if title.endswith(")") and "(" in title:
        lp = title.rfind("(")
        year = title[lp + 1:-1].strip()
        title = title[:lp].strip()
    return {"slug": slug, "title": title, "year": year, "poster_url": poster_url,
            "tmdb_id": _between(body, "themoviedb.org/movie/", "/"), "rating": 0}


def list_film_slugs(user, list_slug, limit):
    """Film slugs of a public Letterboxd list, in list order. Lists have no RSS,
    so this reads the list page HTML (each poster carries data-item-slug)."""
    url = "https://letterboxd.com/%s/list/%s/" % (user, list_slug)
    r = lb_get(url)
    if r.status_code == 404:
        sys.exit("error: list '%s/list/%s' not found (404)." % (user, list_slug))
    r.raise_for_status()
    body, slugs, seen = r.text, [], set()
    marker = 'data-item-slug="'  # list poster rows carry the film slug here
    i = body.find(marker)
    while i >= 0 and len(slugs) < limit:
        j = i + len(marker)
        k = body.find('"', j)
        s = body[j:k].strip("/").split("/")[-1] if k > j else ""
        if s and s not in seen:
            seen.add(s)
            slugs.append(s)
        i = body.find(marker, k) if k >= 0 else -1
    return slugs


def user_list_slugs(user):
    """Every public list slug across all pages of a user's /lists/, in page
    order. The listing paginates (12 per page), so this walks /lists/page/N/
    until a page adds nothing new. Lets a bake pull the union of every list a
    user has with `--list all`."""
    slugs, seen = [], set()
    for page in range(1, 51):  # generous cap; stops as soon as a page is empty
        url = ("https://letterboxd.com/%s/lists/" % user if page == 1
               else "https://letterboxd.com/%s/lists/page/%d/" % (user, page))
        r = lb_get(url)
        if page == 1 and r.status_code == 404:
            sys.exit("error: Letterboxd user '%s' not found (404)." % user)
        if r.status_code != 200:
            break
        found = 0
        for m in re.finditer(r"/%s/list/([a-z0-9-]+)/" % re.escape(user), r.text):
            s = m.group(1)
            if s not in seen:
                seen.add(s)
                slugs.append(s)
                found += 1
        if found == 0:
            break
        time.sleep(0.4)  # polite pacing between listing pages
    return slugs


def letterboxd_films(film_slugs):
    """Pull each film's page for its poster + metadata (keyless), in order."""
    films = []
    for s in film_slugs:
        info = letterboxd_film(s)
        if info and info["poster_url"]:
            films.append(info)
        else:
            print("  MISS  %-40s (film page unavailable)" % s)
        time.sleep(0.6)  # be a polite guest; keeps us under the 429 threshold
    return films


# ------------------------------------------------------------------ app.star
def load_pack(text):
    """Read CANON / CANON_ORDER / BY_TITLE / POSTERS out of the marker block.
    The block is pure literals, so ast.literal_eval is safe (no code runs)."""
    after = text.split(PACK_BEGIN, 1)[1].split("\n", 1)[1]  # drop rest of marker line
    block = after.split(PACK_END, 1)[0]
    ns = {}
    for node in ast.parse(block).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            ns[node.targets[0].id] = ast.literal_eval(node.value)
    return ns["CANON"], ns["CANON_ORDER"], ns["BY_TITLE"], ns["POSTERS"]


def render_pack(canon, order):
    """Emit the marker block body from the canon dict + order."""
    lines = ["CANON = {"]
    for slug in order:
        e = canon[slug]
        t, y, rt, d, poster = e[0], e[1], e[2], e[3], e[4]
        rating = e[5] if len(e) > 5 else 0  # tolerate hand-edited 5-field entries
        cert = e[6] if len(e) > 6 else ""   # and pre-certificate 6-field ones
        lines.append(
            '    %s: [%s, %s, %s, %s, %s, %d, %s],'
            % (_q(slug), _q(t), _q(y), _q(rt), _q(d), _q(poster), rating, _q(cert))
        )
    lines.append("}")
    lines.append("CANON_ORDER = [")
    for slug in order:
        lines.append("    %s," % _q(slug))
    lines.append("]")
    lines.append("")
    lines.append("# Slug is the primary match; a title match is the fallback. Written out")
    lines.append("# literally: Starlark allows no `for` at module level. Two films can")
    lines.append("# share a title (e.g. a film that appears under two Letterboxd slugs), so")
    lines.append("# the title maps to the first slug in order -- a dict can't repeat a key.")
    seen_titles = {}
    lines.append("BY_TITLE = {")
    for slug in order:
        title = canon[slug][0]
        if title in seen_titles:
            continue
        seen_titles[title] = slug
        lines.append("    %s: %s," % (_q(title), _q(slug)))
    lines.append("}")
    lines.append("")
    lines.append("# Poster files present in this folder; each must also appear under")
    lines.append("# `assets:` in the manifest or the renderer rejects the draw.")
    lines.append("POSTERS = [")
    for slug in order:
        lines.append("    %s," % _q(canon[slug][4]))
    lines.append("]")
    return "\n".join(lines)


def _q(s):
    return '"%s"' % str(s).replace('"', '\\"')


def splice(text, begin, end, body):
    pat = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    repl = begin + " (generated by tools/refresh_pack.py -- safe to hand-edit)\n"
    repl += body + "\n" + end
    return pat.sub(lambda _: repl, text, count=1)


def update_manifest_assets(path, posters):
    """Rewrite the manifest `assets:` list (between `assets:` and the next
    top-level key) to exactly the poster set, preserving the rest of the file."""
    text = open(path).read()
    block = "assets:\n" + "".join("  - %s\n" % p for p in posters) + "\n"
    pat = re.compile(r"^assets:\n(?:[ \t].*\n|#.*\n|\n)*?(?=^\S)", re.MULTILINE)
    if not pat.search(text):
        sys.exit("error: could not find an assets: block in %s" % path)
    return pat.sub(block, text, count=1), text


def backfill(canon, order, key, limit, force, dry):
    """Fill missing runtime / director / certificate on films already packed.

    The poster path is keyless (Letterboxd OGP), so a pack baked without a TMDB
    key ends up with art but no metadata. This repairs those rows in place: it
    downloads nothing, reorders nothing, and writes only fields that are empty,
    so hand-corrected values survive. Two requests per film (search, then
    details) because the canon stores no TMDB ids.
    """
    todo = []
    for slug in order:
        e = canon[slug]
        runtime = e[2] if len(e) > 2 else ""
        director = e[3] if len(e) > 3 else ""
        cert = e[6] if len(e) > 6 else ""
        if force or not runtime or not director or not cert:
            todo.append(slug)
    if limit:
        todo = todo[:limit]

    print("%d film(s) need metadata%s" % (len(todo), " (forced)" if force else ""))
    filled, failed = 0, []
    for i, slug in enumerate(todo):
        e = list(canon[slug]) + [""] * (7 - len(canon[slug]))
        title, year = e[0], e[1]
        try:
            d = tmdb_lookup(title, year, key)
        except TmdbError as ex:
            failed.append(title)
            print("  warn  %-38s %s" % (title[:38], ex))
            continue
        if not d:
            failed.append(title)
            print("  MISS  %-38s no TMDB match" % title[:38])
            continue
        before = (e[2], e[3], e[6])
        if force or not e[2]:
            e[2] = d["runtime"]
        if force or not e[3]:
            e[3] = d["director"]
        if force or not e[6]:
            e[6] = d["cert"]
        if (e[2], e[3], e[6]) != before:
            filled += 1
            print("  %3d/%d  %-34s %s | %s | %s"
                  % (i + 1, len(todo), title[:34], e[2] or "-", e[3] or "-", e[6] or "-"))
        if not dry:
            canon[slug] = e
        time.sleep(0.05)   # stay well inside TMDB's rate limit

    print("\nFilled %d film(s)%s" % (filled, "; %d unmatched" % len(failed) if failed else ""))
    if failed:
        print("Unmatched: %s" % ", ".join(failed[:12]) + (" ..." if len(failed) > 12 else ""))
    return filled


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Refresh a marquee poster pack from TMDB.")
    ap.add_argument("app_dir")
    ap.add_argument("--user", help="public Letterboxd username")
    ap.add_argument("--list", dest="list_slug",
                    help="public list slug to bake (needs --user); order is kept")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--region", default="US")
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--no-overwrite", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backfill", action="store_true",
                    help="fill missing runtime/director/certificate on films "
                         "already in the canon; downloads no posters")
    ap.add_argument("--drop", nargs="+", metavar="SLUG",
                    help="remove films from the canon by slug (duplicates, bad "
                         "matches). Rewrites the pack and the manifest assets; "
                         "leaves the poster files on disk to delete by hand.")
    ap.add_argument("--backfill-force", action="store_true",
                    help="with --backfill, refetch even fields already set")
    args = ap.parse_args()

    # TMDB is OPTIONAL: with --user, posters + films come from the Letterboxd RSS
    # (keyless); a key only adds runtime + director. Without --user, the canon's
    # own slugs drive a keyless poster re-pull from each film's Letterboxd page
    # (a key instead refreshes posters from TMDB).
    key = os.environ.get("TMDB_API_KEY", "").strip()

    app_dir = args.app_dir.rstrip("/")
    star_path = os.path.join(app_dir, "app.star")
    manifest_path = os.path.join(app_dir, "manifest.yaml")
    if not os.path.exists(star_path):
        sys.exit("error: %s not found." % star_path)

    star = open(star_path).read()
    canon, order, _by, _posters = load_pack(star)

    if args.drop:
        gone, missing = [], []
        for slug in args.drop:
            if slug in canon:
                gone.append((slug, canon[slug][0], canon[slug][4]))
                del canon[slug]
            else:
                missing.append(slug)
        if missing:
            print("not in the canon, ignored: %s" % ", ".join(missing))
        if not gone:
            sys.exit("error: nothing to drop.")
        order = [s for s in order if s in canon]
        for slug, title, poster in gone:
            print("  dropped  %-28s (%s)" % (title, slug))
        if args.dry_run:
            print("\n[dry-run] canon would be %d film(s); nothing written" % len(order))
            return
        star = splice(star, PACK_BEGIN, PACK_END, render_pack(canon, order))
        open(star_path, "w").write(star)
        new_manifest, _old = update_manifest_assets(
            manifest_path, [canon[s][4] for s in order])
        open(manifest_path, "w").write(new_manifest)
        print("\nCanon is now %d film(s)." % len(order))
        print("Orphaned poster file(s) left on disk, safe to delete:")
        for _s, _t, poster in gone:
            print("    %s" % os.path.join(app_dir, poster))
        print("\nNext: gdn validate %s" % app_dir)
        return

    if args.backfill or args.backfill_force:
        if not key:
            sys.exit("error: --backfill needs a TMDB key. "
                     "export TMDB_API_KEY=... (free at themoviedb.org/settings/api)")
        n = backfill(canon, order, key, args.limit if args.limit else 0,
                     args.backfill_force, args.dry_run)
        if args.dry_run:
            print("[dry-run] nothing written")
            return
        if not n:
            print("Nothing to write.")
            return
        star = splice(star, PACK_BEGIN, PACK_END, render_pack(canon, order))
        open(star_path, "w").write(star)
        print("Wrote %s. Next: gdn validate %s" % (star_path, app_dir))
        return

    if args.replace:
        canon, order = {}, []

    # The work list: a public list (in order), else recent diary films, else the
    # existing canon films.
    if args.list_slug:
        if not args.user:
            sys.exit("error: --list needs --user (list URLs are /<user>/list/<slug>/).")
        # --list all      -> every public list the user has
        # --list a,b,c    -> those lists, unioned in order
        # --list slug     -> a single list
        if args.list_slug.strip().lower() == "all":
            list_slugs = user_list_slugs(args.user)
            print("Letterboxd @%s: %d public list(s): %s" % (
                args.user, len(list_slugs), ", ".join(list_slugs)))
        else:
            list_slugs = [s.strip() for s in args.list_slug.split(",") if s.strip()]
        # union the films across the chosen lists, preserving first-seen order
        film_slugs, seen = [], set()
        for ls in list_slugs:
            for s in list_film_slugs(args.user, ls, args.limit):
                if s not in seen:
                    seen.add(s)
                    film_slugs.append(s)
        targets = letterboxd_films(film_slugs)
        print("Letterboxd lists @%s [%s]: %d unique film(s)%s." % (
            args.user, ", ".join(list_slugs), len(targets),
            "" if key else " (keyless; no runtime/director)"))
    elif args.user:
        targets = letterboxd_recent(args.user, args.limit)
        print("Letterboxd @%s: %d recent film(s)%s." % (
            args.user, len(targets), "" if key else " (keyless; no runtime/director)"))
    elif key:
        targets = [{"slug": s, "title": canon[s][0], "year": canon[s][1],
                    "rating": (canon[s][5] if len(canon[s]) > 5 else 0)} for s in order]
        print("No --user: refreshing %d existing canon film(s) from TMDB." % len(targets))
    else:
        # Keyless canon refresh: re-pull every canon film's portrait poster from
        # its Letterboxd film page. runtime/director/rating are preserved in the
        # bake loop, so this just re-renders the art with the current pipeline.
        targets = []
        for s in order:
            info = letterboxd_film(s)
            if info and info["poster_url"]:
                info["rating"] = canon[s][5] if len(canon[s]) > 5 else 0
                targets.append(info)
            else:
                print("  MISS  %-40s (film page unavailable)" % s)
            time.sleep(0.6)  # polite pacing; stays under the 429 threshold
        print("No --user (keyless): refreshing %d canon poster(s) from Letterboxd." % len(targets))

    added, refreshed, missed, processed = [], [], [], []
    for t in targets:
        slug = t["slug"]
        title, year = t["title"].upper(), t["year"]
        runtime, director = "", ""
        poster_url = t.get("poster_url", "")

        # Metadata (and, when there's no diary poster, the image) from TMDB.
        d = None
        if key:
            try:
                d = tmdb_details(t["tmdb_id"], key) if t.get("tmdb_id") \
                    else tmdb_lookup(t["title"], year, key)
                if d:
                    title = d["title"] or title
                    year = d["year"] or year
                    runtime, director = d["runtime"], d["director"]
                    if not poster_url and d["poster_path"]:
                        poster_url = IMG + d["poster_path"]
            except TmdbError as e:
                print("  warn  %-38s (TMDB metadata skipped: %s)" % (t["title"], e))

        if not poster_url:
            missed.append(t["title"])
            print("  MISS  %-40s (no poster available)" % t["title"])
            continue

        poster_file = slug + ".png"
        dest = os.path.join(app_dir, poster_file)
        have = os.path.exists(dest)
        if not (have and args.no_overwrite) and not args.dry_run:
            try:
                fetch_poster(poster_url, dest)
            except (requests.exceptions.RequestException, OSError) as e:
                missed.append(t["title"])
                print("  MISS  %-40s (poster download failed: %s)" % (t["title"], e))
                continue
        # Don't clobber hand-set (or previously enriched) metadata on a keyless
        # re-bake: a list bake without a TMDB key carries no runtime/director, so
        # for a film already in the canon, keep whatever it already had.
        prior = canon.get(slug)
        if prior:
            if not runtime and len(prior) > 2:
                runtime = prior[2]
            if not director and len(prior) > 3:
                director = prior[3]
        rating = t.get("rating", 0)
        if not rating and prior and len(prior) > 5:
            rating = prior[5]
        cert = d["cert"] if (key and d) else ""
        if not cert and prior and len(prior) > 6:
            cert = prior[6]
        entry = [title, year, runtime, director, poster_file, rating, cert]
        (refreshed if slug in canon else added).append(title)
        canon[slug] = entry
        if slug not in processed:
            processed.append(slug)

    # Keep the freshly pulled films in source order at the front (a list keeps
    # its rank; recent watches keep newest-first), then any untouched canon.
    order = processed + [s for s in order if s not in processed]
    posters = [canon[s][4] for s in order]

    if args.dry_run:
        print("\n[dry-run] canon would be %d film(s): %s" % (len(order), ", ".join(order)))
        return

    star = splice(star, PACK_BEGIN, PACK_END, render_pack(canon, order))
    open(star_path, "w").write(star)
    new_manifest, _old = update_manifest_assets(manifest_path, posters)
    open(manifest_path, "w").write(new_manifest)

    print("\nAdded:     %s" % (", ".join(added) or "-"))
    print("Refreshed: %s" % (", ".join(refreshed) or "-"))
    if missed:
        print("Missed:    %s" % ", ".join(missed))
    print("\nCanon is now %d film(s). Next: gdn validate %s" % (len(order), app_dir))


if __name__ == "__main__":
    main()

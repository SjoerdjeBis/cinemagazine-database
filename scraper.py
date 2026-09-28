#!/usr/bin/env python3
"""
Cinemagazine recensies scraper
Haalt recensies op via de WordPress REST API en slaat ze op als data/reviews.json.

Gebruik:
    python3 scraper.py          # nieuwe recensies ophalen
    python3 scraper.py --imdb   # daarna IMDb-nummers opzoeken via OMDb (vereist
                                # OMDB_API_KEY) en actuele scores ophalen bij IMDb
    python3 scraper.py --justwatch   # JustWatch-pagina's opzoeken (kan samen met --imdb)
    python3 scraper.py --tmdb   # genres en beschrijvingen via TMDB (vereist TMDB_API_KEY)
    python3 scraper.py --trakt  # Trakt-pagina's opzoeken, na --tmdb (vereist TRAKT_CLIENT_ID)

Na de eerste volledige run haalt het script alleen nog nieuwe recensies op:
het stopt bij de eerste pagina waarop een al bekende recensie staat. Bestaande
recensies worden niet opnieuw opgevraagd.
"""

import gzip
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL  = 'https://www.cinemagazine.nl/wp-json/wp/v2/posts'
FIELDS    = 'id,title,date,link,content'
PER_PAGE  = 100
DELAY     = 0.4   # seconden tussen requests (wees beleefd)
OUTPUT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'reviews.json')
PLOTS     = os.path.join(os.path.dirname(OUTPUT), 'plots.json')   # beschrijvingen, apart: scheelt MB's
USER_AGENT = 'CinemagazineScraper/1.1 (+https://github.com/SjoerdjeBis/cinemagazine-database)'

OMDB_URL    = 'https://www.omdbapi.com/'
OMDB_BUDGET = 900   # max. OMDb-requests per run (gratis limiet: 1000 per dag)
OMDB_DELAY  = 0.15
IMDB_RATINGS_URL = 'https://datasets.imdbws.com/title.ratings.tsv.gz'   # dagelijks bijgewerkt

JW_URL        = 'https://apis.justwatch.com/graphql'
JW_BUDGET     = 1500   # max. JustWatch-requests per run
JW_DELAY      = 0.2
JW_MIN_RATING = 4      # de site toont alleen bij deze films een JustWatch-link
JW_RECHECK_AGE   = 365   # niet-gevonden films uit recensies van max. zo oud (dagen)...
JW_RECHECK_EVERY = 7     # ...om de zoveel dagen opnieuw proberen

TMDB_URL    = 'https://api.themoviedb.org/3'
TMDB_BUDGET = 3000   # max. TMDB-requests per run
TMDB_DELAY  = 0.05

TRAKT_URL        = 'https://api.trakt.tv'
TRAKT_BUDGET     = 900    # max. Trakt-requests per run (limiet: 1000 per 5 minuten)
TRAKT_DELAY      = 0.35
TRAKT_MIN_RATING = 4      # zelfde films als de JustWatch-link
TRAKT_MAX_SECONDS = 600  # nooit langer dan 10 minuten, ook als Trakt ons laat wachten

YEAR_SUFFIX = re.compile(r'\s*\((\d{4})\)[^(]*$')


def strip_tags(s):
    return re.sub(r'<[^>]+>', '', s)


def is_review(link):
    # Interviews, lijstjes e.d. hebben geen '-recensie' in de URL
    return bool(re.search(r'-recensie/?$', link))


def fetch_page(page, retries=3):
    url = (f'{BASE_URL}?per_page={PER_PAGE}&_fields={FIELDS}'
           f'&orderby=date&order=desc&page={page}')
    req = Request(url, headers={'User-Agent': USER_AGENT})
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=30) as resp:
                total = int(resp.headers.get('X-WP-Total', 0))
                pages = int(resp.headers.get('X-WP-TotalPages', 1))
                posts = json.loads(resp.read())
            return posts, total, pages
        except (URLError, HTTPError) as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f'\n  Fout: {e} — wacht {wait}s en probeer opnieuw...')
                time.sleep(wait)
            else:
                raise


def parse_post(post):
    title = html.unescape(strip_tags(post['title']['rendered']))

    # Filmjaar: laatste "(YYYY)" in de titel
    year_match = YEAR_SUFFIX.search(title)
    film_year  = int(year_match.group(1)) if year_match else None

    # Waardering: "Waardering: N" of "Waardering: N,N" in de content
    content      = post.get('content', {}).get('rendered', '')
    rating_match = re.search(r'Waardering[:\s]+([0-9]+[.,]?[0-9]*)', content)
    rating = None
    if rating_match:
        try:
            rating = float(rating_match.group(1).replace(',', '.'))
        except ValueError:
            pass

    return {
        'id':       post['id'],
        'title':    title,
        'url':      post['link'],
        'date':     post['date'],   # ISO 8601 (Nederlandse tijd), bewaard als string
        'filmYear': film_year,
        'rating':   rating,
    }


def load():
    """Laad bestaande data. Geeft (reviews, complete, last_updated) terug."""
    if not os.path.exists(OUTPUT):
        return [], False, None
    with open(OUTPUT, encoding='utf-8') as f:
        data = json.load(f)
    # Opschonen van oudere data: HTML-entiteiten in titels, niet-recensies
    reviews = [dict(r, title=html.unescape(r['title']))
               for r in data.get('reviews', []) if is_review(r['url'])]
    if os.path.exists(PLOTS):
        with open(PLOTS, encoding='utf-8') as f:
            plots = json.load(f)
        for r in reviews:
            if str(r['id']) in plots:
                r['plot'] = plots[str(r['id'])]
    # Bestanden van vóór de 'complete'-vlag waren altijd volledige scrapes
    return reviews, data.get('complete', True), data.get('last_updated')


def save(reviews, complete, last_updated=None):
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    reviews.sort(key=lambda r: r['date'], reverse=True)
    head = {
        'last_updated': last_updated or datetime.now(timezone.utc).isoformat(),
        'complete':     complete,
        'total':        len(reviews),
    }
    # Eén recensie per regel: kleine, leesbare git-diffs
    lines = [json.dumps({k: v for k, v in r.items() if k != 'plot'},
                        ensure_ascii=False, separators=(',', ':')) for r in reviews]
    body  = json.dumps(head, ensure_ascii=False, separators=(',', ':'))[:-1]
    write_atomic(OUTPUT, body + ',"reviews":[\n' + ',\n'.join(lines) + '\n]}\n')

    plots = [json.dumps(str(r['id']), ensure_ascii=False) + ':' + json.dumps(r['plot'], ensure_ascii=False)
             for r in reviews if r.get('plot')]
    write_atomic(PLOTS, '{\n' + ',\n'.join(plots) + '\n}\n')


def write_atomic(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def scrape(reviews, complete):
    existing_ids = {r['id'] for r in reviews}
    if reviews:
        mode = 'volledig (hervatten)' if not complete else 'alleen nieuwe recensies'
        print(f'Bestaand bestand: {len(reviews)} recensies — {mode}')

    print('Verbinding maken met cinemagazine.nl...')
    new_found   = 0
    page        = 1
    total_pages = 1
    while page <= total_pages:
        posts, total, total_pages = fetch_page(page)
        if page == 1:
            print(f'Totaal op de site: {total} posts ({total_pages} pagina\'s)\n')

        seen_known = False
        for post in posts:
            if post['id'] in existing_ids:
                seen_known = True
                continue
            if not is_review(post['link']):
                continue
            reviews.append(parse_post(post))
            existing_ids.add(post['id'])
            new_found += 1

        print(f'\rPagina {page}/{total_pages} — {len(reviews)} recensies '
              f'({new_found} nieuw)', end='', flush=True)

        if complete and seen_known:
            break   # alles vanaf hier is al bekend
        if not complete and page % 5 == 0:
            save(reviews, complete=False)   # tussentijds opslaan bij volledige scrape
        page += 1
        time.sleep(DELAY)

    print(f'\n{new_found} nieuwe recensies gevonden.')
    return new_found


# ─── IMDb ─────────────────────────────────────────────────────────────────────
# Twee stappen:
#   1. OMDb zoekt per film het IMDb-nummer op (eenmalig per film, dagbudget).
#      Veld 'imdbId': ontbreekt = nog niet opgezocht, None = niet gevonden.
#   2. De scores komen uit IMDb's eigen dagelijkse dataset (title.ratings),
#      zodat ze altijd gelijk zijn aan wat IMDb nu toont. OMDb-scores zijn vaak
#      verouderd of ontbreken, daarom worden die niet gebruikt.

class OmdbStop(Exception):
    pass


def omdb_query(title, year, api_key):
    params = {'t': title, 'type': 'movie', 'apikey': api_key}
    if year:
        params['y'] = year
    req = Request(f'{OMDB_URL}?{urlencode(params)}', headers={'User-Agent': USER_AGENT})
    try:
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:   # OMDb geeft 401 bij ongeldige sleutel / limiet
        try:
            data = json.loads(e.read())
        except ValueError:
            raise OmdbStop(f'HTTP {e.code}')
    if data.get('Response') == 'True':
        return data
    error = data.get('Error', '')
    if 'not found' in error.lower():
        return None
    raise OmdbStop(error or 'onbekende fout')


def simplify(s):
    s = unicodedata.normalize('NFKD', s.lower())
    return re.sub(r'[^a-z0-9]', '', ''.join(c for c in s if not unicodedata.combining(c)))


def omdb_match_ok(hit, title, year):
    """Controleer of het OMDb-resultaat echt deze film is."""
    m = re.match(r'(\d{4})', hit.get('Year', ''))
    if year:
        return bool(m) and abs(int(m.group(1)) - year) <= 1
    # Zonder filmjaar alleen bij exact dezelfde titel
    return simplify(hit.get('Title', '')) == simplify(title)


def imdb_candidates(title):
    """Zoektitels: volledige titel, dan de losse delen van 'NL titel – originele titel'."""
    clean = YEAR_SUFFIX.sub('', title).strip()
    parts = [p.strip() for p in re.split(r'\s+[–—-]\s+', clean) if p.strip()]
    return [clean] + (parts if len(parts) > 1 else [])


def lookup_imdb_ids(reviews, api_key, budget=OMDB_BUDGET):
    todo = [r for r in reviews if 'imdbId' not in r]
    print(f'IMDb-nummers: {len(todo)} recensies nog niet opgezocht (budget {budget} requests)')
    used = found = 0
    try:
        for r in todo:   # reviews staan op datum, nieuwste eerst
            tries = imdb_candidates(r['title'])
            if used + len(tries) > budget:
                break
            imdb_id = None
            for t in tries:
                used += 1
                hit = omdb_query(t, r['filmYear'], api_key)
                time.sleep(OMDB_DELAY)
                if hit and omdb_match_ok(hit, t, r['filmYear']):
                    imdb_id = hit.get('imdbID')
                    break
            r['imdbId'] = imdb_id
            found += imdb_id is not None
    except OmdbStop as e:
        print(f'IMDb-nummers: gestopt — OMDb meldt: {e}')
    except (URLError, TimeoutError) as e:
        print(f'IMDb-nummers: gestopt — netwerkfout: {e}')
    print(f'IMDb-nummers: {used} requests, {found} gevonden')


def update_imdb_ratings(reviews):
    """Zet de actuele IMDb-score en het aantal stemmen bij elke gekoppelde film."""
    wanted = {r['imdbId'] for r in reviews if r.get('imdbId')}
    if not wanted:
        return
    print(f'IMDb-scores: dataset ophalen voor {len(wanted)} films...')
    ratings = {}
    req = Request(IMDB_RATINGS_URL, headers={'User-Agent': USER_AGENT})
    with urlopen(req, timeout=120) as resp, \
         gzip.open(resp, mode='rt', encoding='utf-8') as f:
        next(f)   # kopregel: tconst, averageRating, numVotes
        for line in f:
            tconst, avg, votes = line.rstrip('\n').split('\t')
            if tconst in wanted:
                ratings[tconst] = (float(avg), int(votes))
    if len(ratings) < len(wanted) * 0.5:
        # Onverwacht weinig treffers: liever niets aanpassen dan scores wissen
        print(f'IMDb-scores: slechts {len(ratings)} treffers — overgeslagen')
        return
    for r in reviews:
        if r.get('imdbId'):
            rating, votes = ratings.get(r['imdbId'], (None, None))
            r['imdb'], r['imdbVotes'] = rating, votes
        else:
            r.pop('imdb', None)
            r.pop('imdbVotes', None)
    print(f'IMDb-scores: {len(ratings)} films met score')


def urlopen_retry(req, retries=4):
    """Response-body; bij netwerkstoringen en 429/5xx even wachten en opnieuw."""
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=20) as resp:
                return resp.read()
        except HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
        except (URLError, TimeoutError):
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt * 2)


# ─── JustWatch ────────────────────────────────────────────────────────────────
# Per goed beoordeelde film de échte JustWatch-pagina opzoeken, zodat de site
# daar direct naartoe linkt. JustWatch' zoekfunctie vindt kleinere films vaak
# niet, en het jaartal wijkt soms af (festivaljaar vs. releasejaar).
# Veld 'jwPath': ontbreekt = nog niet opgezocht, None = niet gevonden,
# '' = wel bij JustWatch maar zonder Nederlandse pagina (nergens te zien in NL).

class JustWatchStop(Exception):
    pass


def jw_query(query, variables):
    body = json.dumps({'query': query, 'variables': variables}).encode()
    req = Request(JW_URL, data=body, headers={'User-Agent': USER_AGENT,
                                              'Content-Type': 'application/json'})
    try:
        data = json.loads(urlopen_retry(req))
    except HTTPError as e:
        raise JustWatchStop(f'HTTP {e.code}')
    time.sleep(JW_DELAY)
    return data


JW_CONTENT = ('objectType content(country:NL,language:"nl"){'
              'title originalTitle originalReleaseYear fullPath externalIds{imdbId}}')


def jw_search(title):
    q = ('query($f:TitleFilter){popularTitles(country:NL,first:10,filter:$f)'
         '{edges{node{' + JW_CONTENT + '}}}}')
    data = jw_query(q, {'f': {'searchQuery': title, 'objectTypes': ['MOVIE']}})
    edges = ((data.get('data') or {}).get('popularTitles') or {}).get('edges') or []
    return [e['node'] for e in edges]


def jw_by_path(path):
    q = 'query($p:String!){urlV2(fullPath:$p){node{... on MovieOrShow{' + JW_CONTENT + '}}}}'
    data = jw_query(q, {'p': path})
    node = ((data.get('data') or {}).get('urlV2') or {}).get('node')
    return [node] if node and node.get('content') else []


def slugify(s):
    s = unicodedata.normalize('NFKD', s.lower().replace('&', 'and'))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


def jw_match(node, title, year, imdb_id):
    """Controleer of het JustWatch-resultaat echt deze film is."""
    c = node['content']
    if node.get('objectType') != 'MOVIE':
        return False
    if imdb_id and (c.get('externalIds') or {}).get('imdbId') == imdb_id:
        return True
    names = {simplify(c.get('title') or ''), simplify(c.get('originalTitle') or '')}
    if simplify(title) not in names:
        return False
    jw_year = c.get('originalReleaseYear')
    return not year or not jw_year or abs(jw_year - year) <= 1


def jw_find(r, budget_left):
    """Geeft (fullPath of None, gebruikte requests). '' = wel bij JustWatch, geen NL-pagina."""
    year, imdb_id = r['filmYear'], r.get('imdbId')
    used = 0
    for t in imdb_candidates(r['title']):
        # Eerst zoeken, dan de URL raden: /nl/movie/<titel>[-<jaar>]
        slug  = slugify(t)
        years = [year, year + 1, year - 1] if year else []
        tries = [lambda: jw_search(t), lambda: jw_by_path(f'/nl/movie/{slug}')]
        tries += [lambda y=y: jw_by_path(f'/nl/movie/{slug}-{y}') for y in years]
        for attempt in tries:
            if used >= budget_left:
                raise JustWatchStop('budget op')
            used += 1
            hit = next((n for n in attempt() if jw_match(n, t, year, imdb_id)), None)
            if hit:
                return hit['content']['fullPath'] or '', used
    return None, used


def jw_due(r, today):
    if r['rating'] is None or r['rating'] < JW_MIN_RATING:
        return False
    if 'jwPath' not in r:
        return True
    # Geen (NL-)pagina gevonden: recente recensies wekelijks opnieuw proberen,
    # want nieuwe films komen vaak pas later bij JustWatch of op streamingdiensten
    if r['jwPath'] or r['date'][:10] < (today - timedelta(days=JW_RECHECK_AGE)).isoformat():
        return False
    checked = r.get('jwChecked')
    return not checked or checked < (today - timedelta(days=JW_RECHECK_EVERY)).isoformat()


def lookup_justwatch(reviews, budget=JW_BUDGET):
    today = datetime.now(timezone.utc).date()
    todo  = [r for r in reviews if jw_due(r, today)]   # nieuwste eerst
    print(f'JustWatch: {len(todo)} films op te zoeken (budget {budget} requests)')
    used = found = 0
    try:
        for r in todo:
            path, n = jw_find(r, budget - used)
            used += n
            r['jwPath'] = path
            if path:
                r.pop('jwChecked', None)
                found += 1
            else:
                r['jwChecked'] = today.isoformat()
    except JustWatchStop as e:
        print(f'JustWatch: gestopt — {e}')
    except (URLError, TimeoutError, ValueError) as e:
        print(f'JustWatch: gestopt — netwerkfout: {e}')
    print(f'JustWatch: {used} requests, {found} met NL-pagina gevonden')


# ─── TMDB ─────────────────────────────────────────────────────────────────────
# Genres en een korte beschrijving (Nederlands als TMDB die heeft, anders Engels).
# Koppeling via het IMDb-nummer; zonder IMDb-nummer op titel, met het jaar max.
# 1 jaar ernaast. Veld 'tmdbId': ontbreekt = nog niet opgezocht, None = niet gevonden.

class TmdbStop(Exception):
    pass


def tmdb_get(path, api_key, **params):
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    if len(api_key) > 40:   # v4 'API Read Access Token'
        headers['Authorization'] = f'Bearer {api_key}'
    else:                   # v3 API-sleutel
        params['api_key'] = api_key
    req = Request(f'{TMDB_URL}{path}?{urlencode(params)}', headers=headers)
    try:
        data = json.loads(urlopen_retry(req))
    except HTTPError as e:
        if e.code == 404:
            return None
        raise TmdbStop(f'HTTP {e.code}')
    time.sleep(TMDB_DELAY)
    return data


ABBREVIATIONS = {'dr', 'mr', 'mrs', 'ms', 'st', 'jr', 'sr', 'prof', 'mevr', 'dhr', 'ca', 'vs', 'nr', 'bv', 'o.a', 'm.a.w'}


def first_sentences(text, n=2):
    """De eerste n zinnen; 'Dr.', 'J.' e.d. tellen niet als zinseinde."""
    text, out, start = text.strip(), [], 0
    for m in re.finditer(r'[.!?…]+["”’)]?\s+(?=[A-ZÀ-Ý0-9"“‘\'(])', text):
        word = re.split(r'\s', text[start:m.start()])[-1].lower()
        if text[m.start()] == '.' and (word in ABBREVIATIONS or len(word) == 1):
            continue
        out.append(text[start:m.end()].strip())
        start = m.end()
        if len(out) == n:
            return ' '.join(out)
    return ' '.join(out + [text[start:]]).strip()


def tmdb_year(hit):
    m = re.match(r'(\d{4})', hit.get('release_date') or '')
    return int(m.group(1)) if m else None


def tmdb_find(r, api_key):
    """Geeft (TMDB-film of None, gebruikte requests)."""
    if r.get('imdbId'):
        data = tmdb_get(f"/find/{r['imdbId']}", api_key,
                        external_source='imdb_id', language='nl-NL') or {}
        hits = data.get('movie_results') or []
        return (hits[0] if hits else None), 1
    used, year = 0, r['filmYear']
    # Ook in het Engels: zonder NL-titel toont TMDB de originele, soms in een ander schrift
    for t in imdb_candidates(r['title']):
        for lang in ('nl-NL', 'en-US'):
            used += 1
            data = tmdb_get('/search/movie', api_key, query=t, language=lang) or {}
            for hit in data.get('results') or []:
                names = {simplify(hit.get('title') or ''), simplify(hit.get('original_title') or '')}
                y = tmdb_year(hit)
                if simplify(t) in names and (not year or not y or abs(y - year) <= 1):
                    if lang != 'nl-NL':   # Nederlandse tekst en genres alsnog ophalen
                        used += 1
                        hit = dict(tmdb_get(f"/movie/{hit['id']}", api_key, language='nl-NL') or hit,
                                   genre_ids=hit.get('genre_ids', []))
                    return hit, used
    return None, used


def tmdb_genre_names(api_key):
    genres = tmdb_get('/genre/movie/list', api_key, language='nl') or {}
    return {g['id']: g['name'] for g in genres.get('genres', [])}


def tmdb_fill(r, api_key, names):
    """Zoekt één film op en vult tmdbId, genres en plot. Geeft het aantal requests."""
    hit, used = tmdb_find(r, api_key)
    r['tmdbId'] = hit['id'] if hit else None
    if not hit:
        return used
    r['genres'] = [names[g] for g in hit.get('genre_ids', []) if g in names]
    overview = hit.get('overview') or ''
    if not overview:   # geen Nederlandse tekst: dan de Engelse
        used += 1
        overview = (tmdb_get(f"/movie/{hit['id']}", api_key, language='en-US') or {}).get('overview') or ''
    if overview:
        r['plot'] = first_sentences(overview)
    return used


def lookup_tmdb(reviews, api_key, budget=TMDB_BUDGET):
    todo = [r for r in reviews if 'tmdbId' not in r]   # nieuwste eerst
    print(f'TMDB: {len(todo)} films nog niet opgezocht (budget {budget} requests)')
    used = found = 0
    try:
        names = tmdb_genre_names(api_key)
        used += 1
        for r in todo:
            if used + 8 > budget:
                break
            n = tmdb_fill(r, api_key, names)
            used += n
            found += bool(r['tmdbId'])
    except TmdbStop as e:
        print(f'TMDB: gestopt — {e}')
    except (URLError, TimeoutError, ValueError) as e:
        print(f'TMDB: gestopt — netwerkfout: {e}')
    print(f'TMDB: {used} requests, {found} gevonden')


# ─── Trakt ────────────────────────────────────────────────────────────────────
# De Trakt-pagina van een film, zodat je hem vanaf de site op je Trakt-watchlist
# (en daarmee in Stremio) zet. Opzoeken via het TMDB-nummer; zonder slug linkt
# de site naar een zoekopdracht op titel.
# Veld 'traktSlug': ontbreekt = nog niet opgezocht, None = niet gevonden.

class TraktStop(Exception):
    pass


def trakt_slug(tmdb_id, client_id):
    req = Request(f'{TRAKT_URL}/search/tmdb/{tmdb_id}?type=movie',
                  headers={'User-Agent': USER_AGENT, 'Content-Type': 'application/json',
                           'trakt-api-version': '2', 'trakt-api-key': client_id})
    for attempt in range(4):
        try:
            hits = json.loads(urlopen_retry(req))
            break
        except HTTPError as e:
            if e.code == 404:
                return None
            if e.code != 429 or attempt == 3:
                raise TraktStop(f'HTTP {e.code}')
            # Te snel: Trakt zegt hoe lang we moeten wachten
            time.sleep(min(int(e.headers.get('Retry-After') or 30), 120) + 1)
    time.sleep(TRAKT_DELAY)
    movie = next((h.get('movie') for h in hits if h.get('type') == 'movie'), None)
    return (movie or {}).get('ids', {}).get('slug')


def lookup_trakt(reviews, client_id, budget=TRAKT_BUDGET):
    todo = [r for r in reviews if 'traktSlug' not in r and r.get('tmdbId')
            and r['rating'] is not None and r['rating'] >= TRAKT_MIN_RATING]
    print(f'Trakt: {len(todo)} films nog niet opgezocht (budget {budget} requests)')
    used = found = 0
    try:
        deadline = time.time() + TRAKT_MAX_SECONDS
        for r in todo[:budget]:   # nieuwste eerst
            if time.time() > deadline:
                print('Trakt: tijd op — de rest volgt bij de volgende run')
                break
            used += 1
            r['traktSlug'] = trakt_slug(r['tmdbId'], client_id)
            found += r['traktSlug'] is not None
    except TraktStop as e:
        print(f'Trakt: gestopt — {e}')
    except (URLError, TimeoutError, ValueError) as e:
        print(f'Trakt: gestopt — netwerkfout: {e}')
    print(f'Trakt: {used} requests, {found} gevonden')


def main():
    reviews, complete, last_updated = load()
    before = json.dumps(reviews, sort_keys=True)

    new_found = scrape(reviews, complete)

    if '--imdb' in sys.argv:
        api_key = os.environ.get('OMDB_API_KEY', '').strip()
        if api_key:
            lookup_imdb_ids(reviews, api_key)
        else:
            print('IMDb-nummers: overgeslagen — OMDB_API_KEY is niet ingesteld')
        try:
            update_imdb_ratings(reviews)
        except (URLError, OSError, ValueError) as e:
            print(f'IMDb-scores: overgeslagen — {e}')

    if '--tmdb' in sys.argv:
        api_key = os.environ.get('TMDB_API_KEY', '').strip()
        if api_key:
            lookup_tmdb(reviews, api_key)
        else:
            print('TMDB: overgeslagen — TMDB_API_KEY is niet ingesteld')

    if '--trakt' in sys.argv:
        client_id = os.environ.get('TRAKT_CLIENT_ID', '').strip()
        if client_id:
            lookup_trakt(reviews, client_id)
        else:
            print('Trakt: overgeslagen — TRAKT_CLIENT_ID is niet ingesteld')

    if '--justwatch' in sys.argv:
        lookup_justwatch(reviews)

    # Datum alleen verversen als er nieuwe recensies zijn
    changed = json.dumps(reviews, sort_keys=True) != before
    save(reviews, complete=True, last_updated=None if new_found else last_updated)
    print(f'\nKlaar! {len(reviews)} recensies in {OUTPUT}'
          + ('' if changed else ' (geen wijzigingen)'))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Cinemagazine recensies scraper
Haalt recensies op via de WordPress REST API en slaat ze op als data/reviews.json.

Gebruik:
    python3 scraper.py           # nieuwe recensies ophalen
    python3 scraper.py --tmdb    # films koppelen aan TMDB: IMDb-nummer, genres en
                                 # beschrijving (vereist TMDB_API_KEY)
    python3 scraper.py --imdb    # actuele IMDb-scores uit IMDb's dagelijkse dataset
    python3 scraper.py --trakt   # Trakt-pagina's opzoeken, na --tmdb (vereist TRAKT_CLIENT_ID)

Eenmalig of bij twijfel:
    --full      de hele site opnieuw doorlopen: regisseur, speelduur, cijfer en
                titel van bestaande recensies bijwerken
    --rematch   alle films opnieuw aan TMDB koppelen, zonder budget en met
                meerdere verbindingen tegelijk

Na de eerste volledige run haalt het script alleen nog nieuwe recensies op:
het stopt bij de eerste pagina waarop een al bekende recensie staat.
"""

import difflib
import gzip
import html
import http.client
import json
import os
import re
import sys
import threading
import time
import traceback
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL  = 'https://www.cinemagazine.nl/wp-json/wp/v2/posts'
FIELDS    = 'id,title,date,link,content'
PER_PAGE  = 100   # bij de dagelijkse run 20: lichter voor de site, en er zijn zelden meer nieuwe
PER_PAGE_NEW = 20
DELAY     = 0.4   # seconden tussen requests (wees beleefd)
DATA_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
OUTPUT    = os.path.join(DATA_DIR, 'reviews.json')
PLOTS     = os.path.join(DATA_DIR, 'plots.json')       # beschrijvingen, apart: scheelt MB's
OVERRIDES = os.path.join(DATA_DIR, 'overrides.json')   # handmatige koppelingen
USER_AGENT = 'CinemagazineScraper/2.0 (+https://github.com/SjoerdjeBis/cinemagazine-database)'

TMDB_URL          = 'https://api.themoviedb.org/3'
TMDB_BUDGET       = 3000   # max. TMDB-requests per run
TMDB_DELAY        = 0.05
TMDB_MAX_CANDIDATES = 4    # zoveel titeltreffers per film controleren op regisseur
TMDB_RECHECK_AGE   = 365   # niet-gevonden films uit recensies van max. zo oud (dagen)...
TMDB_RECHECK_EVERY = 30    # ...om de zoveel dagen opnieuw proberen (TMDB groeit)
REMATCH_WORKERS    = 8     # gelijktijdige verbindingen bij --rematch

IMDB_RATINGS_URL = 'https://datasets.imdbws.com/title.ratings.tsv.gz'   # dagelijks bijgewerkt

TRAKT_URL         = 'https://api.trakt.tv'
TRAKT_BUDGET      = 900    # max. Trakt-requests per run (limiet: 1000 per 5 minuten)
TRAKT_DELAY       = 0.35
TRAKT_MIN_RATING  = 4      # de site toont alleen bij deze films een Trakt-knop
TRAKT_MAX_SECONDS = 600    # nooit langer dan 10 minuten, ook als Trakt ons laat wachten

# Velden die bij een (nieuwe) TMDB-koppeling horen
MATCH_FIELDS = ('tmdbId', 'imdbId', 'imdb', 'imdbVotes', 'genres', 'plot',
                'traktSlug', 'via', 'checked')

YEAR_SLACK_SAME_DIRECTOR = 3   # jaar mag zoveel afwijken als de regisseur exact klopt

YEAR_SUFFIX = re.compile(r'\s*\((\d{4})\)[^(]*$')


def strip_tags(s):
    return re.sub(r'<[^>]+>', '', s)


def is_review(link):
    # Interviews, lijstjes e.d. hebben geen '-recensie' in de URL
    return bool(re.search(r'-recensie/?$', link))


def simplify(s):
    s = unicodedata.normalize('NFKD', (s or '').lower())
    return re.sub(r'[^a-z0-9]', '', ''.join(c for c in s if not unicodedata.combining(c)))


def fetch_page(page, per_page=PER_PAGE, retries=7):
    url = (f'{BASE_URL}?per_page={per_page}&_fields={FIELDS}'
           f'&orderby=date&order=desc&page={page}')
    req = Request(url, headers={'User-Agent': USER_AGENT})
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get('X-WP-Total', 0))
                pages = int(resp.headers.get('X-WP-TotalPages', 1))
                posts = json.loads(resp.read())
            return posts, total, pages
        # OSError: alle netwerkfouten en time-outs (ook tijdens het lezen);
        # ValueError: de site gaf soms een lege of HTML-pagina i.p.v. JSON
        except (OSError, http.client.HTTPException, ValueError) as e:
            if attempt < retries - 1:
                wait = min(5 * 2 ** attempt, 120)   # samen ruim 4 minuten
                print(f'\n  Fout: {e} — wacht {wait}s en probeer opnieuw...')
                time.sleep(wait)
            else:
                raise


def parse_post(post):
    title = html.unescape(strip_tags(post['title']['rendered']))

    # Filmjaar: laatste "(YYYY)" in de titel
    year_match = YEAR_SUFFIX.search(title)
    film_year  = int(year_match.group(1)) if year_match else None

    content = post.get('content', {}).get('rendered', '')

    # Waardering: "Waardering: N" of "Waardering: N,N" in de content
    rating_match = re.search(r'Waardering[:\s]+([0-9]+[.,]?[0-9]*)', content)
    rating = None
    if rating_match:
        try:
            rating = float(rating_match.group(1).replace(',', '.'))
        except ValueError:
            pass

    # Elke recensie begint met "Regie: … | 95 minuten | genres | Acteurs: …"
    head = re.sub(r'\s+', ' ', html.unescape(strip_tags(content[:3000])))
    director_match = re.search(r'Regie\s*:\s*([^|]+?)\s*\|', head)
    runtime_match  = re.search(r'\|\s*(\d+)\s*min', head)

    return {
        'id':       post['id'],
        'title':    title,
        'url':      post['link'],
        'date':     post['date'],   # ISO 8601 (Nederlandse tijd), bewaard als string
        'filmYear': film_year,
        'rating':   rating,
        'director': director_match.group(1).strip() if director_match else None,
        'runtime':  int(runtime_match.group(1)) if runtime_match else None,
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
    for r in reviews:   # JustWatch wordt niet meer gebruikt
        r.pop('jwPath', None)
        r.pop('jwChecked', None)
    if os.path.exists(PLOTS):
        with open(PLOTS, encoding='utf-8') as f:
            plots = json.load(f)
        for r in reviews:
            if str(r['id']) in plots:
                r['plot'] = plots[str(r['id'])]
    # Bestanden van vóór de 'complete'-vlag waren altijd volledige scrapes
    return reviews, data.get('complete', True), data.get('last_updated')


def load_overrides():
    """{recensie-id: TMDB-nummer of None}. In het bestand mag ook
    {"tmdb": nummer, "waarom": "..."} staan; null = geen film op TMDB."""
    if not os.path.exists(OVERRIDES):
        return {}
    with open(OVERRIDES, encoding='utf-8') as f:
        data = json.load(f)
    return {int(k): (v.get('tmdb') if isinstance(v, dict) else v)
            for k, v in data.items() if not k.startswith('_')}


def save(reviews, complete, last_updated=None):
    os.makedirs(DATA_DIR, exist_ok=True)
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


def reset_match(r):
    for k in MATCH_FIELDS:
        r.pop(k, None)


def scrape(reviews, complete, full=False):
    """Nieuwe recensies toevoegen. Met full=True ook alle bestaande bijwerken;
    verandert daarbij de titel, het jaar of de regisseur, dan wordt de film
    opnieuw gekoppeld."""
    by_id = {r['id']: r for r in reviews}
    if reviews:
        mode = ('volledig (bijwerken)' if full else
                'volledig (hervatten)' if not complete else 'alleen nieuwe recensies')
        print(f'Bestaand bestand: {len(reviews)} recensies — {mode}')

    print('Verbinding maken met cinemagazine.nl...')
    new_found = updated = 0
    page        = 1
    total_pages = 1
    while page <= total_pages:
        posts, total, total_pages = fetch_page(page, PER_PAGE_NEW if complete and not full else PER_PAGE)
        if page == 1:
            print(f'Totaal op de site: {total} posts ({total_pages} pagina\'s)\n')

        seen_known = False
        for post in posts:
            if not is_review(post['link']):
                continue
            if post['id'] in by_id:
                seen_known = True
                if full:
                    r, fresh = by_id[post['id']], parse_post(post)
                    if any(r.get(k) != fresh[k] for k in ('title', 'filmYear', 'director')) \
                            and 'director' in r:   # eerste keer regisseur invullen telt niet
                        reset_match(r)
                    if any(r.get(k) != v for k, v in fresh.items()):
                        r.update(fresh)
                        updated += 1
                continue
            r = parse_post(post)
            reviews.append(r)
            by_id[r['id']] = r
            new_found += 1

        print(f'\rPagina {page}/{total_pages} — {len(reviews)} recensies '
              f'({new_found} nieuw, {updated} bijgewerkt)', end='', flush=True)

        if complete and seen_known and not full:
            break   # alles vanaf hier is al bekend
        if (full or not complete) and page % 5 == 0:
            save(reviews, complete=complete)   # tussentijds opslaan bij volledige scrape
        page += 1
        time.sleep(DELAY)

    print(f'\n{new_found} nieuwe recensies gevonden.')
    return new_found


def urlopen_retry(req, retries=4):
    """Response-body; bij netwerkstoringen en 429/5xx even wachten en opnieuw."""
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=20) as resp:
                return resp.read()
        except HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
        except (OSError, http.client.HTTPException):   # netwerkfouten, time-outs
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt * 2)


# ─── TMDB ─────────────────────────────────────────────────────────────────────
# TMDB is de spil: per recensie wordt de film daar gezocht en gecontroleerd met
# de regisseur uit de recensie. Uit de TMDB-film komen het IMDb-nummer, de
# genres en de beschrijving (Nederlands als die er is, anders Engels).
#
# Zoeken: eerst op titel (Nederlands, Engels en de losse delen van
# 'NL titel – originele titel'), jaar max. 1 ernaast (max. 3 als de regisseur
# exact klopt: festivaljaar vs. releasejaar). Klopt bij geen van die
# treffers de regisseur, dan via de films die de regisseur maakte; dat vindt
# ook films die op TMDB een heel andere titel hebben. Zonder overeenkomende
# regisseur alleen koppelen als er niets tegenspreekt (één treffer, of TMDB
# kent de regisseur niet); liever geen koppeling dan een verkeerde.
#
# Velden: 'tmdbId' ontbreekt = nog niet opgezocht, None = niet gevonden
# ('checked' = datum van de laatste poging). 'via' staat er alleen als de
# koppeling niet op de regisseur berust: 'titel' of 'hand' (overrides.json).

class TmdbStop(Exception):
    pass


class Budget:
    """Telt requests, ook over meerdere threads (bij --rematch)."""
    def __init__(self, limit):
        self.limit, self.used, self.lock = limit, 0, threading.Lock()

    def take(self):
        with self.lock:
            if self.limit is not None and self.used >= self.limit:
                raise TmdbStop('budget op')
            self.used += 1


def tmdb_get(path, api_key, budget, **params):
    budget.take()
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


def title_candidates(title):
    """Zoektitels: volledige titel, dan de losse delen van 'NL titel – originele titel'."""
    clean = YEAR_SUFFIX.sub('', title).strip()
    parts = [p.strip() for p in re.split(r'\s+[–—-]\s+', clean) if p.strip()]
    return [clean] + (parts if len(parts) > 1 else [])


def year_of(movie):
    m = re.match(r'(\d{4})', movie.get('release_date') or '')
    return int(m.group(1)) if m else None


def year_ok(movie, year, slack=1):
    y = year_of(movie)
    return not year or not y or abs(y - year) <= slack


def title_ok(movie, title, loose=False):
    """Titel gelijk; loose: of de ene titel bevat de andere (Piranha / Piranha 3D)."""
    names = {simplify(movie.get('title')), simplify(movie.get('original_title'))} - {''}
    for t in map(simplify, title_candidates(title)):
        if t in names or (loose and len(t) >= 4 and any(t in n or n in t for n in names if len(n) >= 4)):
            return True
    return False


def split_names(s):
    return [n for n in re.split(r'\s*(?:,|&|/|\+|\ben\b|\band\b)\s*', s or '') if simplify(n)]


def same_person(a, b, lenient=False):
    """Dezelfde naam, ook met andere spelling of volgorde (Zhang Yimou / Yimou
    Zhang, Kore-eda / Koreeda, Zvjagintsev / Zvyagintsev, Felipe Gálvez /
    Felipe Gálvez Haberle). lenient: als titel en jaar al kloppen is een
    gedeelde naam genoeg (Ji-Woon Kim / Kim Jee-woon, Zeke / Ezekiel Norton);
    twee films met dezelfde titel hebben vrijwel nooit zulke regisseurs."""
    ta = [simplify(t) for t in re.split(r'[\s\-.]+', a) if simplify(t)]
    tb = [simplify(t) for t in re.split(r'[\s\-.]+', b) if simplify(t)]
    if not ta or not tb:
        return False
    ja, jb = ''.join(sorted(ta)), ''.join(sorted(tb))
    if ja == jb or simplify(a) == simplify(b):
        return True
    ratio = difflib.SequenceMatcher(None, ja, jb).ratio()
    small, big = sorted((set(ta), set(tb)), key=len)
    if ratio >= 0.85 or (len(small) >= 2 and small <= big):
        return True
    if lenient:
        return ratio >= 0.7 or any(len(t) >= 3 for t in set(ta) & set(tb))
    # Alleen de achternaam overeenkomend (bv. 'J. Lee Thompson'), mits lang genoeg
    return len(ta[-1]) >= 4 and ta[-1] == tb[-1] and ta[0][0] == tb[0][0]


def directors_of(details):
    return [c['name'] for c in (details.get('credits') or {}).get('crew', [])
            if c.get('job') == 'Director']


def director_match(r, details, lenient=False):
    """True/False, of None als een van beide geen regisseur kent."""
    ours, theirs = split_names(r.get('director')), directors_of(details)
    if not ours or not theirs:
        return None
    return any(same_person(a, b, lenient) for a in ours for b in theirs)


def tmdb_details(movie_id, api_key, budget):
    # In het Engels: in Nederlandstalige antwoorden staan namen soms in het
    # oorspronkelijke schrift (宮崎駿 i.p.v. Hayao Miyazaki). De Nederlandse
    # beschrijving komt uit de meegestuurde vertalingen.
    return tmdb_get(f'/movie/{movie_id}', api_key, budget, language='en-US',
                    append_to_response='credits,external_ids,translations')


_genre_names, _genre_lock = None, threading.Lock()


def genre_names(api_key, budget):
    """TMDB-genrenummers → Nederlandse namen (één keer per run opgehaald)."""
    global _genre_names
    with _genre_lock:
        if _genre_names is None:
            data = tmdb_get('/genre/movie/list', api_key, budget, language='nl') or {}
            _genre_names = {g['id']: g['name'] for g in data.get('genres', [])}
    return _genre_names


def dutch_overview(details):
    for t in (details.get('translations') or {}).get('translations', []):
        if t.get('iso_639_1') == 'nl' and (t.get('data') or {}).get('overview'):
            return t['data']['overview']
    return ''


def search_by_title(r, api_key, budget):
    """TMDB-films met deze titel en (bijna) dit jaar, meest waarschijnlijke eerst."""
    found = {}
    for t in title_candidates(r['title']):
        # Ook in het Engels: zonder NL-titel toont TMDB de originele, soms in een ander schrift
        for lang in ('nl-NL', 'en-US'):
            data = tmdb_get('/search/movie', api_key, budget, query=t, language=lang) or {}
            for hit in data.get('results') or []:
                if hit['id'] not in found and title_ok(hit, r['title']) \
                        and year_ok(hit, r['filmYear'], YEAR_SLACK_SAME_DIRECTOR):
                    found[hit['id']] = hit
        if found:
            break   # de volledige titel leverde al iets op
    exact = lambda h: year_of(h) == r['filmYear']
    return sorted(found.values(), key=lambda h: (not exact(h), -(h.get('popularity') or 0)))


def search_by_director(r, api_key, budget):
    """Films uit (bijna) dit jaar van de regisseur uit de recensie."""
    found = {}
    for name in split_names(r.get('director'))[:2]:
        people = (tmdb_get('/search/person', api_key, budget, query=name) or {}).get('results') or []
        people = [p for p in people if same_person(name, p.get('name') or '')][:2]
        for p in people:
            credits = tmdb_get(f"/person/{p['id']}/movie_credits", api_key, budget) or {}
            for c in credits.get('crew') or []:
                if c.get('job') == 'Director' and r['filmYear'] and year_of(c) \
                        and abs(year_of(c) - r['filmYear']) <= 1:
                    found[c['id']] = c
    return list(found.values())


def runtime_ok(r, details, slack=5):
    a, b = r.get('runtime'), details.get('runtime')
    return not a or not b or abs(a - b) <= slack


def tmdb_match(r, api_key, budget):
    """Geeft (details van de TMDB-film, 'regie' of 'titel') of (None, None)."""
    hits = search_by_title(r, api_key, budget)[:TMDB_MAX_CANDIDATES + 2]
    checked = []
    for hit in hits:
        d = tmdb_details(hit['id'], api_key, budget)
        if not d:
            continue
        if year_ok(hit, r['filmYear']):
            if director_match(r, d, lenient=True):   # titel en jaar kloppen al
                return d, 'regie'
            checked.append(d)
        elif director_match(r, d):   # festivaljaar vs. releasejaar: dan streng op regisseur
            return d, 'regie'

    if r.get('director'):
        tried = {d['id'] for d in checked}
        films = [f for f in search_by_director(r, api_key, budget) if f['id'] not in tried]
        films.sort(key=lambda f: (not title_ok(f, r['title'], loose=True), year_of(f) != r['filmYear']))
        for f in films[:3]:
            # Zonder (deels) overeenkomende titel alleen als het de enige film van
            # de regisseur in die jaren is én de speelduur bekend is en klopt
            if not title_ok(f, r['title'], loose=True) and len(films) > 1:
                continue
            d = tmdb_details(f['id'], api_key, budget)
            if d and director_match(r, d) and (
                    title_ok(f, r['title'], loose=True)
                    or (r.get('runtime') and d.get('runtime') and runtime_ok(r, d, slack=3))):
                return d, 'regie'

    # Geen regisseur om te vergelijken: alleen als er niets tegenspreekt
    unsure = [d for d in checked if director_match(r, d) is None and runtime_ok(r, d)]
    if len(checked) == 1 and unsure:
        return unsure[0], 'titel'
    return None, None


def tmdb_apply(r, details, via, api_key, budget):
    """Zet alle velden die bij de TMDB-film horen (of wist ze bij None)."""
    old_tmdb = r.get('tmdbId')
    for k in ('imdbId', 'genres', 'plot', 'via', 'checked'):
        r.pop(k, None)
    if not details:
        r['tmdbId'], r['imdbId'] = None, None
        r['checked'] = date.today().isoformat()
        r.pop('traktSlug', None)
        return
    r['tmdbId'] = details['id']
    r['imdbId'] = (details.get('external_ids') or {}).get('imdb_id') or details.get('imdb_id') or None
    names = genre_names(api_key, budget)
    r['genres'] = [names.get(g['id'], g['name']) for g in details.get('genres') or []]
    # Nederlandse beschrijving als die er is, anders de Engelse
    overview = dutch_overview(details) or details.get('overview') or ''
    if overview:
        r['plot'] = first_sentences(overview)
    if via != 'regie':
        r['via'] = via
    if r['tmdbId'] != old_tmdb:
        r.pop('traktSlug', None)


def tmdb_due(r, overrides, today):
    if r['id'] in overrides:
        return r.get('via') != 'hand' or r.get('tmdbId') != overrides[r['id']]
    if 'tmdbId' not in r:
        return True
    if r['tmdbId'] is None:   # niet gevonden: recente films af en toe opnieuw
        age = (today - date.fromisoformat(r['date'][:10])).days
        last = date.fromisoformat(r.get('checked') or '2000-01-01')
        return age <= TMDB_RECHECK_AGE and (today - last).days >= TMDB_RECHECK_EVERY
    return False


def tmdb_one(r, overrides, api_key, budget):
    if r['id'] in overrides:
        tmdb_id = overrides[r['id']]
        details = tmdb_details(tmdb_id, api_key, budget) if tmdb_id else None
        tmdb_apply(r, details, 'hand', api_key, budget)
        r['via'] = 'hand'
    else:
        details, via = tmdb_match(r, api_key, budget)
        tmdb_apply(r, details, via, api_key, budget)
    return bool(r['tmdbId'])


def tmdb_one_patient(r, overrides, api_key, budget, tries=4):
    """tmdb_one, maar bij een netwerkstoring (wifi weg, DNS) even wachten en
    opnieuw proberen. De recensie verandert pas als de film gevonden is."""
    for attempt in range(tries):
        try:
            return tmdb_one(r, overrides, api_key, budget)
        except (OSError, http.client.HTTPException):
            if attempt == tries - 1:
                raise
            time.sleep(30 * (attempt + 1))


def lookup_tmdb(reviews, api_key, rematch=False):
    overrides = load_overrides()
    today = date.today()
    # Bij --rematch alles opnieuw; de oude koppeling blijft staan tot de nieuwe er is
    todo = list(reviews) if rematch else [r for r in reviews if tmdb_due(r, overrides, today)]
    budget = Budget(None if rematch else TMDB_BUDGET)
    print(f'TMDB: {len(todo)} films te koppelen'
          + ('' if rematch else f' (budget {TMDB_BUDGET} requests)'))
    found = done = 0
    try:
        if rematch:
            with ThreadPoolExecutor(REMATCH_WORKERS) as ex:
                for ok in ex.map(lambda r: tmdb_one_patient(r, overrides, api_key, budget), todo):
                    done += 1
                    found += ok
                    if done % 500 == 0:
                        print(f'  {done}/{len(todo)} ({found} gevonden, {budget.used} requests)', flush=True)
        else:   # nieuwste eerst
            for r in todo:
                if budget.used + 20 > budget.limit:
                    break
                found += tmdb_one_patient(r, overrides, api_key, budget)
                done += 1
    except TmdbStop as e:
        print(f'TMDB: gestopt — {e}')
    except (OSError, http.client.HTTPException, ValueError) as e:
        print(f'TMDB: gestopt — netwerkfout: {e}')
    print(f'TMDB: {done} films verwerkt, {found} gekoppeld, {budget.used} requests')


# ─── IMDb ─────────────────────────────────────────────────────────────────────
# De scores komen uit IMDb's eigen dagelijkse dataset (title.ratings), zodat ze
# altijd gelijk zijn aan wat IMDb nu toont. Het IMDb-nummer komt van TMDB.

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


# ─── Trakt ────────────────────────────────────────────────────────────────────
# De Trakt-pagina van een film, zodat je hem vanaf de site op je Trakt-watchlist
# (en daarmee in Stremio) zet. Opzoeken via het TMDB-nummer; Trakt's site kent
# geen links op TMDB-nummer, dus de 'slug' is nodig. Zonder slug linkt de site
# naar een zoekopdracht op titel.
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
    except (OSError, http.client.HTTPException, ValueError) as e:
        print(f'Trakt: gestopt — netwerkfout: {e}')
    print(f'Trakt: {used} requests, {found} gevonden')


def run_step(name, fn, *args, **kwargs):
    """Eén stap mag de rest niet tegenhouden: fout melden, verder met de volgende.
    De '::warning::'-regel maakt er een zichtbare waarschuwing van op GitHub."""
    try:
        fn(*args, **kwargs)
        return True
    except Exception as e:
        print(f'{name}: mislukt — {type(e).__name__}: {e}')
        print(f'::warning title={name} mislukt::{type(e).__name__}: {e}')
        traceback.print_exc()
        return False


def with_key(env, name, fn, *args, **kwargs):
    key = os.environ.get(env, '').strip()
    if not key:
        print(f'{name}: overgeslagen — {env} is niet ingesteld')
        return True
    return run_step(name, fn, *args, key, **kwargs)


def main():
    reviews, complete, last_updated = load()
    before = json.dumps(reviews, sort_keys=True)

    new_found = scrape(reviews, complete, full='--full' in sys.argv)

    ok = True
    if '--tmdb' in sys.argv or '--rematch' in sys.argv:   # levert de IMDb-nummers, dus eerst
        ok &= with_key('TMDB_API_KEY', 'TMDB', lookup_tmdb, reviews,
                       rematch='--rematch' in sys.argv)
    if '--imdb' in sys.argv:
        ok &= run_step('IMDb-scores', update_imdb_ratings, reviews)
    if '--trakt' in sys.argv:
        ok &= with_key('TRAKT_CLIENT_ID', 'Trakt', lookup_trakt, reviews)

    # Datum alleen verversen als er nieuwe recensies zijn
    changed = json.dumps(reviews, sort_keys=True) != before
    save(reviews, complete=True, last_updated=None if new_found else last_updated)
    print(f'\nKlaar! {len(reviews)} recensies in {OUTPUT}'
          + ('' if changed else ' (geen wijzigingen)')
          + ('' if ok else ' — met waarschuwingen, zie hierboven'))


if __name__ == '__main__':
    main()

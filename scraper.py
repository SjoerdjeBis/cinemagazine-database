#!/usr/bin/env python3
"""
Cinemagazine recensies scraper
Haalt recensies op via de WordPress REST API en slaat ze op als data/reviews.json.

Gebruik:
    python3 scraper.py          # nieuwe recensies ophalen
    python3 scraper.py --imdb   # daarna IMDb-nummers opzoeken via OMDb (vereist
                                # OMDB_API_KEY) en actuele scores ophalen bij IMDb

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
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL  = 'https://www.cinemagazine.nl/wp-json/wp/v2/posts'
FIELDS    = 'id,title,date,link,content'
PER_PAGE  = 100
DELAY     = 0.4   # seconden tussen requests (wees beleefd)
OUTPUT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'reviews.json')
USER_AGENT = 'CinemagazineScraper/1.1 (+https://github.com/SjoerdjeBis/cinemagazine-database)'

OMDB_URL    = 'https://www.omdbapi.com/'
OMDB_BUDGET = 900   # max. OMDb-requests per run (gratis limiet: 1000 per dag)
OMDB_DELAY  = 0.15
IMDB_RATINGS_URL = 'https://datasets.imdbws.com/title.ratings.tsv.gz'   # dagelijks bijgewerkt

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
    lines = [json.dumps(r, ensure_ascii=False, separators=(',', ':')) for r in reviews]
    body  = json.dumps(head, ensure_ascii=False, separators=(',', ':'))[:-1]
    tmp   = OUTPUT + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(body + ',"reviews":[\n' + ',\n'.join(lines) + '\n]}\n')
    os.replace(tmp, OUTPUT)


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

    # Datum alleen verversen als er nieuwe recensies zijn
    changed = json.dumps(reviews, sort_keys=True) != before
    save(reviews, complete=True, last_updated=None if new_found else last_updated)
    print(f'\nKlaar! {len(reviews)} recensies in {OUTPUT}'
          + ('' if changed else ' (geen wijzigingen)'))


if __name__ == '__main__':
    main()

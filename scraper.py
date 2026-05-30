#!/usr/bin/env python3
"""
Cinemagazine recensies scraper
Haalt alle recensies op via de WordPress REST API en slaat ze op als data/reviews.json.

Gebruik:
    python3 scraper.py

Het script herneemt automatisch waar het gebleven is als het onderbroken wordt.
"""

import json
import re
import time
import os
import math
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

BASE_URL  = 'https://www.cinemagazine.nl/wp-json/wp/v2/posts'
FIELDS    = 'id,title,date,link,content'
PER_PAGE  = 100
DELAY     = 0.4   # seconden tussen requests (wees beleefd)
OUTPUT    = os.path.join(os.path.dirname(__file__), 'data', 'reviews.json')

HTML_ENTITY_MAP = {
    '&amp;': '&', '&#038;': '&', '&lt;': '<', '&gt;': '>',
    '&quot;': '"', '&#8211;': '–', '&#8212;': '—',
    '&#8216;': '‘', '&#8217;': '’',
    '&#8220;': '“', '&#8221;': '”',
}

def decode_entities(s):
    for entity, char in HTML_ENTITY_MAP.items():
        s = s.replace(entity, char)
    return s

def strip_tags(s):
    return re.sub(r'<[^>]+>', '', s)

def fetch_page(page, retries=3):
    url = (f'{BASE_URL}?per_page={PER_PAGE}&_fields={FIELDS}'
           f'&orderby=date&order=desc&page={page}')
    req = Request(url, headers={'User-Agent': 'CinemagazineScraper/1.0'})
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=30) as resp:
                total   = int(resp.headers.get('X-WP-Total', 0))
                pages   = int(resp.headers.get('X-WP-TotalPages', 1))
                posts   = json.loads(resp.read())
            return posts, total, pages
        except (URLError, HTTPError) as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f'\n  Fout: {e} — wacht {wait}s en probeer opnieuw...')
                time.sleep(wait)
            else:
                raise

def parse_post(post):
    title = decode_entities(strip_tags(post['title']['rendered']))

    # Filmjaar: laatste "(YYYY)" in de titel
    year_match = re.search(r'\((\d{4})\)[^(]*$', title)
    film_year  = int(year_match.group(1)) if year_match else None

    # Waardering: "Waardering: N" of "Waardering: N,N" in de content
    content     = post.get('content', {}).get('rendered', '')
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
        'date':     post['date'],   # ISO 8601, bewaard als string
        'filmYear': film_year,
        'rating':   rating,
    }

def save(reviews):
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    out = {
        'last_updated': datetime.now(timezone.utc).isoformat(),
        'total':        len(reviews),
        'reviews':      reviews,
    }
    with open(OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))

def main():
    # Laad bestaande data (voor hervatting)
    existing_ids = set()
    reviews = []
    if os.path.exists(OUTPUT):
        with open(OUTPUT, encoding='utf-8') as f:
            data = json.load(f)
        reviews     = data.get('reviews', [])
        existing_ids = {r['id'] for r in reviews}
        print(f'Bestaand bestand: {len(reviews)} recensies — hervatten...')

    # Ophalen totaal
    print('Verbinding maken met cinemagazine.nl...')
    _, total, total_pages = fetch_page(1)
    print(f'Totaal op de site: {total} posts ({total_pages} paginas)')
    print()

    new_found = 0
    save_interval = 5  # sla op elke N pagina's

    for page in range(1, total_pages + 1):
        pct = page / total_pages * 100
        print(f'\r[{pct:5.1f}%] Pagina {page}/{total_pages} — '
              f'{len(reviews)} recensies ({new_found} nieuw)', end='', flush=True)

        posts, _, _ = fetch_page(page)
        for post in posts:
            if post['id'] not in existing_ids:
                reviews.append(parse_post(post))
                existing_ids.add(post['id'])
                new_found += 1

        # Tussentijds opslaan
        if page % save_interval == 0 or page == total_pages:
            save(reviews)

        time.sleep(DELAY)

    print(f'\n\nKlaar! {len(reviews)} recensies opgeslagen in:\n  {OUTPUT}')
    print(f'Waarvan {new_found} nieuw toegevoegd.')

if __name__ == '__main__':
    main()

# Cinemagazine Filmwaarderingen

Doorzoekbaar overzicht van alle filmrecensies op [cinemagazine.nl](https://www.cinemagazine.nl), met IMDb-scores.
Live: https://sjoerdjebis.github.io/cinemagazine-database/

## Hoe het werkt

- `scraper.py` haalt recensies op via de WordPress REST API van cinemagazine.nl en schrijft `data/reviews.json`.
  Na de eerste volledige run vraagt hij alleen nieuwe recensies op: hij stopt bij de eerste pagina met een bekende recensie.
- Met `--tmdb` koppelt hij elke recensie aan een film op [TMDB](https://www.themoviedb.org) (max. 3000 requests per
  run). Zoeken gaat op titel (ook de losse delen van "NL titel – originele titel"), met het jaar max. 1 ernaast.
  Elke treffer wordt gecontroleerd met de regisseur uit de vaste kopregel van de recensie ("Regie: … | 95 minuten | …"),
  soepel op spelling (Ji-Woon Kim = Kim Jee-woon). Klopt de regisseur exact, dan mag het jaar tot 3 jaar afwijken
  (festivaljaar vs. releasejaar). Klopt de regisseur bij geen enkele treffer, dan zoekt hij via de films die die
  regisseur maakte; zo worden ook films gevonden die op TMDB een andere titel hebben (Piranha → Piranha 3D). Zonder overeenkomende regisseur koppelt hij alleen als niets
  tegenspreekt (veld `via: "titel"`); liever geen koppeling dan een verkeerde. Van de gekozen film komen in één
  request het IMDb-nummer, de genres en een beschrijving van twee zinnen (Nederlands als die er is, anders Engels).
  Niet gevonden films uit recensies van het afgelopen jaar worden elke 30 dagen opnieuw geprobeerd.
- **Handmatig corrigeren** kan in `data/overrides.json`: recensie-id → TMDB-nummer (uit de URL op themoviedb.org),
  of `null` als de film niet op TMDB staat. Die koppeling gaat altijd voor en blijft bij elke run staan.
- Met `--imdb` haalt hij de scores uit IMDb's dagelijkse
  [ratings-dataset](https://developer.imdb.com/non-commercial-datasets/), zodat ze gelijk zijn aan wat IMDb toont.
- Met `--trakt` zoekt hij voor films met 4+ sterren de Trakt-pagina op via het TMDB-nummer (gratis
  [Trakt-API](https://trakt.docs.apiary.io); max. 900 requests per run). Op de site linkt de knop **+ Trakt** daarheen,
  zodat je een film op je Trakt-watchlist zet (en zo in Stremio). Zonder gevonden pagina: zoeken op titel.
  (Trakt's site kent geen links op TMDB-nummer, vandaar deze stap.)
- De beschrijvingen staan apart in `data/plots.json`, zodat `reviews.json` klein blijft; de site laadt ze pas
  nadat de tabel er staat.
- De GitHub Action `.github/workflows/update-data.yml` draait dit dagelijks en commit de nieuwe data naar `main`,
  waarna GitHub Pages de site bijwerkt. Handmatig starten kan via *Actions → Recensies bijwerken → Run workflow*.
- `index.html` is een statische pagina zonder build-stap.
  Klik je op een cijfer, dan opent `recensie.html`: de Cinemagazine-recensie in een kader, met een balk
  erboven naar Trakt. `links.js` bevat de linklogica die beide pagina's delen.

## Lokaal

```bash
python3 scraper.py                                   # nieuwe recensies
TMDB_API_KEY=... python3 scraper.py --tmdb --imdb    # plus koppeling, genres, beschrijvingen en IMDb-scores
TRAKT_CLIENT_ID=... python3 scraper.py --trakt       # plus Trakt-links
python3 scraper.py --full                            # hele site opnieuw: regisseur, speelduur, cijfers bijwerken
TMDB_API_KEY=... python3 scraper.py --rematch --imdb # alle films opnieuw koppelen (duurt ruim een uur)
./start.command                                      # site op http://localhost:3456
```

## Instellen

De TMDB-sleutel staat als repository-secret `TMDB_API_KEY` en de Trakt Client ID als `TRAKT_CLIENT_ID`
(*Settings → Secrets and variables → Actions*). De secret `OMDB_API_KEY` wordt niet meer gebruikt en mag weg.

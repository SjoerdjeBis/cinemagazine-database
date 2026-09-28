# Cinemagazine Filmwaarderingen

Doorzoekbaar overzicht van alle filmrecensies op [cinemagazine.nl](https://www.cinemagazine.nl), met IMDb-scores.
Live: https://sjoerdjebis.github.io/cinemagazine-database/

## Hoe het werkt

- `scraper.py` haalt recensies op via de WordPress REST API van cinemagazine.nl en schrijft `data/reviews.json`.
  Na de eerste volledige run vraagt hij alleen nieuwe recensies op: hij stopt bij de eerste pagina met een bekende recensie.
- Met `--imdb` zoekt hij per film het IMDb-nummer op via [OMDb](https://www.omdbapi.com) (max. 900 requests per run,
  nieuwste recensies eerst; het jaar moet op max. 1 jaar na kloppen). De scores zelf komen uit IMDb's dagelijkse
  [ratings-dataset](https://developer.imdb.com/non-commercial-datasets/), zodat ze gelijk zijn aan wat IMDb toont.
  OMDb-scores worden niet gebruikt: die bleken vaak verouderd of leeg.
- Met `--tmdb` haalt hij genres en een beschrijving van twee zinnen op bij [TMDB](https://www.themoviedb.org)
  (Nederlands als die er is, anders Engels; max. 3000 requests per run). Koppeling via het IMDb-nummer, of anders
  op titel met het jaar max. 1 jaar ernaast. De beschrijvingen staan apart in `data/plots.json`, zodat
  `reviews.json` klein blijft; de site laadt ze pas nadat de tabel er staat.
- Met `--trakt` zoekt hij voor films met 4+ sterren de Trakt-pagina op via het TMDB-nummer (gratis
  [Trakt-API](https://trakt.docs.apiary.io); max. 900 requests per run). Op de site linkt de knop **+ Trakt** daarheen,
  zodat je een film op je Trakt-watchlist zet (en zo in Stremio). Zonder gevonden pagina: zoeken op titel.
- Met `--justwatch` zoekt hij voor films met 4+ sterren de JustWatch-pagina op (via de GraphQL-API van JustWatch;
  max. 1500 requests per run). Koppeling op IMDb-nummer, of op titel met het jaar max. 1 jaar ernaast.
  Staat de film wel bij JustWatch maar niet in Nederland, dan toont de site "niet in NL". Niet gevonden films uit
  recensies van het afgelopen jaar worden wekelijks opnieuw geprobeerd; de rest krijgt een zoeklink op titel.
- De GitHub Action `.github/workflows/update-data.yml` draait dit dagelijks en commit de nieuwe data naar `main`,
  waarna GitHub Pages de site bijwerkt. Handmatig starten kan via *Actions → Recensies bijwerken → Run workflow*.
- `index.html` is een statische pagina zonder build-stap.

## Lokaal

```bash
python3 scraper.py                            # nieuwe recensies
OMDB_API_KEY=... python3 scraper.py --imdb    # plus IMDb-scores
TMDB_API_KEY=... python3 scraper.py --tmdb    # plus genres en beschrijvingen
TRAKT_CLIENT_ID=... python3 scraper.py --trakt # plus Trakt-links
python3 scraper.py --justwatch               # plus JustWatch-links
./start.command                               # site op http://localhost:3456
```

## Instellen

De OMDb-sleutel staat als repository-secret `OMDB_API_KEY`, de TMDB-sleutel als `TMDB_API_KEY` en de Trakt Client ID als `TRAKT_CLIENT_ID` (*Settings → Secrets and variables → Actions*).
Zonder de OMDb-secret worden er geen nieuwe films gekoppeld; de scores van al gekoppelde films worden wel bijgewerkt.

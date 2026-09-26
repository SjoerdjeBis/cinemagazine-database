# Cinemagazine Filmwaarderingen

Doorzoekbaar overzicht van alle filmrecensies op [cinemagazine.nl](https://www.cinemagazine.nl), met IMDb-scores.
Live: https://sjoerdjebis.github.io/cinemagazine-database/

## Hoe het werkt

- `scraper.py` haalt recensies op via de WordPress REST API van cinemagazine.nl en schrijft `data/reviews.json`.
  Na de eerste volledige run vraagt hij alleen nieuwe recensies op: hij stopt bij de eerste pagina met een bekende recensie.
- Met `--imdb` vult hij IMDb-scores aan via [OMDb](https://www.omdbapi.com) (max. 900 requests per run, nieuwste recensies eerst).
- De GitHub Action `.github/workflows/update-data.yml` draait dit dagelijks en commit de nieuwe data naar `main`,
  waarna GitHub Pages de site bijwerkt. Handmatig starten kan via *Actions → Recensies bijwerken → Run workflow*.
- `index.html` is een statische pagina zonder build-stap.

## Lokaal

```bash
python3 scraper.py                            # nieuwe recensies
OMDB_API_KEY=... python3 scraper.py --imdb    # plus IMDb-scores
./start.command                               # site op http://localhost:3456
```

## Instellen

De OMDb-sleutel staat als repository-secret `OMDB_API_KEY` (*Settings → Secrets and variables → Actions*).
Zonder die secret werkt alles, maar worden er geen IMDb-scores aangevuld.

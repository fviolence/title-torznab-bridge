# Title Torznab Bridge

Small, self-hosted **Torznab proxy** for Sonarr and Radarr. It searches Prowlarr trackers using **Russian and original titles**, resolves aliases via your existing *Arr libraries and public metadata, and returns the original torrent results. No external Python packages are required.

## What it does

- Provides three independent Torznab endpoints: regular TV, anime, and movies.
- Prefers matching titles and `alternateTitles` from Sonarr/Radarr (including TVDB, TMDb and IMDb IDs); falls back to TVmaze, Wikidata and Shikimori as appropriate.
- Discovers enabled, searchable torrent indexers through Prowlarr's **read-only** catalog API; selects them by category and language. English-only indexers receive English titles or a conservative Latin fallback, not Cyrillic translations.
- Searches Prowlarr with multiple localized names, aggregates and deduplicates releases, and respects downstream Torznab `offset`/`limit`.
- Caches RSS in memory and resolved titles in persistent SQLite databases. No API writes to Sonarr, Radarr or Prowlarr.

## Files

- `bridge.py` — Torznab HTTP server, routing, query fan-out and pagination.
- `metadata.py` — TVmaze/Wikidata title resolution.
- `sonarr_library.py`, `radarr_library.py` — read-only local *Arr catalogs.
- `indexer_discovery.py` — automatic Prowlarr indexer routing.
- `storage.py` — SQLite connection helper.
- `indexers.json`, `overrides.json` — empty defaults bundled into the image, replaceable by read-only bind mounts.
- `Dockerfile` — non-root, multi-architecture Python image with built-in healthcheck.
- `tests/` — standard-library unit and local HTTP integration tests (excluded from the image).

## Example `compose.yml`

```yaml
services:
  title-torznab:
    image: fviolence/title-torznab-bridge:latest
    container_name: title-torznab
    restart: unless-stopped
    environment:
      PORT: "8789"
      PROWLARR_URL: "http://prowlarr:9696"
      PROWLARR_API_KEY: "${PROWLARR_API_KEY:?Set PROWLARR_API_KEY in .env}"
      BRIDGE_API_KEY: "${BRIDGE_API_KEY:?Set BRIDGE_API_KEY in .env}"
      SONARR_URL: "${SONARR_URL:-http://sonarr:8989}"
      SONARR_API_KEY: "${SONARR_API_KEY:-}"
      RADARR_URL: "${RADARR_URL:-http://radarr:7878}"
      RADARR_API_KEY: "${RADARR_API_KEY:-}"
      AUTO_INDEXERS: "${AUTO_INDEXERS:-true}"
      AUTO_INDEXERS_TTL: "${AUTO_INDEXERS_TTL:-600}"
      AUTO_MAX_QUERIES: "${AUTO_MAX_QUERIES:-4}"
      AUTO_EXCLUDE_IDS: "${AUTO_EXCLUDE_IDS:-}"
      BROWSE_MAX_INDEXERS: "${BROWSE_MAX_INDEXERS:-4}"
      BROWSE_INDEXER_IDS: "${BROWSE_INDEXER_IDS:-}"
      UPSTREAM_PAGE_SIZE: "${UPSTREAM_PAGE_SIZE:-100}"
      UPSTREAM_MAX_PAGES: "${UPSTREAM_MAX_PAGES:-2}"
      UPSTREAM_MAX_ITEMS: "${UPSTREAM_MAX_ITEMS:-200}"
      CACHE_SECONDS: "${CACHE_SECONDS:-3600}"
      TITLE_CACHE_SECONDS: "${TITLE_CACHE_SECONDS:-604800}"
      NEGATIVE_CACHE_SECONDS: "${NEGATIVE_CACHE_SECONDS:-3600}"
      UPSTREAM_TIMEOUT: "${UPSTREAM_TIMEOUT:-40}"
      MAX_WORKERS: "${MAX_WORKERS:-3}"
      SHIKIMORI_URL: "${SHIKIMORI_URL:-https://shikimori.io/api/graphql}"
      SHIKIMORI_TIMEOUT: "12"
      METADATA_TIMEOUT: "12"
      INDEXERS_FILE: "/app/indexers.json"
      OVERRIDES_FILE: "/app/overrides.json"
      SHIKIMORI_DB: "/app/data/anime-titles.sqlite"
      TITLE_DB_TV: "/app/data/tv-titles.sqlite"
      TITLE_DB_MOVIE: "/app/data/movie-titles.sqlite"
      METADATA_USER_AGENT: "${METADATA_USER_AGENT:-TitleTorznabBridge/0.1 (personal self-hosted proxy)}"
    volumes:
      - title-torznab-data:/app/data
      # Optional custom routing/aliases (uncomment if required):
      # - ./indexers.json:/app/indexers.json:ro
      # - ./overrides.json:/app/overrides.json:ro

volumes:
  title-torznab-data:
```

> The bridge must share a Docker network with Prowlarr and, optionally, Sonarr/Radarr. If these services run in separate Compose projects, attach the bridge to their existing external Docker network.

Environmental variables:

```dotenv
PROWLARR_API_KEY=your_prowlarr_api_key
BRIDGE_API_KEY=generate_a_long_random_secret
SONARR_API_KEY=your_sonarr_api_key
RADARR_API_KEY=your_radarr_api_key
```

Generate your bridge key, for example, using `openssl rand -hex 32`. Sonarr/Radarr integration is optional.

Ready to use [docker image](https://hub.docker.com/r/fviolence/title-torznab-bridge).

## Connect Sonarr / Radarr

Create the following **Custom Torznab** indexers directly in the relevant application:

| Application | Name (example) | URL | API Path | Categories |
| --- | --- | --- | --- | --- |
| Sonarr | Title Bridge TV | `http://title-torznab:8789` | `/tv/api` | `5000` |
| Sonarr (anime) | Title Bridge Anime | `http://title-torznab:8789` | `/anime/api` | `5070` |
| Radarr | Title Bridge Movies | `http://title-torznab:8789` | `/movie/api` | `2000` |

## Routing and pagination

`AUTO_INDEXERS=true` is the recommended setting. The bridge reads `GET /api/v1/indexer` with `X-Api-Key` and caches it for 600 seconds. Only **enabled**, **search-capable**, **torrent-protocol** indexers with matching categories are selected. Anime-only categories are conservatively excluded from normal TV/movie routes.

`indexers.json` (optional file override) uses the same schema as the bundled file:

```json
{
  "anime": {},
  "tv": {"4": {"language_order": ["russian", "english", "synonym", "original"], "auto_max_queries": 3}},
  "movie": {"3": {"disabled": true}}
}
```

In auto mode, per-ID settings can specify `language_order`, `auto_max_queries` (1–5), `category`, `test_query` and `disabled`. For explicit, manual routing set `AUTO_INDEXERS=false` and supply numeric indexer IDs per mode with `max_queries`; empty maps will not work in manual mode. Optional `AUTO_EXCLUDE_IDS=2,7` excludes IDs globally. `BROWSE_INDEXER_IDS` is only a *preference* order for empty-query/Test probes, never a forced allowlist.

`overrides.json` (optional) lets you provide exact alias mappings:

```json
{
  "shows": [
    {"types": ["tv"], "match": ["Breaking Bad"], "titles": {"russian": ["Во все тяжкие"], "english": ["Breaking Bad"]}},
    {"types": ["movie"], "match": ["The Matrix"], "titles": {"russian": ["Матрица"], "english": ["The Matrix"]}},
    {"types": ["anime"], "match": ["Kuroko's Basketball"], "indexers": {"6": ["Баскетбол Куроко"]}}
  ]
}
```

By default, each (indexer, title, category) query is bounded by UPSTREAM_MAX_ITEMS=200 upstream results.

| Setting | Default | Meaning |
| --- | --- | --- |
| `AUTO_INDEXERS`, `AUTO_INDEXERS_TTL` | `true`, `600` | Discovery and catalog TTL (seconds) |
| `AUTO_MAX_QUERIES`, `AUTO_EXCLUDE_IDS` | `4`, empty | Terms per indexer (1–5), global blacklist |
| `BROWSE_MAX_INDEXERS`, `BROWSE_INDEXER_IDS` | `4`, empty | Browse/Test limit (1–20), preferred IDs |
| `UPSTREAM_PAGE_SIZE`, `UPSTREAM_MAX_PAGES`, `UPSTREAM_MAX_ITEMS` | `100`, `2`, `200` | Per-term pagination caps (1–100, 1–5, 1–500) |
| `MAX_WORKERS`, `UPSTREAM_TIMEOUT` | `3`, `40` | Concurrent term searches (1–6), timeout (seconds) |
| `CACHE_SECONDS` | `3600` | In-memory RSS cache lifetime (seconds) |
| `TITLE_CACHE_SECONDS`, `NEGATIVE_CACHE_SECONDS` | `604800`, `3600` | SQLite title and miss TTLs (seconds) |
| `SHIKIMORI_URL`, `METADATA_USER_AGENT` | built-in | Anime metadata endpoint, identifying User-Agent |
| `SONARR_URL`, `RADARR_URL` | `http://sonarr:8989`, `http://radarr:7878` | Optional read-only local *Arr catalogs; API key enables each |

## Endpoints / diagnostics

- `/health` — unauthenticated lightweight health check (`{"status":"ok"}`).
- `/tv/api`, `/anime/api`, `/movie/api` — authenticated Torznab (supports `t=caps` and searches).
- `/debug/indexers` — authenticated, read-only active/excluded routing diagnostics, with exclusion reason codes (no tracker search).
- `/debug/tv/resolve`, `/debug/anime/resolve`, `/debug/movie/resolve` — authenticated title/ID resolution and planned upstream queries (may consult metadata APIs, but does not search trackers).

## Tests

Run the deterministic unit and local HTTP tests from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

These tests cover title matching, routing, Torznab RSS and pagination, and storage error behavior. They do not certify a live Prowlarr catalog, tracker access, or real multi-architecture Docker runtime. External metadata services can be unavailable or incomplete, so no search is guaranteed to find every Russian alias.

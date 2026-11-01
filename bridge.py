#!/usr/bin/env python3
"""Multilingual title resolver + Prowlarr Torznab feeds for Sonarr and Radarr.

Python standard library only; never edits torrent metadata, seasons or episodes.
"""
from __future__ import annotations

import copy
from contextlib import closing
import difflib
import hmac
import json
import logging
import os
import re
import sqlite3
from storage import connect_sqlite
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from metadata import GenericResolver
from sonarr_library import SonarrLibrary
from radarr_library import RadarrLibrary
from indexer_discovery import ProwlarrCatalog, DEFAULT_CATEGORIES, DEFAULT_LANGUAGE_ORDER

VERSION = '0.1'
TORZNAB_NS = 'http://torznab.com/schemas/2015/feed'
ET.register_namespace('torznab', TORZNAB_NS)
NEWZNAB_NS = 'http://www.newznab.com/DTD/2010/feeds/attributes/'
ET.register_namespace('newznab', NEWZNAB_NS)
LOG = logging.getLogger('title_bridge')
GRAPHQL_QUERY = '''query AnimeAliases($search: String!) {
  animes(search: $search, limit: 10) {
    id name russian english synonyms
  }
}'''


def norm(value: str) -> str:
    """Case/punctuation-insensitive matching, incl. Sonarr's possessive handling."""
    value = unicodedata.normalize('NFKC', value or '').casefold()
    value = value.replace('’', "'").replace("'s", 's')
    return ' '.join(re.sub(r'[^\w\s]', ' ', value, flags=re.UNICODE).split())


def clean_query_candidates(query: str) -> list[str]:
    """Try literal title before removing numeric episode/season suffix."""
    query = ' '.join(query.split())
    variants = [query]
    without = re.sub(r'\s+(?:\d{1,3})$', '', query).strip()
    if without and without != query and len(without.split()) >= 2:
        variants.append(without)
    return variants


def titles_from_anime(anime: dict[str, Any]) -> dict[str, list[str]]:
    def collect(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        return [x.strip() for x in value if isinstance(x, str) and x.strip()] if isinstance(value, list) else []

    result = {'russian': collect(anime.get('russian')),
              'romaji': collect(anime.get('name')),
              'english': collect(anime.get('english')),
              'synonym': collect(anime.get('synonyms'))}
    # Cyrillic synonyms should also be selectable by Russian-first indexers.
    for value in result['synonym']:
        if re.search(r'[А-Яа-яЁё]', value):
            result['russian'].append(value)
        else:
            result['romaji'].append(value)
    return result


def select_anime(animes: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """Only trust an exact normalized alias or a highly similar, unambiguous hit."""
    key = norm(query)
    scored: list[tuple[float, dict[str, Any]]] = []
    for anime in animes:
        if not isinstance(anime, dict):
            continue
        names = [v for values in titles_from_anime(anime).values() for v in values]
        if not names:
            continue
        score = max((difflib.SequenceMatcher(None, key, norm(n)).ratio() for n in names), default=0.0)
        scored.append((score, anime))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored:
        return None
    best, selected = scored[0]
    if best == 1.0:
        return selected
    # Avoid accepting a merely plausible search result, e.g. a different season.
    if len(key) >= 10 and best >= 0.94 and (len(scored) == 1 or best - scored[1][0] >= 0.04):
        return selected
    return None


def load_json(path: str, default: Any) -> Any:
    file = Path(path)
    if not file.is_file():
        return default
    with file.open(encoding='utf-8') as stream:
        return json.load(stream)


class TTLCache:
    """Small in-process TTL cache with per-key single flight."""
    def __init__(self, ttl: int):
        self.ttl = ttl
        self._items: dict[Any, tuple[float, Any]] = {}
        self._busy: set[Any] = set()
        self._cv = threading.Condition()

    def get_or_call(self, key: Any, factory):
        with self._cv:
            while True:
                item = self._items.get(key)
                if item and item[0] > time.monotonic():
                    return copy.deepcopy(item[1])
                if key not in self._busy:
                    self._busy.add(key)
                    break
                self._cv.wait()
        try:
            value = factory()
            with self._cv:
                self._items[key] = (time.monotonic() + self.ttl, copy.deepcopy(value))
            return value
        finally:
            with self._cv:
                self._busy.discard(key)
                self._cv.notify_all()


class Resolver:
    def __init__(self, shiki_url: str, cache_path: str, ttl: int, negative_ttl: int,
                 overrides: dict[str, Any], *, timeout: int = 12, user_agent: str = 'TitleTorznabBridge/0.1 (Shikimori title resolver)', fetch=None):
        self.url = shiki_url
        self.db_path = cache_path
        self.ttl = ttl
        self.negative_ttl = negative_ttl
        self.timeout = timeout
        self.user_agent = user_agent
        self.fetch = fetch or self._fetch
        self._lock = threading.Lock()
        self._busy: set[str] = set()
        self._cv = threading.Condition()
        self._retry_after = 0.0
        self._request_lock = threading.Lock()
        self._next_request = 0.0
        self._overrides: dict[str, dict[str, Any]] = {}
        for show in overrides.get('shows', []):
            types = show.get('types', show.get('type', ['anime']))
            types = [types] if isinstance(types, str) else types
            if 'anime' not in types:
                continue
            for alias in show.get('match', []):
                key = norm(alias)
                if key in self._overrides:
                    raise ValueError(f'Duplicate manual alias: {alias!r}')
                self._overrides[key] = show
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS titles (
                query TEXT PRIMARY KEY, expires REAL NOT NULL, result TEXT NOT NULL
            )''')

    def _db(self):
        return connect_sqlite(self.db_path)

    def _cached(self, key: str) -> tuple[bool, dict | None]:
        with closing(self._db()) as db, db:
            row = db.execute('SELECT expires, result FROM titles WHERE query=?', (key,)).fetchone()
        if row and row[0] > time.time():
            return True, json.loads(row[1])
        return False, None

    def _save(self, keys: list[str], result: dict | None) -> None:
        expiry = time.time() + (self.ttl if result else self.negative_ttl)
        with closing(self._db()) as db, db:
            db.executemany('INSERT OR REPLACE INTO titles (query, expires, result) VALUES (?, ?, ?)',
                           [(k, expiry, json.dumps(result, ensure_ascii=False)) for k in set(keys)])

    def _fetch(self, search: str) -> list[dict[str, Any]]:
        body = json.dumps({'query': GRAPHQL_QUERY, 'variables': {'search': search}}).encode('utf-8')
        request = urllib.request.Request(self.url, data=body, method='POST', headers={
            'User-Agent': self.user_agent,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        })
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = response.read(1_000_001)
        if len(payload) > 1_000_000:
            raise ValueError('Shikimori response is too large')
        data = json.loads(payload)
        if data.get('errors'):
            raise ValueError('Shikimori returned GraphQL errors')
        animes = data.get('data', {}).get('animes')
        if not isinstance(animes, list):
            raise ValueError('Shikimori response did not contain animes')
        return animes

    def resolve(self, query: str) -> dict[str, Any]:
        candidates = clean_query_candidates(query)
        # Manual overrides have priority over remote metadata and work offline.
        for candidate in candidates:
            manual = self._overrides.get(norm(candidate))
            if manual:
                if manual.get('merge_shikimori'):
                    remote = self._resolve_remote(candidates)
                    return {'source': 'manual+shikimori', 'titles': remote.get('titles', {}) if remote else {},
                            'indexers': manual.get('indexers', {})}
                return {'source': 'manual', 'titles': {}, 'indexers': manual.get('indexers', {})}
        remote = self._resolve_remote(candidates)
        if remote:
            return remote
        return {'source': 'fallback', 'titles': {'original': [candidates[-1]]}, 'indexers': {}}

    def _resolve_remote(self, candidates: list[str]) -> dict | None:
        with self._cv:
            # Search by one common base key; collapse concurrent Sonarr episode queries.
            key = norm(candidates[-1])
            while key in self._busy:
                self._cv.wait()
            # Cache lookup is intentionally inside single-flight lock.
            for candidate in reversed(candidates):
                hit, result = self._cached(norm(candidate))
                if hit:
                    return result
            if time.monotonic() < self._retry_after:
                return None
            self._busy.add(key)
        try:
            # Use the series title without Sonarr's absolute episode suffix.
            # If Shikimori's text search does not recognize the spelling,
            # probe the first distinctive word, but still REQUIRE a close
            # match against the complete title before accepting a hit.
            base = candidates[-1]
            probes = [base]
            words = base.split()
            if len(words) >= 2 and len(words[0]) >= 5:
                head = words[0]
                # "Kurokos Basketball" is a common Sonarr spelling of
                # "Kuroko's Basketball"; Shikimori may index "Kuroko".
                if head.lower().endswith('s') and len(head) >= 6:
                    head = head[:-1]
                if norm(head) != norm(base):
                    probes.append(head)
            for probe in probes:
                try:
                    # Keep request pace below Shikimori's documented
                    # 5 rps / 90 rpm. Across simultaneous incoming searches,
                    # use a conservative <=60 rpm global cap.
                    with self._request_lock:
                        delay = self._next_request - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                        self._next_request = time.monotonic() + 1.0
                        matches = self.fetch(probe)
                except Exception as exc:
                    # Don't include exception text: request URLs may leak information.
                    LOG.warning('Shikimori lookup unavailable: %s', type(exc).__name__)
                    with self._cv:
                        self._retry_after = time.monotonic() + 90
                    return None
                anime = select_anime(matches, base)
                if anime:
                    result = {'source': 'shikimori', 'titles': titles_from_anime(anime),
                              'shikimori_id': anime.get('id'), 'indexers': {}}
                    # Cache known aliases so different Sonarr spellings converge.
                    names = [n for group in result['titles'].values() for n in group]
                    self._save([norm(c) for c in candidates] + [norm(n) for n in names], result)
                    LOG.info('Shikimori title resolved (id=%s)', str(anime.get('id', '?'))[:32])
                    return result
            self._save([norm(c) for c in candidates], None)
            LOG.info('Shikimori: no confident title match')
            return None
        finally:
            with self._cv:
                self._busy.discard(key)
                self._cv.notify_all()


def search_terms(info: dict[str, Any], upstream_id: str, settings: dict[str, Any], query: str) -> list[str]:
    order = settings.get('language_order', ['russian', 'romaji', 'english', 'synonym'])
    limit = max(1, min(5, int(settings.get('max_queries', 2))))
    terms: list[str] = []
    def add(term):
        if isinstance(term, str) and term.strip() and norm(term) not in {norm(x) for x in terms}:
            terms.append(term.strip())
    custom = info.get('indexers', {}).get(upstream_id, [])
    for term in ([custom] if isinstance(custom, str) else custom):
        add(term)
    for kind in order:
        for term in info.get('titles', {}).get(kind, []):
            add(term)
    # On failure, always try the original query; no dependency on external metadata.
    if not terms and info.get('titles', {}).get('original') != []:
        add(clean_query_candidates(query)[-1])
    return terms[:limit]


def automatic_search_terms(info: dict[str, Any], upstream_id: str, settings: dict[str, Any],
                           query: str, *, english_only: bool) -> list[str]:
    """Round-robin language families so Russian aliases cannot crowd out English.

    For English-only indexers use only verified English aliases. If metadata
    lookup failed, a Latin-script input is the least-surprising fallback.
    """
    limit = max(1, min(5, int(settings.get('max_queries', 3))))
    titles = info.get('titles', {})
    if english_only:
        order = ['english']
    else:
        order = settings.get('language_order', ['russian', 'romaji', 'english', 'synonym', 'original'])
    pools = {}
    custom = info.get('indexers', {}).get(upstream_id, [])
    if not english_only and custom:
        pools['custom'] = [custom] if isinstance(custom, str) else custom
        order = ['custom'] + list(order)
    for key in order:
        if key != 'custom':
            pools[key] = titles.get(key, [])
    if english_only and not titles.get('english'):
        # Only fall back to the input query if NO metadata resolved the title.
        # E.g. an unknown English-language show, but not a known Russian title.
        if info.get('source') == 'fallback' and query.isascii() and re.search(r'[A-Za-z]', query):
            pools['english'] = clean_query_candidates(query)[-1:]
    elif not english_only and not any(pools.values()):
        pools['original'] = titles.get('original', [])
        order = list(order) + ['original']

    seen = set()
    result = []
    # One variant per language per round, then try next aliases if budget remains.
    for position in range(max((len(pools.get(k, [])) for k in order), default=0)):
        for key in order:
            variants = pools.get(key, [])
            if not isinstance(variants, list) or position >= len(variants):
                continue
            term = variants[position]
            if not isinstance(term, str) or not term.strip():
                continue
            value = term.strip()
            normalized = norm(value)
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(value)
                if len(result) == limit:
                    return result
    return result


def arr_titles(entry: dict[str, Any], source: str) -> dict[str, Any]:
    """Local *Arr title and verified aliases; no additional external ID needed.

    Non-Cyrillic, non-Latin variants are synonyms; release names are NEVER edited.
    """
    titles: dict[str, list[str]] = {'russian': [], 'english': [], 'synonym': []}
    raw = [entry.get('title'), entry.get('originalTitle')]
    raw += [alt.get('title') if isinstance(alt, dict) else alt
            for alt in (entry.get('alternateTitles') or [])]
    for name in raw:
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        bucket = ('russian' if re.search(r'[А-Яа-яЁё]', name)
                  else 'english' if re.search(r'[A-Za-z]', name) else 'synonym')
        if norm(name) not in {norm(v) for v in titles[bucket]}:
            titles[bucket].append(name)
    return {'source': source, 'titles': titles, 'indexers': {}}


def merge_titles(primary: dict, secondary: dict) -> dict:
    """Merge trusted identities without replacing the preferred local title order."""
    result = copy.deepcopy(primary)
    for kind, values in secondary.get('titles', {}).items():
        target = result['titles'].setdefault(kind, [])
        for value in values:
            if isinstance(value, str) and norm(value) not in {norm(v) for v in target}:
                target.append(value)
    extra_source = secondary['source']
    if extra_source.startswith(primary['source'] + '+'):
        extra_source = extra_source[len(primary['source']) + 1:]
    result['source'] = primary['source'] + '+' + extra_source
    return result


def read_rss(xml: bytes) -> list[ET.Element]:
    root = ET.fromstring(xml)
    if root.tag != 'rss':
        raise ValueError('Upstream response is not RSS')
    channel = root.find('channel')
    if channel is None:
        raise ValueError('Upstream RSS channel missing')
    return [copy.deepcopy(item) for item in channel.findall('item')]


def item_identity(item: ET.Element) -> str:
    return item.findtext('guid') or item.findtext('link') or ET.tostring(item, encoding='unicode')


def unique_items(items: list[ET.Element]) -> list[ET.Element]:
    seen: set[str] = set()
    output = []
    for item in items:
        key = item_identity(item)
        if key not in seen:
            seen.add(key)
            output.append(item)
    return output


def rss(items: list[ET.Element], *, offset: int | None = None, total: int | None = None) -> bytes:
    root = ET.Element('rss', version='2.0')
    channel = ET.SubElement(root, 'channel')
    ET.SubElement(channel, 'title').text = 'Title Torznab Bridge'
    ET.SubElement(channel, 'description').text = 'Multilingual search via Prowlarr (original releases unchanged)'
    ET.SubElement(channel, 'link').text = 'http://localhost/'
    ET.SubElement(channel, 'lastBuildDate').text = format_datetime(datetime.now(timezone.utc))
    if offset is not None and total is not None:
        ET.SubElement(channel, f'{{{NEWZNAB_NS}}}response', offset=str(offset), total=str(total))
    for item in items:
        channel.append(item)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def caps(mode: str = 'anime') -> bytes:
    root = ET.Element('caps')
    ET.SubElement(root, 'server', title='Title Torznab Bridge', version=VERSION)
    searching = ET.SubElement(root, 'searching')
    ET.SubElement(searching, 'search', available='yes', supportedParams='q')
    if mode != 'movie':
        ET.SubElement(searching, 'tv-search', available='yes', supportedParams='q,season,ep,tvdbid,imdbid')
    if mode == 'movie':
        ET.SubElement(searching, 'movie-search', available='yes', supportedParams='q')
    categories = ET.SubElement(root, 'categories')
    if mode == 'anime':
        category = ET.SubElement(categories, 'category', id='5000', name='TV')
        ET.SubElement(category, 'subcat', id='5070', name='Anime')
    elif mode == 'tv':
        ET.SubElement(categories, 'category', id='5000', name='TV')
    elif mode == 'movie':
        ET.SubElement(categories, 'category', id='2000', name='Movies')
    else:
        raise ValueError('unknown mode')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def normalized_search_input(query: str, mode: str) -> list[str]:
    """Try exact title first, then a likely numeric suffix if appropriate.

    No release filtering; this only affects finding metadata search aliases.
    """
    query = ' '.join(query.split())
    if mode == 'anime':
        return clean_query_candidates(query)
    variants = [query]
    if mode == 'tv':
        reduced = re.sub(r'\s+\d{1,3}$', '', query).strip()
    else:
        reduced = re.sub(r'\s+\(?\b(?:18|19|20)\d{2}\)?$', '', query).strip()
    if reduced and reduced != query and len(reduced.split()) >= 2:
        variants.append(reduced)
    return variants

class Application:
    def __init__(self, env: dict[str, str] | None = None, *, resolver=None, fetch_upstream=None,
                 generic_resolvers: dict | None = None, sonarr_library=None, radarr_library=None):
        env = env if env is not None else os.environ
        self.key = env.get('BRIDGE_API_KEY', '')
        self.prowlarr_key = env.get('PROWLARR_API_KEY', '')
        if not self.key or not self.prowlarr_key:
            raise ValueError('Both BRIDGE_API_KEY and PROWLARR_API_KEY are required')
        self.prowlarr_url = env.get('PROWLARR_URL', 'http://prowlarr:9696').rstrip('/')
        raw = load_json(env.get('INDEXERS_FILE', '/app/indexers.json'), {})
        if raw and all(str(i).isdigit() for i in raw):
            # Backwards compatibility with v0.3 indexers.json (anime only).
            raw = {'anime': raw}
        self.indexers_by_mode = {mode: raw.get(mode, {}) for mode in ('anime', 'tv', 'movie')}
        self.auto_indexers = env.get('AUTO_INDEXERS', 'false').strip().lower() in ('1', 'yes', 'true', 'on')
        if not self.auto_indexers and not any(self.indexers_by_mode.values()):
            raise ValueError('Configure at least one indexer in indexers.json')
        for indexers in self.indexers_by_mode.values():
            if not all(str(k).isdigit() and isinstance(v, dict) for k, v in indexers.items()):
                raise ValueError('Indexer IDs must be numeric strings and settings must be objects')
        self.indexers = self.indexers_by_mode['anime']  # legacy tests / extensions
        self.discovery_excluded = {v.strip() for v in env.get('AUTO_EXCLUDE_IDS', '').split(',') if v.strip()}
        self.auto_max_queries = max(1, min(5, int(env.get('AUTO_MAX_QUERIES', '4'))))
        if self.auto_indexers:
            self.catalog = ProwlarrCatalog(
                self.prowlarr_url, self.prowlarr_key,
                ttl=int(env.get('AUTO_INDEXERS_TTL', '600')),
                timeout=int(env.get('AUTO_INDEXERS_TIMEOUT', '15')),
            )
        else:
            self.catalog = None
        self.overrides = load_json(env.get('OVERRIDES_FILE', '/app/overrides.json'), {'shows': []})
        # Optional explicit browse order, no hard-coded Prowlarr indexer ID.
        self.browse_ids = [str(i).strip() for i in env.get('BROWSE_INDEXER_IDS', '').split(',') if i.strip()]
        self.browse_max_indexers = max(1, min(20, int(env.get('BROWSE_MAX_INDEXERS', '4'))))
        # Safety limits apply per indexer + title combination. Browse only fetches one page.
        self.upstream_page_size = max(1, min(100, int(env.get('UPSTREAM_PAGE_SIZE', '100'))))
        self.upstream_max_pages = max(1, min(5, int(env.get('UPSTREAM_MAX_PAGES', '2'))))
        self.upstream_max_items = max(1, min(500, int(env.get('UPSTREAM_MAX_ITEMS', '200'))))
        self.cache = TTLCache(int(env.get('CACHE_SECONDS', '3600')))
        self.max_workers = max(1, min(6, int(env.get('MAX_WORKERS', '3'))))
        self.timeout = max(3, int(env.get('UPSTREAM_TIMEOUT', '40')))
        self.fetch_upstream = fetch_upstream or self._upstream
        self.resolver = resolver or Resolver(
            shiki_url=env.get('SHIKIMORI_URL', 'https://shikimori.io/api/graphql'),
            cache_path=env.get('SHIKIMORI_DB', '/app/data/anime-titles.sqlite'),
            ttl=int(env.get('TITLE_CACHE_SECONDS', '604800')),
            negative_ttl=int(env.get('NEGATIVE_CACHE_SECONDS', '7200')),
            overrides=self.overrides,
            timeout=int(env.get('SHIKIMORI_TIMEOUT', '12')),
            user_agent=env.get('METADATA_USER_AGENT', 'TitleTorznabBridge/0.1 (personal self-hosted proxy)'),
        )
        if generic_resolvers is None:
            generic_resolvers = {mode: GenericResolver(
                mode, env.get('TITLE_DB_' + mode.upper(), '/app/data/' + mode + '-titles.sqlite'),
                ttl=int(env.get('TITLE_CACHE_SECONDS', '604800')),
                negative_ttl=int(env.get('NEGATIVE_CACHE_SECONDS', '7200')),
                timeout=int(env.get('METADATA_TIMEOUT', '12')),
                tvmaze_url=env.get('TVMAZE_URL', 'https://api.tvmaze.com'),
                wikidata_url=env.get('WIKIDATA_URL', 'https://www.wikidata.org/w/api.php'),
                user_agent=env.get('METADATA_USER_AGENT', 'TitleTorznabBridge/0.1 (personal self-hosted proxy)'),
            ) for mode in ('tv', 'movie')}
        self.generic_resolvers = generic_resolvers
        # Optional read-only local catalogs. Both are fetched only if configured.
        self.sonarr_library = sonarr_library if sonarr_library is not None else (
            SonarrLibrary(env.get('SONARR_URL', 'http://sonarr:8989'), env['SONARR_API_KEY'])
            if env.get('SONARR_API_KEY') else None
        )
        self.radarr_library = radarr_library if radarr_library is not None else (
            RadarrLibrary(env.get('RADARR_URL', 'http://radarr:7878'), env['RADARR_API_KEY'])
            if env.get('RADARR_API_KEY') else None
        )

    def mode_indexers(self, mode: str) -> dict[str, dict[str, Any]]:
        """Select currently enabled indexers using live Prowlarr capabilities.

        Existing per-indexer config acts as an override, not an allowlist.
        """
        if mode not in self.indexers_by_mode:
            raise ValueError('unknown mode')
        if not self.auto_indexers:
            return self.indexers_by_mode[mode]
        result: dict[str, dict[str, Any]] = {}
        for record in self.catalog.get():
            idx = record['id']
            if mode not in record['modes'] or idx in self.discovery_excluded:
                continue
            settings = {
                'category': DEFAULT_CATEGORIES[mode],
                'language_order': DEFAULT_LANGUAGE_ORDER[mode],
                'max_queries': max(1, min(5, int(self.auto_max_queries))),
                '_english_only': record['english_only'],
                '_name': record['name'],
                '_language': record['language'],
            }
            # Preserve language_order, max_queries and test_query customizations.
            # Language restriction is ALWAYS enforced for English-only indexers.
            override = self.indexers_by_mode[mode].get(idx, {})
            # Legacy max_queries=2 was tuned for a two-indexer setup and would
            # silently omit available language families in discovery mode.
            # The new auto_max_queries key makes auto budgets explicit.
            settings.update({k: v for k, v in override.items() if k != 'max_queries'})
            if 'auto_max_queries' in override:
                settings['max_queries'] = max(1, min(5, int(override['auto_max_queries'])))
            if settings.get('disabled'):
                continue
            result[idx] = settings
        return result

    def indexer_diagnostics(self) -> dict[str, Any]:
        """Explain selected and rejected routes without searching any trackers."""
        selected = {}
        excluded = {}
        catalog = self.catalog.get_all() if self.auto_indexers else []
        for mode in ('anime', 'tv', 'movie'):
            active = self.mode_indexers(mode)
            selected[mode] = [
                {'id': idx, 'name': cfg.get('_name', idx),
                 'language': cfg.get('_language', ''),
                 'english_only': bool(cfg.get('_english_only')),
                 'category': str(cfg.get('category')),
                 'max_queries': cfg.get('max_queries')}
                for idx, cfg in active.items()
            ]
            excluded[mode] = []
            for record in catalog:
                idx = record['id']
                if idx in active:
                    continue
                reasons = list(record['eligibility_reasons'])
                if mode not in record['modes']:
                    reasons.append('category_not_suitable_for_' + mode)
                if idx in self.discovery_excluded:
                    reasons.append('excluded_by_auto_exclude_ids')
                if self.indexers_by_mode[mode].get(idx, {}).get('disabled'):
                    reasons.append('disabled_by_indexers_json')
                excluded[mode].append({
                    'id': idx,
                    'name': record['name'],
                    'language': record['language'],
                    'categories': record['categories'],
                    'reasons': reasons or ['not_selected'],
                })
        return {
            'routing': 'auto' if self.auto_indexers else 'manual',
            'modes': selected,
            'excluded': excluded,
            'notes': 'Exclusion reasons describe routing, not tracker availability or search quality.',
        }

    def resolve(self, mode: str, query: str, identifiers: dict | None = None) -> dict[str, Any]:
        """Prefer strict *Arr identity over fuzzy public names.

        With q='05', Sonarr's tvdbid identifies the series, while '05' must
        never be sent to Prowlarr as a title. External ID failures are safe.
        """
        if mode not in self.indexers_by_mode:
            raise ValueError('unknown mode')
        ids = identifiers or {}
        def arg(key):
            val = ids.get(key, '')
            return str(val[0] if isinstance(val, list) else val or '').strip()
        tvdbid, imdbid, tmdbid = arg('tvdbid'), arg('imdbid'), arg('tmdbid')
        variants = normalized_search_input(query, mode) if query.strip() else []
        episode_only = mode in ('tv', 'anime') and bool(re.fullmatch(r'\d{1,3}', query.strip()))

        # Respect explicit anime overrides from the legacy Shikimori resolver.
        if mode == 'anime' and not episode_only:
            manual_keys = getattr(self.resolver, '_overrides', {})
            if any(norm(candidate) in manual_keys for candidate in variants):
                return self.resolver.resolve(query)

        if mode != 'anime' and not episode_only:
            for candidate in variants:
                for show in self.overrides.get('shows', []):
                    types = show.get('types', show.get('type', ['anime']))
                    types = [types] if isinstance(types, str) else types
                    if mode in types and norm(candidate) in {norm(s) for s in show.get('match', [])}:
                        return {'source': 'manual', 'titles': show.get('titles', {}),
                                'indexers': show.get('indexers', {})}

        # Exact Sonarr identifiers work for both the TV and anime endpoints.
        if mode in ('tv', 'anime') and self.sonarr_library:
            show = (self.sonarr_library.match_by_ids(tvdb_id=tvdbid, imdb_id=imdbid)
                    if tvdbid or imdbid else None)
            if show is None and query.strip() and not episode_only and not (tvdbid or imdbid):
                for candidate in variants:
                    show = self.sonarr_library.match(candidate)
                    if show:
                        break
            if show:
                local = arr_titles(show, 'sonarr')
                if mode == 'anime':
                    title = str(show.get('title') or show.get('originalTitle') or '').strip()
                    if title:
                        shiki = self.resolver.resolve(title)
                        if shiki['source'] != 'fallback':
                            return merge_titles(local, shiki)
                    return local
                # TVmaze lacks IDs for some valid series (Highway/Трасса).
                # Local Russian alternateTitles are enough to search immediately.
                if local['titles']['russian']:
                    return local
                extra = self.generic_resolvers['tv'].resolve_by_external_ids(
                    str(show.get('title') or query), tvdb_id=show.get('tvdbId'),
                    imdb_id=show.get('imdbId'))
                return merge_titles(local, extra) if extra else local

        if mode == 'movie' and self.radarr_library:
            movie = (self.radarr_library.match_by_ids(tmdb_id=tmdbid, imdb_id=imdbid)
                     if tmdbid or imdbid else None)
            if movie is None and query.strip() and not episode_only and not (tmdbid or imdbid):
                for candidate in variants:
                    movie = self.radarr_library.match(candidate)
                    if movie:
                        break
            if movie:
                local = arr_titles(movie, 'radarr')
                if local['titles']['russian']:
                    return local
                extra = self.generic_resolvers['movie'].resolve_movie_by_external_ids(
                    tmdb_id=movie.get('tmdbId'), imdb_id=movie.get('imdbId'))
                return merge_titles(local, extra) if extra else local

        # Allow standalone exact ID lookup without local *Arr catalog.
        if mode == 'tv' and (tvdbid or imdbid):
            exact = self.generic_resolvers['tv'].resolve_by_external_ids(
                query, tvdb_id=tvdbid, imdb_id=imdbid)
            if exact:
                return exact
        if mode == 'movie' and (tmdbid or imdbid):
            exact = self.generic_resolvers['movie'].resolve_movie_by_external_ids(
                tmdb_id=tmdbid, imdb_id=imdbid)
            if exact:
                return exact

        # An episode number alone is not a searchable show name. Never send it
        # to Prowlarr, even if local metadata or external providers are offline.
        if not query.strip() or episode_only:
            return {'source': 'fallback', 'titles': {'original': []}, 'indexers': {}}

        if mode == 'anime':
            return self.resolver.resolve(query)
        for candidate in variants:
            resolved = self.generic_resolvers[mode].resolve(candidate)
            if resolved['source'] != 'fallback':
                return resolved
        return {'source': 'fallback', 'titles': {'original': [variants[-1]]}, 'indexers': {}}

    def _upstream(self, idx: str, term: str, category: str = '5070', *,
                  browse: bool = False) -> list[ET.Element]:
        """Page an individual Prowlarr query, safely, without interpreting releases.

        Some Torznab sources ignore offset; no-new-guid and page signature guards
        ensure those providers cannot generate an unbounded request loop.
        """
        page_size = self.upstream_page_size
        max_pages = 1 if browse else self.upstream_max_pages
        max_items = page_size if browse else self.upstream_max_items
        collected: list[ET.Element] = []
        seen_items: set[str] = set()
        seen_pages: set[tuple[str, ...]] = set()
        for page in range(max_pages):
            if len(collected) >= max_items:
                break
            offset = page * page_size
            params = urllib.parse.urlencode({
                't': 'search', 'q': term, 'cat': category, 'extended': '1',
                'offset': str(offset), 'limit': str(page_size),
                'apikey': self.prowlarr_key,
            })
            request = urllib.request.Request(f'{self.prowlarr_url}/{idx}/api?{params}', headers={
                'User-Agent': f'TitleTorznabBridge/{VERSION}',
            })
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read(6_000_001)
                if len(payload) > 6_000_000:
                    raise ValueError('Upstream RSS response too large')
                entries = read_rss(payload)
            except Exception as exc:
                if page == 0:
                    raise
                # Keep a previously successful page if a later one fails.
                LOG.warning('Prowlarr indexer %s additional page offset=%d failed: %s',
                            idx, offset, type(exc).__name__)
                break
            if not entries:
                break
            identities = tuple(item_identity(item) for item in entries)
            if identities in seen_pages:
                LOG.info('Prowlarr indexer %s repeated page offset=%d; stopping', idx, offset)
                break
            seen_pages.add(identities)
            fresh = 0
            for item, identity in zip(entries, identities):
                if identity not in seen_items:
                    seen_items.add(identity)
                    collected.append(item)
                    fresh += 1
                    if len(collected) >= max_items:
                        break
            if fresh == 0:
                LOG.info('Prowlarr indexer %s page offset=%d has no new releases; stopping', idx, offset)
                break
            if len(entries) < page_size:
                break
        return collected

    def _search_one(self, idx: str, term: str, category: str, *, browse: bool = False) -> list[ET.Element]:
        def producer():
            # Keep support for injected/upstream test functions with 2 or 3 args.
            if self.fetch_upstream == self._upstream:
                return self._upstream(idx, term, category, browse=browse)
            if getattr(self.fetch_upstream, '_legacy_two_args', False):
                return self.fetch_upstream(idx, term)
            return self.fetch_upstream(idx, term, category)
        items = self.cache.get_or_call((idx, norm(term), category, 'browse' if browse else 'search'), producer)
        LOG.info('Prowlarr indexer %s category %s: %d results (cache enabled)', idx, category, len(items))
        return items

    def browse(self, mode: str = 'anime') -> list[ET.Element]:
        """Try dynamically selected indexers, with a bounded number of probes."""
        indexers = self.mode_indexers(mode)
        if not indexers:
            return []
        order = [idx for idx in self.browse_ids if idx in indexers]
        order += [idx for idx in indexers if idx not in order]
        for idx in order[:self.browse_max_indexers]:
            settings = indexers[idx]
            category = str(settings.get('category', DEFAULT_CATEGORIES[mode]))
            try:
                items = self._search_one(idx, '', category, browse=True)
                if items:
                    return unique_items(items)
            except Exception as exc:
                LOG.warning('Browse %s indexer %s failed: %s', mode, idx, type(exc).__name__)
            fallback = settings.get('test_query')
            if fallback:
                try:
                    items = self._search_one(idx, fallback, category, browse=True)
                    if items:
                        return unique_items(items)
                except Exception as exc:
                    LOG.warning('Browse test query %s indexer %s failed: %s', mode, idx, type(exc).__name__)
        return []

    def terms(self, mode: str, query: str, identifiers: dict | None = None):
        info = self.resolve(mode, query, identifiers)
        targets = []
        for idx, settings in self.mode_indexers(mode).items():
            category = str(settings.get('category', DEFAULT_CATEGORIES[mode]))
            if self.auto_indexers:
                terms = automatic_search_terms(
                    info, idx, settings, query, english_only=bool(settings.get('_english_only'))
                )
            else:
                terms = search_terms(info, idx, settings, query)
            for title in terms:
                targets.append((idx, title, category))
        return info, targets

    def search(self, query: str, mode: str = 'anime', identifiers: dict | None = None) -> list[ET.Element]:
        if mode not in self.indexers_by_mode:
            raise ValueError('unknown mode')
        if not query.strip() and not identifiers:
            return self.browse(mode)
        info, targets = self.terms(mode, query, identifiers)
        LOG.info('%s search resolution=%s: %d upstream searches', mode, info['source'], len(targets))
        if not targets:
            return []
        items: list[ET.Element] = []
        errors = 0
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(targets))) as pool:
            futures = [(idx, pool.submit(self._search_one, idx, term, category)) for idx, term, category in targets]
            for idx, future in futures:
                try:
                    items.extend(future.result())
                except Exception as exc:
                    errors += 1
                    LOG.warning('Prowlarr indexer %s failed: %s', idx, type(exc).__name__)
        if errors == len(targets):
            raise RuntimeError('All upstream indexers failed')
        return unique_items(items)


class Handler(BaseHTTPRequestHandler):
    app: Application

    def log_message(self, _format, *_args):
        # HTTP request lines contain API keys; do not log them.
        return

    def send(self, status: int, payload: bytes, content_type: str = 'application/xml'):
        self.send_response(status)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == '/health':
            self.send(200, b'{"status":"ok"}', 'application/json')
            return
        if parsed.path == '/debug/indexers':
            args = urllib.parse.parse_qs(parsed.query)
            if not hmac.compare_digest(args.get('apikey', [''])[0], self.app.key):
                self.send(401, b'Unauthorized', 'text/plain')
                return
            try:
                payload = self.app.indexer_diagnostics()
            except Exception as exc:
                LOG.warning('Prowlarr discovery failed: %s', type(exc).__name__)
                self.send(502, b'Bad Gateway', 'text/plain')
                return
            self.send(200, json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8'), 'application/json')
            return
        if parsed.path in ('/anime/api', '/tv/api', '/movie/api'):
            mode = parsed.path.strip('/').split('/')[0]
            debug = False
        elif parsed.path in ('/debug/anime/resolve', '/debug/tv/resolve', '/debug/movie/resolve'):
            mode = parsed.path.split('/')[2]
            debug = True
        else:
            self.send(404, b'Not Found', 'text/plain')
            return
        args = urllib.parse.parse_qs(parsed.query)
        if not hmac.compare_digest(args.get('apikey', [''])[0], self.app.key):
            self.send(401, b'Unauthorized', 'text/plain')
            return
        identifiers = {key: args[key] for key in ('tvdbid', 'imdbid', 'tmdbid') if key in args}
        if debug:
            query = args.get('q', [''])[0].strip()
            if not query and not identifiers:
                self.send(400, b'Missing q or identifier', 'text/plain')
                return
            try:
                resolved, targets = self.app.terms(mode, query, identifiers)
            except Exception:
                self.send(502, b'Bad Gateway', 'text/plain')
                return
            queries: dict[str, list[str]] = {}
            for idx, term, _cat in targets:
                queries.setdefault(idx, []).append(term)
            result = {'type': mode, 'resolution': resolved, 'queries': queries}
            self.send(200, json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8'), 'application/json')
            return
        operation = args.get('t', ['search'])[0]
        if operation == 'caps':
            self.send(200, caps(mode))
            return
        allowed = ('search', 'tvsearch') if mode != 'movie' else ('search', 'movie')
        if operation not in allowed:
            self.send(200, rss([]))
            return
        query = args.get('q', [''])[0].strip()
        # Sonarr pages by offset/limit. Always slice the merged result and
        # report total: returning the first 100 for offset=700 causes loops.
        def numeric(name, default):
            raw = args.get(name, [str(default)])[0]
            return int(raw) if raw.isdecimal() else default
        offset = max(0, min(100000, numeric('offset', 0)))
        limit = max(1, min(100, numeric('limit', 100)))
        try:
            items = self.app.search(query, mode, identifiers)
        except Exception as exc:
            LOG.warning('%s search failed: %s', mode, type(exc).__name__)
            self.send(502, b'Bad Gateway', 'text/plain')
            return
        self.send(200, rss(items[offset:offset + limit], offset=offset, total=len(items)))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    app = Application()
    handler = type('BridgeHandler', (Handler,), {'app': app})
    port = int(os.environ.get('PORT', '8789'))
    LOG.info('Title Torznab Bridge v%s listening on %s; discovery=%s; manual_overrides=%s', VERSION, port, app.auto_indexers, {m: list(v) for m, v in app.indexers_by_mode.items()})
    ThreadingHTTPServer(('0.0.0.0', port), handler).serve_forever()


if __name__ == '__main__':
    main()

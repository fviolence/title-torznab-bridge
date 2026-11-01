"""Keyless multilingual metadata providers for television series and films.

TVmaze covers TV series and AKAs. Wikidata covers films and can supplement TV
names. Confidence checks are deliberately conservative: never select a merely
similar work and never alter torrent titles or reject releases by season.
"""
from __future__ import annotations

from contextlib import closing
import copy
import difflib
import json
import logging
from pathlib import Path
import re
import sqlite3
from storage import connect_sqlite
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Callable

LOG = logging.getLogger('title_bridge')


def _norm(text: str) -> str:
    import unicodedata
    text = unicodedata.normalize('NFKC', str(text or '')).casefold().replace('’', "'").replace("'s", 's')
    return ' '.join(re.sub(r'[^\w\s]', ' ', text).split())


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        value = str(value or '').strip()
        if value and _norm(value) not in seen:
            seen.add(_norm(value))
            out.append(value)
    return out


def _json_url(url: str, params: dict[str, str] | None = None, *, timeout: int = 12,
              user_agent: str = 'TitleTorznabBridge/0.1 (personal self-hosted proxy)') -> Any:
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': user_agent, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError('Metadata response too large')
    return json.loads(data)


class GenericResolver:
    """Persistent cache + TVmaze / Wikidata, no API keys required.

    fetch_json can be supplied by tests to avoid network access.
    """
    def __init__(self, kind: str, db_path: str, *, ttl: int = 604800,
                 negative_ttl: int = 3600, timeout: int = 12,
                 tvmaze_url: str = 'https://api.tvmaze.com',
                 wikidata_url: str = 'https://www.wikidata.org/w/api.php',
                 user_agent: str = 'TitleTorznabBridge/0.1 (personal self-hosted proxy)',
                 fetch_json: Callable | None = None):
        if kind not in ('tv', 'movie'):
            raise ValueError(f'unsupported kind: {kind}')
        self.kind, self.db_path = kind, db_path
        self.ttl, self.negative_ttl, self.timeout = ttl, negative_ttl, timeout
        self.tvmaze_url, self.wikidata_url, self.user_agent = tvmaze_url.rstrip('/'), wikidata_url, user_agent
        self.fetch_json = fetch_json or _json_url
        self._cache_lock = threading.Condition()
        self._busy: set[str] = set()
        self._last_error_until: dict[str, float] = {}
        self._request_lock = threading.Lock()
        self._next_request = 0.0
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS titles (query TEXT PRIMARY KEY, expires REAL NOT NULL, result TEXT NOT NULL)')

    def _db(self):
        return connect_sqlite(self.db_path)

    def _cached(self, key: str) -> tuple[bool, dict | None]:
        with closing(self._db()) as db:
            row = db.execute('SELECT expires, result FROM titles WHERE query=?', (key,)).fetchone()
        return (True, json.loads(row[1])) if row and row[0] > time.time() else (False, None)

    def _save(self, keys: list[str], value: dict | None):
        expiry = time.time() + (self.ttl if value else self.negative_ttl)
        with closing(self._db()) as db, db:
            db.executemany('INSERT OR REPLACE INTO titles VALUES (?,?,?)',
                           [(key, expiry, json.dumps(value, ensure_ascii=False)) for key in set(keys) if key])

    def _get(self, url: str, params: dict[str, str] | None = None):
        # Conservative global pacing shared by both providers within a resolver.
        with self._request_lock:
            wait = self._next_request - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next_request = time.monotonic() + 0.35
        return self.fetch_json(url, params, timeout=self.timeout, user_agent=self.user_agent)

    def _tvmaze(self, title: str) -> dict | None:
        results = self._get(self.tvmaze_url + '/search/shows', {'q': title})
        if not isinstance(results, list):
            return None
        # We intentionally avoid fuzzy match that could conflate remakes/years.
        candidates = [hit.get('show', {}) for hit in results[:10] if isinstance(hit, dict)]
        show = next((s for s in candidates if _norm(s.get('name', '')) == _norm(title)), None)
        if not show or not isinstance(show.get('id'), int):
            return None
        return self._tvmaze_from_show(show)

    def resolve_by_external_ids(self, title: str, *, tvdb_id=None, imdb_id=None) -> dict | None:
        """Find a TVmaze show by authoritative IDs from Sonarr, never by fuzzy title.

        Fails open to existing textual resolution. Caches positive and negative
        ID lookups in the same SQLite DB, but does not cache network failures.
        """
        if self.kind != 'tv':
            return None
        attempts = []
        if str(tvdb_id or '').isdigit() and int(tvdb_id) > 0:
            attempts.append(('thetvdb', str(tvdb_id)))
        if re.fullmatch(r'tt\d{5,12}', str(imdb_id or '')):
            attempts.append(('imdb', str(imdb_id)))
        for kind, value in attempts:
            cache_key = f'external:{kind}:{value}'
            cached, result = self._cached(cache_key)
            if cached:
                if result:
                    return copy.deepcopy(result)
                continue
            try:
                show = self._get(self.tvmaze_url + '/lookup/shows', {kind: value})
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    self._save([cache_key], None)
                else:
                    LOG.warning('TVmaze ID lookup failed: HTTP %s', exc.code)
                continue
            except Exception as exc:
                LOG.warning('TVmaze ID lookup failed: %s', type(exc).__name__)
                continue
            if not isinstance(show, dict) or not isinstance(show.get('id'), int):
                continue
            try:
                result = self._tvmaze_from_show(show, original_title=title)
            except Exception as exc:
                LOG.warning('TVmaze aliases lookup failed: %s', type(exc).__name__)
                continue
            result['source'] = 'sonarr+tvmaze'
            self._save([cache_key, _norm(title)], result)
            LOG.info('TV title resolved using Sonarr %s ID (TVmaze id=%s)', kind, show['id'])
            return copy.deepcopy(result)
        return None

    def resolve_movie_by_external_ids(self, *, tmdb_id=None, imdb_id=None) -> dict | None:
        """Exact TMDb/IMDb movie ID -> Wikidata Q-item -> localized labels.

        An unavailable WDQS never prevents use of Radarr's local aliases.
        Never choose between two conflicting Wikidata items.
        """
        if self.kind != 'movie':
            return None
        attempts = []
        if str(tmdb_id or '').isdigit() and int(tmdb_id) > 0:
            attempts.append(('P4947', str(tmdb_id)))
        if re.fullmatch(r'tt\d{5,12}', str(imdb_id or '')):
            attempts.append(('P345', str(imdb_id)))
        for prop, value in attempts:
            key = f'external:movie:{prop}:{value}'
            hit, cached = self._cached(key)
            if hit:
                if cached:
                    return copy.deepcopy(cached)
                continue
            # Only validated digits / IMDb IDs are interpolated into SPARQL.
            sparql = (f'SELECT DISTINCT ?item WHERE {{ ?item wdt:{prop} "{value}" . }} LIMIT 3')
            try:
                response = self._get('https://query.wikidata.org/sparql',
                                     {'query': sparql, 'format': 'json'})
                bindings = ((response.get('results') or {}).get('bindings') or [])
                ids = []
                for row in bindings:
                    uri = ((row.get('item') or {}).get('value') or '')
                    match = re.fullmatch(r'https?://www\.wikidata\.org/entity/(Q\d+)', uri)
                    if match:
                        ids.append(match.group(1))
                ids = list(dict.fromkeys(ids))
                if len(ids) > 1:
                    LOG.warning('Ambiguous Wikidata ID %s: %d entities', prop, len(ids))
                    continue
                if not ids:
                    self._save([key], None)
                    continue
                entity_data = self._get(self.wikidata_url, {
                    'action': 'wbgetentities', 'ids': ids[0], 'props': 'labels|aliases|descriptions',
                    'languages': 'en|ru', 'format': 'json'})
                entity = (entity_data.get('entities') or {}).get(ids[0], {})
                if not isinstance(entity, dict) or entity.get('missing') is not None:
                    continue
                titles = {}
                for language, bucket in (('en', 'english'), ('ru', 'russian')):
                    labels = entity.get('labels') or {}
                    aliases = entity.get('aliases') or {}
                    names = [((labels.get(language) or {}).get('value') or '')]
                    names += [x.get('value', '') for x in aliases.get(language, [])
                              if isinstance(x, dict)]
                    titles[bucket] = _unique(names)
                if not any(titles.values()):
                    continue
                result = {'source': 'wikidata-id', 'titles': titles,
                          'wikidata_id': ids[0], 'indexers': {}}
                self._save([key], result)
                return copy.deepcopy(result)
            except Exception as exc:
                LOG.warning('Wikidata ID lookup failed: %s', type(exc).__name__)
        return None

    def _tvmaze_from_show(self, show: dict, *, original_title: str = '') -> dict:
        aka = self._get(self.tvmaze_url + f"/shows/{show['id']}/akas")
        titles: dict[str, list[str]] = {'english': [], 'russian': [], 'synonym': []}
        for value in (show.get('name', ''), original_title):
            if isinstance(value, str) and value.strip():
                category = 'russian' if re.search(r'[А-Яа-яЁё]', value) else 'english'
                titles[category].append(value)
        if isinstance(aka, list):
            for alt in aka:
                if not isinstance(alt, dict):
                    continue
                value = alt.get('name', '')
                code = (alt.get('country') or {}).get('code', '') if isinstance(alt.get('country'), dict) else ''
                if code == 'RU' or re.search(r'[А-Яа-яЁё]', str(value)):
                    titles['russian'].append(value)
                elif code in ('US', 'GB', 'CA', 'AU'):
                    titles['english'].append(value)
                else:
                    titles['synonym'].append(value)
        return {'source': 'tvmaze', 'titles': {k: _unique(v) for k, v in titles.items()},
                'tvmaze_id': str(show['id']), 'indexers': {}}

    def _wikidata(self, title: str) -> dict | None:
        raw = self._get(self.wikidata_url, {'action': 'wbsearchentities', 'search': title,
                     'language': 'en' if re.search(r'[A-Za-z]', title) else 'ru',
                     'limit': '10', 'format': 'json', 'type': 'item'})
        hits = raw.get('search', []) if isinstance(raw, dict) else []
        ids = [h['id'] for h in hits if isinstance(h, dict) and re.fullmatch(r'Q\d+', str(h.get('id', '')))][:8]
        if not ids:
            return None
        body = self._get(self.wikidata_url, {'action': 'wbgetentities', 'ids': '|'.join(ids),
                         'props': 'labels|aliases|descriptions|claims',
                         'languages': 'en|ru', 'format': 'json'})
        entities = body.get('entities', {}) if isinstance(body, dict) else {}
        matches = []
        for i, key in enumerate(ids):
            entity = entities.get(key, {})
            if not isinstance(entity, dict):
                continue
            labels = entity.get('labels') or {}
            aliases = entity.get('aliases') or {}
            descriptions = entity.get('descriptions') or {}
            def vals(lang):
                vs = [labels.get(lang, {}).get('value', '')]
                vs.extend(x.get('value', '') for x in aliases.get(lang, []) if isinstance(x, dict))
                return _unique(vs)
            en, ru = vals('en'), vals('ru')
            # Selection must be by a known title, not a merely similar description.
            known = en + ru
            if _norm(title) not in {_norm(x) for x in known}:
                continue
            instance_ids = set()
            for claim in (entity.get('claims') or {}).get('P31', []):
                value = (claim.get('mainsnak') or {}).get('datavalue', {}).get('value', {})
                if isinstance(value, dict):
                    instance_ids.add(value.get('id'))
            desc = ' '.join((descriptions.get(x, {}).get('value', '') for x in ('en', 'ru'))).casefold()
            if self.kind == 'movie':
                classes = {'Q11424', 'Q24869', 'Q202866', 'Q506240'}  # film, feature, animated, TV film
                label_match = bool(instance_ids & classes) or bool(re.search(r'\b(film|movie|фильм|кинофильм|мультфильм)\b', desc))
            else:
                classes = {'Q5398426', 'Q15416', 'Q1259759'}  # TV series, TV programme, miniseries
                label_match = bool(instance_ids & classes) or bool(re.search(r'\b(television series|tv series|телесериал|телевизионный сериал)\b', desc))
            if not label_match:
                continue
            # Prefer direct P31 over textual description, preserving search rank otherwise.
            matches.append((0 if instance_ids & classes else 1, i, key, en, ru))
        if not matches:
            return None
        matches.sort()
        _, _, key, en, ru = matches[0]
        return {'source': 'wikidata', 'titles': {'english': en, 'russian': ru},
                'wikidata_id': key, 'indexers': {}}

    def resolve(self, query: str) -> dict[str, Any]:
        # Do NOT strip years here; film remakes can share a name.
        query = ' '.join(query.split())
        key = _norm(query)
        if not key:
            return {'source': 'fallback', 'titles': {'original': []}, 'indexers': {}}
        with self._cache_lock:
            while key in self._busy:
                self._cache_lock.wait()
            hit, value = self._cached(key)
            if hit:
                return value or self._fallback(query)
            self._busy.add(key)
        try:
            outcome: dict | None = None
            providers = [('tvmaze', self._tvmaze), ('wikidata', self._wikidata)] if self.kind == 'tv' else [('wikidata', self._wikidata)]
            for provider, func in providers:
                if self._last_error_until.get(provider, 0) > time.monotonic():
                    continue
                try:
                    candidate = func(query)
                except Exception as exc:
                    LOG.warning('%s metadata lookup failed: %s', provider, type(exc).__name__)
                    self._last_error_until[provider] = time.monotonic() + 90
                    continue
                if candidate:
                    if not outcome:
                        outcome = candidate
                    else:
                        # Merge only if both sources agreed on at least one full name.
                        names_a = {_norm(x) for vals in outcome['titles'].values() for x in vals}
                        names_b = {_norm(x) for vals in candidate['titles'].values() for x in vals}
                        if names_a & names_b:
                            for k, vals in candidate['titles'].items():
                                outcome['titles'][k] = _unique(outcome['titles'].get(k, []) + vals)
                            outcome['source'] += '+wikidata'
            if outcome:
                values = [x for vals in outcome['titles'].values() for x in vals]
                self._save([key] + [_norm(x) for x in values], outcome)
                LOG.info('%s title resolved via %s', self.kind, outcome['source'])
                return copy.deepcopy(outcome)
            # Only cache an actual negative response. If services are failing,
            # do not persist negative cache; a later request can recover.
            if not any(self._last_error_until.get(p, 0) > time.monotonic() for p, _ in providers):
                self._save([key], None)
            return self._fallback(query)
        finally:
            with self._cache_lock:
                self._busy.remove(key)
                self._cache_lock.notify_all()

    @staticmethod
    def _fallback(title):
        return {'source': 'fallback', 'titles': {'original': [title]}, 'indexers': {}}

"""Optional, read-only Sonarr series identity lookup.

Use the user's existing Sonarr library as the source of TVDB/IMDb IDs so
translated titles can be obtained without guessing or manually listing aliases.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
import urllib.request

LOG = logging.getLogger('title_bridge')


def _norm(value: str) -> str:
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    value = value.replace('’', "'").replace("'s", 's')
    return ' '.join(re.sub(r'[^\w\s]', ' ', value).split())


def _unarticle(value: str) -> str:
    return re.sub(r'^(?:the|a|an)\s+', '', value)


class SonarrLibrary:
    def __init__(self, url: str, api_key: str, *, ttl: int = 300,
                 timeout: int = 10, fetch=None):
        self.url = url.rstrip('/')
        self.key = api_key
        self.ttl = ttl
        self.timeout = timeout
        self.fetch = fetch or self._fetch
        self._lock = threading.Lock()
        self._expires = 0.0
        self._series: list[dict] = []

    def _fetch(self):
        if not self.url.startswith(('http://', 'https://')):
            raise ValueError('Invalid Sonarr URL')
        req = urllib.request.Request(
            self.url + '/api/v3/series',
            headers={'X-Api-Key': self.key, 'Accept': 'application/json',
                     'User-Agent': 'TitleTorznabBridge/SonarrLibrary'},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError('Sonarr library response too large')
        result = json.loads(raw)
        if not isinstance(result, list):
            raise ValueError('Sonarr series API did not return an array')
        return result

    def _all(self):
        with self._lock:
            if time.monotonic() >= self._expires:
                try:
                    self._series = self.fetch()
                    self._expires = time.monotonic() + self.ttl
                    LOG.info('%s title catalog refreshed (%d entries)', type(self).__name__, len(self._series))
                except Exception as exc:
                    # Keep previous data if available and retry no sooner than 60s.
                    LOG.warning('%s title catalog unavailable: %s', type(self).__name__, type(exc).__name__)
                    self._expires = time.monotonic() + 60
            return list(self._series)

    def match(self, query: str) -> dict | None:
        key = _norm(query)
        if not key:
            return None
        shows = self._all()

        def titles(show):
            values = [show.get('title', ''), show.get('originalTitle', '')]
            for entry in show.get('alternateTitles') or []:
                if isinstance(entry, dict):
                    values.append(entry.get('title', ''))
                elif isinstance(entry, str):
                    values.append(entry)
            return {_norm(v) for v in values if isinstance(v, str) and v.strip()}

        exact = [s for s in shows if isinstance(s, dict) and key in titles(s)]
        if len(exact) == 1:
            return exact[0]
        if exact:  # Ambiguous even at exact-match level: do not guess.
            return None
        # Sonarr may drop an English article ('Highway' vs 'The Highway').
        # Permit this only when precisely ONE library entry has that article variant.
        relaxed = [s for s in shows if isinstance(s, dict) and any(
            _unarticle(t) == _unarticle(key) for t in titles(s) if t
        )]
        return relaxed[0] if len(relaxed) == 1 else None

    def match_by_ids(self, *, tvdb_id=None, imdb_id=None) -> dict | None:
        """Use authoritative IDs over a query that may only contain an episode number."""
        tvdb = str(tvdb_id or '').strip()
        imdb = str(imdb_id or '').strip().casefold()
        if not (tvdb.isdecimal() and int(tvdb) > 0) and not re.fullmatch(r'tt\d{5,12}', imdb):
            return None
        shows = [s for s in self._all() if isinstance(s, dict)]
        # Prefer TVDB for Sonarr, and never return an ambiguous ID match.
        for field, value in (('tvdbId', tvdb if tvdb.isdecimal() and int(tvdb) > 0 else ''),
                             ('imdbId', imdb if re.fullmatch(r'tt\d{5,12}', imdb) else '')):
            if value:
                matches = [s for s in shows if str(s.get(field) or '').strip().casefold() == value]
                if len(matches) == 1:
                    # Contradicting supplied IDs are not evidence of identity.
                    other_field = 'imdbId' if field == 'tvdbId' else 'tvdbId'
                    other_value = imdb if other_field == 'imdbId' else tvdb
                    stored = str(matches[0].get(other_field) or '').strip().casefold()
                    if other_value and stored and stored != other_value:
                        return None
                    return matches[0]
                if len(matches) > 1:
                    return None
        return None

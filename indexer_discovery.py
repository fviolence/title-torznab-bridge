"""Read-only Prowlarr indexer discovery and conservative Torznab-category routing.

Nothing is inferred from tracker names. Prowlarr category declarations can be
imprecise: a tracker with only generic TV + TV/Anime is treated as anime-only.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
import urllib.request
from typing import Any

LOG = logging.getLogger('title_bridge')

DEFAULT_CATEGORIES = {'anime': '5070', 'tv': '5000', 'movie': '2000'}
DEFAULT_LANGUAGE_ORDER = {
    'anime': ['russian', 'romaji', 'english', 'synonym', 'original'],
    'tv': ['russian', 'english', 'synonym', 'original'],
    'movie': ['russian', 'english', 'synonym', 'original'],
}


def category_ids(value: Any) -> set[int]:
    """Flatten Prowlarr's category/subCategories hierarchy."""
    result: set[int] = set()
    def visit(item: Any) -> None:
        if not isinstance(item, dict):
            return
        raw = item.get('id')
        try:
            if raw is not None:
                result.add(int(raw))
        except (ValueError, TypeError):
            pass
        for sub in item.get('subCategories') or []:
            visit(sub)
    for item in value if isinstance(value, list) else []:
        visit(item)
    return result


def appropriate_modes(categories: set[int]) -> set[str]:
    result: set[str] = set()
    movie_categories = {c for c in categories if 2000 <= c < 3000}
    tv_categories = {c for c in categories if 5000 <= c < 6000}
    anime_only_tv = 5070 in tv_categories and tv_categories <= {5000, 5070}
    # A tracker advertising only Movies (root) plus TV/Anime often exposes
    # anime feature films rather than a general film search catalog.
    anime_only_movie = movie_categories == {2000} and anime_only_tv
    if movie_categories and not anime_only_movie:
        result.add('movie')
    if tv_categories and not anime_only_tv:
        result.add('tv')
    if 5070 in tv_categories:
        result.add('anime')
    return result


def english_only(language: Any) -> bool:
    """en, en-US, en_GB => English-only; ru/ja/multi/unknown => unrestricted."""
    if not isinstance(language, str):
        return False
    return language.strip().lower().replace('_', '-').split('-')[0] == 'en'


def assessed_indexers(catalog: Any) -> list[dict[str, Any]]:
    """Inspect every identifiable source, including disabled/unsupported ones.

    Eligibility reasons are stable machine-readable codes exposed by the debug API.
    No credentials, indexer configuration fields or tracker URLs are returned.
    """
    if not isinstance(catalog, list):
        raise ValueError('Invalid Prowlarr indexer catalog')
    output = []
    seen = set()
    for entry in catalog:
        if not isinstance(entry, dict):
            continue
        try:
            identifier = str(int(entry.get('id')))
        except (TypeError, ValueError):
            continue
        if identifier in seen:
            continue
        seen.add(identifier)
        reasons = []
        if entry.get('enable') is not True:
            reasons.append('disabled_in_prowlarr')
        if entry.get('supportsSearch') is not True:
            reasons.append('search_not_supported')
        if str(entry.get('protocol', '')).casefold() != 'torrent':
            reasons.append('not_torrent_protocol')
        cats = category_ids((entry.get('capabilities') or {}).get('categories', []))
        modes = appropriate_modes(cats)
        if not modes:
            reasons.append('no_matching_search_categories')
        output.append({
            'id': identifier,
            'name': str(entry.get('name') or identifier),
            'language': str(entry.get('language') or ''),
            'english_only': english_only(entry.get('language')),
            'modes': sorted(modes),
            'categories': sorted(cats),
            'priority': entry.get('priority', 25),
            'tags': [str(tag) for tag in entry.get('tags', []) if isinstance(tag, int)],
            'eligibility_reasons': reasons,
        })
    output.sort(key=lambda x: int(x['id']))
    return output


def eligible_indexers(catalog: Any) -> list[dict[str, Any]]:
    return [{key: value for key, value in record.items() if key != 'eligibility_reasons'}
            for record in assessed_indexers(catalog) if not record['eligibility_reasons']]


class ProwlarrCatalog:
    """Single-flight TTL cache; fail closed if Prowlarr catalog is unavailable."""
    def __init__(self, base_url: str, api_key: str, *, ttl: int = 600, timeout: int = 15, fetch=None):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.ttl = max(30, ttl)
        self.timeout = timeout
        self.fetch = fetch or self._fetch
        self._cached: list[dict] | None = None
        self._all_cached: list[dict] | None = None
        self._expires = 0.0
        self._busy = False
        self._cv = threading.Condition()

    def _fetch(self):
        request = urllib.request.Request(
            self.base_url + '/api/v1/indexer',
            headers={'X-Api-Key': self.api_key, 'User-Agent': 'TitleTorznabBridge/discovery'},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = response.read(3_000_001)
        if len(data) > 3_000_000:
            raise ValueError('Prowlarr indexer catalog too large')
        return json.loads(data)

    def get_all(self) -> list[dict]:
        with self._cv:
            while True:
                if self._cached is not None and time.monotonic() < self._expires:
                    return copy.deepcopy(self._all_cached)
                if not self._busy:
                    self._busy = True
                    break
                self._cv.wait()
        try:
            all_entries = assessed_indexers(self.fetch())
            parsed = [{key: value for key, value in record.items() if key != 'eligibility_reasons'}
                      for record in all_entries if not record['eligibility_reasons']]
            with self._cv:
                self._all_cached = all_entries
                self._cached = parsed
                self._expires = time.monotonic() + self.ttl
                LOG.info('Prowlarr indexer discovery: %d eligible sources', len(parsed))
            return copy.deepcopy(all_entries)
        finally:
            with self._cv:
                self._busy = False
                self._cv.notify_all()

    def get(self) -> list[dict]:
        # Uses the same single-flight cached catalog as diagnostics.
        self.get_all()
        with self._cv:
            return copy.deepcopy(self._cached)

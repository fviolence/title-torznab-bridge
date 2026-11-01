"""Optional, read-only Radarr movie catalog lookup (API v3)."""
from __future__ import annotations

import json
import re
import urllib.request
from sonarr_library import SonarrLibrary, _norm, _unarticle


class RadarrLibrary(SonarrLibrary):
    """Reuse Sonarr's cached read-only catalog, but query Radarr /movie."""

    def _fetch(self):
        if not self.url.startswith(('http://', 'https://')):
            raise ValueError('Invalid Radarr URL')
        req = urllib.request.Request(
            self.url + '/api/v3/movie',
            headers={'X-Api-Key': self.key, 'Accept': 'application/json',
                     'User-Agent': 'TitleTorznabBridge/RadarrLibrary'},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError('Radarr catalog too large')
        result = json.loads(raw)
        if not isinstance(result, list):
            raise ValueError('Radarr movie API did not return an array')
        return result

    def match_by_ids(self, *, tmdb_id=None, imdb_id=None):
        tmdb = str(tmdb_id or '').strip()
        imdb = str(imdb_id or '').strip().casefold()
        if not (tmdb.isdecimal() and int(tmdb) > 0) and not re.fullmatch(r'tt\d{5,12}', imdb):
            return None
        movies = [m for m in self._all() if isinstance(m, dict)]
        for field, value in (('tmdbId', tmdb if tmdb.isdecimal() and int(tmdb) > 0 else ''),
                             ('imdbId', imdb if re.fullmatch(r'tt\d{5,12}', imdb) else '')):
            if value:
                matches = [m for m in movies if str(m.get(field) or '').strip().casefold() == value]
                if len(matches) == 1:
                    other_field = 'imdbId' if field == 'tmdbId' else 'tmdbId'
                    other_value = imdb if other_field == 'imdbId' else tmdb
                    stored = str(matches[0].get(other_field) or '').strip().casefold()
                    if other_value and stored and stored != other_value:
                        return None
                    return matches[0]
                if len(matches) > 1:
                    return None
        return None

    def match(self, query: str):
        """Match exact movie title (optionally followed by release year)."""
        query = str(query or '').strip()
        year_match = re.search(r'\s+\(?(18\d{2}|19\d{2}|20\d{2})\)?$', query)
        year = int(year_match.group(1)) if year_match else None
        stem = query[:year_match.start()].strip() if year_match else query
        key = _norm(stem)
        if not key:
            return None
        def all_titles(movie):
            vals = [movie.get('title', ''), movie.get('originalTitle', '')]
            vals += [(alt.get('title') if isinstance(alt, dict) else alt)
                     for alt in movie.get('alternateTitles') or []]
            return {_norm(v) for v in vals if isinstance(v, str) and v.strip()}
        movies = [m for m in self._all() if isinstance(m, dict)
                  and (not year or str(m.get('year') or '') == str(year))]
        matched = [m for m in movies if key in all_titles(m)]
        if len(matched) == 1:
            return matched[0]
        if matched:
            return None
        # Article normalization only when it produces precisely one candidate.
        matched = [m for m in movies if any(_unarticle(s) == _unarticle(key)
                                             for s in all_titles(m) if s)]
        return matched[0] if len(matched) == 1 else None

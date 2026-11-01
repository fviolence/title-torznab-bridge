"""Regression tests for Highway, numeric q, Radarr IDs, and paging."""
import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer
from pathlib import Path

import bridge
from metadata import GenericResolver
from radarr_library import RadarrLibrary
from sonarr_library import SonarrLibrary

HIGHWAY = {
    'title': 'Highway', 'originalTitle': None,
    'alternateTitles': [{'title': 'Трасса', 'seasonNumber': -1}],
    'tvdbId': 454122, 'imdbId': 'tt30415293'
}
KUROKO = {
    'title': "Kuroko's Basketball", 'originalTitle': 'Kuroko no Basuke',
    'alternateTitles': [{'title': 'Баскетбол Куроко'}],
    'tvdbId': 257765, 'imdbId': 'tt2348803'
}
MATRIX = {
    'title': 'The Matrix', 'originalTitle': 'The Matrix',
    'alternateTitles': [{'title': 'Матрица'}],
    'tmdbId': 603, 'imdbId': 'tt0133093', 'year': 1999
}


class NoMetadata:
    def resolve(self, query):
        raise AssertionError('Remote provider must not be called with local Russian aliases')
    def resolve_by_external_ids(self, *args, **kwargs):
        raise AssertionError('TVmaze must not be needed for Highway')
    def resolve_movie_by_external_ids(self, *args, **kwargs):
        raise AssertionError('Wikidata must not be needed for Matrix when Radarr has Russian alias')


class NoAnime:
    def resolve(self, query):
        return {'source': 'fallback', 'titles': {'original': [query]}, 'indexers': {}}


class IdentityAndPagingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        (self.path / 'indexers.json').write_text(json.dumps({
            'anime': {'6': {'category': '5070', 'language_order': ['russian'], 'max_queries': 1}},
            'tv': {'4': {'category': '5000', 'language_order': ['russian', 'english'], 'max_queries': 2}},
            'movie': {'3': {'category': '2000', 'language_order': ['russian', 'english'], 'max_queries': 2}},
        }))
        (self.path / 'overrides.json').write_text('{"shows": []}')
        self.calls = []
        def upstream(idx, term, cat):
            self.calls.append((idx, term, cat))
            result = []
            # 65 results per upstream search, allowing test of merged paging.
            for i in range(65):
                item = ET.Element('item')
                ET.SubElement(item, 'guid').text = f'{idx}:{term}:{i}'
                ET.SubElement(item, 'title').text = f'Original release title {term} #{i}'
                ET.SubElement(item, 'link').text = f'https://example.test/{i}'
                result.append(item)
            return result
        self.app = bridge.Application(
            env={'BRIDGE_API_KEY': 'test-key', 'PROWLARR_API_KEY': 'upstream-key',
                 'INDEXERS_FILE': str(self.path / 'indexers.json'),
                 'OVERRIDES_FILE': str(self.path / 'overrides.json')},
            resolver=NoAnime(), generic_resolvers={'tv': NoMetadata(), 'movie': NoMetadata()},
            sonarr_library=SonarrLibrary('http://sonarr', 'secret', fetch=lambda: [HIGHWAY, KUROKO]),
            radarr_library=RadarrLibrary('http://radarr', 'secret', fetch=lambda: [MATRIX]),
            fetch_upstream=upstream)

    def test_highway_uses_own_sonarr_alias_even_without_tvmaze_id(self):
        for title in ('Highway', 'The Highway', 'Трасса'):
            with self.subTest(title=title):
                info, targets = self.app.terms('tv', title)
                self.assertEqual(info['source'], 'sonarr')
                self.assertEqual(info['titles']['russian'], ['Трасса'])
                self.assertEqual(targets[0], ('4', 'Трасса', '5000'))

    def test_numeric_tv_episode_uses_tvdb_identity_not_05(self):
        info, targets = self.app.terms('tv', '05', {'tvdbid': ['257765'], 'imdbid': ['tt2348803']})
        self.assertEqual(info['source'], 'sonarr')
        self.assertEqual(targets[0], ('4', 'Баскетбол Куроко', '5000'))
        self.assertNotIn('05', [row[1] for row in targets])
        info, targets = self.app.terms('tv', '05')
        self.assertEqual(targets, [])
        self.assertEqual(info['titles']['original'], [])
        self.assertEqual(self.app.search('05', 'tv'), [])
        self.assertEqual(self.calls, [])

    def test_numeric_anime_episode_uses_sonarr_identity_and_skips_fallback(self):
        info, targets = self.app.terms('anime', '05', {'tvdbid': ['257765']})
        self.assertEqual(info['source'], 'sonarr')
        self.assertEqual(targets, [('6', 'Баскетбол Куроко', '5070')])

    def test_radarr_russian_alias_and_id_only_query(self):
        for query, ids in [('The Matrix', None), ('', {'tmdbid': ['603']}),
                           ('', {'imdbid': ['tt0133093']})]:
            with self.subTest(query=query, ids=ids):
                info, terms = self.app.terms('movie', query, ids)
                self.assertEqual(info['source'], 'radarr')
                self.assertEqual(terms[0], ('3', 'Матрица', '2000'))

    def test_mismatched_ids_never_select_catalog_entry(self):
        cat = SonarrLibrary('http://sonarr', 'secret', fetch=lambda: [HIGHWAY])
        self.assertIsNone(cat.match_by_ids(tvdb_id=454122, imdb_id='tt9999999'))
        cat = RadarrLibrary('http://radarr', 'secret', fetch=lambda: [MATRIX])
        self.assertIsNone(cat.match_by_ids(tmdb_id=603, imdb_id='tt9999999'))

    def test_radarr_year_disambiguates_and_unspecified_is_ambiguous(self):
        cat = RadarrLibrary('http://radarr', 'secret', fetch=lambda: [
            {'title': 'A Movie', 'year': 1990, 'tmdbId': 1},
            {'title': 'A Movie', 'year': 2020, 'tmdbId': 2},
        ])
        self.assertIsNone(cat.match('A Movie'))
        self.assertEqual(cat.match('A Movie (2020)')['tmdbId'], 2)
        self.assertEqual(cat.match_by_ids(tmdb_id=1)['year'], 1990)

    def test_debug_http_honors_ids_without_contacting_trackers(self):
        handler = type('DebugHandler', (bridge.Handler,), {'app': self.app})
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            query = urllib.parse.urlencode({
                'apikey': 'test-key', 'q': '05', 'tvdbid': '257765',
                'imdbid': 'tt2348803'})
            with urllib.request.urlopen(
                f'http://127.0.0.1:{server.server_port}/debug/tv/resolve?{query}', timeout=5) as r:
                data = json.load(r)
            self.assertEqual(data['resolution']['source'], 'sonarr')
            self.assertEqual(data['queries']['4'][0], 'Баскетбол Куроко')
            self.assertNotIn('05', data['queries']['4'])
            self.assertEqual(self.calls, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_numeric_movie_title_not_treated_as_episode(self):
        class FallbackMovie:
            def resolve(self, q):
                return {'source': 'fallback', 'titles': {'original': [q]}, 'indexers': {}}
        self.app.generic_resolvers['movie'] = FallbackMovie()
        info, targets = self.app.terms('movie', '9')
        self.assertEqual(targets[0], ('3', '9', '2000'))

    def test_pagination_stops_repeated_pages_and_retains_torrent_data(self):
        handler = type('TestHandler', (bridge.Handler,), {'app': self.app})
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def query(offset):
            args = urllib.parse.urlencode({
                'apikey': 'test-key', 't': 'tvsearch', 'q': 'Highway',
                'offset': offset, 'limit': 100})
            with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/tv/api?{args}', timeout=5) as r:
                root = ET.fromstring(r.read())
            paging = root.find(f'./channel/{{{bridge.NEWZNAB_NS}}}response')
            return root.findall('./channel/item'), paging.attrib
        first, p0 = query(0)
        second, p100 = query(100)
        later, p700 = query(700)
        self.assertEqual((len(first), len(second), len(later)), (100, 30, 0))
        self.assertEqual(p0, {'offset': '0', 'total': '130'})
        self.assertEqual(p100, {'offset': '100', 'total': '130'})
        self.assertEqual(p700, {'offset': '700', 'total': '130'})
        self.assertEqual(first[0].findtext('title'), 'Original release title Трасса #0')
        self.assertEqual(len(self.calls), 2)  # Cached upstream searches; not 6.


class WikidataMovieIdTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / 'movie.db')
        self.calls = []

    def fake_get(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        if url.endswith('/sparql'):
            self.assertIn('P4947 "603"', params['query'])
            return {'results': {'bindings': [
                {'item': {'value': 'http://www.wikidata.org/entity/Q83495'}}]}}
        if params and params.get('action') == 'wbgetentities':
            return {'entities': {'Q83495': {
                'labels': {'en': {'value': 'The Matrix'}, 'ru': {'value': 'Матрица'}},
                'aliases': {'ru': [{'value': 'Матрица (фильм)'}]},
            }}}
        raise AssertionError(url)

    def test_id_lookup_uses_exact_tmdb_property(self):
        resolver = GenericResolver('movie', self.path, fetch_json=self.fake_get)
        info = resolver.resolve_movie_by_external_ids(tmdb_id=603)
        self.assertEqual(info['source'], 'wikidata-id')
        self.assertEqual(info['titles']['russian'][0], 'Матрица')
        self.assertEqual(info['wikidata_id'], 'Q83495')
        self.assertEqual(resolver.resolve_movie_by_external_ids(tmdb_id=603), info)
        self.assertEqual(len(self.calls), 2)

    def test_ambiguous_id_is_not_guessed(self):
        def ambiguous(url, params=None, **kw):
            return {'results': {'bindings': [
                {'item': {'value': 'http://www.wikidata.org/entity/Q1'}},
                {'item': {'value': 'http://www.wikidata.org/entity/Q2'}}]}}
        resolver = GenericResolver('movie', self.path, fetch_json=ambiguous)
        self.assertIsNone(resolver.resolve_movie_by_external_ids(tmdb_id=603))


if __name__ == '__main__':
    unittest.main()

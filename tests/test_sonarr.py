"""Regression tests for optional, read-only Sonarr title resolution."""
import json
import tempfile
import unittest
from pathlib import Path

from sonarr_library import SonarrLibrary
from metadata import GenericResolver
import bridge


SERIES = [
    {'title': 'The Highway', 'tvdbId': 454122, 'imdbId': 'tt30415293',
     'alternateTitles': [], 'year': 2024},
    {'title': 'Breaking Bad', 'tvdbId': 81189, 'imdbId': 'tt0903747',
     'alternateTitles': [{'title': 'Во все тяжкие'}]},
]


class SonarrCatalogTests(unittest.TestCase):
    def test_exact_matches_and_unique_article_normalization(self):
        catalog = SonarrLibrary('http://sonarr:8989', 'test-key', fetch=lambda: SERIES)
        self.assertEqual(catalog.match('The Highway')['tvdbId'], 454122)
        self.assertEqual(catalog.match('Highway')['tvdbId'], 454122)
        self.assertEqual(catalog.match('Во все тяжкие')['tvdbId'], 81189)
        self.assertIsNone(catalog.match('Stranger Things'))

    def test_ambiguous_leading_article_is_never_guessed(self):
        catalog = SonarrLibrary('http://sonarr:8989', 'test-key', fetch=lambda: [
            {'title': 'The Highway'}, {'title': 'A Highway'}
        ])
        self.assertIsNone(catalog.match('Highway'))

    def test_catalog_is_cached(self):
        counter = []
        def fetch():
            counter.append(1)
            return SERIES
        catalog = SonarrLibrary('http://sonarr:8989', 'test-key', fetch=fetch)
        catalog.match('Highway')
        catalog.match('Breaking Bad')
        self.assertEqual(len(counter), 1)

    def test_catalog_error_is_safe(self):
        catalog = SonarrLibrary('http://sonarr:8989', 'test-key', fetch=lambda: (_ for _ in ()).throw(OSError()))
        self.assertIsNone(catalog.match('Highway'))


class TVmazeIdLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []

    def fake_get(self, url, params=None, **_kw):
        self.calls.append((url, params))
        if url.endswith('/lookup/shows'):
            self.assertEqual(params, {'thetvdb': '454122'})
            return {'id': 72847, 'name': 'Трасса'}
        if url.endswith('/shows/72847/akas'):
            return []
        self.fail('Unexpected URL: ' + url)

    def test_tvdb_mapping_no_akas(self):
        r = GenericResolver('tv', str(Path(self.tmp.name) / 'tv.db'), fetch_json=self.fake_get)
        found = r.resolve_by_external_ids('The Highway', tvdb_id=454122)
        self.assertEqual(found['source'], 'sonarr+tvmaze')
        self.assertEqual(found['titles']['russian'], ['Трасса'])
        self.assertEqual(found['titles']['english'], ['The Highway'])
        n = len(self.calls)
        self.assertEqual(r.resolve_by_external_ids('The Highway', tvdb_id=454122), found)
        self.assertEqual(len(self.calls), n)
        self.assertEqual(r.resolve('The Highway')['titles']['russian'], ['Трасса'])

    def test_without_valid_ids_returns_none(self):
        r = GenericResolver('tv', str(Path(self.tmp.name) / 'tv.db'), fetch_json=self.fake_get)
        self.assertIsNone(r.resolve_by_external_ids('Highway'))
        self.assertEqual(self.calls, [])


class ApplicationIdentityTests(unittest.TestCase):
    def test_sonarr_title_resolves_before_ordinary_text_search(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'indexers.json').write_text(json.dumps({
                'tv': {'4': {'category': '5000', 'language_order': ['russian', 'english'], 'max_queries': 2}}
            }))
            (root/'overrides.json').write_text('{"shows":[]}')
            def fake_get(url, params=None, **_kw):
                if '/lookup/shows' in url:
                    return {'id': 72847, 'name': 'Трасса'}
                if '/shows/72847/akas' in url:
                    return []
                raise AssertionError('Unexpected metadata lookup: ' + url)
            r = GenericResolver('tv', str(root/'tv.db'), fetch_json=fake_get)
            catalog = SonarrLibrary('http://sonarr:8989', 'test', fetch=lambda: SERIES)
            # Disable remote Shikimori by using a test double.
            class NoAnime:
                def resolve(self, query):
                    return {'source': 'fallback', 'titles': {'original': [query]}, 'indexers': {}}
            app = bridge.Application(
                env={'BRIDGE_API_KEY': 'bridge-key', 'PROWLARR_API_KEY': 'prowlarr-key',
                     'INDEXERS_FILE': str(root/'indexers.json'),
                     'OVERRIDES_FILE': str(root/'overrides.json')},
                resolver=NoAnime(), generic_resolvers={'tv': r, 'movie': r},
                sonarr_library=catalog, fetch_upstream=lambda *args: []
            )
            resolved = app.resolve('tv', 'Highway')
            self.assertEqual(resolved['source'], 'sonarr+tvmaze')
            self.assertEqual(resolved['titles']['russian'], ['Трасса'])
            info, searches = app.terms('tv', 'Highway')
            self.assertEqual(searches[0], ('4', 'Трасса', '5000'))
            self.assertEqual(info['source'], 'sonarr+tvmaze')


if __name__ == '__main__':
    unittest.main()

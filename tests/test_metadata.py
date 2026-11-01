"""Network-free checks for keyless international metadata sources."""
import json
import tempfile
import unittest
from pathlib import Path

from metadata import GenericResolver


def entity(qid, name_en, name_ru, *, kind, english_aliases=(), russian_aliases=()):
    return {
        'labels': {'en': {'value': name_en}, 'ru': {'value': name_ru}},
        'aliases': {
            'en': [{'value': x} for x in english_aliases],
            'ru': [{'value': x} for x in russian_aliases],
        },
        'claims': {'P31': [{'mainsnak': {'datavalue': {'value': {'id': kind}}}}]},
        'descriptions': {'en': {'value': 'film' if kind == 'Q11424' else 'television series'}},
    }


class KeylessMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []

    def get(self, url, params=None, **_kw):
        self.calls.append((url, params))
        if '/search/shows' in url:
            return [{'show': {'id': 169, 'name': 'Breaking Bad'}},
                    {'show': {'id': 100, 'name': 'Break Even'}}]
        if '/shows/169/akas' in url:
            return [{'name': 'Во все тяжкие', 'country': {'code': 'RU'}},
                    {'name': 'Breaking Bad', 'country': {'code': 'US'}}]
        if params and params['action'] == 'wbsearchentities':
            if params['search'] == 'The Matrix':
                return {'search': [{'id': 'Q83495'}, {'id': 'Q123456'}]}
            if params['search'] == 'Breaking Bad':
                return {'search': [{'id': 'Q1079'}]}
            return {'search': [{'id': 'Q1079'}]}
        if params and params['action'] == 'wbgetentities':
            return {'entities': {
                'Q83495': entity('Q83495', 'The Matrix', 'Матрица', kind='Q11424'),
                'Q123456': entity('Q123456', 'The Matrix', 'Матрица', kind='Q5398426'),
                'Q1079': entity('Q1079', 'Breaking Bad', 'Во все тяжкие', kind='Q5398426'),
            }}
        self.fail('Unexpected URL: ' + url)

    def resolver(self, kind):
        return GenericResolver(kind, str(Path(self.tmp.name) / (kind + '.db')),
                               fetch_json=self.get)

    def test_tvmaze_russian_aka_with_wikidata_merge(self):
        r = self.resolver('tv')
        found = r.resolve('Breaking Bad')
        self.assertIn('tvmaze', found['source'])
        self.assertEqual(found['titles']['russian'], ['Во все тяжкие'])
        self.assertEqual(found['titles']['english'], ['Breaking Bad'])
        self.assertEqual(r.resolve('Во все тяжкие')['source'], found['source'])
        before = len(self.calls)
        # Multiple aliases share a persistent cache.
        r2 = self.resolver('tv')
        self.assertEqual(r2.resolve('Breaking Bad')['titles']['russian'], ['Во все тяжкие'])
        self.assertEqual(len(self.calls), before)

    def test_movies_use_wikidata_not_tvmaze(self):
        found = self.resolver('movie').resolve('The Matrix')
        self.assertEqual(found['source'], 'wikidata')
        self.assertEqual(found['titles']['russian'], ['Матрица'])
        self.assertFalse(any('/search/shows' in url for url, _ in self.calls))

    def test_exact_match_type_conflict_is_rejected(self):
        found = self.resolver('movie').resolve('Breaking Bad')
        self.assertEqual(found['source'], 'fallback')
        self.assertEqual(found['titles']['original'], ['Breaking Bad'])

    def test_fallback_when_provider_unavailable(self):
        def fail(*_args, **_kwargs):
            raise TimeoutError('metadata offline')
        r = GenericResolver('movie', str(Path(self.tmp.name) / 'fail.db'), fetch_json=fail)
        self.assertEqual(r.resolve('Any Movie')['source'], 'fallback')


if __name__ == '__main__':
    unittest.main()

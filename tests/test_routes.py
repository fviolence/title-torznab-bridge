"""HTTP contract checks for independent anime/TV/movie Torznab endpoints."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer
from pathlib import Path

import bridge


class FakeResolver:
    def __init__(self, result):
        self.result = result
        self.queries = []
    def resolve(self, q):
        self.queries.append(q)
        return self.result


class HttpModesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        config = {
            'anime': {'6': {'category': '5070', 'language_order': ['russian'], 'max_queries': 1}},
            'tv': {'4': {'category': '5000', 'language_order': ['russian'], 'max_queries': 1}},
            'movie': {'3': {'category': '2000', 'language_order': ['russian'], 'max_queries': 1}},
        }
        (root / 'indexers.json').write_text(json.dumps(config))
        (root / 'overrides.json').write_text('{"shows": []}')
        self.env = {
            'BRIDGE_API_KEY': 'test-key', 'PROWLARR_API_KEY': 'secret',
            'INDEXERS_FILE': str(root / 'indexers.json'),
            'OVERRIDES_FILE': str(root / 'overrides.json'),
            'SHIKIMORI_DB': str(root / 'shikimori.db'),
            'TITLE_DB_TV': str(root / 'tv.db'),
            'TITLE_DB_MOVIE': str(root / 'movie.db'),
        }
        self.calls = []
        def fake_indexer(idx, text, cat):
            self.calls.append((idx, text, cat))
            item = ET.Element('item')
            ET.SubElement(item, 'title').text = f'{idx}: {text or "browse"}'
            ET.SubElement(item, 'guid').text = f'{idx}:{text}'
            ET.SubElement(item, 'link').text = 'https://example.invalid/file.torrent'
            ET.SubElement(item, '{%s}attr' % bridge.TORZNAB_NS, name='category', value=cat)
            return [item]
        anime = FakeResolver({'source': 'shikimori', 'titles': {'russian': ['Баскетбол Куроко']}, 'indexers': {}})
        tv = FakeResolver({'source': 'tvmaze', 'titles': {'russian': ['Во все тяжкие']}, 'indexers': {}})
        movie = FakeResolver({'source': 'wikidata', 'titles': {'russian': ['Матрица']}, 'indexers': {}})
        app = bridge.Application(self.env, resolver=anime,
                                  generic_resolvers={'tv': tv, 'movie': movie},
                                  fetch_upstream=fake_indexer)
        self.resolvers = {'anime': anime, 'tv': tv, 'movie': movie}
        handler = type('TestHandler', (bridge.Handler,), {'app': app})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, **kwargs):
        params = {'apikey': 'test-key', **kwargs}
        return urllib.request.urlopen(self.base + path + '?' + urllib.parse.urlencode(params))

    def test_no_legacy_api(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request('/api', t='caps')
        self.assertEqual(exc.exception.code, 404)

    def test_distinct_caps(self):
        expected = [('anime', '5070'), ('tv', '5000'), ('movie', '2000')]
        for mode, cat in expected:
            caps = ET.fromstring(self.request(f'/{mode}/api', t='caps').read())
            self.assertTrue(any(e.get('id') == cat for e in caps.iter() if e.tag in ('category', 'subcat')))

    def test_queries_go_only_to_matching_indexers(self):
        cases = [('anime', 'tvsearch', 'Kurokos Basketball', ('6', 'Баскетбол Куроко', '5070')),
                 ('tv', 'tvsearch', 'Breaking Bad', ('4', 'Во все тяжкие', '5000')),
                 ('movie', 'movie', 'The Matrix', ('3', 'Матрица', '2000'))]
        for mode, operation, title, expected in cases:
            with self.subTest(mode=mode):
                feed = self.request(f'/{mode}/api', t=operation, q=title, season='1', ep='2').read()
                items = bridge.read_rss(feed)
                self.assertEqual(len(items), 1)
                self.assertEqual(self.calls[-1], expected)
                self.assertEqual(self.resolvers[mode].queries, [title])
        self.assertEqual(len(self.calls), 3)

    def test_keywordless_indexer_validation_and_debug(self):
        self.assertEqual(len(bridge.read_rss(self.request('/tv/api', t='tvsearch', cat='5000').read())), 1)
        self.assertEqual(self.calls[-1], ('4', '', '5000'))
        result = json.load(self.request('/debug/movie/resolve', q='The Matrix'))
        self.assertEqual(result['type'], 'movie')
        self.assertEqual(result['queries']['3'], ['Матрица'])


if __name__ == '__main__':
    unittest.main()

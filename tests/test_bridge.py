import contextlib
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer
from pathlib import Path

import bridge


ANIME = {'id': '7752', 'name': 'Kuroko no Basket', 'russian': 'Баскетбол Куроко',
         'english': "Kuroko's Basketball", 'synonyms': ['Kuroko no Basuke']}


def rss_item(guid, title='Баскетбол Куроко ТВ-3 [25 из 25]'):
    item = ET.Element('item')
    ET.SubElement(item, 'title').text = title
    ET.SubElement(item, 'guid').text = guid
    ET.SubElement(item, 'link').text = f'https://prowlarr.example/download/{guid}'
    ET.SubElement(item, f'{{{bridge.TORZNAB_NS}}}attr', name='category', value='5070')
    return item


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'cache.db')
    def tearDown(self):
        self.tmp.cleanup()
    def resolver(self, fetch=None, shows=None):
        return bridge.Resolver('https://shikimori.one/api/graphql', self.path, 600, 120,
                               {'shows': shows or []}, fetch=fetch)

    def test_name_matching_punctuation_and_russian(self):
        self.assertEqual(bridge.norm("Kuroko's Basketball"), bridge.norm('Kurokos Basketball'))
        self.assertEqual(bridge.select_anime([ANIME], 'Kurokos Basketball')['id'], '7752')
        self.assertEqual(bridge.select_anime([ANIME], 'Баскетбол Куроко')['id'], '7752')
        self.assertIsNone(bridge.select_anime([ANIME], 'Completely unrelated title'))

    def test_auto_resolve_and_persistent_cache_with_episode_suffix(self):
        calls = []
        def fetch(name):
            calls.append(name)
            return [ANIME]
        resolver = self.resolver(fetch)
        result = resolver.resolve('Kurokos Basketball 25')
        self.assertEqual(result['source'], 'shikimori')
        self.assertEqual(result['titles']['russian'], ['Баскетбол Куроко'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], 'Kurokos Basketball')
        self.assertEqual(resolver.resolve('Kuroko no Basuke 24')['source'], 'shikimori')
        self.assertEqual(len(calls), 1)
        # SQLite persistence across process restarts
        resolver2 = self.resolver(lambda _: self.fail('cache miss'))
        self.assertEqual(resolver2.resolve('Kurokos Basketball')['source'], 'shikimori')

    def test_search_shorter_probe_when_original_not_indexed(self):
        calls = []
        def fetch(q):
            calls.append(q)
            return [ANIME] if q == 'Kuroko' else []
        resolver = self.resolver(fetch)
        result = resolver.resolve('Kurokos Basketball 25')
        self.assertEqual(result['source'], 'shikimori')
        self.assertEqual(calls, ['Kurokos Basketball', 'Kuroko'])

    def test_no_false_positive_and_fallback(self):
        result = self.resolver(lambda _: [ANIME]).resolve('A completely different show')
        self.assertEqual(result['source'], 'fallback')
        self.assertEqual(result['titles']['original'], ['A completely different show'])

    def test_shikimori_failure_is_not_negative_cached(self):
        resolver = self.resolver(lambda _: (_ for _ in ()).throw(urllib.error.URLError('offline')))
        self.assertEqual(resolver.resolve('Unknown Show')['source'], 'fallback')
        resolver.fetch = lambda _: [{'id': 5, 'name': 'Unknown Show', 'russian': 'Неведомый сериал'}]
        # Don't wait the 90 second failure circuit breaker in test.
        resolver._retry_after = 0
        self.assertEqual(resolver.resolve('Unknown Show')['source'], 'shikimori')

    def test_manual_overrides_priority(self):
        show = {'match': ['Kurokos Basketball'], 'indexers': {'6': ['Баскетбол Куроко']}}
        resolver = self.resolver(lambda _: self.fail('should not call Shiki'), [show])
        result = resolver.resolve('Kurokos Basketball 25')
        self.assertEqual(result['source'], 'manual')
        self.assertEqual(result['indexers']['6'], ['Баскетбол Куроко'])

    def test_terms_per_indexer(self):
        info = {'titles': bridge.titles_from_anime(ANIME), 'indexers': {}}
        self.assertEqual(bridge.search_terms(info, '6', {'language_order': ['russian', 'romaji'], 'max_queries': 2}, 'X'),
                         ['Баскетбол Куроко', 'Kuroko no Basket'])
        self.assertEqual(bridge.search_terms(info, '7', {'language_order': ['romaji', 'english'], 'max_queries': 2}, 'X'),
                         ['Kuroko no Basket', 'Kuroko no Basuke'])

    def test_unknown_title_removes_trailing_episode_before_fallback(self):
        resolver = self.resolver(lambda _: [])
        result = resolver.resolve('Unknown Anime 13')
        self.assertEqual(result['titles']['original'], ['Unknown Anime'])


class FeedTests(unittest.TestCase):
    def test_feed_keeps_torznab_metadata_and_download_link(self):
        item = rss_item('torrent-1')
        parsed = bridge.read_rss(bridge.rss([item]))
        self.assertEqual(parsed[0].findtext('guid'), 'torrent-1')
        self.assertEqual(parsed[0].findtext('link'), 'https://prowlarr.example/download/torrent-1')
        self.assertEqual(parsed[0].find(f'{{{bridge.TORZNAB_NS}}}attr').attrib['value'], '5070')
        self.assertEqual(len(bridge.unique_items([item, item])), 1)

    def test_caps_category(self):
        root = ET.fromstring(bridge.caps())
        self.assertIsNotNone(root.find("./categories/category[@id='5000']/subcat[@id='5070']"))


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.idxfile = self.dir / 'indexers.json'
        self.idxfile.write_text(json.dumps({
            '6': {'language_order': ['russian', 'romaji'], 'max_queries': 1},
            '7': {'language_order': ['romaji', 'russian'], 'max_queries': 1, 'test_query': 'Kuroko no Basuke'}
        }))
        self.overfile = self.dir / 'overrides.json'
        self.overfile.write_text('{"shows": []}')
        self.env = {'BRIDGE_API_KEY': 'bridge-secret', 'PROWLARR_API_KEY': 'upstream-secret',
                    'INDEXERS_FILE': str(self.idxfile), 'OVERRIDES_FILE': str(self.overfile),
                    'SHIKIMORI_DB': str(self.dir / 'titles.db'), 'BROWSE_INDEXER_IDS': '7'}
    def tearDown(self):
        self.tmp.cleanup()

    def test_search_two_indexers_cache_and_browse(self):
        calls = []
        def up(idx, text, category):
            calls.append((idx, text))
            return [rss_item(f'{idx}-{text or "browse"}')]
        app = bridge.Application(self.env, resolver=bridge.Resolver('https://x', str(self.dir / 'r.db'),
                                 600, 120, {'shows': []}, fetch=lambda _: [ANIME]), fetch_upstream=up)
        results = app.search('Kurokos Basketball 25')
        self.assertEqual(len(results), 2)
        self.assertEqual(calls, [('6', 'Баскетбол Куроко'), ('7', 'Kuroko no Basket')])
        self.assertEqual(len(app.search('Kurokos Basketball 24')), 2)
        self.assertEqual(len(calls), 2) # source cache avoids duplicate lookups
        self.assertEqual(len(app.search('')), 1)
        self.assertEqual(calls[-1], ('7', ''))

    def test_http_auth_and_debug(self):
        resolver = bridge.Resolver('https://x', str(self.dir / 'db.sqlite'), 600, 120,
                                   {'shows': []}, fetch=lambda _: [ANIME])
        app = bridge.Application(self.env, resolver=resolver, fetch_upstream=lambda *_: [rss_item('good')])
        handler = type('TestHandler', (bridge.Handler,), {'app': app})
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            self.assertEqual(urllib.request.urlopen(url + '/health').status, 200)
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(url + '/anime/api?t=caps')
            self.assertEqual(err.exception.code, 401)
            debug = urllib.request.urlopen(url + '/debug/anime/resolve?' + urllib.parse.urlencode(
                {'apikey': 'bridge-secret', 'q': 'Kurokos Basketball'}))
            body = json.load(debug)
            self.assertEqual(body['queries']['6'][0], 'Баскетбол Куроко')
            self.assertEqual(body['resolution']['source'], 'shikimori')
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(url + '/api?' + urllib.parse.urlencode({'apikey': 'bridge-secret', 't': 'caps'}))
            self.assertEqual(missing.exception.code, 404)
            found = urllib.request.urlopen(url + '/anime/api?' + urllib.parse.urlencode(
                {'apikey': 'bridge-secret', 't': 'tvsearch', 'q': 'Kurokos Basketball', 'season': 1, 'ep': 25}))
            self.assertEqual(len(bridge.read_rss(found.read())), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()

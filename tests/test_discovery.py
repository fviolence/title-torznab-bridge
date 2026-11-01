"""Deterministic regression tests for Prowlarr capabilities and language routing."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bridge
from indexer_discovery import ProwlarrCatalog, appropriate_modes, category_ids, eligible_indexers


def make_indexer(idx, name, language, cats, *, enabled=True, supports=True, protocol='torrent'):
    return {
        'id': idx, 'name': name, 'language': language, 'enable': enabled,
        'supportsSearch': supports, 'protocol': protocol, 'priority': 25,
        'tags': [], 'capabilities': {'categories': [{'id': int(cat)} for cat in cats]},
    }

CATALOG = [
    make_indexer(2, 'The Pirate Bay', 'en-US', [2000, 2020, 5000, 5040, 5050]),
    make_indexer(3, 'Kinozal', 'ru-RU', [2000, 2020, 5000, 5050, 5070]),
    make_indexer(4, 'RuTracker.org', 'ru-RU', [2000, 2010, 5000, 5020, 5070, 5080]),
    make_indexer(6, 'Anidub', 'ru-RU', [2000, 5000, 5070]),
    make_indexer(7, 'AnimeLayer', 'ru-RU', [5000, 5070]),
    make_indexer(8, 'Rustorka', 'ru-RU', [2000, 5000, 5070], enabled=False),
]

class CapabilitiesTests(unittest.TestCase):
    def test_nested_category_ids(self):
        cats = category_ids([{'id': '5000', 'subCategories': [{'id': 5040}, {'id': 5070, 'subCategories': [{'id': 5071}]}]}])
        self.assertEqual(cats, {5000, 5040, 5070, 5071})

    def test_classification(self):
        routes = {record['id']: record['modes'] for record in eligible_indexers(CATALOG)}
        self.assertEqual(routes, {'2': ['movie', 'tv'], '3': ['anime', 'movie', 'tv'],
                                  '4': ['anime', 'movie', 'tv'], '6': ['anime'], '7': ['anime']})
        self.assertNotIn('8', routes)

    def test_only_enabled_searchable_torrents(self):
        extras = [make_indexer(9, 'no search', 'de', [5000], supports=False),
                  make_indexer(10, 'usenet', 'en', [5000], protocol='usenet'),
                  make_indexer(11, 'disabled', 'en', [5000], enabled=False)]
        self.assertFalse(eligible_indexers(extras))

    def test_parent_5000_only_is_general_tv(self):
        self.assertEqual(appropriate_modes({5000}), {'tv'})
        self.assertEqual(appropriate_modes({5000, 5070}), {'anime'})
        self.assertEqual(appropriate_modes({5000, 5040, 5070}), {'tv', 'anime'})

    def test_strict_english_only_terms(self):
        info = {'source': 'sonarr', 'titles': {'russian': ['Трасса'], 'english': ['Highway'], 'original': []}, 'indexers': {}}
        settings = {'max_queries': 5, 'language_order': ['russian', 'english']}
        self.assertEqual(bridge.automatic_search_terms(info, '2', settings, 'Highway', english_only=True), ['Highway'])
        self.assertEqual(bridge.automatic_search_terms(info, '4', settings, 'Highway', english_only=False), ['Трасса', 'Highway'])
        self.assertEqual(bridge.automatic_search_terms(info, '2', settings, 'Трасса', english_only=True), ['Highway'])

    def test_round_robin_balances_languages(self):
        info = {'source': 'sonarr', 'titles': {'russian': ['Первое', 'Второе', 'Третье'], 'english': ['First']}, 'indexers': {}}
        settings = {'max_queries': 2, 'language_order': ['russian', 'english']}
        self.assertEqual(bridge.automatic_search_terms(info, '4', settings, 'First', english_only=False), ['Первое', 'First'])

    def test_unknown_title_english_safe_fallback(self):
        info = {'source': 'fallback', 'titles': {'original': ['Unknown Show']}, 'indexers': {}}
        self.assertEqual(bridge.automatic_search_terms(info, '2', {'max_queries': 3}, 'Unknown Show', english_only=True), ['Unknown Show'])
        self.assertEqual(bridge.automatic_search_terms(info, '2', {'max_queries': 3}, 'Трасса', english_only=True), [])

    def test_single_flight_catalog_cache(self):
        calls = []
        def fetch():
            calls.append(1)
            return CATALOG
        catalog = ProwlarrCatalog('http://localhost', 'secret', ttl=600, fetch=fetch)
        self.assertEqual(len(catalog.get()), 5)
        catalog.get()[0]['name'] = 'hijacked'
        self.assertEqual(catalog.get()[0]['name'], 'The Pirate Bay')
        self.assertEqual(len(calls), 1)

    def test_http_catalog_uses_header_and_never_puts_key_in_url(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_GET(self):
                received.append((self.path, self.headers.get('X-Api-Key')))
                data = json.dumps(CATALOG).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            catalog = ProwlarrCatalog(f'http://127.0.0.1:{server.server_port}', 'secret')
            self.assertEqual(len(catalog.get()), 5)
            self.assertEqual(received, [('/api/v1/indexer', 'secret')])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_error_does_not_reuse_stale_indexes(self):
        catalog = ProwlarrCatalog('http://localhost', 'secret', ttl=30, fetch=lambda: CATALOG)
        catalog.get()
        catalog._expires = 0
        catalog.fetch = lambda: (_ for _ in ()).throw(OSError('offline'))
        with self.assertRaises(OSError):
            catalog.get()

class AutoApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        (directory / 'indexers.json').write_text(json.dumps({
            'tv': {'4': {'language_order': ['russian', 'english'], 'max_queries': 2}},
            'anime': {'7': {'language_order': ['romaji', 'english', 'russian'], 'max_queries': 2}},
        }))
        (directory / 'overrides.json').write_text('{"shows": []}')
        env = {'BRIDGE_API_KEY': 'bridge', 'PROWLARR_API_KEY': 'prowlarr',
               'INDEXERS_FILE': str(directory / 'indexers.json'),
               'OVERRIDES_FILE': str(directory / 'overrides.json'),
               'AUTO_INDEXERS': 'true', 'AUTO_INDEXERS_TTL': '600',
               'SHIKIMORI_DB': str(directory / 'shiki.db'),
               'TITLE_DB_TV': str(directory / 'tv.db'),
               'TITLE_DB_MOVIE': str(directory / 'movie.db')}
        self.calls = []
        self.tv = {'source': 'sonarr', 'titles': {'russian': ['Трасса'], 'english': ['Highway']}, 'indexers': {}}
        class Generic:
            def __init__(self, x): self.x=x
            def resolve(self, *_): return self.x
        def upstream(idx, term, cat):
            self.calls.append((idx, term, cat))
            root = ET.Element('item')
            ET.SubElement(root, 'title').text = term
            ET.SubElement(root, 'guid').text = f'{idx}:{term}'
            return [root]
        self.app = bridge.Application(env, generic_resolvers={'tv': Generic(self.tv), 'movie': Generic(self.tv)},
                                      fetch_upstream=upstream)
        self.app.catalog.fetch = lambda: CATALOG

    def test_dynamic_select_matches_real_catalog(self):
        d = self.app.indexer_diagnostics()
        self.assertEqual(d['routing'], 'auto')
        self.assertEqual([x['id'] for x in d['modes']['tv']], ['2', '3', '4'])
        self.assertEqual([x['id'] for x in d['modes']['movie']], ['2', '3', '4'])
        self.assertEqual([x['id'] for x in d['modes']['anime']], ['3', '4', '6', '7'])
        self.assertTrue(next(x for x in d['modes']['tv'] if x['id'] == '2')['english_only'])

    def test_legacy_limit_does_not_hide_languages_in_auto_mode(self):
        info = {'source': 'sonarr', 'titles': {
            'russian': ['Баскетбол Куроко'], 'romaji': ['Kuroko no Basuke'],
            'english': ["Kuroko's Basketball"]}, 'indexers': {}}
        settings = self.app.mode_indexers('anime')['7']
        self.assertEqual(settings['max_queries'], 4)  # legacy max_queries=2 ignored
        self.assertEqual(bridge.automatic_search_terms(info, '7', settings, '', english_only=False),
                         ['Kuroko no Basuke', "Kuroko's Basketball", 'Баскетбол Куроко'])

    def test_per_indexer_auto_disable_cannot_enable_disabled_prowlarr(self):
        self.app.indexers_by_mode['tv']['3'] = {'disabled': True}
        self.assertEqual(list(self.app.mode_indexers('tv')), ['2', '4'])
        self.app.indexers_by_mode['tv']['8'] = {'disabled': False}
        self.assertNotIn('8', self.app.mode_indexers('tv'))

    def test_actual_targets_including_english_only_tpb(self):
        info, targets = self.app.terms('tv', 'Highway')
        self.assertEqual(info['source'], 'sonarr')
        self.assertEqual(targets, [
            ('2', 'Highway', '5000'),
            ('3', 'Трасса', '5000'), ('3', 'Highway', '5000'),
            ('4', 'Трасса', '5000'), ('4', 'Highway', '5000'),
        ])
        self.assertEqual(len(self.app.search('Highway', 'tv')), 5)
        self.assertNotIn('8', {idx for idx, _, _ in self.calls})

    def test_disabling_live_indexer_after_cache_expires(self):
        self.app.indexer_diagnostics()
        self.app.catalog._expires = 0
        self.app.catalog.fetch = lambda: [dict(x, enable=False) if x['id'] == 2 else x for x in CATALOG]
        self.assertEqual(list(self.app.mode_indexers('tv')), ['3', '4'])

    def test_manual_exclude(self):
        self.app.discovery_excluded = {'2', '7'}
        self.assertEqual(list(self.app.mode_indexers('tv')), ['3', '4'])
        self.assertEqual(list(self.app.mode_indexers('anime')), ['3', '4', '6'])

    def test_debug_indexers_authorization(self):
        handler = type('TestHandler', (bridge.Handler,), {'app': self.app})
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            host = f'http://127.0.0.1:{server.server_port}'
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(host + '/debug/indexers')
            self.assertEqual(raised.exception.code, 401)
            params = urllib.parse.urlencode({'apikey': 'bridge'})
            with urllib.request.urlopen(host + '/debug/indexers?' + params) as resp:
                data = json.load(resp)
            self.assertEqual(len(data['modes']['tv']), 3)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)

if __name__ == '__main__':
    unittest.main()

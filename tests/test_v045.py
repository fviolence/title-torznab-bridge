"""v0.4.5 regression: bounded upstream pagination, browse selection, routing diagnostics."""
from __future__ import annotations

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
from indexer_discovery import ProwlarrCatalog, assessed_indexers


def item(guid):
    element = ET.Element('item')
    ET.SubElement(element, 'guid').text = str(guid)
    ET.SubElement(element, 'title').text = f'Release {guid}'
    return element


def catalog_entry(idx, *, enabled=True, search=True, protocol='torrent', categories=(5000,), language='ru-RU'):
    return {'id': idx, 'name': 'Source ' + str(idx), 'enable': enabled,
            'supportsSearch': search, 'protocol': protocol, 'language': language,
            'capabilities': {'categories': [{'id': c} for c in categories]}}


class FakeFeed:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        feed = self

        class HTTP(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                offset = int(params.get('offset', ['0'])[0])
                limit = int(params.get('limit', ['100'])[0])
                feed.calls.append((parsed.path, offset, limit))
                status, guids = feed.handler(offset, limit)
                if status != 200:
                    self.send_response(status)
                    self.end_headers()
                    return
                rss = ET.Element('rss')
                channel = ET.SubElement(rss, 'channel')
                for guid in guids:
                    channel.append(item(guid))
                raw = ET.tostring(rss, encoding='utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/xml')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), HTTP)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(3)


class ApplicationMixin:
    def make_app(self, *, env_extra=None, manual=None, fetch=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)
        (path / 'indexers.json').write_text(json.dumps(manual or {'tv': {'4': {}}, 'anime': {'6': {}}}))
        (path / 'overrides.json').write_text('{"shows": []}')
        env = {'BRIDGE_API_KEY': 'bridge', 'PROWLARR_API_KEY': 'hidden',
               'INDEXERS_FILE': str(path / 'indexers.json'),
               'OVERRIDES_FILE': str(path / 'overrides.json'),
               'SHIKIMORI_DB': str(path / 'anime.db'),
               'TITLE_DB_TV': str(path / 'tv.db'),
               'TITLE_DB_MOVIE': str(path / 'movie.db')}
        env.update(env_extra or {})
        class Resolver:
            def resolve(self, *_):
                return {'source': 'fallback', 'titles': {'original': ['Highway'],
                                                       'russian': ['Трасса']}, 'indexers': {}}
        return bridge.Application(env, resolver=Resolver(),
                                  generic_resolvers={'tv': Resolver(), 'movie': Resolver()},
                                  fetch_upstream=fetch)


class PagingTests(ApplicationMixin, unittest.TestCase):
    def run_feed(self, fn, *, extra=None, browse=False):
        feed = FakeFeed(fn)
        self.addCleanup(feed.close)
        app = self.make_app(env_extra={'PROWLARR_URL': feed.url, **(extra or {})})
        results = app._upstream('4', 'Трасса', '5000', browse=browse)
        return [i.findtext('guid') for i in results], feed.calls, app

    def test_fetches_second_page(self):
        results, calls, _ = self.run_feed(lambda off, limit: (200, range(off, min(off+limit, 150))))
        self.assertEqual(len(results), 150)
        self.assertEqual([v[1] for v in calls], [0, 100])

    def test_default_bounds_at_two_pages(self):
        results, calls, _ = self.run_feed(lambda off, limit: (200, range(off, off+limit)))
        self.assertEqual(len(results), 200)
        self.assertEqual([v[1] for v in calls], [0, 100])

    def test_max_items_partial_last_page(self):
        results, calls, _ = self.run_feed(lambda off, limit: (200, range(off, off+limit)),
                                         extra={'UPSTREAM_MAX_PAGES': '5', 'UPSTREAM_MAX_ITEMS': '120'})
        self.assertEqual(len(results), 120)
        self.assertEqual([v[1] for v in calls], [0, 100])

    def test_stops_at_short_first_page(self):
        results, calls, _ = self.run_feed(lambda off, limit: (200, range(19)))
        self.assertEqual(len(results), 19)
        self.assertEqual(len(calls), 1)

    def test_repeated_page_stops_even_if_offset_ignored(self):
        results, calls, _ = self.run_feed(lambda off, limit: (200, range(100)),
                                         extra={'UPSTREAM_MAX_PAGES': '5', 'UPSTREAM_MAX_ITEMS': '500'})
        self.assertEqual(len(results), 100)
        self.assertEqual([v[1] for v in calls], [0, 100])

    def test_partial_overlap_merged_by_guid(self):
        results, calls, _ = self.run_feed(
            lambda off, limit: (200, range(100) if off == 0 else list(range(90, 190))),
            extra={'UPSTREAM_MAX_PAGES': '2'})
        self.assertEqual(len(results), 190)
        self.assertEqual(len(results), len(set(results)))
        self.assertEqual(len(calls), 2)

    def test_page_two_error_keeps_page_one(self):
        results, calls, _ = self.run_feed(lambda off, limit: (200, range(100)) if off == 0 else (429, []))
        self.assertEqual(len(results), 100)
        self.assertEqual(len(calls), 2)

    def test_first_page_error_raises(self):
        feed = FakeFeed(lambda off, limit: (429, []))
        self.addCleanup(feed.close)
        app = self.make_app(env_extra={'PROWLARR_URL': feed.url})
        with self.assertRaises(urllib.error.HTTPError):
            app._upstream('4', 'Трасса', '5000')

    def test_browse_gets_only_first_page(self):
        results, calls, _ = self.run_feed(lambda off, limit: (200, range(off, off+limit)),
                                         extra={'UPSTREAM_MAX_PAGES': '5'}, browse=True)
        self.assertEqual(len(results), 100)
        self.assertEqual([v[1] for v in calls], [0])

    def test_cache_reuses_complete_paged_result(self):
        feed = FakeFeed(lambda off, limit: (200, range(off, min(off+limit, 150))))
        self.addCleanup(feed.close)
        app = self.make_app(env_extra={'PROWLARR_URL': feed.url})
        self.assertEqual(len(app._search_one('4', 'Трасса', '5000')), 150)
        self.assertEqual(len(app._search_one('4', 'Трасса', '5000')), 150)
        self.assertEqual(len(feed.calls), 2)

    def test_bridge_http_pagination_after_upstream_merge(self):
        feed = FakeFeed(lambda off, limit: (200, range(off, min(off+limit, 150))))
        self.addCleanup(feed.close)
        app = self.make_app(env_extra={'PROWLARR_URL': feed.url}, manual={'tv': {'4': {'max_queries': 1}}})
        server = ThreadingHTTPServer(('127.0.0.1', 0), type('Handler', (bridge.Handler,), {'app': app}))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), worker.join(3)))
        query = urllib.parse.urlencode({'apikey': 'bridge', 'q': 'Highway', 't': 'tvsearch',
                                        'offset': '100', 'limit': '100'})
        with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/tv/api?{query}') as response:
            root = ET.fromstring(response.read())
        self.assertEqual(len(root.findall('./channel/item')), 50)
        self.assertEqual(root.find(f'./channel/{{{bridge.NEWZNAB_NS}}}response').attrib,
                         {'offset': '100', 'total': '150'})
        self.assertEqual([x[1] for x in feed.calls], [0, 100])


class BrowseTests(ApplicationMixin, unittest.TestCase):
    def test_no_fixed_indexer_and_bounded_probes(self):
        calls = []
        def fake(idx, term, cat):
            calls.append(idx)
            return [item(idx)] if idx == '6' else []
        app = self.make_app(fetch=fake, manual={'anime': {'4': {}, '6': {}, '7': {}}},
                            env_extra={'BROWSE_MAX_INDEXERS': '2'})
        self.assertEqual([i.findtext('guid') for i in app.browse('anime')], ['6'])
        self.assertEqual(calls, ['4', '6'])

    def test_no_probing_outside_limit(self):
        calls = []
        def fake(idx, term, cat):
            calls.append(idx)
            return []
        app = self.make_app(fetch=fake, manual={'anime': {'3': {}, '4': {}, '6': {}, '7': {}}},
                            env_extra={'BROWSE_MAX_INDEXERS': '2'})
        self.assertEqual(app.browse('anime'), [])
        self.assertEqual(calls, ['3', '4'])

    def test_optional_browse_preference(self):
        calls = []
        def fake(idx, term, cat):
            calls.append(idx)
            return [item(idx)]
        app = self.make_app(fetch=fake, manual={'anime': {'3': {}, '7': {}}},
                            env_extra={'BROWSE_INDEXER_IDS': '7'})
        self.assertEqual([i.findtext('guid') for i in app.browse('anime')], ['7'])
        self.assertEqual(calls, ['7'])


class DiagnosticTests(ApplicationMixin, unittest.TestCase):
    def test_assessed_catalog_includes_disabled_and_other_protocols(self):
        cat = [catalog_entry(8, enabled=False), catalog_entry(9, protocol='usenet'),
               catalog_entry(10, search=False), catalog_entry(11, categories=(3000,))]
        result = assessed_indexers(cat)
        self.assertEqual([r['id'] for r in result], ['8', '9', '10', '11'])
        self.assertIn('disabled_in_prowlarr', result[0]['eligibility_reasons'])
        self.assertIn('not_torrent_protocol', result[1]['eligibility_reasons'])
        self.assertIn('search_not_supported', result[2]['eligibility_reasons'])
        self.assertIn('no_matching_search_categories', result[3]['eligibility_reasons'])

    def test_debug_reports_all_exclusion_reasons(self):
        cat = [catalog_entry(2, categories=(2000, 5040), language='en-US'),
               catalog_entry(3, categories=(2000, 5000, 5070)),
               catalog_entry(6, categories=(5000, 5070)),
               catalog_entry(8, enabled=False, categories=(5000,)),
               catalog_entry(9, protocol='usenet', categories=(5000,))]
        app = self.make_app(manual={'tv': {'3': {'disabled': True}}},
                            env_extra={'AUTO_INDEXERS': 'true', 'AUTO_EXCLUDE_IDS': '2'})
        count = []
        app.catalog.fetch = lambda: (count.append(True) or cat)
        diag = app.indexer_diagnostics()
        self.assertEqual(count, [True])
        self.assertEqual(diag['modes']['tv'], [])
        rejected = {entry['id']: entry['reasons'] for entry in diag['excluded']['tv']}
        self.assertIn('excluded_by_auto_exclude_ids', rejected['2'])
        self.assertIn('disabled_by_indexers_json', rejected['3'])
        self.assertIn('category_not_suitable_for_tv', rejected['6'])
        self.assertIn('disabled_in_prowlarr', rejected['8'])
        self.assertIn('not_torrent_protocol', rejected['9'])
        self.assertEqual(len(app.catalog.get()), 3)
        self.assertEqual(count, [True])

    def test_debug_rejects_unauthenticated_and_is_read_only(self):
        app = self.make_app(env_extra={'AUTO_INDEXERS': 'true'})
        calls = []
        app.catalog.fetch = lambda: (calls.append('catalog') or [catalog_entry(4)])
        server = ThreadingHTTPServer(('127.0.0.1', 0), type('Handler', (bridge.Handler,), {'app': app}))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), worker.join(3)))
        base = f'http://127.0.0.1:{server.server_port}/debug/indexers'
        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base)
        self.assertEqual(exc.exception.code, 401)
        with urllib.request.urlopen(base + '?apikey=bridge') as response:
            payload = json.load(response)
        self.assertEqual(calls, ['catalog'])
        self.assertEqual([x['id'] for x in payload['modes']['tv']], ['4'])
        self.assertTrue(payload['excluded']['anime'])
        self.assertNotIn('hidden', json.dumps(payload))


if __name__ == '__main__':
    unittest.main()

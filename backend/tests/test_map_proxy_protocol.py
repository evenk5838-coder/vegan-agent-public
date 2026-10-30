"""Pure protocol tests; unittest works without loading app conftest/config/DB."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.map_proxy_protocol import (  # noqa: E402
    ALLOWED_PATHS, ProtocolError, encode_jsonp, normalize_proxy_request,
)


class MapProxyProtocolTests(unittest.TestCase):
    def test_real_sdk_coordinate_request_shape(self):
        query = {
            'appname': 'http://127.0.0.1:3000/', 'callback': 'AMap.__jsonp1',
            'coordsys': 'gps', 'csid': 'trace-abc', 'key': 'client-public-key',
            'locations': '116.397128,39.916527', 'logversion': '2.0',
            'platform': 'JS', 's': 'sdk-signature', 'sdkversion': '2.0',
            'jscode': 'untrusted-client-secret', 'url': 'http://127.0.0.1/admin',
        }
        path, params, callback = normalize_proxy_request(
            '/_AMapService/v3/assistant/coordinate/convert', query)
        self.assertEqual(path, 'v3/assistant/coordinate/convert')
        self.assertEqual(callback, 'AMap.__jsonp1')
        self.assertEqual(params['locations'], query['locations'])
        self.assertEqual(params['coordsys'], 'gps')
        self.assertEqual(params['output'], 'JSON')
        for name in ('appname', 'platform', 'sdkversion', 'logversion', 's', 'csid'):
            self.assertEqual(params[name], query[name])
        for name in ('key', 'jscode', 'url', 'callback'):
            self.assertNotIn(name, params)
        # Credential ownership remains with the caller, even with client spoofing.
        params.update(key='server-js-key', jscode='server-secret')
        self.assertEqual(params['key'], 'server-js-key')
        self.assertEqual(params['jscode'], 'server-secret')

    def test_existing_poi_json_paths_remain_compatible(self):
        for path in ALLOWED_PATHS - {'v3/assistant/coordinate/convert'}:
            with self.subTest(path=path):
                normalized, params, callback = normalize_proxy_request(path, {
                    'location': '116.4,39.9', 'city': '北京', 'keywords': '素食',
                    'types': '050120', 'radius': '5000', 'page': '1', 'offset': '20',
                    'key': 'evil', 'jscode': 'evil', 'output': 'XML', 'unknown': 'drop',
                })
                self.assertEqual(normalized, path)
                self.assertIsNone(callback)
                self.assertEqual(params['city'], '北京')
                self.assertEqual(params['location'], '116.4,39.9')
                self.assertFalse({'key', 'jscode', 'output', 'unknown'} & params.keys())

    def test_jsonp_is_removed_from_upstream_and_json_requested(self):
        _, params, callback = normalize_proxy_request('v3/place/text', {
            'city': '北京', 'callback': '_amap_jsonp.$cb_2', 'output': 'XML',
        })
        self.assertEqual(callback, '_amap_jsonp.$cb_2')
        self.assertEqual(params, {'city': '北京', 'output': 'JSON'})

    def test_unknown_paths_and_ssrf_are_rejected(self):
        for path in ('https://restapi.amap.com/v3/ip', 'http://127.0.0.1/admin',
                     '//evil.test/v3/place/text', '_AMapService/../admin',
                     'v3/place/../text', 'v3/place/text?url=evil',
                     'v3/place/text#fragment', '_AMapService/_AMapService/v3/place/text',
                     'v3/ip', 'v3/assistant/coordinate/convert/extra',
                     '%2e%2e/v3/place/text', 'v3/place/text\\admin'):
            with self.subTest(path=path), self.assertRaises(ProtocolError) as caught:
                normalize_proxy_request(path, {})
            self.assertEqual(caught.exception.status, 404)
            self.assertEqual(caught.exception.code, 'NOT_FOUND')

    def test_callback_injection_and_dangerous_paths_rejected(self):
        for callback in ('', 'alert(1)', 'cb);alert(1)//', 'x["cb"]',
                         'x[0]', 'x?.cb', 'x;cb', 'cb\nalert', 'cb%28x%29',
                         'x.constructor', '__proto__.cb', 'x.prototype.cb',
                         'a' * 129, '.'.join(['a'] * 9)):
            with self.subTest(callback=callback), self.assertRaises(ProtocolError):
                normalize_proxy_request('v3/place/text', {'callback': callback})
            with self.subTest(encoding=callback), self.assertRaises(ProtocolError):
                encode_jsonp({}, callback)

    def test_missing_conversion_parameters_rejected(self):
        for query in ({}, {'coordsys': 'gps'}, {'locations': '116,40'}):
            with self.subTest(query=query), self.assertRaises(ProtocolError) as caught:
                normalize_proxy_request('v3/assistant/coordinate/convert', query)
            self.assertEqual(caught.exception.code, 'INVALID_MAP_REQUEST')
            self.assertEqual(caught.exception.status, 400)

    def test_bad_conversion_values_rejected(self):
        for value in ('181,40', '116,91', '-181,0', '0,-91', 'NaN,40',
                      'Infinity,40', '1e99,40', '116', '116,40,0',
                      '116,40;', '', '116,40&url=evil'):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                normalize_proxy_request('v3/assistant/coordinate/convert',
                                        {'locations': value, 'coordsys': 'gps'})
        for coordsys in ('GPS', 'unknown', '', 'gps;alert(1)'):
            with self.subTest(coordsys=coordsys), self.assertRaises(ProtocolError):
                normalize_proxy_request('v3/assistant/coordinate/convert',
                                        {'locations': '116,40', 'coordsys': coordsys})

    def test_coordinate_ranges_and_bounded_batches(self):
        for coordsys in ('gps', 'baidu', 'mapbar', 'autonavi'):
            _, params, callback = normalize_proxy_request('v3/assistant/coordinate/convert',
                {'locations': '-180,-90|180,90|0,0', 'coordsys': coordsys})
            self.assertEqual(params['coordsys'], coordsys)
            self.assertIsNone(callback)
        normalize_proxy_request('v3/assistant/coordinate/convert',
                                {'locations': '|'.join(['116,40'] * 40), 'coordsys': 'gps'})
        with self.assertRaises(ProtocolError):
            normalize_proxy_request('v3/assistant/coordinate/convert',
                                    {'locations': '|'.join(['116,40'] * 41), 'coordsys': 'gps'})
        with self.assertRaises(ProtocolError):
            normalize_proxy_request('v3/place/around', {'location': '181,40'})

    def test_metadata_length_and_control_chars_are_bounded(self):
        for name, limit in {'platform': 32, 'sdkversion': 40, 'logversion': 40,
                            'appname': 512, 's': 256, 'csid': 128}.items():
            for value in ('a' * (limit + 1), 'line\nnext', 'bad\x00value'):
                with self.subTest(name=name, value=value[:20]), self.assertRaises(ProtocolError):
                    normalize_proxy_request('v3/place/text', {name: value})

    def test_jsonp_escapes_script_delimiters_and_round_trips(self):
        data = {'status': '1', 'locations': '116.4,39.9',
                'text': '</script><script>alert("x")</script>&\u2028\u2029中文'}
        result = encode_jsonp(data, 'AMap.$callback')
        self.assertTrue(result.startswith('AMap.$callback('))
        self.assertTrue(result.endswith(');'))
        self.assertNotIn('<', result)
        self.assertNotIn('>', result)
        self.assertNotIn('&', result)
        self.assertNotIn('\u2028', result)
        self.assertEqual(json.loads(result[len('AMap.$callback('):-2]), data)

    def test_non_finite_json_data_not_serialized_as_javascript(self):
        with self.assertRaises(ValueError):
            encode_jsonp({'number': float('nan')}, 'callback')


if __name__ == '__main__':
    unittest.main()

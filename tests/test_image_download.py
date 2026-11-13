"""Same-origin authenticated PNG attachment round-trip without provider/billing."""
from __future__ import annotations

import base64
from contextlib import closing
from html.parser import HTMLParser
import unittest
from urllib.parse import urlencode

import test_image_generation as images
import test_credit_gateway as fixture

ORIGIN = {'Origin': fixture.BASE_URL}
ENDPOINT = '/developer/images/download'
LIMIT = 20 * 1024 * 1024


class FormInspector(HTMLParser):
    def __init__(self):
        super().__init__(); self.depth = 0; self.nested = False; self.download = None; self.fields = set(); self.button = None
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'form':
            if self.depth: self.nested = True
            self.depth += 1
            if values.get('id') == 'image-download-form': self.download = values
        if tag == 'input' and values.get('id', '').startswith('image-download-'): self.fields.add(values.get('name'))
        if tag == 'button' and values.get('id') == 'image-download': self.button = values
    def handle_endtag(self, tag):
        if tag == 'form': self.depth -= 1


class ImageDownloadTests(unittest.TestCase):
    for name in ('setUp', 'cleanup', 'open_database', 'register_and_login', 'issue_key', 'accounting',
                 'fund', 'upstream', 'restore_catalog'):
        locals()[name] = getattr(images.ImageGatewayTests, name)

    def billing_state(self):
        with closing(self.open_database()) as conn:
            return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]
                    for table in ('api_usage_logs', 'credit_holds', 'credit_ledger', 'credit_wallets')}

    def download(self, fields=None, **kwargs):
        return self.client.post(ENDPOINT, data=fields or {'b64_json': images.png_fixture(), 'size': '1024x1024'},
                                headers=kwargs.pop('headers', ORIGIN), **kwargs)

    def test_valid_png_returns_http_attachment_without_upstream_or_accounting(self):
        before = self.billing_state()
        result = self.download()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content, base64.b64decode(images.png_fixture()))
        self.assertEqual(result.headers['content-type'], 'image/png')
        self.assertEqual(result.headers['content-disposition'], 'attachment; filename="generated.png"')
        self.assertEqual(result.headers['cache-control'], 'no-store')
        self.assertEqual(result.headers['x-content-type-options'], 'nosniff')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.billing_state(), before)

    def test_three_standard_sizes_round_trip_only_the_submitted_pixels(self):
        for width, height in [(1024, 1024), (1024, 1536), (1536, 1024)]:
            encoded = images.png_fixture(width, height, color=6)
            result = self.download({'b64_json': encoded, 'size': f'{width}x{height}'})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.content, base64.b64decode(encoded))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.billing_state()['api_usage_logs'], [])

    def test_authentication_and_same_origin_are_required(self):
        before = self.billing_state()
        for headers in ({}, {'Origin': 'https://evil.invalid'}, {**ORIGIN, 'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.download(headers=headers).status_code, 403)
        self.assertEqual(self.billing_state(), before)
        self.client.cookies.clear()
        self.assertEqual(self.download().status_code, 401)
        self.assertEqual(self.calls, [])

    def test_bad_base64_png_size_and_extra_fields_are_rejected_without_effects(self):
        before = self.billing_state()
        valid = {'b64_json': images.png_fixture(), 'size': '1024x1024'}
        cases = [{'b64_json': '!!!!', 'size': '1024x1024'},
                 {'b64_json': base64.b64encode(b'<svg/>').decode(), 'size': '1024x1024'},
                 {'b64_json': images.png_fixture(compressed=b'bad deflate'), 'size': '1024x1024'},
                 {'b64_json': valid['b64_json'], 'size': '1536x1024'},
                 {'b64_json': valid['b64_json'], 'size': 'auto'},
                 {'b64_json': valid['b64_json'], 'size': '2000x2000'},
                 {**valid, 'url': 'https://attacker.invalid/image.png'},
                 {**valid, 'filename': '../secret.png'}, {**valid, 'user_id': '1'},
                 {'size': '1024x1024'}, {'b64_json': valid['b64_json']}]
        for fields in cases:
            with self.subTest(keys=list(fields)):
                self.assertEqual(self.download(fields).status_code, 400)
        for body in [urlencode(valid) + '&size=1024x1024', 'b64_json=x&b64_json=y', 'b64_json=x&size']:
            result = self.client.post(ENDPOINT, content=body, headers={**ORIGIN, 'Content-Type': 'application/x-www-form-urlencoded'})
            self.assertEqual(result.status_code, 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.billing_state(), before)

    def test_oversized_header_and_streaming_body_are_bounded(self):
        before = self.billing_state()
        headers = {**ORIGIN, 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': str(LIMIT + 1)}
        self.assertEqual(self.client.post(ENDPOINT, content='x', headers=headers).status_code, 413)
        chunks = (b'x' * (1024 * 1024) for _ in range(21))
        result = self.client.post(ENDPOINT, content=chunks, headers={**ORIGIN, 'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(result.status_code, 413)
        self.assertEqual(self.billing_state(), before)
        self.assertEqual(self.calls, [])

    def test_only_form_post_is_accepted(self):
        self.assertEqual(self.client.post(ENDPOINT, json={'b64_json': images.png_fixture(), 'size': '1024x1024'}, headers=ORIGIN).status_code, 415)
        self.assertEqual(self.client.get(ENDPOINT).status_code, 405)
        self.assertEqual(self.calls, [])

    def test_download_uses_non_nested_http_post_form_and_safe_value_assignment(self):
        response = self.client.get('/developer/images')
        self.assertEqual(response.status_code, 200)
        parser = FormInspector(); parser.feed(response.text)
        self.assertFalse(parser.nested)
        self.assertEqual(parser.depth, 0)
        self.assertEqual((parser.download['action'], parser.download['method']), (ENDPOINT, 'post'))
        self.assertEqual(parser.fields, {'b64_json', 'size'})
        self.assertEqual(parser.button['type'], 'submit')
        script = self.client.get('/static/images.js').text
        self.assertIn("$('image-download-data').value = encoded", script)
        self.assertIn("$('image-download-size').value = payload.size", script)
        self.assertNotIn("$('image-download').href", script)
        self.assertNotIn('createObjectURL', script)
        self.assertNotIn('innerHTML', script)
        self.assertNotIn('name="b64_json" value="data:', response.text)
        self.assertEqual(self.calls, [])


if __name__ == '__main__': unittest.main()

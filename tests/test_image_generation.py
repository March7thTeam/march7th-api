"""Images API admission, modality prices, accounting and authenticated console.

Temporary SQLite and MockTransport only; no provider requests or real keys.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import struct
import unittest
import zlib
from contextlib import closing
from unittest.mock import patch

import httpx

import test_credit_gateway as gateway_fixture
BASE_URL = gateway_fixture.BASE_URL
from app import ai_catalog
from app.credit_ledger import wallet_snapshot, pending_overrun_units
from app.credit_pricing import PricingError, billing_details, calculate_charge, PRICE_DATA
from app.image_generation import (IMAGE_PRICES, image_models, prepare_image_request,
                                  valid_image_response, image_budget_exceeded)

FUND = 200_000_000


def png_fixture(width=1024, height=1024, *, color=2, bit_depth=8, compressed=None, scanlines=None,
                compression=0, filtering=0, interlace=0, split_idat=False):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    if compressed is None:
        if scanlines is None:
            pixel = b'\xff\0\0' if color == 2 else b'\xff\0\0\xff'
            scanlines = (b'\0' + pixel * width) * height
        compressed = zlib.compress(scanlines)
    idat = chunk(b'IDAT', compressed)
    if split_idat:
        middle = len(compressed) // 2
        idat = chunk(b'IDAT', compressed[:middle]) + chunk(b'IDAT', compressed[middle:])
    return base64.b64encode(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, bit_depth, color,
        compression, filtering, interlace)) + idat + chunk(b'IEND', b'')).decode()


def receipt(model='gpt-image-2'):
    if model == 'gpt-image-1.5':
        inputs, images, text = 20, 272, 126
    else:
        inputs, images, text = 24, 196, 0
    return {'created': 1794528000, 'data': [{'b64_json': png_fixture(), 'generation_id': 'fixture'}],
            'usage': {'input_tokens': inputs, 'input_tokens_details': {'text_tokens': inputs, 'image_tokens': 0},
                      'output_tokens': images + text, 'output_tokens_details': {'image_tokens': images, 'text_tokens': text},
                      'total_tokens': inputs + images + text}}


class ImagePricingTests(unittest.TestCase):
    def quote(self, **changes):
        return prepare_image_request({'model': 'gpt-image-2', 'prompt': 'fixture', 'quality': 'low', **changes})

    def test_defaults_and_auto_are_normalized_without_text_price_changes(self):
        old_version = PRICE_DATA['version']
        for extra in ({}, {'quality': 'auto', 'size': 'auto'}):
            outgoing, quote = prepare_image_request({'model': 'gpt-image-2', 'prompt': 'fixture', **extra})
            self.assertEqual((outgoing['quality'], outgoing['size'], outgoing['n']), ('medium', '1024x1024', 1))
            self.assertNotIn('stream', outgoing)
            self.assertEqual(json.loads(quote.snapshot)['kind'], 'image_generation')
        self.assertEqual(PRICE_DATA['version'], old_version)
        self.assertFalse(set(image_models()) & set(PRICE_DATA['models']))

    def test_rectangular_ceiling_scales_area_without_changing_actual_charge(self):
        for quality, square_bound in [('low', 512), ('medium', 2048), ('high', 8192)]:
            _, square = self.quote(quality=quality)
            for size in ('1024x1536', '1536x1024'):
                _, rectangle = self.quote(quality=quality, size=size)
                snapshot = json.loads(rectangle.snapshot)
                self.assertEqual(snapshot['image_output_bound'], square_bound * 3 // 2)
                self.assertEqual(snapshot['input_bound'], json.loads(square.snapshot)['input_bound'])
                self.assertGreater(rectangle.amount_units, square.amount_units)
                self.assertEqual(calculate_charge(rectangle.snapshot,
                    billing_details(receipt(), protocol='images.generations')), 4_320_000)
        self.assertEqual(json.loads(self.quote(model='gpt-image-1.5', size='1536x1024')[1].snapshot)['text_output_bound'], 4096)

    def test_multimodal_exact_charge_and_snapshot_immutability(self):
        for model, expected in [('gpt-image-2', 4_320_000), ('gpt-image-1.5', 7_246_080)]:
            _, quote = self.quote(model=model)
            details = billing_details(receipt(model), protocol='images.generations')
            self.assertEqual(calculate_charge(quote.snapshot, details), expected)
            with patch.dict(IMAGE_PRICES['models'][model], {'image_output': '999'}):
                self.assertEqual(calculate_charge(quote.snapshot, details), expected)
        _, quote = self.quote()
        snap = json.loads(quote.snapshot)
        snap['card']['image_output'] = '0.0000001'
        data = receipt(); data['usage'] = {'input_tokens': 0, 'input_tokens_details': {'text_tokens': 0, 'image_tokens': 0},
            'output_tokens': 1, 'output_tokens_details': {'text_tokens': 0, 'image_tokens': 1}, 'total_tokens': 1}
        self.assertEqual(calculate_charge(json.dumps(snap), billing_details(data, protocol='images.generations')), 1)

    def test_unsupported_parameters_and_bad_shapes(self):
        invalid = [{'prompt': ''}, {'prompt': '  '}, {'prompt': 'a' * 32001}, {'prompt': []},
                   {'model': 'gpt-6-luna'}, {'quality': None}, {'quality': []}, {'size': '2048x2048'}, {'size': []},
                   {'n': True}, {'n': 1.0}, {'n': 2}, {'stream': True}, {'stream': 0},
                   {'output_format': 'jpeg'}, {'response_format': 'b64_json'}, {'background': 'transparent'},
                   {'tools': []}, {'partial_images': 1}, {'image': 'https://example.test/a.png'},
                   {'complexity': 'low'}, {'moderation': 'low'}, {'upstream_url': 'https://attacker.invalid'}]
        for change in invalid:
            with self.subTest(change=str(change)[:80]), self.assertRaises(PricingError):
                self.quote(**change)

    def test_missing_contradictory_negative_bool_and_unknown_usage(self):
        for mutate in [lambda u: u.pop('input_tokens_details'), lambda u: u.update(input_tokens=-1),
                       lambda u: u.update(output_tokens=True), lambda u: u.update(total_tokens=0),
                       lambda u: u['input_tokens_details'].update(image_tokens=1),
                       lambda u: u['output_tokens_details'].update(text_tokens=1),
                       lambda u: u.update(cache_tokens=1),
                       lambda u: u['input_tokens_details'].update(audio_tokens=0),
                       lambda u: u.update(output_tokens_details=None)]:
            body = receipt(); mutate(body['usage'])
            self.assertIsNone(billing_details(body, protocol='images.generations'))
        body = receipt('gpt-image-1.5'); body['usage'].pop('output_tokens_details')
        details = billing_details(body, protocol='images.generations')
        with self.assertRaises(PricingError):
            calculate_charge(self.quote(model='gpt-image-1.5')[1].snapshot, details)

    def test_all_modality_ceilings_and_model_mismatch_require_review(self):
        for field, value in [('input_text_tokens', 5000), ('output_image_tokens', 513), ('output_text_tokens', 4097)]:
            model = 'gpt-image-1.5' if field == 'output_text_tokens' else 'gpt-image-2'
            details = billing_details(receipt(model), protocol='images.generations')
            details[field] = value
            details['input_tokens'] = details['input_text_tokens']
            details['output_tokens'] = details['output_image_tokens'] + details['output_text_tokens']
            details['total_tokens'] = details['input_tokens'] + details['output_tokens']
            snapshot = self.quote(model=model)[1].snapshot
            self.assertGreater(calculate_charge(snapshot, details), 0)
            self.assertTrue(image_budget_exceeded(json.loads(snapshot), details))
        details = billing_details(receipt(), protocol='images.generations'); details['model'] = 'wrong-model'
        with self.assertRaises(PricingError): calculate_charge(self.quote()[1].snapshot, details)

    def test_one_valid_png_required(self):
        self.assertTrue(valid_image_response(receipt(), expected_size="1024x1024"))
        for change in [{'data': []}, {'data': [receipt()['data'][0]] * 2}, {'data': [{'url': 'https://example.test/a.png'}]},
                       {'data': [{'b64_json': base64.b64encode(b'<svg/>').decode()}]},
                       {'data': [{'b64_json': png_fixture()[:-5] + 'AAAAA'}]}]:
            self.assertFalse(valid_image_response({**receipt(), **change}, expected_size="1024x1024"))

    def test_png_requires_real_idat_legal_ihdr_exact_canvas_and_scanlines(self):
        for invalid in [png_fixture(compressed=b'not a zlib stream'), png_fixture(bit_depth=0),
                        png_fixture(width=1024, height=1536), png_fixture(compression=1),
                        png_fixture(filtering=1), png_fixture(interlace=1), png_fixture(color=3),
                        png_fixture(scanlines=b'\0' * 100),
                        png_fixture(scanlines=(b'\x05' + b'\0' * (1024 * 3)) * 1024),
                        png_fixture(compressed=zlib.compress(b'\0' * (32 * 1024 * 1024))),
                        png_fixture(compressed=zlib.compress(b'\0' * (1024 * 3 + 1) * 1024)[:-1]),
                        png_fixture(compressed=zlib.compress(b'\0' * (1024 * 3 + 1) * 1024) + b'extra')]:
            body = receipt(); body['data'][0]['b64_json'] = invalid
            with self.subTest(encoded_length=len(invalid)):
                self.assertFalse(valid_image_response(body, expected_size='1024x1024'))
        self.assertFalse(valid_image_response(receipt(), expected_size='100000x100000'))

    def test_png_rgb_rgba_all_five_filters_and_split_idat_decode(self):
        for width, height, color, split in [(1024, 1024, 2, False), (1024, 1536, 6, True), (1536, 1024, 2, True)]:
            channels = 3 if color == 2 else 4
            scanlines = b''.join(bytes([row % 5]) + b'\0' * (width * channels) for row in range(height))
            body = receipt(); body['data'][0]['b64_json'] = png_fixture(width, height, color=color,
                scanlines=scanlines, split_idat=split)
            self.assertTrue(valid_image_response(body, expected_size=f'{width}x{height}'))



class ImageGatewayTests(unittest.TestCase):
    # Reuse the real application's isolated database/login/key fixture without
    # inheriting and re-running unrelated text test methods.
    for name in ('setUp', 'cleanup', 'open_database', 'register_and_login', 'issue_key', 'accounting', 'fund'):
        locals()[name] = getattr(gateway_fixture.CreditGatewayTests, name)

    def setUp(self):
        gateway_fixture.CreditGatewayTests.setUp(self)
        self.stack.enter_context(patch.dict(os.environ, {'GATEWAY_IMAGE_GENERATION': 'true',
            'GATEWAY_IMAGE_API_KEY': 'isolated-image-provider-key'}))
        real_async_client = httpx.AsyncClient
        self.stack.enter_context(patch.object(ai_catalog.httpx, 'AsyncClient',
            side_effect=lambda **kwargs: real_async_client(transport=httpx.MockTransport(self.upstream), **kwargs)))
        self.response_json = receipt()
        self.provider_error = None
        self.catalog_models = ['gpt-6-luna', *image_models()]
        self.catalog_status = 200
        self.catalog_error = None
        self.old_cache, self.old_expires = ai_catalog._cache, ai_catalog._expires
        ai_catalog._cache = None; ai_catalog._expires = 0
        self.addCleanup(self.restore_catalog)

    def restore_catalog(self):
        ai_catalog._cache, ai_catalog._expires = self.old_cache, self.old_expires

    async def upstream(self, request):
        if request.url.path == '/v1/models':
            if self.catalog_error is not None: raise self.catalog_error
            return httpx.Response(self.catalog_status, json={'data': [{'id': m} for m in self.catalog_models]}, request=request)
        self.assertEqual(str(request.url), 'https://api.openai.com/v1/images/generations')
        self.assertEqual(request.headers['authorization'], 'Bearer isolated-image-provider-key')
        self.assertNotIn('x-api-key', request.headers)
        self.assertNotIn('openai-organization', request.headers)
        with self.calls_lock:
            self.calls.append({'path': request.url.path, 'body': json.loads(request.content)})
        self.outbound_started.set()
        if self.provider_error is not None: raise self.provider_error
        if self.pause_outbound:
            import asyncio
            await asyncio.to_thread(self.allow_completion.wait, 10)
        return httpx.Response(self.response_status, json=self.response_json, request=request)

    def payload(self, model='gpt-image-2', **changes):
        return {'model': model, 'prompt': 'a test train', 'quality': 'low', **changes}

    def generate(self, payload=None, **headers):
        return self.client.post('/v1/images/generations', json=payload or self.payload(),
                                headers={'Authorization': 'Bearer ' + self.key, **headers})

    def pending(self):
        usage, hold, wallet, ledger = self.accounting()
        self.assertEqual((usage['credit_status'], hold['status']), ('reconciliation_required', 'reconciliation_required'))
        self.assertEqual(wallet['balance_units'], FUND)
        with closing(self.open_database()) as conn:
            reserved = conn.execute("SELECT SUM(amount_units) FROM credit_holds WHERE user_id=? AND status IN ('held','reconciliation_required')", (self.user_id,)).fetchone()[0]
        self.assertEqual(wallet['reserved_units'], reserved)
        self.assertFalse(any(entry['kind'] == 'usage_charge' for entry in ledger))

    def test_two_models_dispatch_to_fixed_official_api_and_settle_exactly(self):
        self.fund(FUND)
        for model, charge in [('gpt-image-2', 4_320_000), ('gpt-image-1.5', 7_246_080)]:
            self.response_json = receipt(model)
            response = self.generate(self.payload(model))
            self.assertEqual(response.status_code, 200, response.text[:200])
            self.assertEqual(len(response.json()['data']), 1)
            self.assertNotIn('generation_id', response.json()['data'][0])
            usage, hold, wallet, ledger = self.accounting()
            self.assertEqual((usage['protocol'], usage['credit_status'], hold['actual_units']), ('images.generations', 'settled', charge))
            self.assertEqual(wallet['reserved_units'], 0)
            self.assertNotIn(png_fixture(), json.dumps(usage))
            self.assertNotIn('service_tier', self.calls[-1]['body'])
            self.assertEqual(self.calls[-1]['body']['output_format'], 'png')
        self.assertEqual(wallet['balance_units'], FUND - 4_320_000 - 7_246_080)
        summary = self.client.get('/api/developer/usage').json()
        self.assertEqual(summary['summary']['requests'], 2)
        self.assertEqual(summary['summary']['total_tokens'], 638)

    def test_zero_balance_never_dispatches(self):
        response = self.generate()
        self.assertEqual(response.status_code, 402)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.accounting()[1])

    def test_new_keys_include_image_models_and_scope_but_old_grants_stay_restricted(self):
        with closing(self.open_database()) as conn, conn:
            grants = {r[0] for r in conn.execute('SELECT model_id FROM api_key_models WHERE api_key_id=?', (self.key_id,))}
            scopes = conn.execute('SELECT scopes FROM api_keys WHERE id=?', (self.key_id,)).fetchone()[0]
            self.assertTrue(set(image_models()) <= grants)
            self.assertIn('images:generate', scopes)
            conn.execute("UPDATE api_keys SET scopes='models:read,codexproxy:use',allowed_routes='codexproxy/*' WHERE id=?", (self.key_id,))
        self.assertEqual(self.generate().status_code, 403)
        self.assertEqual(self.calls, [])

    def test_model_denied_expired_revoked_and_owner_disabled(self):
        self.fund(FUND)
        for statement in ["DELETE FROM api_key_models WHERE api_key_id=? AND model_id='gpt-image-2'",
                          "UPDATE api_keys SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                          "UPDATE api_keys SET status='revoked' WHERE id=?"]:
            with closing(self.open_database()) as conn, conn: conn.execute(statement, (self.key_id,))
            self.assertIn(self.generate().status_code, (401, 403))
        self.assertEqual(self.calls, [])

    def test_nonjson_and_unknown_image_fields_rejected_before_dispatch(self):
        self.fund(FUND)
        for extra in [{'n': 2}, {'stream': True}, {'tools': []}, {'response_format': 'b64_json'}, {'background': 'transparent'}]:
            self.assertEqual(self.generate(self.payload(**extra)).status_code, 400)
        response = self.client.post('/v1/images/generations', content='x', headers={
            'Authorization': 'Bearer ' + self.key, 'Content-Type': 'text/plain'})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.calls, [])

    def test_missing_and_conflicting_receipts_stay_pending(self):
        self.fund(FUND)
        self.response_json.pop('usage')
        self.assertEqual(self.generate().status_code, 502)
        self.pending()
        self.response_json = receipt(); self.response_json['usage']['total_tokens'] = 1
        self.assertEqual(self.generate().status_code, 502)
        self.pending()

    def test_priceable_single_modality_overrun_preserves_cost_and_delivers_png(self):
        self.fund(FUND)
        self.response_json['usage'].update(output_tokens=513, total_tokens=537)
        self.response_json['usage']['output_tokens_details']['image_tokens'] = 513
        response = self.generate()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['x-credit-status'], 'reconciliation_required')
        self.assertTrue(valid_image_response(response.json(), expected_size='1024x1024'))
        usage, hold, wallet, ledger = self.accounting()
        expected = (24 * 5 + 513 * 30) * 720
        self.assertLess(expected, hold['amount_units'])
        self.assertEqual((hold['status'], hold['actual_units']), ('reconciliation_required', expected))
        self.assertEqual(json.loads(hold['usage_json'])['output_image_tokens'], 513)
        self.assertEqual(json.loads(usage['credit_usage_json']), json.loads(hold['usage_json']))
        self.assertEqual((usage['total_tokens'], usage['success'], usage['credit_charged_units']), (537, 1, 0))
        self.assertIn('ceiling', hold['reason'])
        self.assertEqual(wallet['balance_units'], FUND)
        self.assertFalse(any(row['kind'] == 'usage_charge' for row in ledger))

    def test_known_image_credit_overrun_retains_risk_and_blocks_new_generation(self):
        from app.ai_usage import complete_usage
        self.fund(FUND)
        self.response_json['usage'].update(output_tokens=2000, total_tokens=2024)
        self.response_json['usage']['output_tokens_details']['image_tokens'] = 2000
        response = self.generate()
        self.assertEqual(response.status_code, 200)
        expected = (24 * 5 + 2000 * 30) * 720
        usage, hold, wallet, ledger = self.accounting()
        self.assertEqual((hold['status'], hold['actual_units']), ('reconciliation_required', expected))
        details = billing_details(self.response_json, protocol='images.generations')
        self.assertEqual(json.loads(hold['usage_json']), details)
        self.assertEqual(wallet['risk_units'], expected - hold['amount_units'])
        with closing(self.open_database()) as conn:
            self.assertEqual(pending_overrun_units(conn, self.user_id), expected - hold['amount_units'])
        self.assertEqual(self.generate().status_code, 409)
        self.assertEqual(len(self.calls), 1)
        # An identical receipt cannot settle or erase review; a conflicting
        # smaller receipt must not replace the first authoritative cost.
        for incoming in [details, billing_details(receipt(), protocol='images.generations'), details]:
            with closing(self.open_database()) as conn:
                self.assertTrue(complete_usage(conn, usage['id'], status_code=200, response_time_ms=1,
                    first_byte_ms=1, outcome='succeeded', success=True, billing_usage=incoming))
        after = self.accounting()
        self.assertEqual((after[1]['status'], after[1]['actual_units']), ('reconciliation_required', expected))
        self.assertEqual(json.loads(after[1]['usage_json']), details)
        self.assertEqual(after[2]['balance_units'], FUND)
        self.assertEqual(after[2]['risk_units'], expected - hold['amount_units'])
        self.assertFalse(any(row['kind'] == 'usage_charge' for row in after[3]))
        self.assertEqual(sum(row['kind'] == 'usage_receipt_conflict' for row in after[3]), 1)

    def test_invalid_image_cannot_be_success_or_charged(self):
        self.fund(FUND)
        self.response_json['data'] = [{'b64_json': 'not-a-png'}]
        self.assertEqual(self.generate().status_code, 502)
        self.pending()

    def test_explicit_pregeneration_rejection_releases_hold(self):
        self.fund(FUND)
        self.response_status = 429
        self.response_json = {'error': {'code': 'rate_limit_exceeded', 'message': 'fixture limited'}}
        self.assertEqual(self.generate().status_code, 429)
        usage, hold, wallet, ledger = self.accounting()
        self.assertEqual(hold['status'], 'released')
        self.assertEqual((wallet['balance_units'], wallet['reserved_units']), (FUND, 0))

    def test_upstream_timeout_after_dispatch_stays_pending(self):
        self.fund(FUND)
        self.provider_error = httpx.ReadTimeout('fixture timeout')
        self.assertEqual(self.generate().status_code, 504)
        self.pending()

    def test_unmetered_administrator_still_validates_shape_and_records_usage(self):
        from app.admin_key_policy import ADMIN_UNMETERED_SCOPE
        with closing(self.open_database()) as conn, conn:
            conn.execute("UPDATE users SET role='admin' WHERE id=?", (self.user_id,))
            conn.execute("UPDATE api_keys SET scopes=scopes || ? WHERE id=?", (',' + ADMIN_UNMETERED_SCOPE, self.key_id))
        self.assertEqual(self.generate(self.payload(n=2)).status_code, 400)
        self.assertEqual(self.generate().status_code, 200)
        usage, hold, wallet, ledger = self.accounting()
        self.assertEqual(usage['credit_status'], 'admin_exempt')
        self.assertEqual(json.loads(usage['credit_usage_json'])['output_image_tokens'], 196)
        self.assertIsNone(hold)
        self.assertEqual(wallet['balance_units'], 0)

    def test_dispatch_marker_failure_releases_unsent_hold(self):
        self.fund(FUND)
        with patch('app.cliproxy_gateway._mark_dispatch', side_effect=RuntimeError('fixture marker failure')):
            self.assertEqual(self.generate().status_code, 500)
        usage, hold, wallet, ledger = self.accounting()
        self.assertEqual((hold['status'], usage['dispatch_state']), ('released', 'not_dispatched'))
        self.assertEqual(wallet['reserved_units'], 0)
        self.assertEqual(self.calls, [])

    def test_cancelled_nonstream_image_request_after_dispatch_retains_hold(self):
        import asyncio
        from starlette.requests import Request
        from app.cliproxy_gateway import execute_gateway
        from app import main
        self.fund(FUND)
        self.pause_outbound = True
        async def scenario():
            async def receive():
                return {'type': 'http.request', 'body': json.dumps(self.payload()).encode(), 'more_body': False}
            request = Request({'type': 'http', 'method': 'POST', 'scheme': 'https',
                'path': '/v1/images/generations', 'query_string': b'',
                'headers': [(b'content-type', b'application/json')], 'app': main.app}, receive)
            task = asyncio.create_task(execute_gateway('/v1/images/generations', request, raw_key=self.key))
            for _ in range(100):
                if self.outbound_started.is_set(): break
                await asyncio.sleep(.01)
            self.assertTrue(self.outbound_started.is_set())
            task.cancel()
            self.allow_completion.set()
            with self.assertRaises(asyncio.CancelledError): await task
        asyncio.run(scenario())
        self.pending()
        self.assertEqual(self.accounting()[0]['outcome'], 'client_disconnect')

    def test_late_image_receipt_is_idempotent(self):
        from app.ai_usage import complete_usage
        self.fund(FUND)
        self.response_json.pop('usage')
        self.assertEqual(self.generate().status_code, 502)
        usage_id = self.accounting()[0]['id']
        details = billing_details(receipt(), protocol='images.generations')
        for _ in range(2):
            with closing(self.open_database()) as conn:
                self.assertTrue(complete_usage(conn, usage_id, status_code=200, response_time_ms=1,
                    first_byte_ms=1, outcome='succeeded', success=True, billing_usage=details))
        usage, hold, wallet, ledger = self.accounting()
        self.assertEqual(hold['status'], 'settled')
        self.assertEqual(wallet['balance_units'], FUND - 4_320_000)
        self.assertEqual(sum(row['kind'] == 'usage_charge' for row in ledger), 1)
        self.assertEqual(usage['outcome'], 'image_receipt_invalid')

    def test_two_image_keys_cannot_double_spend_frozen_balance(self):
        from concurrent.futures import ThreadPoolExecutor
        quote = prepare_image_request(self.payload())[1]
        self.fund(quote.amount_units)
        second_key, _, _, _ = self.issue_key('parallel-image-key')
        self.pause_outbound = True
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.generate)
            try:
                self.assertTrue(self.outbound_started.wait(5))
                second = self.client.post('/v1/images/generations', json=self.payload(),
                    headers={'Authorization': 'Bearer ' + second_key})
                self.assertEqual(second.status_code, 402)
                self.assertEqual(len(self.calls), 1)
            finally:
                self.allow_completion.set()
            self.assertEqual(first.result(timeout=5).status_code, 200)
        self.assertEqual(self.accounting()[2]['reserved_units'], 0)

    def test_daily_image_quota_and_owner_status_remain_enforced(self):
        self.fund(FUND)
        with closing(self.open_database()) as conn, conn:
            conn.execute('UPDATE api_keys SET daily_quota=1 WHERE id=?', (self.key_id,))
        self.assertEqual(self.generate().status_code, 200)
        self.assertEqual(self.generate().status_code, 429)
        with closing(self.open_database()) as conn, conn:
            conn.execute("UPDATE users SET status='disabled' WHERE id=?", (self.user_id,))
        self.assertIn(self.generate().status_code, (401, 403))
        self.assertEqual(len(self.calls), 1)

    def test_feature_flag_and_server_key_control_availability(self):
        with patch.dict(os.environ, {'GATEWAY_IMAGE_GENERATION': 'false'}):
            self.assertEqual(self.generate().status_code, 404)
        with patch.dict(os.environ, {'GATEWAY_IMAGE_API_KEY': ''}):
            self.assertEqual(self.generate().status_code, 503)
        self.assertEqual(self.calls, [])

    def test_playground_own_key_same_origin_and_image_only_models(self):
        self.fund(FUND)
        envelope = {'key_id': self.key_id, 'endpoint': 'images.generations', 'payload': self.payload()}
        response = self.client.post('/developer/playground/run', json=envelope, headers={'Origin': 'https://evil.invalid'})
        self.assertEqual(response.status_code, 403)
        envelope['key_id'] = self.key_id + 999
        self.assertEqual(self.client.post('/developer/playground/run', json=envelope, headers={'Origin': BASE_URL}).status_code, 403)
        envelope['key_id'] = self.key_id
        self.assertEqual(self.client.post('/developer/playground/run', json=envelope, headers={'Origin': BASE_URL}).status_code, 200)
        self.assertEqual(self.accounting()[0]['public_path'], 'developer/playground/run/images.generations')
        ai_catalog._cache = None; ai_catalog._expires = 0
        models = self.client.get(f'/developer/playground/models?key_id={self.key_id}&protocol=images.generations').json()['data']
        self.assertEqual({m['id'] for m in models}, set(image_models()))
        self.assertTrue(all(m['callable'] and m['image_generation'] and not m['text_candidate'] for m in models))
        text_models = self.client.get(f'/developer/playground/models?key_id={self.key_id}').json()['data']
        self.assertFalse(any(m['id'].startswith('gpt-image-') for m in text_models))

    def test_image_catalog_survives_missing_empty_and_failed_text_catalog(self):
        for models, status, error in [(['gpt-6-luna'], 200, None), ([], 200, None),
                ([], 503, None), ([], 200, httpx.ConnectError('fixture text catalog unavailable'))]:
            with self.subTest(models=models, status=status, error=error):
                self.catalog_models, self.catalog_status, self.catalog_error = models, status, error
                ai_catalog._cache = None; ai_catalog._expires = 0
                catalog = self.client.get('/api/public/ai-catalog').json()
                images = [m for m in catalog['models'] if m['image_generation']]
                self.assertEqual({m['id'] for m in images}, set(image_models()))
                self.assertTrue(all(m['callable'] and not m['text_candidate'] and not m['verification'] for m in images))
                if status != 200 or error is not None: self.assertEqual(catalog['status'], 'degraded')
                self.assertNotIn(catalog['default_model'], image_models())
                options = self.client.get(f'/developer/playground/models?key_id={self.key_id}&protocol=images.generations').json()['data']
                self.assertEqual({m['id'] for m in options}, set(image_models()))
                text_options = self.client.get(f'/developer/playground/models?key_id={self.key_id}').json()['data']
                self.assertFalse(any(m['id'] in image_models() for m in text_options))
        with patch.dict(os.environ, {'GATEWAY_IMAGE_GENERATION': 'false'}):
            ai_catalog._cache = None; ai_catalog._expires = 0
            self.assertEqual(self.client.get('/api/public/ai-catalog').json()['models'], [])

    def test_pages_catalog_prices_and_safe_preview_are_reachable(self):
        ai_catalog._cache = None; ai_catalog._expires = 0
        for path in ['/developer/images', '/models', '/pricing', '/docs']:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn('gpt-image-2', response.text) if path != '/developer/images' else self.assertIn('image-form', response.text)
        page = self.client.get('/developer/images')
        self.assertIn("img-src 'self' data:", page.headers['content-security-policy'])
        self.assertEqual(page.headers['cache-control'], 'no-store')
        self.assertIn('image_generation', self.client.get('/api/public/ai-catalog').text)
        self.assertIn('/developer/images', self.client.get('/developer').text)
        self.assertIn('/developer/images', self.client.get('/developer/playground').text)
        script = self.client.get('/static/images.js').text
        self.assertNotIn('innerHTML', script)
        self.assertNotIn('GATEWAY_IMAGE_API_KEY', script)
        self.assertIn('data:image/png;base64,', script)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/developer/images').status_code, 401)


if __name__ == '__main__': unittest.main()

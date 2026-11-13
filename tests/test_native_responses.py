"""Optional official Responses transport and precise Chat pre-generation rejection.

All credentials, providers and SQLite state are fixtures. No provider calls.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import unittest
from contextlib import closing
from unittest.mock import patch

import httpx

import test_credit_gateway as fixture
from app.cliproxy_gateway import _explicit_generation_rejection
from app.credit_pricing import PRICE_DATA

MODEL = 'gpt-6-astra'
FUND = 200_000_000
CHARGE = 943_200


def tool_response():
    return {'id': 'resp_native_fixture', 'object': 'response', 'status': 'completed', 'model': MODEL,
            'service_tier': 'default', 'output': [{'type': 'function_call', 'id': 'fc_fixture',
                'call_id': 'call_fixture', 'name': 'report_ready', 'arguments': '{"ready":true}', 'status': 'completed'}],
            'usage': {'input_tokens': 61, 'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
                      'output_tokens': 14, 'total_tokens': 75}}


def compatibility_error(model=MODEL):
    return {'error': {'type': 'invalid_request_error', 'message': (
        f"Function tools with reasoning_effort are not supported for {model} in /v1/chat/completions. "
        "To use function tools, use /v1/responses or set reasoning_effort to 'none'.")}}


class PreciseCompatibilityRejectionTests(unittest.TestCase):
    def test_exact_structured_requested_model_message_is_required(self):
        original = compatibility_error()
        self.assertTrue(_explicit_generation_rejection(original, 400, 'chat.completions', request_model=MODEL))
        cases = [(original, 500, 'chat.completions', MODEL), (original, 400, 'responses', MODEL),
                 (original, 400, 'chat.completions', 'other-model'), (original, 400, 'chat.completions', None),
                 ({'error': {'type': 'server_error', 'message': original['error']['message']}}, 400, 'chat.completions', MODEL),
                 ({'error': {'message': original['error']['message']}}, 400, 'chat.completions', MODEL),
                 ({'error': {'type': 'invalid_request_error', 'message': original['error']['message'] + ' '}}, 400, 'chat.completions', MODEL),
                 ({'error': {'type': 'invalid_request_error', 'message': original['error']['message'][:-1]}}, 400, 'chat.completions', MODEL)]
        for body, status, protocol, model in cases:
            with self.subTest(status=status, protocol=protocol, model=model, body=body):
                self.assertFalse(_explicit_generation_rejection(body, status, protocol, request_model=model))

    def test_any_receipt_or_output_field_even_empty_or_nested_prevents_release(self):
        for key in ('usage', 'choices', 'output', 'response', 'content', 'data'):
            for value in (None, [], {}, ''):
                for nested in (False, True):
                    body = compatibility_error()
                    (body['error'] if nested else body)[key] = value
                    with self.subTest(key=key, value=value, nested=nested):
                        self.assertFalse(_explicit_generation_rejection(body, 400, 'chat.completions', request_model=MODEL))


class NativeResponsesTests(unittest.TestCase):
    for name in ('cleanup', 'open_database', 'register_and_login', 'issue_key', 'accounting', 'fund'):
        locals()[name] = getattr(fixture.CreditGatewayTests, name)

    def setUp(self):
        fixture.CreditGatewayTests.setUp(self)
        self.stack.enter_context(patch.dict(os.environ, {'GATEWAY_NATIVE_RESPONSES': 'true',
            'GATEWAY_NATIVE_RESPONSES_API_KEY': 'fixture-native-response-provider-key'}))
        self.response_json = tool_response()
        self.provider_error = None
        self.expected_native = True
        self.price_version = PRICE_DATA['version']

    async def upstream(self, request):
        body = json.loads(request.content) if request.content else None
        with self.calls_lock:
            self.calls.append({'url': str(request.url), 'path': request.url.path, 'body': body, 'headers': dict(request.headers)})
        if request.url.path == '/v1/images/generations':
            self.assertEqual(str(request.url), 'https://api.openai.com/v1/images/generations')
            self.assertEqual(request.headers['authorization'], 'Bearer fixture-image-provider-key')
            self.assertEqual(request.headers['accept'], 'application/json')
        elif self.expected_native and request.url.path == '/v1/responses':
            self.assertEqual(str(request.url), 'https://api.openai.com/v1/responses')
            self.assertEqual(request.headers['authorization'], 'Bearer fixture-native-response-provider-key')
            self.assertEqual(request.headers['accept'], 'text/event-stream' if body.get('stream') else 'application/json')
            self.assertEqual(request.headers['content-type'], 'application/json')
        else:
            self.assertEqual(request.url.host, 'upstream.example.test')
            self.assertEqual(request.headers['authorization'], 'Bearer mock-upstream-key-never-used-on-a-network')
        self.outbound_started.set()
        if self.provider_error is not None: raise self.provider_error
        if self.pause_outbound: await asyncio.to_thread(self.allow_completion.wait, 10)
        if self.response_sse is not None:
            return httpx.Response(self.response_status, headers={'content-type': 'text/event-stream'},
                stream=fixture.FixtureStream(self.response_sse), request=request)
        return httpx.Response(self.response_status, json=self.response_json, request=request)

    def payload(self, **changes):
        return {'model': MODEL, 'input': 'Report readiness with the function.', 'max_output_tokens': 128,
                'reasoning': {'effort': 'low'}, 'parallel_tool_calls': False,
                'tools': [{'type': 'function', 'name': 'report_ready', 'description': 'Report ready state', 'strict': True,
                    'parameters': {'type': 'object', 'properties': {'ready': {'type': 'boolean'}},
                                   'required': ['ready'], 'additionalProperties': False}}],
                'tool_choice': {'type': 'function', 'name': 'report_ready'}, **changes}

    def generate(self, payload=None, **headers):
        return self.client.post('/v1/responses', json=payload or self.payload(),
            headers={'Authorization': 'Bearer ' + self.key, **headers})

    def assert_settled(self):
        usage, hold, wallet, ledger = self.accounting()
        self.assertEqual((usage['protocol'], hold['status'], hold['actual_units']), ('responses', 'settled', CHARGE))
        self.assertEqual((usage['input_tokens'], usage['output_tokens'], usage['total_tokens']), (61, 14, 75))
        self.assertEqual((wallet['balance_units'], wallet['reserved_units']), (FUND - CHARGE, 0))
        self.assertEqual(sum(row['kind'] == 'usage_charge' for row in ledger), 1)
        self.assertEqual(PRICE_DATA['version'], self.price_version)

    def assert_pending(self):
        usage, hold, wallet, ledger = self.accounting()
        self.assertEqual((hold['status'], hold['actual_units']), ('reconciliation_required', None))
        with closing(self.open_database()) as conn:
            reserved = conn.execute("SELECT SUM(amount_units) FROM credit_holds WHERE user_id=? AND status IN ('held','reconciliation_required')", (self.user_id,)).fetchone()[0]
        self.assertEqual((wallet['balance_units'], wallet['reserved_units']), (FUND, reserved))
        self.assertFalse(any(row['kind'] == 'usage_charge' for row in ledger))

    def test_native_json_function_result_preserves_request_and_settles_usage(self):
        self.fund(FUND)
        payload = self.payload()
        result = self.generate(payload, **{'OpenAI-Organization': 'caller-org', 'OpenAI-Project': 'caller-project',
            'X-API-Key': self.key, 'X-Request-ID': 'fixture-native-request',
            'User-Agent': 'caller-user-agent', 'Cookie': 'caller-cookie=bad', 'Idempotency-Key': 'caller-idempotency'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), tool_response())
        sent = self.calls[-1]
        for key in ('model', 'input', 'max_output_tokens', 'reasoning', 'tools', 'tool_choice', 'parallel_tool_calls'):
            self.assertEqual(sent['body'][key], payload[key])
        self.assertEqual(sent['body']['store'], False)
        self.assertEqual(sent['body']['service_tier'], 'default')
        for key in ('openai-organization', 'openai-project', 'x-api-key', 'cookie', 'x-request-id', 'idempotency-key'):
            self.assertNotIn(key, sent['headers'])
        self.assertNotEqual(sent['headers']['user-agent'], 'caller-user-agent')
        self.assert_settled()

    def test_native_sse_function_call_and_authoritative_receipt_settle(self):
        self.fund(FUND)
        self.response_sse = (fixture.sse_event({'type': 'response.created', 'response': {'id': 'resp_native_fixture', 'model': MODEL}}, 'response.created')
            + fixture.sse_event({'type': 'response.output_item.added', 'item': tool_response()['output'][0]}, 'response.output_item.added')
            + fixture.sse_event({'type': 'response.completed', 'response': tool_response()}, 'response.completed'))
        result = self.generate(self.payload(stream=True))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content, self.response_sse)
        self.assert_settled()

    def test_flag_off_keeps_original_proxy_even_with_native_key(self):
        self.fund(FUND)
        self.expected_native = False
        with patch.dict(os.environ, {'GATEWAY_NATIVE_RESPONSES': 'false'}):
            self.assertEqual(self.generate().status_code, 200)
        self.assertEqual(self.calls[-1]['url'], 'https://upstream.example.test/v1/responses')
        self.assert_settled()

    def test_reviewed_alias_uses_native_and_requires_native_key(self):
        self.fund(FUND)
        self.response_json = tool_response()
        self.response_json['model'] = 'gpt-4o-mini'
        result = self.generate(self.payload(model='gpt-4o-mini'))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.calls[-1]['url'], 'https://api.openai.com/v1/responses')
        with patch.dict(os.environ, {'GATEWAY_NATIVE_RESPONSES_API_KEY': ''}):
            self.assertEqual(self.generate(self.payload(model='gpt-4o-mini')).status_code, 503)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.accounting()[2]['reserved_units'], 0)

    def test_unreviewed_legacy_model_stays_proxy_even_without_native_key(self):
        self.fund(FUND)
        self.expected_native = False
        self.response_json = tool_response(); self.response_json['model'] = 'gpt-3.5-turbo'
        with patch.dict(os.environ, {'GATEWAY_NATIVE_RESPONSES_API_KEY': ''}):
            result = self.generate(self.payload(model='gpt-3.5-turbo'))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.calls[-1]['url'], 'https://upstream.example.test/v1/responses')

    def test_enabled_missing_key_does_not_reserve_dispatch_or_fallback(self):
        self.fund(FUND)
        with patch.dict(os.environ, {'GATEWAY_NATIVE_RESPONSES_API_KEY': ''}):
            result = self.generate()
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()['error']['code'], 'upstream_unconfigured')
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.accounting()[0])
        self.assertIsNone(self.accounting()[1])

    def test_chat_stays_on_proxy_and_known_compatibility_error_releases(self):
        self.fund(FUND)
        self.response_status = 400
        self.response_json = compatibility_error()
        payload = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'Use a function'}], 'reasoning_effort': 'low',
                   'max_completion_tokens': 128,
                   'tools': [{'type': 'function', 'function': {'name': 'report_ready', 'parameters': {'type': 'object'}}}]}
        result = self.client.post('/v1/chat/completions', json=payload, headers={'Authorization': 'Bearer ' + self.key})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.calls[-1]['url'], 'https://upstream.example.test/v1/chat/completions')
        self.assertEqual(self.calls[-1]['body']['reasoning_effort'], 'low')
        self.assertEqual(self.calls[-1]['body']['tools'], payload['tools'])
        usage, hold, wallet, ledger = self.accounting()
        self.assertEqual((hold['status'], hold['actual_units']), ('released', 0))
        self.assertEqual((wallet['balance_units'], wallet['reserved_units']), (FUND, 0))

    def test_unknown_400_or_exact_error_with_usage_stays_pending(self):
        self.fund(FUND)
        self.response_status = 400
        self.response_json = {'error': {'type': 'invalid_request_error', 'message': 'Unknown provider rejection'}}
        self.assertEqual(self.generate().status_code, 400)
        self.assert_pending()
        # Use Chat here so this is the exact recognized protocol/message, but
        # the receipt evidence prevents a no-charge release.
        self.response_json = {**compatibility_error(), 'usage': {}}
        result = self.client.post('/v1/chat/completions', json={'model': MODEL,
            'messages': [{'role': 'user', 'content': 'Use function'}], 'max_completion_tokens': 128},
            headers={'Authorization': 'Bearer ' + self.key})
        self.assertEqual(result.status_code, 400)
        self.assert_pending()

    def test_scope_model_expiry_and_disabled_key_still_block_native_dispatch(self):
        self.fund(FUND)
        scenarios = [("UPDATE api_keys SET scopes='models:read,chat:completions' WHERE id=?", (self.key_id,)),
                     ("UPDATE api_keys SET scopes='models:read,responses:create' WHERE id=?", (self.key_id,)),
                     ("DELETE FROM api_key_models WHERE api_key_id=? AND model_id=?", (self.key_id, MODEL))]
        for index, (sql, params) in enumerate(scenarios):
            with closing(self.open_database()) as conn, conn: conn.execute(sql, params)
            if index != 1: self.assertIn(self.generate().status_code, (401, 403))
        with closing(self.open_database()) as conn, conn:
            conn.execute('INSERT INTO api_key_models(api_key_id,model_id) VALUES (?,?)', (self.key_id, MODEL))
            conn.execute("UPDATE api_keys SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (self.key_id,))
        self.assertEqual(self.generate().status_code, 403)
        with closing(self.open_database()) as conn, conn:
            conn.execute("UPDATE api_keys SET expires_at=NULL,status='revoked' WHERE id=?", (self.key_id,))
        self.assertEqual(self.generate().status_code, 401)
        self.assertEqual(self.calls, [])

    def test_dispatch_marker_failure_releases_before_send(self):
        self.fund(FUND)
        with patch('app.cliproxy_gateway._mark_dispatch', side_effect=RuntimeError('fixture marker failure')):
            self.assertEqual(self.generate().status_code, 500)
        usage, hold, wallet, ledger = self.accounting()
        self.assertEqual((usage['dispatch_state'], hold['status']), ('not_dispatched', 'released'))
        self.assertEqual(wallet['reserved_units'], 0)
        self.assertEqual(self.calls, [])

    def test_timeout_after_dispatch_stays_pending(self):
        self.fund(FUND)
        self.provider_error = httpx.ReadTimeout('fixture native timeout')
        self.assertEqual(self.generate().status_code, 504)
        self.assert_pending()
        self.assertEqual(self.accounting()[0]['dispatch_state'], 'dispatched')

    def test_missing_or_conflicting_native_usage_is_not_guessed(self):
        self.fund(FUND)
        self.response_json.pop('usage')
        self.assertEqual(self.generate().status_code, 200)
        self.assert_pending()
        self.response_json = tool_response()
        self.response_json['usage']['total_tokens'] = 1
        self.assertEqual(self.generate().status_code, 200)
        self.assert_pending()

    def test_images_still_work_with_native_responses_enabled_without_its_key(self):
        import test_image_generation as images
        self.fund(FUND)
        self.response_json = images.receipt()
        with patch.dict(os.environ, {'GATEWAY_IMAGE_GENERATION': 'true',
                'GATEWAY_IMAGE_API_KEY': 'fixture-image-provider-key', 'GATEWAY_NATIVE_RESPONSES_API_KEY': ''}):
            result = self.client.post('/v1/images/generations', json={'model': 'gpt-image-2', 'prompt': 'fixture', 'quality': 'low'},
                headers={'Authorization': 'Bearer ' + self.key})
        self.assertEqual(result.status_code, 200)
        usage, hold, wallet, ledger = self.accounting()
        self.assertEqual((usage['protocol'], hold['status'], hold['actual_units']), ('images.generations', 'settled', 4_320_000))

    def test_native_transport_is_independent_of_image_flags_or_key(self):
        self.fund(FUND)
        with patch.dict(os.environ, {'GATEWAY_IMAGE_GENERATION': 'false', 'GATEWAY_IMAGE_API_KEY': ''}):
            self.assertEqual(self.generate().status_code, 200)
        self.assert_settled()


if __name__ == '__main__': unittest.main()

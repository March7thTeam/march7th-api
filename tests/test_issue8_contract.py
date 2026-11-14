"""Issue8: native coverage, model output caps and distinct tool evidence."""
from __future__ import annotations

import copy
from contextlib import closing, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx

import test_credit_gateway as fixture
import test_native_responses as native
from app import ai_catalog
from app.cliproxy_gateway import _auto_output_limit, _explicit_generation_rejection
from app.credit_pricing import PRICE_DATA, PricingError, checked_output_limit, prepare_billable_request
from app.text_capabilities import (NATIVE_RESPONSES_MODELS, TEXT_CAPABILITY_DATA,
                                   automatic_output_tiers, output_token_ceiling)
from app.model_lines import verification_note
from scripts.issue8_contract_probe import _audit, _identity_matches, run_contract, main as probe_main


class ModelCapabilityTests(unittest.TestCase):
    def test_reviewed_caps_and_native_routes_share_one_metadata_table(self):
        self.assertEqual(len(NATIVE_RESPONSES_MODELS), 22)
        expected = {'gpt-4o': 16384, 'gpt-4o-mini': 16384, 'gpt-4.1': 32768,
                    'gpt-4.1-mini': 32768, 'o3': 100000}
        for model, entry in TEXT_CAPABILITY_DATA['models'].items():
            self.assertEqual(output_token_ceiling(model), expected.get(model, 128000))
            self.assertIn('responses', entry['supported_endpoints'])
            self.assertTrue(entry['source'].startswith('https://developers.openai.com/'))
            self.assertLessEqual(automatic_output_tiers(model)[0], output_token_ceiling(model))
            self.assertFalse(model.startswith('gpt-image-'))
        self.assertEqual(automatic_output_tiers('gpt-4o-mini'), (16384, 8192, 4096, 2048))
        self.assertEqual(automatic_output_tiers('legacy-unknown'), (2048,))
        self.assertEqual(output_token_ceiling('legacy-unknown'), 128000)

    def test_explicit_caps_are_validated_not_silently_clamped(self):
        for model, limit in [('gpt-4o-mini', 16384), ('gpt-4.1', 32768), ('o3', 100000), ('gpt-5.5', 128000)]:
            for protocol, field in [('responses', 'max_output_tokens'), ('chat.completions', 'max_tokens')]:
                payload = {'model': model, field: limit}
                self.assertEqual(checked_output_limit(payload, protocol=protocol), limit)
                payload[field] = limit + 1
                with self.assertRaises(PricingError): checked_output_limit(payload, protocol=protocol)
        for invalid in (True, 1.5, '100', None, 0):
            with self.assertRaises(PricingError):
                checked_output_limit({'model': 'gpt-4o-mini', 'max_output_tokens': invalid}, protocol='responses')

    def test_automatic_cap_and_balance_thresholds(self):
        payload = {'model': 'gpt-4o-mini', 'input': 'fixture'}
        quotes = {tier: prepare_billable_request(payload, protocol='responses', default_output_tokens=tier)[1]
                  for tier in (16384, 8192, 4096, 2048)}
        self.assertEqual(_auto_output_limit(payload, 'responses', quotes[16384].amount_units), 16384)
        self.assertEqual(_auto_output_limit(payload, 'responses', quotes[16384].amount_units - 1), 8192)
        self.assertEqual(_auto_output_limit(payload, 'responses', quotes[4096].amount_units - 1), 2048)
        self.assertEqual(_auto_output_limit(payload, 'responses', 0), 2048)
        outgoing, _ = prepare_billable_request(payload, protocol='responses', default_output_tokens=32768)
        self.assertEqual(outgoing['max_output_tokens'], 16384)
        legacy = {'model': 'gpt-3.5-turbo', 'input': 'fixture'}
        self.assertEqual(_auto_output_limit(legacy, 'responses', 10**9), 2048)
        outgoing, _ = prepare_billable_request(legacy, protocol='responses', default_output_tokens=32768)
        self.assertEqual(outgoing['max_output_tokens'], 2048)
        outgoing, _ = prepare_billable_request({**legacy, 'max_output_tokens': 50000}, protocol='responses')
        self.assertEqual(outgoing['max_output_tokens'], 50000)

    def test_legacy_text_evidence_never_implies_tools(self):
        old = {'protocols': ['Chat Completions', 'Responses'], 'checked_at': '2026-10-15T12:00:00Z'}
        original = copy.deepcopy(old)
        public = ai_catalog.public_verification({'gpt-5.5': old}, 'gpt-5.5')
        self.assertNotIn('tool_protocols', public)
        self.assertIn('工具待实测', verification_note(public, True))
        new = {**old, 'tool_protocols': ['Responses', 'Responses SSE'], 'tools_checked_at': '2026-11-13T14:00:00Z'}
        public = ai_catalog.public_verification({'gpt-5.5': new}, 'gpt-5.5')
        self.assertEqual(public['checked_at'], '2026-10-15T12:00:00+00:00')
        self.assertEqual(public['tools_checked_at'], '2026-11-13T14:00:00+00:00')
        self.assertEqual(public['tool_protocols'], ['Responses', 'Responses SSE'])
        self.assertEqual(old, original)
        invalid = {**old, 'tool_protocols': ['Images'], 'tools_checked_at': 'bad'}
        self.assertNotIn('tool_protocols', ai_catalog.public_verification({'x': invalid}, 'x'))


class Issue8GatewayTests(unittest.TestCase):
    for name in ('cleanup', 'open_database', 'register_and_login', 'issue_key', 'fund', 'accounting'):
        locals()[name] = getattr(fixture.CreditGatewayTests, name)

    def setUp(self):
        fixture.CreditGatewayTests.setUp(self)
        self.stack.enter_context(patch.dict(os.environ, {'GATEWAY_NATIVE_RESPONSES': 'true',
            'GATEWAY_NATIVE_RESPONSES_API_KEY': 'fixture-native-key'}))
        real_client = httpx.AsyncClient
        self.stack.enter_context(patch.object(ai_catalog.httpx, 'AsyncClient',
            side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(self.upstream), **kwargs)))
        self.old_cache, self.old_expiry = ai_catalog._cache, ai_catalog._expires
        ai_catalog._cache = None; ai_catalog._expires = 0
        self.addCleanup(self.restore_catalog)
        self.force_error = False
        self.forced_status = 400

    def restore_catalog(self):
        ai_catalog._cache, ai_catalog._expires = self.old_cache, self.old_expiry

    async def upstream(self, request):
        if request.method == 'GET': return httpx.Response(503, json={'error': 'fixture directory down'}, request=request)
        body = json.loads(request.content)
        self.calls.append({'url': str(request.url), 'body': body})
        if self.force_error:
            return httpx.Response(self.forced_status, json=self.response_json, request=request)
        data = {'model': body['model'], 'status': 'completed', 'output': [],
                'usage': {'input_tokens': 11, 'output_tokens': 2, 'total_tokens': 13,
                          'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}}
        if body.get('stream'):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                content=fixture.sse_event({'type': 'response.completed', 'response': data}), request=request)
        return httpx.Response(200, json=data, request=request)

    def generate(self, model='gpt-5.5', **changes):
        return self.client.post('/v1/responses', json={'model': model, 'input': 'fixture', **changes},
                                headers={'Authorization': 'Bearer ' + self.key})

    def test_52_55_native_tools_retain_explicit_fields_for_json_and_stream(self):
        self.fund(500_000_000)
        for model in ('gpt-5.2', 'gpt-5.5'):
            for stream in (False, True):
                extra = {'max_output_tokens': 160, 'reasoning': {'effort': 'low'},
                         'tools': [{'type': 'function', 'name': 'ready', 'parameters': {'type': 'object'}}],
                         'tool_choice': {'type': 'function', 'name': 'ready'}, 'stream': stream}
                self.assertEqual(self.generate(model, **extra).status_code, 200)
                sent = self.calls[-1]
                self.assertEqual(sent['url'], 'https://api.openai.com/v1/responses')
                self.assertNotIn('max_tokens', sent['body'])
                for field, value in extra.items(): self.assertEqual(sent['body'][field], value)
                self.assertEqual(self.accounting()[1]['status'], 'settled')

    def test_4omini_default_and_explicit_limits_and_no_dispatch_rejection(self):
        self.fund(500_000_000)
        self.assertEqual(self.generate('gpt-4o-mini').status_code, 200)
        self.assertEqual(self.calls[-1]['body']['max_output_tokens'], 16384)
        self.assertEqual(self.generate('gpt-4o-mini', max_output_tokens=15000).status_code, 200)
        self.assertEqual(self.calls[-1]['body']['max_output_tokens'], 15000)
        count = len(self.calls)
        self.assertEqual(self.generate('gpt-4o-mini', max_output_tokens=16385).status_code, 400)
        self.assertEqual(len(self.calls), count)

    def test_native_missing_key_and_flag_off(self):
        self.fund(500_000_000)
        with patch.dict(os.environ, {'GATEWAY_NATIVE_RESPONSES_API_KEY': ''}):
            self.assertEqual(self.generate('gpt-5.2', max_output_tokens=16).status_code, 503)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.accounting()[1])
        with patch.dict(os.environ, {'GATEWAY_NATIVE_RESPONSES': 'false'}):
            self.assertEqual(self.generate('gpt-5.5', max_output_tokens=16).status_code, 200)
        self.assertEqual(self.calls[-1]['url'], 'https://upstream.example.test/v1/responses')

    def test_unmetered_applies_limits_without_billing(self):
        with closing(self.open_database()) as conn, conn:
            conn.execute("UPDATE users SET role='admin' WHERE id=?", (self.user_id,))
            conn.execute("UPDATE api_keys SET scopes=scopes || ',admin:unmetered' WHERE id=?", (self.key_id,))
        self.assertEqual(self.generate('gpt-4o-mini', max_output_tokens=16385).status_code, 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.generate('gpt-4o-mini').status_code, 200)
        self.assertEqual(self.calls[-1]['body']['max_output_tokens'], 16384)
        usage, hold, wallet, _ = self.accounting()
        self.assertIsNone(hold)
        self.assertEqual(usage['credit_status'], 'admin_exempt')
        self.assertEqual(wallet['balance_units'], 0)

    def test_zero_balance_default_cannot_dispatch(self):
        self.assertEqual(self.generate('gpt-4o-mini').status_code, 402)
        self.assertEqual(self.calls, [])

    def test_friendly_chat_error_uses_original_evidence_before_rewrite(self):
        self.fund(500_000_000)
        self.force_error = True
        self.response_json = native.compatibility_error()
        original = copy.deepcopy(self.response_json)
        response = self.client.post('/v1/chat/completions', json={'model': native.MODEL,
            'messages': [{'role': 'user', 'content': 'ready'}], 'max_completion_tokens': 128,
            'reasoning_effort': 'low', 'tools': [{'type': 'function', 'function': {'name': 'ready'}}]},
            headers={'Authorization': 'Bearer ' + self.key})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'chat_tools_require_responses')
        self.assertNotIn('none', response.json()['error']['message'])
        self.assertIn('/v1/responses', response.json()['error']['message'])
        self.assertEqual(self.accounting()[1]['status'], 'released')
        self.assertEqual(self.response_json, original)
        self.response_json = {**original, 'usage': {}}
        contradiction = self.client.post('/v1/chat/completions', json={'model': native.MODEL, 'messages': [], 'max_completion_tokens': 128},
            headers={'Authorization': 'Bearer ' + self.key})
        self.assertEqual(self.accounting()[1]['status'], 'reconciliation_required')
        self.assertEqual(contradiction.json()['error']['message'], original['error']['message'])

    def test_exact_official_credit_exhaustion_releases_and_empty_usage_does_not(self):
        self.fund(500_000_000)
        self.force_error = True; self.forced_status = 429
        self.response_json = {'error': {'type': 'insufficient_quota', 'code': 'credit_balance_exhausted',
                                        'param': None, 'message': 'OpenAI credits exhausted'}}
        self.assertEqual(self.generate(max_output_tokens=16).status_code, 429)
        self.assertEqual(self.accounting()[1]['status'], 'released')
        self.response_json['usage'] = {}
        self.assertEqual(self.generate(max_output_tokens=16).status_code, 429)
        self.assertEqual(self.accounting()[1]['status'], 'reconciliation_required')

    def test_native_catalog_survives_proxy_failure_without_claiming_paid_validation(self):
        result = self.client.get('/api/public/ai-catalog').json()
        self.assertEqual(result['status'], 'degraded')
        items = {item['id']: item for item in result['models']}
        self.assertTrue(NATIVE_RESPONSES_MODELS <= items.keys())
        self.assertTrue(items['gpt-5.5']['native_responses'])
        self.assertEqual(items['gpt-4o-mini']['max_output_tokens'], 16384)
        self.assertFalse(items['gpt-6.1-terra']['billable'])
        self.assertNotIn('tool_protocols', items['gpt-5.5']['verification'])
        text = self.client.get('/models').text
        self.assertIn('原生 Responses', text)
        self.assertIn('工具请求尚未实测', text)


class ExactQuotaRejectionTests(unittest.TestCase):
    def test_only_exact_code_type_status_and_absent_generation_fields_release(self):
        body = {'error': {'type': 'insufficient_quota', 'code': 'credit_balance_exhausted', 'param': None}}
        self.assertTrue(_explicit_generation_rejection(body, 429, 'responses'))
        for status, error_type, code in [(400, 'insufficient_quota', 'credit_balance_exhausted'),
                (429, 'invalid_request_error', 'credit_balance_exhausted'), (429, 'insufficient_quota', 'unknown_credit_error')]:
            self.assertFalse(_explicit_generation_rejection({'error': {'type': error_type, 'code': code}}, status, 'responses'))
        for field in ('usage', 'choices', 'output', 'response', 'content', 'data'):
            for value in (None, {}, [], ''):
                for nested in (False, True):
                    candidate = copy.deepcopy(body)
                    (candidate['error'] if nested else candidate)[field] = value
                    self.assertFalse(_explicit_generation_rejection(candidate, 429, 'responses'))


from app.price_store import seed_release


class ProbeTests(unittest.TestCase):
    @staticmethod
    def chat_tool_response(model=native.MODEL):
        return {'id': 'chatcmpl_fixture', 'object': 'chat.completion', 'model': model,
                'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                    'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call_fixture',
                        'type': 'function', 'function': {'name': 'report_ready', 'arguments': '{"ready":true}'}}]}}],
                'usage': {'prompt_tokens': 61, 'completion_tokens': 14, 'total_tokens': 75}}

    @staticmethod
    def chat_rejection():
        return {'error': {'type': 'invalid_request_error', 'code': 'chat_tools_require_responses',
                          'message': 'Use /v1/responses preserving reasoning and tools.', 'param': None}}

    def api_only_probe(self, transform=None, *, prefix=b'', suffix=b'', transform_defaults=False,
                       chat_payload=None, chat_status=400, chat_model=native.MODEL, ledger_db=None):
        def handler(request):
            body = json.loads(request.content)
            if request.url.path.endswith('chat/completions') and body.get('tools'):
                return httpx.Response(chat_status, json=self.chat_rejection() if chat_payload is None else chat_payload)
            data = native.tool_response(); data['model'] = body['model']
            if transform and (body.get('tools') or transform_defaults):
                transform(data)
            if body.get('stream'):
                complete = fixture.sse_event({'type': 'response.completed', 'response': data})
                return httpx.Response(200, content=prefix + complete + suffix)
            return httpx.Response(200, json=data)
        with httpx.Client(base_url='http://127.0.0.1', transport=httpx.MockTransport(handler)) as client:
            return run_contract(client, models=['gpt-5.5'], chat_model=chat_model, ledger_db=ledger_db)

    def test_chat_tools_accepts_real_completion_and_reviewed_snapshot_without_chat_badge(self):
        for requested, actual in [(native.MODEL, native.MODEL), ('gpt-5.5', 'gpt-5.5-2026-04-23')]:
            with self.subTest(requested=requested, actual=actual):
                payload = self.chat_tool_response(actual); payload['error'] = None
                result = self.api_only_probe(chat_payload=payload, chat_status=200, chat_model=requested)
                row = next(row for row in result['checks'] if row['case'] == 'chat_tools_negative')
                self.assertTrue(result['passed'])
                self.assertEqual(row['chat_tools_outcome'], 'supported_200')
                self.assertTrue(row['tool_call_ok'])
                self.assertEqual(row['usage'], {'input_tokens': 61, 'output_tokens': 14, 'total_tokens': 75})
                self.assertEqual(row['actual_model'], actual)
                self.assertFalse(row['stream'])
                self.assertTrue(all(record['tool_protocols'] == ['Responses', 'Responses SSE']
                                    for record in result['verified_tool_records'].values()))
        with patch.dict(PRICE_DATA, {'version': 'unreviewed-price-version'}), patch('app.price_store.current_release', side_effect=lambda: {**seed_release(), 'text': PRICE_DATA}):
            result = self.api_only_probe(chat_payload=self.chat_tool_response('gpt-5.5-2026-04-23'),
                                         chat_status=200, chat_model='gpt-5.5')
            self.assertFalse(result['passed'])

    def test_chat_tools_rejects_non_chat_malformed_or_unfinished_tool_calls(self):
        def message(data): return data['choices'][0]['message']
        def function(data): return message(data)['tool_calls'][0]['function']
        transforms = [
            lambda data: data.update(object='response'),
            lambda data: data.pop('choices'),
            lambda data: data.update(choices=[]),
            lambda data: data['choices'].append('invalid'),
            lambda data: data['choices'][0].update(finish_reason='stop'),
            lambda data: data['choices'][0].update(finish_reason='length'),
            lambda data: data['choices'][0].update(message=None),
            lambda data: message(data).update(role='user'),
            lambda data: message(data).pop('tool_calls'),
            lambda data: message(data).update(tool_calls=[]),
            lambda data: message(data)['tool_calls'].append({}),
            lambda data: message(data)['tool_calls'][0].update(type='custom'),
            lambda data: message(data)['tool_calls'][0].pop('function'),
            lambda data: function(data).update(name='other_tool'),
            lambda data: function(data).update(arguments='not-json'),
            lambda data: function(data).update(arguments={'ready': True}),
            lambda data: function(data).update(arguments='[]'),
            lambda data: function(data).update(arguments='{"ready":false}'),
            lambda data: function(data).update(arguments='{"ready":1}'),
        ]
        for index, transform in enumerate(transforms):
            with self.subTest(index=index):
                payload = self.chat_tool_response(); transform(payload)
                result = self.api_only_probe(chat_payload=payload, chat_status=200)
                row = next(row for row in result['checks'] if row['case'] == 'chat_tools_negative')
                self.assertFalse(result['passed'])
                self.assertFalse(row['tool_call_ok'])
                self.assertIsNone(row['chat_tools_outcome'])
        self.assertFalse(self.api_only_probe(chat_payload=native.tool_response(), chat_status=200)['passed'])

    def test_chat_tools_success_requires_valid_identity_usage_and_no_error(self):
        transforms = [lambda data: data.pop('model'), lambda data: data.update(model='gpt-4o-mini'),
                      lambda data: data.update(model=native.MODEL + '-unreviewed'),
                      lambda data: data.update(model={'invalid': True}),
                      lambda data: data.pop('usage'), lambda data: data.update(usage={}),
                      lambda data: data['usage'].update(prompt_tokens=-1),
                      lambda data: data['usage'].update(completion_tokens=True),
                      lambda data: data['usage'].update(total_tokens=999)]
        transforms += [lambda data, error=error: data.update(error=error)
                       for error in ({}, [], '', False, {'code': 'credit_balance_exhausted'})]
        for index, transform in enumerate(transforms):
            with self.subTest(index=index):
                payload = self.chat_tool_response(); transform(payload)
                result = self.api_only_probe(chat_payload=payload, chat_status=200)
                row = next(row for row in result['checks'] if row['case'] == 'chat_tools_negative')
                self.assertFalse(row['passed'])
                self.assertIsNone(row['chat_tools_outcome'])
        for status in (201, 202, 204, 400, 429, 500, 503):
            with self.subTest(status=status):
                self.assertFalse(self.api_only_probe(chat_payload=self.chat_tool_response(), chat_status=status)['passed'])

    def test_chat_tools_known_guidance_is_distinct_and_unknown_errors_fail(self):
        result = self.api_only_probe()
        row = next(row for row in result['checks'] if row['case'] == 'chat_tools_negative')
        self.assertTrue(result['passed'])
        self.assertEqual(row['chat_tools_outcome'], 'guided_rejection_400')
        self.assertIsNone(row['usage'])
        transforms = [lambda data: data.update(error=None),
                      lambda data: data['error'].pop('type'),
                      lambda data: data['error'].update(type='server_error'),
                      lambda data: data['error'].update(code='other_error'),
                      lambda data: data['error'].update(message='Try again later.'),
                      lambda data: data['error'].update(message=['/v1/responses']),
                      lambda data: data['error'].update(message='Use /v1/responses or set reasoning to none.'),
                      lambda data: data['error'].update(param={})]
        for index, transform in enumerate(transforms):
            with self.subTest(index=index):
                payload = self.chat_rejection(); transform(payload)
                result = self.api_only_probe(chat_payload=payload)
                row = next(row for row in result['checks'] if row['case'] == 'chat_tools_negative')
                self.assertFalse(row['passed'])
                self.assertIsNone(row['chat_tools_outcome'])
        for status in (200, 401, 429, 503):
            with self.subTest(status=status):
                self.assertFalse(self.api_only_probe(chat_status=status)['passed'])

    def test_chat_tools_rejection_cannot_contain_even_empty_or_nested_generation_evidence(self):
        for field in ('usage', 'choices', 'output', 'response', 'content', 'data', 'tool_calls', 'function_call'):
            for value in (None, {}, [], ''):
                for location in ('top', 'error', 'nested'):
                    with self.subTest(field=field, value=value, location=location):
                        payload = self.chat_rejection()
                        target = payload if location == 'top' else payload['error']
                        if location == 'nested':
                            payload['error']['extra'] = [{'detail': {field: value}}]
                        else:
                            target[field] = value
                        result = self.api_only_probe(chat_payload=payload)
                        self.assertFalse(result['passed'])

    def test_chat_tools_audit_requires_consistent_settlement_or_zero_cost_release(self):
        settled = {'credit_status': 'settled', 'hold_status': 'settled', 'expected_charge_units': 1,
                   'credit_charged_units': 1, 'actual_units': 1, 'output_bound': 16384}
        released = {'credit_status': 'released', 'hold_status': 'released',
                    'credit_charged_units': 0, 'actual_units': 0}
        cases = [(200, self.chat_tool_response(), settled, [
            {'actual_units': 2}, {'credit_charged_units': 2}, {'expected_charge_units': 2},
            {'actual_units': None}, {'actual_units': True}, {'expected_charge_units': None},
            {'hold_status': 'held'}, {'credit_status': 'reconciliation_required'},
            {'expected_charge_units': -1, 'credit_charged_units': -1, 'actual_units': -1}]),
            (400, self.chat_rejection(), released, [
            {'actual_units': 1}, {'credit_charged_units': 1}, {'actual_units': None},
            {'credit_charged_units': False}, {'hold_status': 'held'}, {'hold_status': None},
            {'credit_status': 'reconciliation_required'}])]
        for status, payload, valid, failures in cases:
            for changes in [None, *failures, {'credit_status': None, 'hold_status': None}]:
                with self.subTest(status=status, changes=changes):
                    def audit(_path, _id, data, _protocol):
                        if 'choices' in data or 'error' in data:
                            return {**valid, **(changes or {})}
                        return settled
                    with patch('scripts.issue8_contract_probe._audit', side_effect=audit):
                        result = self.api_only_probe(chat_payload=payload, chat_status=status,
                                                     ledger_db=Path('issue8-candidate.sqlite'))
                    row = next(row for row in result['checks'] if row['case'] == 'chat_tools_negative')
                    self.assertEqual(result['passed'], changes is None)
                    self.assertEqual(row['passed'], changes is None)
                    self.assertEqual(row['chat_tools_outcome'],
                                     ('supported_200' if status == 200 else 'guided_rejection_400') if changes is None else None)

    def test_nonstring_model_and_output_types_fail_without_echoing_objects(self):
        secret = 'fixture-sensitive-api-key'
        wrong_model = self.api_only_probe(lambda data: data.update(model={secret: 'invalid'}))
        self.assertFalse(wrong_model['passed'])
        self.assertNotIn(secret, json.dumps(wrong_model))
        self.assertTrue(all(row.get('actual_model') is None for row in wrong_model['checks']
                            if row['case'].startswith('responses_tools')))
        wrong_type = self.api_only_probe(lambda data: data['output'].append({'type': {secret: 'invalid'}}))
        self.assertFalse(wrong_type['passed'])
        self.assertNotIn(secret, json.dumps(wrong_type))
        self.assertTrue(all(not row['output_shape_valid'] for row in wrong_type['checks']
                            if row['case'].startswith('responses_tools')))
        self.assertEqual(wrong_type['verified_tool_records'], {})

    def test_final_json_redacts_credential_from_nested_keys_and_values(self):
        secret = 'fixture-sensitive-api-key'
        malicious = {'passed': False, 'checks': [{secret: {secret: secret}, 'ordinary': secret}],
                     'verified_tool_records': {}, secret: 'nested-key'}
        with patch('sys.argv', ['probe', '--base-url', 'http://127.0.0.1:8790']), \
             patch.dict(os.environ, {'LUOMO_API_KEY': secret}), \
             patch('scripts.issue8_contract_probe.run_contract', return_value=malicious):
            output = StringIO()
            with redirect_stdout(output): self.assertEqual(probe_main(), 1)
        self.assertNotIn(secret, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertIn('[redacted]', parsed)
        self.assertEqual(parsed['checks'][0]['[redacted]']['[redacted]'], '[redacted]')

    def test_api_only_wrong_missing_or_unreviewed_model_cannot_verify_tools(self):
        for transform in (lambda data: data.update(model='gpt-4o-mini'), lambda data: data.pop('model'),
                          lambda data: data.update(model='gpt-5.5-2026-04-24')):
            result = self.api_only_probe(transform)
            self.assertFalse(result['passed'])
            self.assertFalse(result['ledger_audited'])
            self.assertEqual(result['verified_tool_records'], {})
            self.assertTrue(all(not row['identity_verified'] and not row['passed']
                                for row in result['checks'] if row['case'].startswith('responses_tools')))
        # The default-budget success cases are subject to the same identity gate.
        result = self.api_only_probe(lambda data: data.update(model='gpt-4o-mini')
            if data['model'] == 'gpt-5.5' else data.update(model='gpt-5.5'), transform_defaults=True)
        self.assertTrue(all(not row['passed'] for row in result['checks'] if row['case'].startswith('mini_default_output')))

    def test_api_only_exact_snapshot_and_error_null_are_valid_identity(self):
        result = self.api_only_probe(lambda data: data.update(model='gpt-5.5-2026-04-23', error=None))
        self.assertTrue(result['passed'])
        self.assertFalse(result['ledger_audited'])
        self.assertEqual(result['verified_tool_records']['gpt-5.5']['tool_protocols'], ['Responses', 'Responses SSE'])
        self.assertTrue(_identity_matches('gpt-5.5', 'gpt-5.5-2026-04-23'))
        with patch.dict(PRICE_DATA, {'version': 'unreviewed-price-version'}), patch('app.price_store.current_release', side_effect=lambda: {**seed_release(), 'text': PRICE_DATA}):
            self.assertFalse(_identity_matches('gpt-5.5', 'gpt-5.5-2026-04-23'))
            self.assertFalse(self.api_only_probe(lambda data: data.update(model='gpt-5.5-2026-04-23'))['passed'])
        with patch.dict(PRICE_DATA['models']['gpt-5.5'], {'input': '999'}), patch('app.price_store.current_release', side_effect=lambda: {**seed_release(), 'text': PRICE_DATA}):
            self.assertFalse(_identity_matches('gpt-5.5', 'gpt-5.5-2026-04-23'))
        self.assertFalse(_identity_matches('gpt-4o', 'gpt-4o-2024-05-13'))

    def test_nonnull_error_and_success_shaped_environment_blocker_never_pass(self):
        errors = ({}, '', 'provider error', [], False,
                  {'type': 'insufficient_quota', 'code': 'credit_balance_exhausted'})
        for error in errors:
            with self.subTest(error=error):
                result = self.api_only_probe(lambda data: data.update(error=error))
                self.assertFalse(result['passed'])
                self.assertEqual(result['verified_tool_records'], {})
                self.assertTrue(all(row['error_present'] and not row['passed']
                                    for row in result['checks'] if row['case'].startswith('responses_tools')))
        result = self.api_only_probe(lambda data: data.update(error={'code': 'credit_balance_exhausted'}))
        self.assertEqual(result['environment_blocked'], ['credit_balance_exhausted'])

    def test_sse_failure_cannot_be_overwritten_by_a_later_completed_event(self):
        for failed in ({'type': 'response.failed', 'response': {'status': 'failed'}},
                       {'type': 'response.incomplete', 'response': {'status': 'incomplete'}},
                       {'type': 'error', 'error': {'code': 'provider_failed'}},
                       {'type': 'response.created', 'response': {'error': {}}}):
            with self.subTest(failed=failed):
                result = self.api_only_probe(prefix=fixture.sse_event(failed))
                self.assertFalse(result['passed'])
                self.assertEqual(result['verified_tool_records'], {})
                row = next(row for row in result['checks'] if row['case'] == 'responses_tools_sse')
                self.assertTrue(row['stream_failed'])
                self.assertFalse(row['passed'])
        result = self.api_only_probe(suffix=fixture.sse_event({'type': 'error', 'error': 'failed'}))
        self.assertFalse(result['passed'])
        self.assertEqual(result['verified_tool_records'], {})
        named_error = b'event: error\ndata: {"message":"failed"}\n\n'
        self.assertFalse(self.api_only_probe(prefix=named_error)['passed'])

    def test_script_optional_ledger_audit_is_read_only_and_checks_exact_charge(self):
        import hashlib
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'issue8-candidate.sqlite'
            quote = prepare_billable_request({'model': native.MODEL, 'input': 'ready', 'max_output_tokens': 128}, protocol='responses')[1]
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE api_usage_logs (id,request_id,credit_status,credit_charged_units,credit_pricing_snapshot)')
                conn.execute('CREATE TABLE credit_holds (usage_id,status,actual_units,amount_units)')
                conn.execute('INSERT INTO api_usage_logs VALUES (1,?,?,?,?)', ('fixture-id', 'settled', native.CHARGE, quote.snapshot))
                conn.execute('INSERT INTO credit_holds VALUES (1,?,?,?)', ('settled', native.CHARGE, quote.amount_units))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            audit = _audit(path, 'fixture-id', native.tool_response(), 'responses')
            self.assertEqual(audit['expected_charge_units'], native.CHARGE)
            self.assertEqual(audit['output_bound'], 128)
            self.assertEqual(audit['credit_status'], 'settled')
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            with self.assertRaises(ValueError): _audit(Path(directory) / 'production.sqlite', 'x', {}, 'responses')

    def test_probe_covers_issue_models_and_both_default_limit_protocols(self):
        calls = []
        def handler(request):
            body = json.loads(request.content); calls.append((request.url.path, body))
            if request.url.path.endswith('chat/completions') and body['model'] == native.MODEL:
                return httpx.Response(400, json=self.chat_rejection())
            data = native.tool_response(); data['model'] = body['model']
            if body.get('stream'):
                return httpx.Response(200, content=fixture.sse_event({'type': 'response.completed', 'response': data}))
            return httpx.Response(200, json=data)
        def audit(_path, _id, payload, _protocol):
            if payload.get('error'):
                return {'credit_status': 'released', 'hold_status': 'released', 'credit_charged_units': 0, 'actual_units': 0}
            return {'credit_status': 'settled', 'hold_status': 'settled', 'expected_charge_units': 1,
                    'credit_charged_units': 1, 'actual_units': 1, 'output_bound': 16384}
        models = ['gpt-4.1', 'gpt-4o-mini', 'gpt-5.2', 'gpt-5.5', 'gpt-6-luna', 'gpt-6-astra']
        with httpx.Client(base_url='http://127.0.0.1', transport=httpx.MockTransport(handler)) as client, \
             patch('scripts.issue8_contract_probe._audit', side_effect=audit):
            result = run_contract(client, models=models, chat_model=native.MODEL, ledger_db=Path('issue8-candidate.sqlite'))
        self.assertTrue(result['passed'])
        self.assertEqual(len(result['checks']), 15)
        chat_tools = [(path, body) for path, body in calls if path.endswith('chat/completions') and body.get('tools')]
        self.assertEqual(len(chat_tools), 1)
        self.assertFalse(chat_tools[0][1].get('stream', False))
        defaults = [row for row in result['checks'] if row['case'].startswith('mini_default_output')]
        self.assertEqual({row['protocol'] for row in defaults}, {'chat.completions', 'responses'})
        self.assertTrue(all(row['ledger']['output_bound'] <= 16384 and row['max_output_tokens_sent'] is None for row in defaults))
        with httpx.Client(base_url='http://127.0.0.1', transport=httpx.MockTransport(handler)) as client, \
             patch('scripts.issue8_contract_probe._audit', side_effect=lambda *args: {**audit(*args), 'output_bound': 32768}):
            exceeded = run_contract(client, models=['gpt-5.5'], chat_model=native.MODEL, ledger_db=Path('issue8-candidate.sqlite'))
        self.assertFalse(exceeded['passed'])
        self.assertTrue(all(not row['passed'] for row in exceeded['checks'] if row['case'].startswith('mini_default_output')))
        for path, body in calls:
            if path.endswith('responses') and body.get('tools'):
                if body['model'] in {'gpt-4.1', 'gpt-4o-mini'}:
                    self.assertNotIn('reasoning', body)
                else:
                    self.assertEqual(body['reasoning'], {'effort': 'low'})
            if body['model'] == 'gpt-4o-mini' and not body.get('tools'):
                self.assertFalse({'max_tokens', 'max_completion_tokens', 'max_output_tokens'} & body.keys())

    def test_probe_environment_blockers_fail_exit_and_never_verify_tools(self):
        real_client = httpx.Client
        cases = [(429, {'type': 'insufficient_quota', 'code': 'credit_balance_exhausted'}, 'credit_balance_exhausted'),
                 (429, {'type': 'insufficient_quota'}, 'insufficient_quota'),
                 (503, {'type': 'server_error', 'code': 'internal_server_error', 'message': 'auth_unavailable: fixture'}, 'auth_unavailable')]
        for status, error, expected in cases:
            def handler(request):
                return httpx.Response(status, json={'error': error})
            with self.subTest(expected=expected), \
                 patch('sys.argv', ['probe', '--base-url', 'http://127.0.0.1:8790', '--models', 'gpt-4.1']), \
                 patch.dict(os.environ, {'LUOMO_API_KEY': 'fixture-do-not-print'}), \
                 patch('scripts.issue8_contract_probe.httpx.Client', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
                output = StringIO()
                with redirect_stdout(output): self.assertEqual(probe_main(), 1)
            result = json.loads(output.getvalue())
            self.assertFalse(result['passed'])
            self.assertEqual(result['verified_tool_records'], {})
            self.assertEqual(result['environment_blocked'], [expected])
            self.assertTrue(all(row['environment_blocked'] == expected and not row['passed'] for row in result['checks']))
            self.assertNotIn('fixture-do-not-print', output.getvalue())

    def test_replay_script_contract_and_json_output_never_prints_key(self):
        secret = 'fixture-never-print-this-api-key'
        calls = []
        def handler(request):
            payload = json.loads(request.content); calls.append(payload)
            if request.url.path.endswith('chat/completions') and payload['model'] == 'gpt-6-astra':
                return httpx.Response(400, json=self.chat_rejection())
            data = native.tool_response(); data['model'] = payload['model']
            if payload.get('stream'):
                return httpx.Response(200, content=fixture.sse_event({'type': 'response.completed', 'response': data}))
            return httpx.Response(200, json=data)
        real = httpx.Client
        with patch('sys.argv', ['probe', '--base-url', 'http://127.0.0.1:8790']), \
             patch.dict(os.environ, {'LUOMO_API_KEY': secret}), \
             patch('scripts.issue8_contract_probe.httpx.Client', side_effect=lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            out = StringIO()
            with redirect_stdout(out): self.assertEqual(probe_main(), 0)
        result = json.loads(out.getvalue())
        self.assertNotIn(secret, out.getvalue())
        self.assertTrue(result['passed'])
        self.assertEqual(len(result['checks']), 7)
        self.assertEqual(result['verified_tool_records']['gpt-5.5']['tool_protocols'], ['Responses', 'Responses SSE'])
        self.assertIn('max_output_tokens', calls[0]); self.assertNotIn('max_tokens', calls[0])
        self.assertNotIn('max_output_tokens', calls[-1]); self.assertNotIn('max_tokens', calls[-1])


if __name__ == '__main__': unittest.main()

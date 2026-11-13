"""Version/card-bound exact current snapshots; no model name rewriting."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from contextlib import closing
from decimal import Decimal, ROUND_CEILING
from unittest.mock import patch

import test_credit_gateway as fixture
from app.credit_pricing import (MODEL_ALIAS_DATA, PRICE_DATA, PricingError, billing_details,
                                calculate_charge, prepare_billable_request, priced_models)
from app.image_generation import prepare_image_request

EXPECTED_PAIRS = {
    'gpt-4.1': 'gpt-4.1-2025-04-14', 'gpt-4.1-mini': 'gpt-4.1-mini-2025-04-14',
    'gpt-4o': 'gpt-4o-2024-08-06', 'gpt-4o-mini': 'gpt-4o-mini-2024-07-18',
    'gpt-5': 'gpt-5-2025-08-07', 'gpt-5-mini': 'gpt-5-mini-2025-08-07', 'gpt-5-nano': 'gpt-5-nano-2025-08-07',
    'gpt-5.1': 'gpt-5.1-2025-11-13', 'gpt-5.2': 'gpt-5.2-2025-12-11',
    'gpt-5.4': 'gpt-5.4-2026-03-05', 'gpt-5.4-mini': 'gpt-5.4-mini-2026-03-17',
    'gpt-5.4-nano': 'gpt-5.4-nano-2026-03-17', 'gpt-5.5': 'gpt-5.5-2026-04-23', 'o3': 'o3-2025-04-16',
}


def quote(model='gpt-4o-mini'):
    return prepare_billable_request({'model': model, 'messages': [{'role': 'user', 'content': 'hello'}],
                                     'max_completion_tokens': 8}, protocol='chat.completions')[1]


def response(model='gpt-4o-mini-2024-07-18'):
    return {'id': 'chatcmpl_snapshot_fixture', 'object': 'chat.completion', 'model': model,
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'fixture'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 11, 'completion_tokens': 2, 'total_tokens': 13,
                  'prompt_tokens_details': {'cached_tokens': 0}}}


class SnapshotAliasPricingTests(unittest.TestCase):
    def details(self, model):
        return billing_details(response(model), protocol='chat.completions')

    def test_all_fourteen_official_pairs_use_exact_rates_and_ceiling_rounding(self):
        self.assertEqual({key: value['snapshot'] for key, value in MODEL_ALIAS_DATA['models'].items()}, EXPECTED_PAIRS)
        for alias, snapshot in EXPECTED_PAIRS.items():
            with self.subTest(alias=alias):
                quoted = quote(alias)
                details = self.details(snapshot)
                before = copy.deepcopy(details)
                card = PRICE_DATA['models'][alias]
                expected = int(((11 * Decimal(card['input']) + 2 * Decimal(card['output'])) * 720)
                               .to_integral_value(rounding=ROUND_CEILING))
                self.assertEqual(calculate_charge(quoted.snapshot, details), expected)
                self.assertEqual(details, before)
                self.assertEqual(json.loads(quoted.snapshot)['model'], alias)
                self.assertNotIn(snapshot, priced_models())
        # 1*.15 + 1*.60, times720 =540; 1*.05 + 1*.40,
        # times720 =324. A cache token makes a fractional microcredit sum.
        details = self.details(EXPECTED_PAIRS['gpt-4o-mini'])
        details.update(input_tokens=1, output_tokens=0, total_tokens=1, cached_tokens=1)
        self.assertEqual(calculate_charge(quote().snapshot, details), 54)
        details = self.details(EXPECTED_PAIRS['gpt-5-nano'])
        details.update(input_tokens=1, output_tokens=0, total_tokens=1, cached_tokens=1)
        self.assertEqual(calculate_charge(quote('gpt-5-nano').snapshot, details), 4)  # .005 *720 =3.6 ->4

    def test_metadata_records_sources_version_and_full_price_card_hash(self):
        self.assertEqual(MODEL_ALIAS_DATA['price_version'], PRICE_DATA['version'])
        self.assertEqual(MODEL_ALIAS_DATA['pricing_source'], PRICE_DATA['source'])
        self.assertEqual(MODEL_ALIAS_DATA['pricing_retrieved_at'], PRICE_DATA['retrieved_at'])
        self.assertEqual(MODEL_ALIAS_DATA['snapshot_retrieved_at'], '2026-11-13')
        for alias, entry in MODEL_ALIAS_DATA['models'].items():
            card = PRICE_DATA['models'][alias]
            canonical = json.dumps(card, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
            self.assertEqual(entry['price_card_sha256'], hashlib.sha256(canonical).hexdigest())
            self.assertEqual(entry['snapshot_source'], 'https://developers.openai.com/api/docs/models/' + alias)

    def test_unknown_cross_family_expensive_snapshot_and_reverse_mapping_rejected(self):
        cases = [('gpt-4o-mini', 'gpt-4o-mini-2026-11-13'),
                 ('gpt-4o-mini', 'gpt-4o-2024-08-06'),
                 ('gpt-4o', 'gpt-4o-2024-05-13'),
                 ('gpt-5.5', 'gpt-5.5-2026-04-24'),
                 ('gpt-5', 'gpt-5-mini-2025-08-07'),
                 ('gpt-6-luna', 'gpt-6-luna-2026-11-13')]
        for alias, actual in cases:
            with self.subTest(alias=alias, actual=actual), self.assertRaises(PricingError) as error:
                calculate_charge(quote(alias).snapshot, self.details(actual))
            self.assertEqual(error.exception.code, 'usage_model_mismatch')
        snap = json.loads(quote().snapshot); snap['model'] = EXPECTED_PAIRS['gpt-4o-mini']
        with self.assertRaises(PricingError) as error:
            calculate_charge(json.dumps(snap), self.details('gpt-4o-mini'))
        self.assertEqual(error.exception.code, 'usage_model_mismatch')

    def test_version_and_every_card_field_pin_the_receipt_equivalence(self):
        original = json.loads(quote().snapshot)
        for version in ('future-price-version', '', None):
            snap = copy.deepcopy(original); snap['version'] = version
            with self.subTest(version=version), self.assertRaises(PricingError) as error:
                calculate_charge(json.dumps(snap), self.details(EXPECTED_PAIRS['gpt-4o-mini']))
            self.assertEqual(error.exception.code, 'usage_model_mismatch')
        snap = copy.deepcopy(original); snap.pop('version')
        with self.assertRaises(PricingError): calculate_charge(json.dumps(snap), self.details(EXPECTED_PAIRS['gpt-4o-mini']))
        for field in original['card']:
            snap = copy.deepcopy(original)
            snap['card'][field] = '999' if isinstance(snap['card'][field], str) else 999
            with self.subTest(field=field), self.assertRaises(PricingError) as error:
                calculate_charge(json.dumps(snap), self.details(EXPECTED_PAIRS['gpt-4o-mini']))
            self.assertEqual(error.exception.code, 'usage_model_mismatch')
        snap = copy.deepcopy(original); snap['card']['new_unreviewed_rate'] = '0'
        with self.assertRaises(PricingError): calculate_charge(json.dumps(snap), self.details(EXPECTED_PAIRS['gpt-4o-mini']))
        snap = copy.deepcopy(original); snap['card'].pop('long_output')
        with self.assertRaises(PricingError): calculate_charge(json.dumps(snap), self.details(EXPECTED_PAIRS['gpt-4o-mini']))
        reordered = copy.deepcopy(original); reordered['card'] = dict(reversed(list(original['card'].items())))
        self.assertEqual(calculate_charge(json.dumps(reordered), self.details(EXPECTED_PAIRS['gpt-4o-mini'])), 2052)

    def test_quoted_card_is_immutable_but_new_unreviewed_live_card_fails(self):
        old_quote = quote()
        with patch.dict(PRICE_DATA['models']['gpt-4o-mini'], {'input': '999'}):
            self.assertEqual(calculate_charge(old_quote.snapshot, self.details(EXPECTED_PAIRS['gpt-4o-mini'])), 2052)
            with self.assertRaises(PricingError):
                calculate_charge(quote().snapshot, self.details(EXPECTED_PAIRS['gpt-4o-mini']))

    def test_none_and_same_name_keep_existing_unrestricted_version_behavior(self):
        snap = json.loads(quote().snapshot); snap['version'] = 'legacy-unlisted-version'
        for model in (None, 'gpt-4o-mini'):
            self.assertEqual(calculate_charge(json.dumps(snap), self.details(model)), 2052)

    def test_image_kind_does_not_consult_or_accept_text_snapshot_aliases(self):
        import test_image_generation as images
        for model in ('gpt-image-2', 'gpt-image-1.5'):
            image_quote = prepare_image_request({'model': model, 'prompt': 'fixture', 'quality': 'low'})[1]
            details = billing_details(images.receipt(model), protocol='images.generations')
            expected = 4_320_000 if model == 'gpt-image-2' else 7_246_080
            with patch('app.credit_pricing._price_equivalent_snapshot', side_effect=AssertionError('Text alias consulted for image')):
                self.assertEqual(calculate_charge(image_quote.snapshot, details), expected)
                details['model'] = model + '-2026-11-13'
                with self.assertRaises(PricingError): calculate_charge(image_quote.snapshot, details)


class SnapshotAliasGatewayTests(unittest.TestCase):
    for name in ('setUp', 'cleanup', 'open_database', 'register_and_login', 'issue_key', 'fund', 'upstream',
                 'accounting', 'assert_pending'):
        locals()[name] = getattr(fixture.CreditGatewayTests, name)

    def generate(self):
        return self.client.post('/v1/chat/completions', json={'model': 'gpt-4o-mini',
            'messages': [{'role': 'user', 'content': 'fixture'}], 'max_completion_tokens': 8},
            headers={'Authorization': 'Bearer ' + self.key})

    def test_real_shape_dated_snapshot_settles_without_rewriting_body_grants_or_receipt(self):
        self.fund()
        self.response_json = response()
        result = self.generate()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), response())
        self.assertEqual(self.calls[-1]['body']['model'], 'gpt-4o-mini')
        usage, hold, wallet, ledger = self.accounting()
        self.assertEqual((hold['status'], hold['actual_units'], usage['model']), ('settled', 2052, 'gpt-4o-mini'))
        self.assertEqual(json.loads(hold['usage_json'])['model'], 'gpt-4o-mini-2024-07-18')
        self.assertEqual(json.loads(hold['pricing_snapshot'])['model'], 'gpt-4o-mini')
        self.assertEqual(wallet['reserved_units'], 0)
        self.assertEqual(wallet['balance_units'], fixture.FUND_UNITS - 2052)
        with closing(self.open_database()) as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM api_key_models WHERE api_key_id=? AND model_id=?',
                (self.key_id, 'gpt-4o-mini-2024-07-18')).fetchone())
        self.assertEqual(sum(row['kind'] == 'usage_charge' for row in ledger), 1)

    def test_streamed_dated_snapshot_retains_actual_model_and_settles_once(self):
        self.fund()
        self.response_sse = (fixture.sse_event({'model': 'gpt-4o-mini-2024-07-18',
            'choices': [{'index': 0, 'delta': {'content': 'fixture'}, 'finish_reason': None}]})
            + fixture.sse_event({'model': 'gpt-4o-mini-2024-07-18', 'choices': [{'finish_reason': 'stop'}]})
            + fixture.sse_event({'model': 'gpt-4o-mini-2024-07-18', 'choices': [], 'usage': response()['usage']})
            + fixture.sse_event('[DONE]'))
        result = self.client.post('/v1/chat/completions', json={'model': 'gpt-4o-mini',
            'messages': [{'role': 'user', 'content': 'fixture'}], 'max_completion_tokens': 8, 'stream': True},
            headers={'Authorization': 'Bearer ' + self.key})
        self.assertEqual((result.status_code, result.content), (200, self.response_sse))
        usage, hold, wallet, ledger = self.accounting()
        self.assertEqual((hold['status'], hold['actual_units']), ('settled', 2052))
        self.assertEqual(json.loads(hold['usage_json'])['model'], 'gpt-4o-mini-2024-07-18')
        self.assertEqual(wallet['reserved_units'], 0)
        self.assertEqual(sum(row['kind'] == 'usage_charge' for row in ledger), 1)

    def test_actual_model_switch_is_still_reconciliation_required(self):
        self.fund()
        self.response_json = response('gpt-4o-2024-08-06')
        self.assertEqual(self.generate().status_code, 200)
        self.assert_pending()
        self.assertIn('differs from the priced request model', self.accounting()[1]['reason'])


if __name__ == '__main__': unittest.main()

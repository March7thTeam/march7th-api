"""Bounded real gateway contract probe; never print API keys or provider bodies.

Example (key supplied in the environment, never on argv):
  python -m scripts.issue8_contract_probe --base-url http://127.0.0.1:8790 \
      --ledger-db /app/data/issue8-candidate.sqlite --output /tmp/issue8-results.json

The optional SQLite audit is read-only and restricted to a named isolated DB.
This script sends billable requests; the executing operator chooses the target/key.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import quote, urlsplit

import httpx

from app.credit_pricing import (PRICE_DATA, PricingError, _price_equivalent_snapshot,
                                billing_details, calculate_charge, model_price)
from app.text_capabilities import NATIVE_RESPONSES_MODELS

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
NON_REASONING_MODELS = frozenset({"gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"})


def _response_payload(raw: bytes, streamed: bool) -> tuple[dict, bool]:
    if not streamed:
        payload = json.loads(raw)
        return (payload if isinstance(payload, dict) else {}), False
    final, failed = {}, False
    failures = {'error', 'response.error', 'response.failed', 'response.incomplete'}
    bad_statuses = {'failed', 'incomplete', 'error', 'cancelled', 'canceled'}
    for frame in raw.decode('utf-8').replace('\r\n', '\n').split('\n\n'):
        lines = frame.splitlines()
        if any(line[6:].strip() in failures for line in lines if line.startswith('event:')):
            failed = True
        text = '\n'.join(line[5:].lstrip() for line in lines if line.startswith('data:'))
        if not text or text == '[DONE]':
            continue
        event = json.loads(text)
        if not isinstance(event, dict):
            failed = True
            continue
        response = event.get('response') if isinstance(event.get('response'), dict) else {}
        if (event.get('type') in failures or event.get('status') in bad_statuses
                or response.get('status') in bad_statuses
                or event.get('error') is not None or response.get('error') is not None):
            failed = True
        if event.get('type') in {'response.completed', 'response.failed', 'response.incomplete'}:
            final = response
    return final, failed


def _identity_matches(requested: str, actual: object) -> bool:
    """Protocol identity only; financial acceptance still needs the ledger audit."""
    if not isinstance(actual, str) or not actual:
        return False
    if actual == requested:
        return True
    try:
        snapshot = {'model': requested, 'version': PRICE_DATA['version'], 'card': model_price(requested)}
        return _price_equivalent_snapshot(snapshot, actual)
    except (KeyError, TypeError, ValueError, PricingError):
        return False


def _audit(path: Path | None, request_id: str, payload: dict, protocol: str) -> dict | None:
    if path is None:
        return None
    # Refuse the production filename and prevent the read connection creating DBs.
    if path.name != 'issue8-candidate.sqlite' or not path.is_file():
        raise ValueError('Use an existing isolated issue8-candidate.sqlite for ledger auditing.')
    with closing(sqlite3.connect('file:' + quote(str(path.resolve())) + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('''SELECT a.credit_status,a.credit_charged_units,a.credit_pricing_snapshot,
            h.status AS hold_status,h.actual_units,h.amount_units FROM api_usage_logs a
            LEFT JOIN credit_holds h ON h.usage_id=a.id WHERE a.request_id=? ORDER BY a.id DESC LIMIT 1''',
            (request_id,)).fetchone()
    if row is None:
        return {'found': False}
    result = {key: row[key] for key in ('credit_status', 'credit_charged_units', 'hold_status', 'actual_units', 'amount_units')}
    result['found'] = True
    snapshot = row['credit_pricing_snapshot']
    if snapshot:
        result['output_bound'] = json.loads(snapshot).get('output_bound')
        details = billing_details(payload, protocol=protocol)
        if details is not None:
            result['expected_charge_units'] = calculate_charge(snapshot, details)
    return result


def run_contract(client: httpx.Client, *, models: list[str], chat_model: str,
                 max_output_tokens: int = 1024, ledger_db: Path | None = None) -> dict:
    """Execute a bounded matrix and return only a credential-free summary."""
    rows = []
    started_at = datetime.now(timezone.utc).isoformat()

    def call(case: str, model: str, protocol: str, body: dict, streamed: bool = False):
        request_id = 'issue8-' + secrets.token_hex(12)
        row = {'case': case, 'model': model, 'protocol': protocol, 'stream': streamed, 'request_id': request_id,
               'request_fields': sorted(body), 'max_output_tokens_sent': body.get('max_output_tokens'),
               'environment_blocked': None}
        started = time.monotonic()
        try:
            path = '/v1/responses' if protocol == 'responses' else '/v1/chat/completions'
            with client.stream('POST', path, json=body, headers={'X-Request-ID': request_id,
                    'Accept': 'text/event-stream' if streamed else 'application/json'}) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise ValueError('response_too_large')
                row['status'] = response.status_code
                row['request_id'] = response.headers.get('x-request-id', request_id)
            payload, stream_failed = _response_payload(bytes(data), streamed and row['status'] == 200)
            row['stream_failed'] = stream_failed
            row['error_present'] = payload.get('error') is not None
            row['identity_verified'] = _identity_matches(model, payload.get('model'))
            error = payload.get('error') if isinstance(payload.get('error'), dict) else {}
            message = str(error.get('message', ''))
            code = error.get('code')
            if code in {'credit_balance_exhausted', 'insufficient_quota', 'auth_unavailable'}:
                row['environment_blocked'] = code
            elif error.get('type') == 'insufficient_quota':
                row['environment_blocked'] = 'insufficient_quota'
            elif row['status'] in {401, 503} and message.startswith('auth_unavailable:'):
                row['environment_blocked'] = 'auth_unavailable'
            outputs = payload.get('output') if isinstance(payload.get('output'), list) else []
            row['output_shape_valid'] = (('output' not in payload or isinstance(payload['output'], list))
                and all(isinstance(item, dict) and isinstance(item.get('type'), str) for item in outputs))
            row.update(actual_model=payload.get('model') if isinstance(payload.get('model'), str) else None,
                       output_types=[item['type'] for item in outputs if isinstance(item, dict) and isinstance(item.get('type'), str)],
                       error_code=error.get('code'), recommends_none='none' in message.lower(),
                       recommends_responses='/v1/responses' in message)
            details = billing_details(payload, protocol=protocol)
            row['usage'] = ({key: details[key] for key in ('input_tokens', 'output_tokens', 'total_tokens')} if details else None)
            tool_ok = False
            for item in outputs:
                if isinstance(item, dict) and item.get('type') == 'function_call' and item.get('name') == 'report_ready':
                    try:
                        tool_ok |= json.loads(item.get('arguments', '{}')).get('ready') is True
                    except (ValueError, TypeError, AttributeError):
                        pass
            row['tool_call_ok'] = tool_ok
            audit = _audit(ledger_db, row['request_id'], payload, protocol)
            row['ledger'] = audit
            if case == 'chat_tools_negative':
                passed = row['status'] == 400 and row['error_code'] == 'chat_tools_require_responses' and row['recommends_responses'] and not row['recommends_none']
                if audit is not None:
                    passed &= audit.get('credit_status') == 'released' and audit.get('actual_units') == 0
            else:
                passed = (row['status'] == 200 and details is not None and row['identity_verified']
                          and not row['error_present'] and not row['environment_blocked'] and not stream_failed
                          and row['output_shape_valid'])
                if case.startswith('responses_tools'):
                    passed &= tool_ok and payload.get('status') == 'completed'
                if audit is not None:
                    passed &= audit.get('credit_status') == 'settled' and audit.get('hold_status') == 'settled'
                    passed &= audit.get('expected_charge_units') == audit.get('credit_charged_units')
                    if case.startswith('mini_default_output'):
                        passed &= 0 < (audit.get('output_bound') or 0) <= 16384
            row['passed'] = bool(passed)
        except Exception as exc:
            # Exception text or a provider response may contain credentials.
            row.update(passed=False, failure_type=type(exc).__name__)
        row['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        rows.append(row)

    schema = {'type': 'object', 'properties': {'ready': {'type': 'boolean'}}, 'required': ['ready'], 'additionalProperties': False}
    for model in models:
        for streamed in (False, True):
            body = {'model': model, 'input': 'Call report_ready with ready=true.',
                    'tools': [{'type': 'function', 'name': 'report_ready', 'strict': True, 'parameters': schema}],
                    'tool_choice': {'type': 'function', 'name': 'report_ready'},
                    'max_output_tokens': max_output_tokens, 'stream': streamed}
            if model not in NON_REASONING_MODELS:
                body['reasoning'] = {'effort': 'low'}
            call('responses_tools_sse' if streamed else 'responses_tools_json', model, 'responses', body, streamed)
    call('chat_tools_negative', chat_model, 'chat.completions', {
        'model': chat_model, 'messages': [{'role': 'user', 'content': 'Call report_ready with ready=true.'}],
        'reasoning_effort': 'low', 'tools': [{'type': 'function', 'function': {'name': 'report_ready', 'parameters': schema}}],
        'max_completion_tokens': max_output_tokens})
    call('mini_default_output_chat', 'gpt-4o-mini', 'chat.completions', {
        'model': 'gpt-4o-mini', 'messages': [{'role': 'user', 'content': 'Reply only OK.'}]})
    call('mini_default_output_responses', 'gpt-4o-mini', 'responses', {
        'model': 'gpt-4o-mini', 'input': 'Reply only OK.'})
    checked_at = datetime.now(timezone.utc).isoformat()
    verified = {}
    for model in dict.fromkeys(models):
        tool_rows = [row for row in rows if row['model'] == model and row['case'].startswith('responses_tools')]
        if ({row['case'] for row in tool_rows} == {'responses_tools_json', 'responses_tools_sse'}
                and all(row['passed'] and not row['environment_blocked'] for row in tool_rows)):
            verified[model] = {'tool_protocols': ['Responses', 'Responses SSE'], 'tools_checked_at': checked_at}
    return {'schema_version': 1, 'started_at': started_at, 'finished_at': checked_at,
            'passed': all(row['passed'] for row in rows), 'ledger_audited': ledger_db is not None,
            'checks': rows, 'verified_tool_records': verified,
            'environment_blocked': sorted({row['environment_blocked'] for row in rows if row['environment_blocked']})}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--api-key-env', default='LUOMO_API_KEY')
    parser.add_argument('--models', nargs='+', choices=sorted(NATIVE_RESPONSES_MODELS), default=['gpt-5.2', 'gpt-5.5'])
    parser.add_argument('--chat-model', choices=sorted(NATIVE_RESPONSES_MODELS), default='gpt-6-astra')
    parser.add_argument('--max-output-tokens', type=int, default=1024)
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--ledger-db', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    target = urlsplit(args.base_url)
    if target.scheme not in {'http', 'https'} or not target.hostname or target.username or target.password or target.query or target.fragment:
        parser.error('Use an HTTP(S) gateway base URL without credentials/query/fragment.')
    if target.scheme == 'http' and target.hostname not in {'localhost', '127.0.0.1', '::1'}:
        parser.error('Plain HTTP is allowed only for loopback candidate services.')
    if not re.fullmatch('[A-Z_][A-Z0-9_]*', args.api_key_env):
        parser.error('Invalid API key environment variable name.')
    secret = os.getenv(args.api_key_env, '')
    if not secret:
        parser.error('API key environment variable is empty.')
    if not 1 <= args.max_output_tokens <= 16384 or not 0 < args.timeout <= 600:
        parser.error('Use output limit1..16384 and timeout1..600 seconds.')
    if args.ledger_db and (args.ledger_db.name != 'issue8-candidate.sqlite' or not args.ledger_db.is_file()):
        parser.error('Ledger auditing requires an existing isolated issue8-candidate.sqlite.')
    with httpx.Client(base_url=args.base_url.rstrip('/'), headers={'Authorization': 'Bearer ' + secret},
                      timeout=args.timeout, follow_redirects=False, trust_env=False) as client:
        report = run_contract(client, models=args.models, chat_model=args.chat_model,
                              max_output_tokens=args.max_output_tokens, ledger_db=args.ledger_db)
    # Defense in depth: no upstream-controlled string may expose the key.
    def redact(value):
        if isinstance(value, str):
            return value.replace(secret, '[redacted]')
        if isinstance(value, dict):
            return {redact(key): redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value
    encoded = json.dumps(redact(report), ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded, end='')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

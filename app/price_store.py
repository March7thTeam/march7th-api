"""Immutable operator price releases. JSON files are seeds, never a live rate source.

The active pointer and activation history change together under BEGIN IMMEDIATE.
Quotes copy the entire card and conversion into the hold; settlement never
consults today's release. A local CLI is the trusted operator interface.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from decimal import Decimal, localcontext
from functools import wraps
from pathlib import Path
from app.utils.timefmt import now_iso

def exact_context(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = 80
            return function(*args, **kwargs)
    return wrapped


CREDITS_PER_CNY = 100  # Stable wallet/payment denomination, including historical orders.


def exact(raw, name, *, zero=False) -> str:
    if not isinstance(raw, (str, int)) or isinstance(raw, bool) or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,8})?", str(raw)):
        raise ValueError(f"{name} must be an exact decimal string with at most 8 decimal places.")
    if len(str(raw)) > 24:
        raise ValueError(f"{name} is too long.")
    value = Decimal(str(raw))
    if not 0 <= value <= 1_000_000 or (not zero and value == 0):
        raise ValueError(f"{name} is outside its supported range.")
    return format(value, 'f')


def validate_release(data: dict) -> dict:
    from app.config import public_url
    if not isinstance(data, dict) or set(data) != {'version', 'text', 'image', 'usd_to_cny', 'billing_multiplier'}:
        raise ValueError('Release requires version, text, image, usd_to_cny and billing_multiplier.')
    if not isinstance(data['version'], str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', data['version']):
        raise ValueError('Invalid release version.')
    result = json.loads(json.dumps(data, allow_nan=False))
    result['usd_to_cny'] = exact(data['usd_to_cny'], 'usd_to_cny')
    result['billing_multiplier'] = exact(data['billing_multiplier'], 'billing_multiplier')
    for kind, fields in [('text', {'input','cached_input','cache_write','output','long_input','long_cached_input','long_cache_write','long_output','long_context_threshold'}), ('image', {'text_input','image_input','image_output','text_output'})]:
        section = result[kind]
        if not isinstance(section, dict) or not isinstance(section.get('version'), str) or not section['version'] or not isinstance(section.get('models'), dict) or not section['models']:
            raise ValueError(f'{kind}: missing version or models.')
        public_url(section.get('source', '').split('#', 1)[0], f'{kind}.source')
        if len(section['models']) > 1000:
            raise ValueError('Too many models.')
        for model, card in section['models'].items():
            if not re.fullmatch(r'[A-Za-z0-9._:/-]{1,120}', model) or not isinstance(card, dict) or set(card) != fields:
                raise ValueError(f'{kind}: invalid model/card {model}.')
            for field, value in card.items():
                if field == 'long_context_threshold':
                    if value is not None and (type(value) is not int or not 1 <= value <= 10_000_000):
                        raise ValueError('Invalid long context threshold.')
                elif value is not None:
                    card[field] = exact(value, f'{model}.{field}', zero=True)
            required = ['input','output'] if kind == 'text' else ['text_input','image_input','image_output']
            if any(card[f] is None for f in required):
                raise ValueError('Required rates cannot be null.')
            if kind == 'text' and card['long_context_threshold'] and any(card[f] is None for f in ['long_input','long_output']):
                raise ValueError('Long context rates are required when a threshold is set.')
    return result


def seed_release() -> dict:
    here = Path(__file__).parent
    return validate_release({'version': 'seed-20261114',
        'text': json.loads((here/'credit_prices.json').read_text()),
        'image': json.loads((here/'image_generation_prices.json').read_text()),
        'usd_to_cny': os.getenv('USD_TO_CNY', '7.2'),
        'billing_multiplier': os.getenv('BILLING_MULTIPLIER', '1')})


def install_schema(conn):
    conn.execute('''CREATE TABLE price_versions (
        version TEXT PRIMARY KEY, payload TEXT NOT NULL, sha256 TEXT NOT NULL,
        imported_at TEXT NOT NULL, imported_by TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE price_current (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        version TEXT NOT NULL REFERENCES price_versions(version))''')
    conn.execute('''CREATE TABLE price_activations (id INTEGER PRIMARY KEY, version TEXT NOT NULL REFERENCES price_versions(version),
        activated_at TEXT NOT NULL, activated_by TEXT NOT NULL)''')
    for table in ('price_versions','price_activations'):
        for operation in ('UPDATE','DELETE'):
            conn.execute(f"CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable pricing history'); END")
    release = seed_release()
    import_release(conn, release, actor='schema seed')
    activate_release(conn, release['version'], actor='schema seed')


def import_release(conn, data, *, actor):
    if not conn.in_transaction:
        raise RuntimeError('Price imports require a transaction.')
    checked = validate_release(data)
    encoded = json.dumps(checked, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    existing = conn.execute('SELECT sha256 FROM price_versions WHERE version=?', (checked['version'],)).fetchone()
    if existing:
        if existing[0] != digest:
            raise ValueError('Version already exists with different contents; choose a new version.')
        return False
    conn.execute('INSERT INTO price_versions VALUES (?,?,?,?,?)', (checked['version'], encoded, digest, now_iso(), actor))
    return True


def activate_release(conn, version, *, actor):
    if not conn.in_transaction:
        raise RuntimeError('Price activation requires a transaction.')
    if not conn.execute('SELECT 1 FROM price_versions WHERE version=?', (version,)).fetchone():
        raise ValueError('Unknown price version.')
    conn.execute('INSERT INTO price_current VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET version=excluded.version', (version,))
    conn.execute('INSERT INTO price_activations(version,activated_at,activated_by) VALUES (?,?,?)', (version,now_iso(),actor))


def current_release() -> dict:
    from app.config import settings
    from app.db import connect
    # Pure pricing unit tests can use the bundled seed before DB initialization.
    # Once migrated, missing/corrupt active data fails closed instead of repricing.
    if not settings.database_path.exists():
        return seed_release()
    with closing(connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_versions'").fetchone():
            return seed_release()
        row = conn.execute('SELECT payload FROM price_versions JOIN price_current USING(version) WHERE singleton=1').fetchone()
        if not row:
            raise RuntimeError('No active price version.')
        return json.loads(row[0])


@exact_context
def conversion(release=None) -> dict:
    release = release or current_release()
    rate = Decimal(release['usd_to_cny']) * CREDITS_PER_CNY * Decimal(release['billing_multiplier'])
    return {'price_release': release['version'], 'usd_to_cny': release['usd_to_cny'],
        'billing_multiplier': release['billing_multiplier'], 'credits_per_cny': CREDITS_PER_CNY,
        'base_credits_per_usd': format(Decimal(release['usd_to_cny']) * CREDITS_PER_CNY, 'f'),
        'credits_per_usd': format(rate, 'f')}

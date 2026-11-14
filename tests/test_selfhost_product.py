"""Self-hosted contracts: temporary SQLite, fake upstream and no live credentials."""
import copy
import json
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from unittest.mock import patch
from test_backend_v2 import BackendCase, ORIGIN
import test_credit_gateway as fixture
from app import config, main
from app.ai_usage import reserve_usage, model_allowed, _quota_limits, _key_expired
from app.credit_pricing import BillingQuote, calculate_charge, prepare_billable_request
from app.credit_ledger import CreditError, reserve_credit, settle_credit, usd_to_units, wallet_snapshot, resolve_credit_hold
from app.image_generation import prepare_image_request, image_charge
from app.migrations import apply_migrations, schema_version
from app.price_store import current_release, conversion, import_release, activate_release, validate_release
from app.key_budget import key_committed_units
from app.key_settings import save_owner_limits, parse_owner_limits
from app.request_details import receipt, csv_safe
from app.utils.timefmt import now_iso


class ProductTests(BackendCase):
    def activate(self, **changes):
        release = copy.deepcopy(current_release()); release.update(version='test-next', **changes)
        with closing(self.open_database()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            import_release(conn, release, actor='fixture')
            activate_release(conn, release['version'], actor='fixture')
        return release

    def keyrow(self):
        return self.rows('SELECT * FROM api_keys WHERE id=?',(self.key_id,))[0]

    def reserve(self, request_id, amount=200):
        with closing(self.open_database()) as conn:
            return reserve_usage(conn, self.keyrow(), request_id=request_id, protocol='chat.completions',
                model=fixture.MODEL, streamed=False, method='POST', public_path='v1/chat/completions',
                model_id=fixture.MODEL, billing_quote=BillingQuote(amount,'{"version":"fixture"}'))

    def save(self, **changes):
        defaults=dict(credit_limit='',expires_on='',rpm='60',models=[fixture.MODEL]);defaults.update(changes)
        with closing(self.open_database()) as conn:
            save_owner_limits(conn,self.user_id,self.key_id,parse_owner_limits(**defaults))

    def test_price_release_changes_both_quotes_and_preserves_old_snapshot(self):
        old=prepare_billable_request(self.request_payload(),protocol='chat.completions')[1]
        image=prepare_image_request({'model':'gpt-image-2','prompt':'fixture'})[1]
        details=fixture.chat_usage()
        from app.credit_pricing import billing_details
        actual=billing_details(fixture.chat_response(),protocol='chat.completions')
        old_charge=calculate_charge(old.snapshot,actual)
        self.activate(usd_to_cny='8',billing_multiplier='1.25')
        new=prepare_billable_request(self.request_payload(),protocol='chat.completions')[1]
        new_image=prepare_image_request({'model':'gpt-image-2','prompt':'fixture'})[1]
        self.assertEqual(json.loads(new.snapshot)['credits_per_usd'],'1000.00')
        self.assertEqual(calculate_charge(old.snapshot,actual),old_charge)
        self.assertGreater(new.amount_units,old.amount_units)
        self.assertGreater(new_image.amount_units,image.amount_units)
        snap=json.loads(new_image.snapshot)
        self.assertEqual((snap['usd_to_cny'],snap['billing_multiplier']),('8','1.25'))
        self.assertEqual(usd_to_units('1'),800_000_000)  # Top-ups omit model markup.
        prices=self.client.get('/pricing').text
        self.assertIn('data-credits-per-usd="1000.00"',prices)
        self.assertIn('1000.00',self.client.get('/developer/credits').text)

    def test_price_version_immutable_duplicate_import_and_activation_history(self):
        release=self.activate()
        with closing(self.open_database()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self.assertFalse(import_release(conn,release,actor='retry'))
            with self.assertRaises(ValueError):
                import_release(conn,{**release,'usd_to_cny':'9'},actor='conflict')
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE price_versions SET payload='{}'")
            self.assertEqual(len(conn.execute('SELECT * FROM price_activations').fetchall()),2)

    def test_release_validation_rejects_float_and_malformed_card(self):
        for changes in [{'billing_multiplier':1.1},{'usd_to_cny':'NaN'},{'billing_multiplier':'0'}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                validate_release({**current_release(),**changes})
        release=copy.deepcopy(current_release());release['text']['models'][fixture.MODEL]['output']=None
        with self.assertRaises(ValueError):validate_release(release)

    def test_migrations_are_idempotent_and_failed_step_rolls_back(self):
        with closing(self.open_database()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.assertEqual(apply_migrations(conn),[])
            self.assertEqual(schema_version(conn),3)
            conn.rollback()
            def broken(c):
                c.execute('CREATE TABLE migration_failure(id INTEGER)')
                raise ValueError('fixture failure')
            with patch('app.migrations.MIGRATIONS',((4,'broken',broken),)):
                conn.execute('BEGIN IMMEDIATE')
                with self.assertRaises(ValueError):apply_migrations(conn)
                conn.rollback()
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='migration_failure'").fetchone())
            self.assertEqual(schema_version(conn),3)

    def test_owner_overlay_cannot_remove_admin_rate_expiry_or_models(self):
        expiry=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        self.execute('UPDATE api_keys SET rate_limit_per_minute=2,expires_at=?,credit_limit_units=100 WHERE id=?',(expiry,self.key_id))
        self.save(credit_limit='5',rpm='60')
        self.save(credit_limit='',rpm='60')
        row=self.keyrow()
        self.assertEqual((row['credit_limit_units'],row['expires_at'],row['rate_limit_per_minute']),(100,expiry,2))
        self.assertEqual(_quota_limits(row)[0],2)
        self.assertTrue(_key_expired(row,datetime.now(timezone.utc)+timedelta(days=2)))
        self.execute('DELETE FROM api_key_models WHERE api_key_id=? AND model_id!=?',(self.key_id,fixture.MODEL))
        with self.assertRaises(Exception):self.save(models=['gpt-6-luna'])
        with closing(self.open_database()) as conn:
            self.assertFalse(model_allowed(conn,self.keyrow(),'gpt-6-luna'))

    def test_parallel_reservations_cannot_exceed_key_cap(self):
        self.fund()
        self.execute('UPDATE api_keys SET owner_credit_limit_units=300 WHERE id=?',(self.key_id,))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(self.reserve,['parallel-one','parallel-two']))
        self.assertEqual(sum(result[0] is not None for result in results),1)
        self.assertEqual([r[1].code for r in results if r[1]],['key_credit_limit_exceeded'])
        with closing(self.open_database()) as conn:
            self.assertEqual(key_committed_units(conn,self.key_id),200)
            self.assertEqual(wallet_snapshot(conn,self.user_id)['reserved_units'],200)

    def test_settlement_rechecks_cap_after_inflight_tightening_and_does_not_charge(self):
        self.fund()
        one,_=self.reserve('first');two,_=self.reserve('second')
        self.execute('UPDATE api_keys SET owner_credit_limit_units=350 WHERE id=?',(self.key_id,))
        with closing(self.open_database()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            result=settle_credit(conn,one.id,180,reason='reliable fixture',usage_json='{"tokens":1}')
            self.assertEqual(result['status'],'reconciliation_required')
        self.assertEqual(self.rows("SELECT COUNT(*) n FROM credit_ledger WHERE kind='usage_charge'")[0]['n'],0)
        with closing(self.open_database()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            with self.assertRaises(CreditError) as error:
                resolve_credit_hold(conn,one.id,180,actor_id=self.user_id,reason='cannot bypass cap')
            self.assertEqual(error.exception.code,'key_credit_limit_exceeded')

    def test_settled_plus_other_inflight_plus_overrun_is_pending(self):
        self.fund()
        one,_=self.reserve('already-settled');two,_=self.reserve('other-inflight');three,_=self.reserve('overrun')
        with closing(self.open_database()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE');settle_credit(conn,one.id,100,reason='settled fixture')
        self.execute('UPDATE api_keys SET owner_credit_limit_units=550 WHERE id=?',(self.key_id,))
        with closing(self.open_database()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            result=settle_credit(conn,three.id,300,reason='real overrun',usage_json='{"tokens":1}')
            self.assertEqual(result['status'],'reconciliation_required')
            self.assertEqual(key_committed_units(conn,self.key_id),600)
        self.assertEqual(self.rows("SELECT SUM(-delta_units) n FROM credit_ledger WHERE kind='usage_charge'")[0]['n'],100)

    def test_initial_key_restrictions_and_csrf(self):
        from app.self_service_keys import self_key_nonce
        response=self.client.post('/developer/keys/request',data={'name':'limited at birth','key_nonce':self_key_nonce(self.user_id),
            'credit_limit':'0.001','rate_limit_per_minute':'2','models':fixture.MODEL},headers=ORIGIN)
        self.assertEqual(response.status_code,200)
        row=self.rows("SELECT * FROM api_keys WHERE name='limited at birth'")[0]
        self.assertEqual((row['owner_credit_limit_units'],row['owner_rpm']),(1000,2))
        self.assertEqual(self.client.post(f'/developer/keys/{row["id"]}/limits',data={},headers={'Origin':'https://evil.example'}).status_code,403)

    def test_receipt_exact_identity_owner_isolation_and_csv_filters(self):
        self.fund();response=self.generate();self.assertEqual(response.status_code,200)
        request_id=response.headers['x-request-id']
        data=self.client.get('/api/developer/requests/'+request_id).json()
        self.assertEqual(data['charged_credits'],'0.15624')
        self.assertEqual(data['pricing_snapshot']['price_release'],'seed-20261114')
        self.assertTrue(any(t['event']=='派发边界已提交' for t in data['timeline']))
        self.assertNotIn('fixture reply',json.dumps(data))
        first=data['id']
        # Duplicate caller request IDs are legitimate; exact UI links never drift to the latest row.
        self.execute("INSERT INTO api_usage_logs(user_id,created_at,method,public_path,request_id,model) VALUES (?,?,'GET','fixture',?,'duplicate-second')",(self.user_id,now_iso(),request_id))
        page=self.client.get(f'/developer/usage/requests/{first}').text
        self.assertIn(fixture.MODEL,page);self.assertNotIn('duplicate-second',page)
        latest=self.client.get('/api/developer/requests/'+request_id).json()
        self.assertEqual(latest['model'],'duplicate-second')
        csv=self.client.get('/developer/usage.csv',params={'request_id':request_id,'model':fixture.MODEL}).text
        self.assertIn(fixture.MODEL,csv);self.assertNotIn('duplicate-second',csv)
        other=self.new_user();self.execute('UPDATE api_usage_logs SET user_id=? WHERE id=?',(other,first))
        self.assertEqual(self.client.get(f'/developer/usage/requests/{first}').status_code,404)
        self.as_admin();self.assertEqual(self.client.get(f'/developer/usage/requests/{first}').status_code,200)
        for value in ['=SUM(1,2)',' +cmd','@SUM(A1)','\tformula']:
            self.assertTrue(csv_safe(value).startswith("'"))

    def test_disabled_catalog_hides_navigation_docs_and_rejects_gateway(self):
        disabled=replace(config.settings,api_catalog_enabled=False)
        with patch.object(config,'settings',disabled),patch.object(main,'settings',disabled),patch.object(__import__('app.public_stats',fromlist=['settings']),'settings',disabled),patch.dict(__import__('app.web.templating',fromlist=['templates']).templates.env.globals,{'api_catalog_enabled':False}):
            for path in ['/apis','/tester','/admin/routes','/api/public/routes','/api/public/stats','/api/public/v1/anything']:
                self.assertEqual(self.client.get(path).status_code,404,path)
            for path in ['/developer','/docs']:
                body=self.client.get(path).text
                self.assertNotIn('id="endpoints"',body)
                self.assertNotIn('LuomoCore health',body)
        self.assertEqual(self.client.get('/openapi.json').status_code,404)
        self.assertEqual(self.client.get('/llms.txt').status_code,200)

    def test_canonical_upstream_alias_and_url_validation(self):
        with patch.dict('os.environ',{'UPSTREAM_API_KEY':'canonical','CODEXPROXY_API_KEY':'legacy'}):
            self.assertEqual(config.upstream_api_key(),'canonical')
            self.assertEqual(config.integration_secret('CODEXPROXY_API_KEY'),'canonical')
        for value in ['javascript:alert(1)','https://user:pass@example.test','//example.test','https://example.test/a?q=1','https://example.test/#x']:
            with self.assertRaises(ValueError):config.public_url(value,'fixture')

    def test_unconfigured_deployment_does_not_probe_or_inherit_verification(self):
        from app import ai_catalog
        with patch.dict('os.environ',{'UPSTREAM_API_KEY':'','CODEXPROXY_API_KEY':'','MODEL_VERIFICATION_PATH':'',
            'GATEWAY_IMAGE_GENERATION':'false','GATEWAY_NATIVE_RESPONSES':'false'}),patch.object(ai_catalog,'_cache',None),patch.object(ai_catalog,'_expires',0),patch.object(ai_catalog.httpx,'AsyncClient',side_effect=AssertionError('unconfigured service attempted network')):
            data=self.client.get('/api/public/ai-catalog').json()
            self.assertEqual(data['models'],[])
            self.assertTrue(any(c['detail']=='未配置上游凭据' for c in data['components']))
        self.assertEqual(self.client.get('/ready').status_code,200)

    def test_image_settlement_and_text_rounding_use_snapshot_under_low_decimal_context(self):
        image_quote=prepare_image_request({'model':'gpt-image-2','prompt':'fixture'})[1]
        usage=dict(kind='image_generation',model='gpt-image-2',input_tokens=10,output_tokens=20,total_tokens=30,
            input_text_tokens=10,input_image_tokens=0,output_text_tokens=0,output_image_tokens=20,output_breakdown_reported=True)
        self.assertEqual(image_charge(json.loads(image_quote.snapshot),usage),468_000)
        self.activate(usd_to_cny='8',billing_multiplier='1.25')
        new=prepare_image_request({'model':'gpt-image-2','prompt':'fixture'})[1]
        self.assertEqual(image_charge(json.loads(new.snapshot),usage),650_000)
        self.assertEqual(image_charge(json.loads(image_quote.snapshot),usage),468_000)
        with localcontext() as context:
            context.prec=3
            self.assertEqual(image_charge(json.loads(new.snapshot),usage),650_000)
            self.assertEqual(conversion()['credits_per_usd'],'1000.00')

    def test_seed_does_not_promote_registered_bootstrap_name_or_reactivate_admin(self):
        import scripts.seed_default_apis as seeding
        username=self.rows('SELECT username FROM users WHERE id=?',(self.user_id,))[0]['username']
        base=replace(config.settings,admin_username=username,admin_password_hash='')
        with patch.object(seeding,'settings',base):
            seeding.seed()
        self.assertEqual(self.rows('SELECT role FROM users WHERE id=?',(self.user_id,))[0]['role'],'developer')
        with patch.object(seeding,'settings',replace(base,admin_password_hash='configured-hash')):
            with self.assertRaises(RuntimeError):seeding.seed()
        self.execute("UPDATE users SET role='admin',status='suspended' WHERE id=?",(self.user_id,))
        with patch.object(seeding,'settings',base):seeding.seed()
        self.assertEqual(self.rows('SELECT status FROM users WHERE id=?',(self.user_id,))[0]['status'],'suspended')

"""Reviewed regressions: assert transport destinations and real SQLite read snapshots."""
import os
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from decimal import localcontext
from urllib.parse import quote
from unittest.mock import patch
import httpx
import test_selfhost_product as product_fixture
from test_backend_v2 import BackendCase
import test_credit_gateway as fixture
from app import ai_catalog
from app.credit_ledger import settle_credit, usd_to_units
from app.http_clients import create_http_client
from app.price_store import current_release, import_release, activate_release
from app.request_details import receipt


class ReviewFixTests(BackendCase):
    keyrow = product_fixture.ProductTests.keyrow
    def test_upstream_precedence_reaches_real_outbound_urls_and_headers(self):
        self.fund()
        old = self.client.app.state.codex_http_client
        self.client.portal.call(old.aclose)
        outbound = []
        def upstream(request):
            outbound.append((str(request.url), request.headers.get('authorization')))
            if request.method == 'GET':
                return httpx.Response(200, json={'object':'list','data':[{'id':fixture.MODEL,'object':'model'}]})
            body = fixture.responses_response() if request.url.path.endswith('/responses') else fixture.chat_response()
            return httpx.Response(200, json=body)
        self.client.app.state.codex_http_client = create_http_client(httpx.Timeout(5),transport=httpx.MockTransport(upstream))
        original_async_client = httpx.AsyncClient
        def catalog_client(**kwargs):
            return original_async_client(**kwargs, transport=httpx.MockTransport(upstream))
        scenarios = [
            ({'UPSTREAM_BASE_URL':'https://canonical.example.test/prefix','UPSTREAM_API_KEY':'canonical-only'},
             'https://canonical.example.test/prefix','canonical-only'),
            ({'CODEXPROXY_BASE_URL':'https://legacy.example.test/prefix','CODEXPROXY_API_KEY':'legacy-only'},
             'https://legacy.example.test/prefix','legacy-only'),
            ({'UPSTREAM_BASE_URL':'https://new.example.test/prefix','UPSTREAM_API_KEY':'new-secret',
              'CODEXPROXY_BASE_URL':'https://old.example.test/wrong','CODEXPROXY_API_KEY':'old-secret'},
             'https://new.example.test/prefix','new-secret'),
        ]
        base_env = {k:v for k,v in os.environ.items() if k not in {'UPSTREAM_BASE_URL','UPSTREAM_API_KEY','CODEXPROXY_BASE_URL','CODEXPROXY_API_KEY'}}
        for variables, base, key in scenarios:
            with self.subTest(variables=tuple(variables)), patch.dict(os.environ,{**base_env,**variables},clear=True):
                outbound.clear()
                models=self.client.get('/v1/models',headers={'Authorization':'Bearer '+self.key})
                self.assertEqual(models.status_code,200,models.text)
                self.assertEqual(self.generate('chat').status_code,200)
                self.assertEqual(self.generate('responses').status_code,200)
                with patch.object(ai_catalog,'_cache',None),patch.object(ai_catalog,'_expires',0),patch.object(ai_catalog.httpx,'AsyncClient',side_effect=catalog_client):
                    catalog=self.client.get('/api/public/ai-catalog')
                    self.assertEqual(catalog.status_code,200)
                    self.assertEqual(catalog.json()['model_count'],1)
                self.assertEqual(outbound,[(base+path,'Bearer '+key) for path in
                    ['/v1/models','/v1/chat/completions','/v1/responses','/v1/models']])

    def test_receipt_uses_one_read_snapshot_during_concurrent_settlement(self):
        self.fund()
        reservation,rejection=product_fixture.ProductTests.reserve(self,'snapshot-reader')
        self.assertIsNone(rejection)
        with closing(self.open_database()) as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
        interleaved=[]
        def settle():
            with closing(self.open_database()) as writer,writer:
                writer.execute('BEGIN IMMEDIATE')
                settle_credit(writer,reservation.id,100,reason='concurrent authoritative fixture')
            interleaved.append('committed')
        with ThreadPoolExecutor(max_workers=1) as pool:
            class FirstRead:
                def __init__(self,cursor):self.cursor=cursor
                def fetchone(inner):
                    row=inner.cursor.fetchone()
                    pool.submit(settle).result(timeout=5)
                    return row
            class SnapshotConnection(sqlite3.Connection):
                def execute(inner,sql,parameters=()):
                    cursor=super().execute(sql,parameters)
                    return FirstRead(cursor) if sql.startswith('SELECT id,request_id,') else cursor
            def reader():
                conn=sqlite3.connect(self.path,timeout=5,factory=SnapshotConnection)
                conn.row_factory=sqlite3.Row
                return conn
            with patch('app.request_details.connect',side_effect=reader):
                before=receipt({'id':self.user_id,'role':'developer'},usage_id=reservation.id)
        self.assertEqual(interleaved,['committed'])
        self.assertEqual((before['credit_status'],before['hold']['status'],before['charged_credits']),('held','held','0'))
        self.assertEqual([row['kind'] for row in before['ledger']],['credit_freeze'])
        after=receipt({'id':self.user_id,'role':'developer'},usage_id=reservation.id)
        self.assertEqual((after['credit_status'],after['hold']['status'],after['charged_credits']),('settled','settled','0.0001'))
        self.assertEqual([row['kind'] for row in after['ledger']],['credit_freeze','credit_release','usage_charge'])

    def test_printable_request_ids_round_trip_through_generation_receipts_and_csv(self):
        self.fund()
        for request_id in ['order/123','r'*121,'r'*128,'order/a?x=1#part% space']:
            with self.subTest(request_id=request_id):
                response=self.client.post('/v1/chat/completions',json=self.request_payload(),
                    headers={'Authorization':'Bearer '+self.key,'X-Request-ID':request_id})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.headers['x-request-id'],request_id)
                result=self.client.get('/api/developer/requests',params={'request_id':request_id})
                self.assertEqual(result.status_code,200,result.text)
                data=result.json()
                self.assertEqual(data['request_id'],request_id)
                self.assertEqual(self.client.get('/api/developer/requests/'+quote(request_id,safe='')).status_code,200)
                page=self.client.get(f'/developer/usage/requests/{data["id"]}')
                self.assertEqual(page.status_code,200)
                self.assertIn('成功 · 已结算',page.text)
                csv=self.client.get('/developer/usage.csv',params={'request_id':request_id})
                self.assertEqual(csv.status_code,200,csv.text)
                self.assertIn(request_id,csv.text)
                other=self.new_user()
                self.execute('UPDATE api_usage_logs SET user_id=? WHERE id=?',(other,data['id']))
                self.assertEqual(self.client.get('/api/developer/requests',params={'request_id':request_id}).status_code,404)
                self.assertEqual(self.client.get(f'/developer/usage/requests/{data["id"]}').status_code,404)
                self.assertNotIn(request_id,self.client.get('/developer/usage.csv',params={'request_id':request_id}).text)
        self.assertEqual(self.client.get('/api/developer/requests',params={'request_id':'x'*129}).status_code,400)
        self.assertEqual(self.client.get('/developer/usage.csv',params={'request_id':'x'*129}).status_code,400)

    def test_usd_conversion_ignores_callers_low_decimal_precision(self):
        release=current_release();release.update(version='precise-usd-fixture',usd_to_cny='7.23456789',billing_multiplier='1.25')
        with closing(self.open_database()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            import_release(conn,release,actor='fixture')
            activate_release(conn,release['version'],actor='fixture')
        with localcontext() as context:
            context.prec=3
            self.assertEqual(usd_to_units('1'),723456789)

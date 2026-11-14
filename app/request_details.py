"""Session-only, ownership-scoped receipts. Prompts and transport credentials are never selected."""
from __future__ import annotations
import csv
import io
import json
from contextlib import closing
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from app.auth import require_developer
from app.db import connect
from app.credit_ledger import units_to_credits
from app.web.templating import templates

router = APIRouter()
FIELDS = ('id','request_id','api_key_id','protocol','model','status_code','outcome','credit_status',
          'dispatch_state','started_at','created_at','completed_at','response_time_ms','input_tokens',
          'output_tokens','total_tokens','credit_reserved_units','credit_charged_units')


def receipt(user, request_id=None, *, usage_id=None):
    with closing(connect()) as conn:
        conditions, args = [], []
        if request_id is not None:
            conditions.append('request_id=?'); args.append(request_id)
        if usage_id is not None:
            conditions.append('id=?'); args.append(usage_id)
        if not conditions:
            raise HTTPException(400, '需要请求 ID 或记录编号。')
        if user['role'] != 'admin':
            conditions.append('user_id=?'); args.append(user['id'])
        row = conn.execute('SELECT '+','.join(FIELDS)+' FROM api_usage_logs WHERE '+' AND '.join(conditions)+' ORDER BY id DESC LIMIT 1', args).fetchone()
        if not row:
            raise HTTPException(404, '请求不存在。')
        result = dict(row)
        hold = conn.execute('SELECT status,dispatch_state,amount_units,actual_units,pricing_snapshot,created_at,dispatched_at,updated_at FROM credit_holds WHERE usage_id=?', (row['id'],)).fetchone()
        entries = [dict(r) for r in conn.execute('SELECT kind,delta_units,delta_reserved_units,created_at FROM credit_ledger WHERE usage_id=? ORDER BY id', (row['id'],))]
    result['billing_status'] = result['credit_status']
    result['ledger'] = entries
    result['hold'] = dict(hold) if hold else None
    result['pricing_snapshot'] = json.loads(result['hold'].pop('pricing_snapshot')) if hold else None
    result['timeline'] = [{'event':'请求登记','at':row['started_at'] or row['created_at']}]
    if hold:
        result['timeline'].append({'event':'冻结预算','at':hold['created_at']})
        if hold['dispatched_at']:
            result['timeline'].append({'event':'派发边界已提交','at':hold['dispatched_at']})
        if hold['status'] in ('settled','released'):
            result['timeline'].append({'event':'结算完成' if hold['status']=='settled' else '释放冻结','at':hold['updated_at']})
        elif hold['status']=='reconciliation_required':
            result['timeline'].append({'event':'转人工核账','at':hold['updated_at']})
    if row['completed_at']:
        result['timeline'].append({'event':'调用记录完成','at':row['completed_at']})
    result['reserved_credits'] = units_to_credits(hold['amount_units'] if hold else row['credit_reserved_units'] or 0)
    result['charged_credits'] = units_to_credits(row['credit_charged_units'] or 0)
    result['released_units'] = sum(max(0,-r['delta_reserved_units']) for r in entries)
    result['released_credits'] = units_to_credits(result['released_units'])
    return result


@router.get('/api/developer/requests/{request_id}')
def receipt_json(request_id: str, usage_id: int | None = None, user=Depends(require_developer)):
    return JSONResponse(receipt(user, request_id, usage_id=usage_id), headers={'Cache-Control':'no-store'})


@router.get('/developer/usage/requests/{usage_id}', response_class=HTMLResponse)
def exact_receipt_page(request: Request, usage_id: int, user=Depends(require_developer)):
    data = receipt(user, usage_id=usage_id)
    return templates.TemplateResponse(request, 'request_detail.html', {'user':user,'receipt':data,
        'snapshot_json':json.dumps(data['pricing_snapshot'],ensure_ascii=False,indent=2)}, headers={'Cache-Control':'no-store'})


@router.get('/developer/requests/{request_id}', response_class=HTMLResponse)
def receipt_page(request: Request, request_id: str, user=Depends(require_developer)):
    data = receipt(user, request_id)
    return templates.TemplateResponse(request, 'request_detail.html', {'user':user,'receipt':data,
        'snapshot_json':json.dumps(data['pricing_snapshot'],ensure_ascii=False,indent=2)}, headers={'Cache-Control':'no-store'})


def filtered_usage(user, *, model='', protocol='', billing='', since='', until='', key_id='', request_id='', limit=100):
    conditions, args = [], []
    if user['role'] != 'admin':
        conditions.append('user_id=?'); args.append(user['id'])
    for field, value, choices in [('request_id',request_id,None), ('model',model,None), ('protocol',protocol,{'chat.completions','responses','images.generations','models','public'}),
                                  ('credit_status',billing,{'held','settled','released','reconciliation_required','admin_exempt','not_billable','legacy_unbilled','unbilled'})]:
        if not value:
            continue
        if len(value)>120 or (choices and value not in choices):
            raise HTTPException(400,'筛选值无效。')
        conditions.append(field+'=?'); args.append(value)
    for raw, operator in [(since,'>='),(until,'<=')]:
        if raw:
            try:
                day = date.fromisoformat(raw)
            except ValueError:
                raise HTTPException(400,'日期格式应为 YYYY-MM-DD。') from None
            conditions.append('substr(COALESCE(started_at,created_at),1,10)'+operator+'?'); args.append(day.isoformat())
    if key_id:
        try:
            kid = int(key_id)
            if not 0 < kid <= 9223372036854775807: raise ValueError
        except ValueError:
            raise HTTPException(400,'密钥 ID 无效。') from None
        conditions.append('api_key_id=?'); args.append(kid)
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute('SELECT '+','.join(FIELDS)+' FROM api_usage_logs'+
            (' WHERE '+' AND '.join(conditions) if conditions else '')+' ORDER BY id DESC LIMIT ?', (*args,limit))]


def csv_safe(value):
    if value is None: return ''
    text = str(value)
    return "'"+text if text.lstrip().startswith(('=','+','-','@')) or text.startswith(('\t','\r','\n')) else text


@router.get('/developer/usage.csv')
def usage_csv(request: Request, user=Depends(require_developer)):
    params = {name:request.query_params.get(name,'') for name in ('model','protocol','billing','since','until','key_id','request_id')}
    rows = filtered_usage(user, **params, limit=10000)
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(FIELDS)
    writer.writerows([[csv_safe(r[k]) for k in FIELDS] for r in rows])
    return Response('\ufeff'+output.getvalue(),media_type='text/csv; charset=utf-8',headers={
        'Cache-Control':'no-store','Content-Disposition':'attachment; filename="usage.csv"'})

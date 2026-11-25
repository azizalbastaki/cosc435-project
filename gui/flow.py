"""Build an honest timeline from recorded events, including legacy audit history."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from gui import backend

PURPOSES = {
    'transactions.php': 'Reconcile earlier orders and read recorded outcomes.',
    'portfolio.php': 'Check cash, holdings, and current portfolio value.',
    'policies.php': 'Read limits, permissions, and executed trade counts.',
    'theory.php': 'Read the active submitted theory and its version.',
    'market_prices.php': 'Get an independent current quote.',
    'market_model.php': 'Read the public price generator for local price replay.',
    'market_news.php': 'Read news as untrusted evidence, never instructions.',
    'trade_propose.php': 'Propose an order; it waits for Guardian review.',
    'trade_pending.php': 'Read the pending queue for independent review.',
    'trade_review.php': 'Submit a Guardian decision; Ledger performs final checks.',
    'guardian_logs.php': 'Read the audit trail of Guardian decisions.',
}


def ts(value):
    try:
        d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()*1000
    except (ValueError,TypeError):
        return 0


def describe(e):
    kind=e.get('event','')
    endpoint=e.get('endpoint','')
    if kind.startswith('api_'):
        why=PURPOSES.get(endpoint,'A recorded Ledger operation.')
        if kind=='api_start':
            return f"{e.get('method','GET')} {endpoint}", why, 'In progress · includes pacing and retries'
        summary=e.get('summary',{})
        outcome=summary.get('status') or e.get('outcome','Unknown')
        return f"{e.get('method','GET')} {endpoint}", why, str(outcome)
    if kind.startswith('model_request_'):
        return f"{e.get('provider','Model')} · {e.get('model','')}", 'Ask for structured JSON; local validation still follows.', ('Awaiting response' if kind.endswith('start') else e.get('outcome','Unknown'))
    if kind=='flow_stage':
        return e.get('stage','').replace('_',' ').title(),e.get('detail',''), 'Local processing' if e.get('stage')!='waiting' else 'Waiting'
    if kind=='model_decision':
        return 'Trader model decision',e.get('reasoning','No explanation logged.'),str(e.get('decision','Unknown'))
    if kind=='model_judgment':
        return 'Guardian model judgment',e.get('reasoning','See the final review for the saved explanation.'),str(e.get('final',e.get('model','Unknown')))
    if kind=='rule_evaluation':
        reasons='; '.join(f.get('reason','') for f in e.get('findings',[]))
        return 'Deterministic security checks', reasons or 'No rule findings; the model and final recheck still follow.', str(e.get('decision'))
    if kind=='proposed':
        return 'Order proposed',f"{e.get('action')} {e.get('qty')} · Waiting for independent Guardian review.",str(e.get('status','PENDING'))
    if kind=='review':
        return 'Guardian review returned',e.get('reason','No explanation logged.'),str(e.get('server_status','Unknown'))
    if kind=='hold':
        return 'Trader holds',e.get('detail',e.get('why','No reason logged.')),'HOLD · no proposal sent'
    if kind=='heartbeat':
        return 'Queue checked','No pending orders were reported.','Waiting'
    if kind=='order_outcome':
        return 'Order reconciled',e.get('reason','Read recorded order status.'),str(e.get('status','Unknown'))
    if kind=='model_call':
        return 'Model response recorded','Legacy log: response recorded, but request start and API path were not logged.','Response received'
    if kind=='session_start':
        return 'Agent started','This role is running independently of the other agent.','Started'
    if kind=='session_end':
        return 'Agent stopped','No further work is scheduled by this process.','Stopped'
    if kind in {'model_failure','model_unavailable','model_output_rejected'}:
        return 'Model unavailable or rejected','The trader holds; the Guardian escalates when a valid review can be submitted.','Failure / rejected'
    return kind.replace('_',' ').title(),str(e.get('detail') or e.get('reason') or e.get('message') or ''),'Recorded'


VISIBLE={'api_start','api_end','model_request_start','model_request_end','flow_stage','model_decision','model_judgment',
         'rule_evaluation','proposed','review','hold','heartbeat','order_outcome','model_call','session_start','session_end',
         'model_failure','model_unavailable','model_output_rejected','review_uncertain','review_reconciled','cooldown','ALERT'}


def timeline():
    rows=[]
    for name,role in [('trading_agent','trader'),('guardian','guardian')]:
        for e in backend.events(name):
            if e.get('event') not in VISIBLE or not ts(e.get('ts')):
                continue
            title,why,outcome=describe(e)
            group=e.get('run_id') or 'legacy-'+str(e.get('ts',''))[:10]
            # Explicit allowlist: do not pass headers, prompts, credentials or raw provider responses to the browser.
            row={k:e[k] for k in ['event','request_id','endpoint','method','duration_ms','http_status','summary','request',
                                  'provider','model','tx','risk_score','features','bounds','findings','stage','decision','server_status'] if k in e}
            row.update(time=ts(e['ts']),role=role,group=group,title=title,why=why,outcome=outcome)
            rows.append(row)
    return sorted(rows,key=lambda e:e['time'])


def active_requests(rows, now_ms, running):
    """Only explicitly unmatched starts can be active. Old or stopped calls are unknown, never live."""
    open_calls={}
    for e in rows:
        key=(e['role'], e.get('request_id'))
        if e['event'] in {'api_start','model_request_start'}:
            open_calls[key]=e
        elif e['event'] in {'api_end','model_request_end'}:
            open_calls.pop(key,None)
        elif e['event']=='session_end':
            open_calls={k:v for k,v in open_calls.items() if v['role']!=e['role']}
    return [e for e in open_calls.values() if running and 0<=now_ms-e['time']<=180000]


def render(rows, mode, running, now_ms, models):
    from gui import theme
    payload={'events':rows[-1500:],'mode':mode,'running':running,'now':now_ms,'models':models,'truncated':len(rows)>1500}
    encoded=json.dumps(payload,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return (Path(__file__).with_name('flow.html')).read_text().replace('__FLOW_DATA__',encoded).replace('__BRAND_CSS__',theme.css())

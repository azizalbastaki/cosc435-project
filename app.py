"""TradeSentry. Launch with: python -m streamlit run app.py"""
from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common.presets import COIN_THEORY, PROVIDERS
from gui import backend as b
from gui import theme

st.set_page_config(page_title=theme.NAME, page_icon=theme.ICON, layout='wide', initial_sidebar_state='expanded')
st.markdown('<style>' + theme.css() + (ROOT / 'gui/style.css').read_text() + '</style>', unsafe_allow_html=True)


def esc(value):
    return html.escape(str(value))


def markup(content):
    st.markdown(content, unsafe_allow_html=True)


def money(value):
    return f'${float(value):,.2f}' if value is not None else '—'


def stamp(value):
    if not value:
        return 'No timestamp'
    try:
        d = datetime.fromtimestamp(value, timezone.utc) if isinstance(value, (int, float)) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(ZoneInfo('Asia/Dubai')).strftime('%d %b · %H:%M:%S GST')
    except (ValueError, TypeError):
        return str(value)


@st.cache_resource
def controller(version: int = 2):  # version 2: supports the unlimited switch
    return b.SessionController()


ctrl = controller()
cfg = b.config('trading_agent')
gcfg = b.config('guardian')
configured_strategy = cfg.get('expected_theory', {}).get('strategy')
observed_theory = b.read_json(ROOT/'state/dashboard_snapshot.json').get('theory') or {}
observed_strategy = observed_theory.get('strategy')
strategy_labels = {'momentum': 'Momentum', 'mean_reversion': 'Mean reversion', 'custom': 'Custom'}
with st.sidebar:
    markup(f'<div class="brand">{theme.WORDMARK}</div><div class="brand-sub">{theme.TAGLINE}</div>')
    markup('<div class="eyebrow">WORKSPACE</div>')
    page = st.radio('Workspace', ['Overview', 'Live flow', 'Guardian', 'Activity', 'Setup'], label_visibility='collapsed')
    st.divider()
    markup('<div class="eyebrow">YOUR ENVIRONMENT</div><span class="pill orange">● &nbsp; Simulated trading</span>')
    markup(f'<div class="account"><b>{esc(cfg.get("member") or "Your workspace")}</b><p>{esc(cfg["symbol"])} · {esc(strategy_labels.get(observed_strategy or configured_strategy, "Strategy not confirmed"))}</p><p>{"Last observed on Ledger" if observed_strategy else "Local configuration"}</p><p>Theory v{esc(observed_theory.get("version") if observed_strategy else cfg.get("expected_theory", {}).get("version", "—"))}</p></div>')
    if observed_strategy and configured_strategy != observed_strategy:
        st.warning('Strategy mismatch: Ledger was last observed as '
                   f'{strategy_labels.get(observed_strategy, observed_strategy)}, but the local pin is '
                   f'{strategy_labels.get(configured_strategy, "not set")}. Review Setup before trading.')
    st.link_button('Open Ledger platform ↗', 'https://ledger.chanyeun.com', width='stretch')
    markup('<div class="footer">TradeSentry · COSC 435<br>Local workspace · Times in UAE (GST)</div>')


def heading(title, subtitle):
    markup(f'<div class="topline"><span>Workspace &nbsp; / &nbsp; <b>{esc(page)}</b></span><span class="pill">◈ &nbsp; TRADESENTRY / WORKSPACE</span></div>')
    st.title(title)
    st.caption(subtitle)


def section(title, subtitle=''):
    markup(f'<div class="section-title">{esc(title)}</div><div class="subtle">{esc(subtitle)}</div>')


def chart(samples):
    if not samples:
        markup('<div class="empty">Your portfolio history will appear after an agent session.</div>')
        return
    frame = pd.DataFrame(samples)
    # log timestamps mix seconds and milliseconds precision; parse each one as ISO 8601
    frame['time'] = pd.to_datetime([datetime.fromisoformat(str(t).replace('Z', '+00:00')) for t in frame.ts],
                                   utc=True).tz_convert('Asia/Dubai')
    fig = go.Figure(go.Scatter(x=frame.time, y=frame.total_value, mode='lines+markers',
                             line=dict(color=theme.COLORS['emerald'], width=3, shape='linear'), marker=dict(size=7, color=theme.COLORS['emerald']),
                             fill='tozeroy', fillcolor='rgba(56,229,173,.05)',
                             hovertemplate='%{x|%d %b %H:%M GST}<br>$%{y:,.2f}<extra></extra>'))
    vals = frame.total_value.astype(float)
    pad = max((vals.max()-vals.min())*.3, abs(vals.mean())*.002, 1)
    fig.update_layout(height=255, margin=dict(l=0,r=12,t=18,b=0), paper_bgcolor='rgba(0,0,0,0)',
                      plot_bgcolor='rgba(0,0,0,0)', font=dict(family=theme.MONO, color=theme.COLORS['steel'],size=11),
                      yaxis=dict(range=[vals.min()-pad, vals.max()+pad], tickprefix='$', tickformat=',.0f',gridcolor=theme.COLORS['graphite'],zeroline=False),
                      xaxis=dict(showgrid=False,tickformat='%H:%M',title=None), showlegend=False)
    st.plotly_chart(fig, width='stretch', config={'displayModeBar':False})
    st.caption(f'{len(samples)} recorded samples · GST · Lines connect observations; gaps are not continuous monitoring.')


def review_table(reviews):
    return [{'Time (GST)':stamp(r.get('ts') or r.get('created_at')), 'Order':r.get('tx',r.get('transaction_id')),
             'Asset':r.get('symbol',cfg['symbol']), 'Action':r.get('action','—'), 'Decision':r.get('decision'),
             'Risk / 100':r.get('risk_score'), 'Ledger status':r.get('server_status','Not in local log')} for r in reversed(reviews)]


def decision_frame(rows):
    frame = pd.DataFrame(rows)
    if 'Decision' in frame.columns:
        return frame.style.map(theme.decision_style, subset=['Decision'])
    return frame


def status_panel():
    state = ctrl.status()
    ready, message = b.readiness()
    section('Session control', 'One session. Two independent roles.')
    for name, description, icon in [('Trading Agent','Analyzes signals & proposes orders','↗'),('AI Guardian','Checks risk before execution','◇')]:
        markup(f'<div class="agent"><div class="agent-icon">{icon}</div><div><div class="agent-name">{name}</div><div class="agent-desc">{description}</div></div></div>')
    if state['running'] and state.get('unlimited'):
        e = int(state['elapsed'])
        st.warning(f'Running · UNLIMITED · {e//3600:d}:{e%3600//60:02d}:{e%60:02d} elapsed — runs until you press Stop.')
        if st.button('Stop both agents', width='stretch', type='primary'):
            ctrl.stop()
            st.rerun()
    elif state['running']:
        seconds = int(state['remaining'])
        st.success(f'Running · {seconds//60:02d}:{seconds%60:02d} remaining')
        st.progress(min(1., max(0., 1-state['remaining']/(ctrl.minutes*60))))
        if st.button('Stop both agents', width='stretch', type='primary'):
            ctrl.stop()
            st.rerun()
    else:
        if state['launched'] and state['exit_code'] not in (0,None,-15):
            st.error('Session stopped with an error. Check your keys, model, and saved configuration. Details are in logs/dashboard_runner.out.')
        elif state['launched']:
            st.info('Session stopped.' if state['stopped'] else 'Session completed.')
        else:
            markup('<span class="pill">● &nbsp; Agents idle</span>')
        unlimited = st.toggle('Unlimited (run until stopped)', value=False, key='session_unlimited',
                              help='Off = one session of at most 10 minutes (course rule). On = no time limit.')
        if unlimited:
            minutes = 10
            st.warning('Unlimited breaks the course rule of at most 10 minutes per session. '
                       'It is recorded as "unlimited" in the session log. Keep the Mac awake and press Stop when done.')
        else:
            minutes = st.select_slider('Session length', options=[1,2,3,4,5,6,7,8,9,10], value=10, format_func=lambda v:f'{v} min', key='session_minutes')
        if st.button('▶  Start trading session', type='primary', width='stretch', disabled=not ready):
            try:
                ctrl.start(minutes, unlimited=unlimited)
                st.rerun()
            except ValueError as e:
                st.error(str(e))
        st.caption(message)
    markup('<div class="notice">Starting a session can place simulated orders and use your model API credits. Both agents stop at the session limit, or when you press Stop in unlimited mode.</div>')


@st.fragment(run_every='5s')
def dashboard():
    data = b.overview()
    snap = data['snapshot']
    last_sample = data['samples'][-1] if data['samples'] else {}
    snap_time = snap.get('fetched_at',0)
    sample_time = pd.Timestamp(last_sample['ts']).timestamp() if last_sample else 0
    port = snap.get('portfolio', {}) if snap_time > sample_time else last_sample
    source_time = snap_time if snap_time > sample_time else last_sample.get('ts')
    if page == 'Overview':
        markup('<div class="topline"><span>Workspace &nbsp; / &nbsp; <b>Overview</b></span><span class="pill">● &nbsp; Local audit history</span></div>')
        markup(f'<div class="hero"><div class="eyebrow">TRADESENTRY · AUTONOMY WITH OVERSIGHT</div><h1>Trade intelligently.<br>Stay protected.</h1><p>{theme.DESCRIPTION}</p><div class="hero-art" aria-hidden="true">{theme.ICON}</div></div>')
        left, right = st.columns([3.25,1.3], gap='large')
        with left:
            c1,c2,c3=st.columns(3)
            c1.metric('Portfolio value',money(port.get('total_value')))
            c2.metric('Cash available',money(port.get('cash_balance',port.get('cash'))))
            c3.metric('Recorded executions',str(sum(r.get('server_status')=='EXECUTED' for r in data['reviews'])))
            st.caption(f'Last portfolio observation: {stamp(source_time)} · Execution count is from local review logs, not the full account.')
            with st.container(border=True):
                section('Portfolio journey',f'{cfg["symbol"]} workspace · Recorded account value')
                chart(data['samples'])
            with st.container(border=True):
                section('Recent decisions','What your Guardian reviewed, and what Ledger returned.')
                if data['reviews']:
                    st.dataframe(decision_frame(review_table(data['reviews'][-5:])), hide_index=True, width='stretch')
                else:
                    st.info('No Guardian decisions recorded yet.')
        with right:
            with st.container(border=True):
                status_panel()
            with st.container(border=True):
                section('Guardrails','Local limits · Ledger policy may be stricter')
                for label,value in [('Order ceiling',money(cfg['limits']['max_order_usd'])),('Trades per hour',cfg['limits']['max_trades_per_hour']),('Theory version',cfg['expected_theory']['version'])]:
                    markup(f'<div class="rule-line"><span>{esc(label)}</span><strong>{esc(value)}</strong></div>')
                if st.button('Refresh from Ledger',width='stretch',disabled=not b.credentials().get('LEDGER_TRADER_KEY')):
                    try:
                        with st.spinner('Reading account data…'):
                            b.sync_account()
                        st.rerun()
                    except Exception:
                        st.error('Could not refresh. Check the trader key and connection; Ledger may require a cooldown.')
                st.caption('Read-only refresh. No trades or model calls.')
    elif page == 'Guardian':
        heading('The second line of defense.', 'Inspect security decisions, triggered rules, and the reasoning behind each review.')
        counts=data['decisions']
        for col,name in zip(st.columns(4), ['APPROVE','MODIFY','BLOCK','ESCALATE']):
            with col:
                markup(f'<div class="decision {name.lower()}">{name}</div>')
                st.metric('Recorded reviews', counts.get(name,0))
        st.write('')
        a,z=st.columns([2.5,1.2],gap='large')
        with a:
            section('Decision explorer','Saved Guardian reviews. Select a decision to read its evidence.')
            decision = st.selectbox('Decision filter',['All','APPROVE','MODIFY','BLOCK','ESCALATE'])
            rows = [r for r in reversed(data['reviews']) if decision=='All' or r.get('decision')==decision]
            if not rows:
                st.info('No decisions match this filter.')
            for r in rows[:30]:
                with st.expander(f"#{r.get('tx')} · {r.get('decision')} · {r.get('symbol')} {r.get('action')} · Risk {r.get('risk_score', '—')}/100"):
                    decision_class = str(r.get('decision', '')).lower()
                    if decision_class in ('approve', 'modify', 'block', 'escalate'):
                        markup(f'<div class="decision {decision_class}">{esc(r.get("decision"))}</div>')
                    st.caption(stamp(r.get('ts')))
                    st.write(r.get('reason','No explanation recorded.'))
                    st.caption(f"Ledger result: {r.get('server_status','Unknown')} · Reduced quantity: {r.get('modified_quantity') or 'Not modified'}")
        with z:
            with st.container(border=True):
                section('Three layers of protection')
                for num,title,desc in [('01','Agent validation','Theory, sizing, and schema checks before a proposal.'),('02','Independent Guardian','Hard rules, model judgment, and a fresh final check.'),('03','Ledger backstop','Platform policy, available cash, and holdings checks.')]:
                    markup(f'<div class="agent"><div class="agent-icon">{num}</div><div><div class="agent-name">{title}</div><div class="agent-desc">{desc}</div></div></div>')
            gs=b.read_json(ROOT/'state/guardian_state.json')
            if gs.get('lockdown'):
                st.warning('Guardian lockdown: '+str(gs['lockdown']))
            st.link_button('Review human escalations ↗','https://ledger.chanyeun.com',width='stretch')
            st.caption('Human approvals are handled on the Ledger dashboard.')
    elif page == 'Activity':
        heading('A clear trail of every decision.', 'Explore sessions and local audit events. Export evidence for your course report.')
        tabs=st.tabs(['Sessions','Guardian reviews','Agent events','Account snapshot'])
        with tabs[0]:
            rows=[]
            for s in data['sessions']:
                r=dict(s)
                try:
                    duration=(pd.Timestamp(s['session_end_utc'])-pd.Timestamp(s['session_start_utc'])).total_seconds()/60
                    r['recorded_duration_minutes']=round(duration,2)
                    r['within_10_minutes']=duration<=10
                except (KeyError,ValueError):
                    pass
                rows.append(r)
            if rows:
                st.dataframe(pd.DataFrame(rows),hide_index=True,width='stretch')
                if any(r.get('within_10_minutes') is False for r in rows):
                    st.warning('A historical session exceeds 10 minutes by its timestamps. The original record is preserved; requested duration is not elapsed duration.')
                st.download_button('Download sessions CSV',b.csv_bytes(rows),'tradesentry-sessions.csv','text/csv')
            else:
                st.info('No completed sessions yet.')
        with tabs[1]:
            rows=review_table(data['reviews'])
            st.dataframe(decision_frame(rows),hide_index=True,width='stretch')
            st.download_button('Download reviews CSV',b.csv_bytes([{**r,'reason':raw.get('reason','')} for r,raw in zip(rows,reversed(data['reviews']))]),'tradesentry-reviews.csv','text/csv',disabled=not rows)
        with tabs[2]:
            kinds=sorted({e.get('event','') for e in data['trader']})
            selected=st.multiselect('Event types',kinds,default=[k for k in ['proposed','order_outcome','hold','model_decision'] if k in kinds])
            rows=[e for e in reversed(data['trader']) if e.get('event') in selected]
            st.dataframe(pd.DataFrame(rows),hide_index=True,width='stretch')
            st.download_button('Download selected events CSV',b.csv_bytes(rows),'tradesentry-events.csv','text/csv',disabled=not rows)
        with tabs[3]:
            if snap:
                st.caption('Read from Ledger: '+stamp(snap.get('fetched_at'))+' · At most the latest 200 transactions.')
                st.dataframe(pd.DataFrame(snap.get('transactions',[])),hide_index=True,width='stretch')
                st.download_button('Download account transactions',b.csv_bytes(snap.get('transactions',[])),'ledger-transactions.csv','text/csv')
            else:
                st.info('Use Refresh from Ledger on Overview to read the latest account data.')


def setup_page():
    heading('Make this workspace yours.', 'Connect your account, choose your assigned currency, and configure your model.')
    if ctrl.running:
        st.info('Stop the active session before changing configuration.')
        return
    a,z=st.columns([2,1],gap='large')
    with a:
        if st.button('Fetch theory from Ledger', key='fetch_theory'):
            try:
                with st.spinner('Reading your submitted theory…'):
                    fetched = b.fetch_theory_settings()
                st.session_state.update(setup_strategy=fetched['strategy'], setup_symbol=fetched['symbol'],
                                        setup_version=fetched['version'], setup_pin=True,
                                        setup_params=json.dumps(fetched['params'], indent=2),
                                        fetched_theory=fetched)
            except Exception:
                st.error('Could not fetch a supported submitted theory. Check your saved trader key, '
                         'Ledger connection, and submitted theory. Existing fields were not changed.')
        st.caption('Uses your saved trader key. Fetching only fills this form; Save workspace applies the settings locally.')
        fetched = st.session_state.get('fetched_theory')
        if fetched:
            st.caption(f"Fetched theory #{fetched['id']} · version {fetched['version']}. Review before saving.")
            if fetched.get('validation_error'):
                st.warning('Fetched successfully, but local trading validation still rejects it: '
                           + fetched['validation_error'] + '. Fetching does not override safety limits.')
        selected_strategy = st.selectbox('Trading strategy', list(strategy_labels)[:2],
                                        index=0 if (observed_strategy or configured_strategy) == 'momentum' else 1,
                                        format_func=lambda value: strategy_labels[value], key='setup_strategy')
        st.caption('Must match your active Ledger theory. Changing this setting does not change Ledger.')
        with st.form('setup'):
            section('Account & strategy','Match these settings to the theory submitted on Ledger.')
            name=st.text_input('Team member',value=cfg.get('member',''))
            c1,c2=st.columns(2)
            symbol=c1.selectbox('Assigned currency',list(COIN_THEORY),index=list(COIN_THEORY).index(cfg['symbol']),key='setup_symbol')
            version=c2.number_input('Submitted theory version',min_value=1,value=int(cfg['expected_theory']['version']),step=1,key='setup_version')
            label=st.text_input('Trader key label',value=gcfg.get('expected_trader_key_labels',[''])[0])
            pin=st.checkbox('Pin my validated theory parameters',value=bool(cfg['expected_theory'].get('params')),key='setup_pin')
            params_text=st.text_area('Validated theory parameters (JSON)',
                                    value=json.dumps(cfg['expected_theory'].get('params', {}), indent=2),
                                    key='setup_params',
                                    help='Enter your replay-validated lookback_minutes, threshold_pct, position_fraction, stop_loss_pct, and take_profit_pct. Mean-reversion presets are not momentum presets.')
            st.divider()
            section('Language model','Both agents use this provider. Model calls may incur charges.')
            provider=st.selectbox('Provider',list(PROVIDERS),index=list(PROVIDERS).index(cfg['model_adapter']['provider']))
            model=st.text_input('Model ID',value=cfg['model_adapter']['model'],help='Enter the exact model ID supported by your provider. Changing provider does not automatically change this value.')
            st.divider()
            section('Private credentials','Leave a key blank to keep its saved value. Keys are never displayed.')
            st.info('Rotate your Ledger trader, Guardian, and model provider API keys at least every two weeks. '
                    'Stop active sessions, create replacement keys with the same role scopes, update them here, '
                    'verify access, then revoke the old keys in Ledger or your provider dashboard. '
                    'This is a reminder; TradeSentry does not automatically expire keys.')
            trader_key=st.text_input('Ledger trader key',type='password')
            guardian_key=st.text_input('Ledger Guardian key',type='password')
            model_key=st.text_input('Model provider key',type='password')
            submitted=st.form_submit_button('Save workspace',type='primary',width='stretch')
        if submitted:
            env={'meta':'MODEL_API_KEY','gemini':'GEMINI_API_KEY','openai':'OPENAI_API_KEY','anthropic':'ANTHROPIC_API_KEY'}[provider]
            try:
                params = json.loads(params_text) if pin else None
                b.save_setup(name,symbol,version,label,provider,model,pin,{'LEDGER_TRADER_KEY':trader_key,'LEDGER_GUARDIAN_KEY':guardian_key,env:model_key},
                             strategy=selected_strategy, params=params)
                st.success('Workspace saved. Open Overview when you are ready to connect or run a session.')
            except ValueError as e:
                st.error(str(e))
    with z:
        with st.container(border=True):
            section('Before your first session')
            st.markdown('1. Submit and activate your theory on Ledger.\n2. Enable your assigned coin in Security Policy.\n3. Create separate **trader** and **guardian** keys.\n4. Save the matching configuration here.\n5. Start a short session from Overview.')
            st.link_button('Open Ledger ↗','https://ledger.chanyeun.com',width='stretch')
        ready,message=b.readiness()
        st.info(message)
        with st.expander('Your current theory preset'):
            st.json(cfg.get('expected_theory',{}))
        st.caption('Credentials stay in a private local .env file. Exclude it when sharing or submitting this project. Use a separate project copy for each member/account.')
        st.caption('The included history belongs to the original SOL workspace. It is historical evidence, not a live connection or a new account’s results.')


if page=='Live flow':
    from gui.flow_page import show
    show(ctrl,cfg,gcfg)
elif page=='Setup':
    setup_page()
else:
    dashboard()
markup('<div class="footer">LEDGER CONTROL ROOM &nbsp; / &nbsp; Simulated assets. Real accountability.</div>')

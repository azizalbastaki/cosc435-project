"""Streamlit host for the animated concurrent-agent trace."""
import time
from datetime import datetime, timezone
import pandas as pd
import streamlit as st
from gui import backend, flow


def show(controller, cfg, gcfg):
    st.title('Live flow')
    st.caption('Two agents, their APIs, and the evidence behind each outcome. Click a diagram node to learn its job.')
    mode=st.radio('Flow mode',['Live now','Replay saved history'],horizontal=True,key='flow_mode')
    models={role: f"{c['model_adapter']['provider']} · {c['model_adapter']['model']}" for role,c in [('trader',cfg),('guardian',gcfg)]}
    if mode=='Live now':
        st.caption('Monitoring sessions started from this dashboard. Updates every 2 seconds; opening this view makes no API calls.')
        @st.fragment(run_every='2s')
        def live():
            state=controller.status()
            all_rows=flow.timeline()
            # A new GUI session must not inherit unmatched calls from an older session.
            since=(controller.started or time.time())*1000
            rows=[e for e in all_rows if e['time']>=since] if state['running'] else all_rows[-100:]
            if not state['running']:
                st.info('Both agents are idle in this dashboard. Use Replay saved history to animate the recorded SOL session, or start a configured session from Overview.')
            st.iframe(flow.render(rows,'live',state['running'],time.time()*1000,models),height=1120)
        live()
    else:
        rows=flow.timeline()
        if not rows:
            st.info('No saved events yet. A session will create a replayable trace.')
            return
        groups=list(dict.fromkeys(e['group'] for e in rows))
        def label(group):
            first=next(e for e in rows if e['group']==group)
            date=datetime.fromtimestamp(first['time']/1000,timezone.utc).strftime('%d %b %Y %H:%M UTC')
            return date+' · '+('Legacy audit history' if group.startswith('legacy-') else 'Session '+group)
        group=st.selectbox('Recorded session',groups,index=len(groups)-1,format_func=label,key='flow_session')
        chosen=[e for e in rows if e['group']==group]
        st.caption('Play, pause, step, or drag through the recorded sequence. Replay is visual only: it never resubmits a trade or calls an API.')
        recorded_models={}
        for role in ['trader','guardian']:
            candidates=[e for e in chosen if e['role']==role and e['event'] in {'session_start','model_request_start','model_decision'} and e.get('model')]
            recorded_models[role]=str(candidates[-1]['model']) if candidates else 'Model not recorded'
        st.iframe(flow.render(chosen,'replay',False,time.time()*1000,recorded_models),height=1120)
        calls=[e for e in chosen if e['event'] in {'api_end','model_request_end'}]
        with st.expander('API call ledger · endpoints, latency, and returned outcomes'):
            if calls:
                table=[{'Role':e['role'],'API':e.get('endpoint'), 'HTTP':e.get('http_status'),
                        'Duration (ms)':e.get('duration_ms'), 'Outcome':e['outcome'], 'Purpose':e['why']} for e in calls]
                st.dataframe(pd.DataFrame(table),hide_index=True,width='stretch')
                st.download_button('Download API trace CSV',backend.csv_bytes(table),'tradesentry-api-trace.csv','text/csv')
            else:
                st.info('These older logs predate API tracing. New sessions record endpoint names, start/end times, latency, and safe response summaries.')
    with st.expander('How to read the flow'):
        st.markdown('''- **Concurrent lanes:** the trader and Guardian run independently. Only recorded, unmatched API starts count as in-flight work; completed calls are not shown as still running.
- **Emerald / blue dots:** trader / Guardian activity. In replay, they highlight the selected recorded transition. Exact API timing is available only for new sessions.
- **Model response ≠ accepted order:** returned JSON still passes local validation. The inspector marks it as unvalidated until the later decision event.
- **APPROVE ≠ EXECUTED:** an approval requests a fill; Ledger has the final say. The outcome node shows the recorded platform result.
- **Reasoning:** the inspector shows the model's returned explanation, deterministic rule findings, sizing bounds, and recorded evidence—not private internal model thoughts.
- **Missing completion:** calls without an end event become unknown after three minutes or when the session stops. The diagram does not invent a result.
- **Privacy:** API tracing excludes keys, headers, full prompts, and raw provider response bodies.''')

    with st.expander('API glossary · what each endpoint does'):
        st.dataframe(pd.DataFrame([{'API endpoint':key,'Purpose':value} for key,value in flow.PURPOSES.items()]),hide_index=True,width='stretch')
        st.caption('Model calls use the configured provider: /chat/completions for the current Meta/OpenAI-compatible adapter, or /messages for Anthropic. Exact calls appear in new traces.')

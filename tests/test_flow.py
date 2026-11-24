"""Telemetry and timeline tests. Network responses are local stubs only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from common.ledger_client import LedgerClient, UncertainWrite
from common.llm import ModelAdapter, ModelFailure
from common.util import AuditLog
from gui import flow


class FlowTests(unittest.TestCase):
    def test_api_trace_pairs_success_without_credentials_or_reasoning(self):
        events=[]
        c=LedgerClient('https://ledger.example/api/v1','TEST_SECRET',log=lambda event,**kw:events.append({'event':event,**kw}))
        body={'action':'BUY','quantity':'1','reasoning':'UNTRUSTED_HIDDEN','api_key':'BODY_SECRET'}
        with patch.object(c,'_request_untraced',return_value={'transaction_id':7,'status':'PENDING','private':'DO_NOT_LOG'}):
            c.post('trade_propose.php',body)
        self.assertEqual([e['event'] for e in events],['api_start','api_end'])
        self.assertEqual(events[0]['request_id'],events[1]['request_id'])
        text=json.dumps(events)
        for secret in ['TEST_SECRET','BODY_SECRET','DO_NOT_LOG','UNTRUSTED_HIDDEN']:
            self.assertNotIn(secret,text)
        self.assertEqual(events[-1]['summary']['status'],'PENDING')

    def test_uncertain_write_records_unknown_without_retry(self):
        events=[]
        c=LedgerClient('https://ledger.example/api/v1','test',log=lambda event,**kw:events.append({'event':event,**kw}))
        with patch.object(c,'_request_untraced',side_effect=UncertainWrite('lost')) as write:
            with self.assertRaises(UncertainWrite): c.post('trade_propose.php',{})
            self.assertEqual(write.call_count,1)
        self.assertEqual(events[-1]['outcome'],'uncertain')

    def test_observability_cannot_change_write_result(self):
        def broken(*a,**k): raise OSError('log inaccessible')
        c=LedgerClient('https://ledger.example/api/v1','test',log=broken)
        with patch.object(c,'_request_untraced',return_value={'status':'PENDING'}):
            self.assertEqual(c.post('trade_propose.php',{})['status'],'PENDING')

    def test_model_return_is_marked_unvalidated(self):
        events=[]
        m=ModelAdapter({},log=lambda event,**kw:events.append({'event':event,**kw}))
        with patch.object(m,'_ask_json_untraced',return_value={'action':'BUY','reasoning':'NOT_IN_API_TRACE'}):
            m.ask_json('SECRET_SYSTEM',{'api_key':'SECRET_PAYLOAD'},{})
        self.assertEqual(events[-1]['outcome'],'returned_unvalidated')
        self.assertNotIn('SECRET',json.dumps(events))
        self.assertNotIn('NOT_IN_API_TRACE',json.dumps(events))

    def test_model_error_closes_span(self):
        events=[]
        m=ModelAdapter({},log=lambda event,**kw:events.append({'event':event,**kw}))
        with patch.object(m,'_ask_json_untraced',side_effect=ModelFailure('error')):
            with self.assertRaises(ModelFailure):m.ask_json('',{},{})
        self.assertEqual(events[-1]['event'],'model_request_end')
        self.assertEqual(events[-1]['outcome'],'error')

    def test_concurrent_calls_require_two_unmatched_starts(self):
        rows=[{'role':'trader','event':'api_start','request_id':'t','time':1000},
              {'role':'guardian','event':'model_request_start','request_id':'g','time':1100}]
        self.assertEqual(len(flow.active_requests(rows,1500,True)),2)
        rows.append({'role':'trader','event':'api_end','request_id':'t','time':1200})
        self.assertEqual(len(flow.active_requests(rows,1500,True)),1)
        self.assertEqual(flow.active_requests(rows,1500,False),[])
        self.assertEqual(flow.active_requests(rows,200000,True),[])

    def test_script_injection_is_escaped(self):
        html=flow.render([{'reasoning':'</script><script>EVIL</script>'}],'replay',False,0,{})
        self.assertNotIn('</script><script>EVIL',html)
        self.assertIn('\\u003c/script',html)

    def test_audit_has_precise_timestamp_and_identity(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'guardian.jsonl'
            AuditLog(p,quiet=True)('heartbeat',pending=0)
            e=json.loads(p.read_text())
            self.assertEqual(e['role'],'guardian')
            self.assertIn('process_id',e)
            self.assertIn('.',e['ts'])

if __name__=='__main__':unittest.main()

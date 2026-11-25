"""Offline dashboard checks. Never calls Ledger or a model provider."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from gui import backend
from common.util import load_private_env
import run_session


class DashboardTests(unittest.TestCase):
    def test_setup_preserves_momentum_and_custom_pins(self):
        params = {'lookback_minutes': 10, 'threshold_pct': 0.5, 'position_fraction': 0.05,
                  'stop_loss_pct': 2, 'take_profit_pct': 5}
        old = {'symbol': 'ETH', 'expected_theory': {'strategy': 'momentum', 'params': params}}
        with tempfile.TemporaryDirectory() as d, patch.object(backend, 'ROOT', Path(d)), \
                patch.object(backend, 'config', return_value=old), \
                patch.object(backend, 'credentials', return_value={}), \
                patch('tools.configure.write_configs') as write:
            backend.save_setup('Member', 'ETH', 1, 'eth-trader', 'meta', 'muse-spark-1.3', True, {})
        self.assertEqual(write.call_args.kwargs, {'strategy': 'momentum', 'params': params})

    def test_csv_escapes_formulas(self):
        text = backend.csv_bytes([{'reason': '=1+1', 'id': 7}]).decode()
        self.assertIn("'=1+1", text)

    def test_partial_log_is_ignored(self):
        with tempfile.TemporaryDirectory() as d, patch.object(backend, 'ROOT', Path(d)):
            p = Path(d)/'logs'
            p.mkdir()
            (p/'guardian.jsonl').write_text('{"event":"review"}\n{"event":')
            self.assertEqual(len(backend.events('guardian')),1)

    def test_role_does_not_load_other_key(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True):
            (Path(d)/'.env').write_text('LEDGER_TRADER_KEY=test-trader\nLEDGER_GUARDIAN_KEY=test-guardian\nMODEL_API_KEY=test-model\n')
            load_private_env(d, role='trader')
            self.assertNotIn('LEDGER_GUARDIAN_KEY',os.environ)
            self.assertIn('LEDGER_TRADER_KEY',os.environ)

    def test_missing_keys_never_launch(self):
        c = backend.SessionController()
        with patch.object(backend,'readiness',return_value=(False,'Missing keys')), patch.object(backend.subprocess,'Popen') as launch:
            with self.assertRaises(ValueError):
                c.start(10)
            launch.assert_not_called()

    def test_duplicate_start_and_stop(self):
        c=backend.SessionController()
        class Running:
            def poll(self): return None
            def terminate(self): self.terminated=True
        c.process=Running()
        with self.assertRaises(ValueError): c.start(1)
        c.stop()
        self.assertTrue(c.process.terminated)

    def test_runner_deadline_and_separate_role_environments(self):
        clock=[0.0]
        spawned=[]
        class Process:
            def __init__(self,args,**kwargs):
                self.returncode=None
                self.pid=12345
                self.args,self.env=args,kwargs.get('env',{})
                spawned.append(self)
            def poll(self): return self.returncode
            def terminate(self): self.returncode=-15
            def wait(self,timeout=None): return self.returncode
            def kill(self): self.returncode=-9
        def sleep(seconds): clock[0]+=seconds
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for role in ['trading_agent','guardian']:
                (root/role).mkdir()
                (root/role/'config.json').write_text(json.dumps(backend.config(role)))
            with patch.object(run_session,'ROOT',root), patch.object(run_session,'load_private_env'), \
                 patch.dict(os.environ,{'LEDGER_TRADER_KEY':'test-t','LEDGER_GUARDIAN_KEY':'test-g','MODEL_API_KEY':'test-m'}), \
                 patch.object(sys,'argv',['run_session.py','--minutes','1','--skip-tests']), \
                 patch.object(run_session.subprocess,'Popen',Process), patch.object(run_session.time,'monotonic',lambda:clock[0]), \
                 patch.object(run_session.time,'sleep',sleep), patch.object(run_session.signal,'signal'), \
                 patch.object(run_session.shutil,'which',return_value=None), contextlib.redirect_stdout(io.StringIO()):
                run_session.main()
            self.assertEqual(len(spawned),2)
            self.assertLessEqual(clock[0],60.25)
            self.assertTrue(all(p.returncode == -15 for p in spawned))
            self.assertNotIn('LEDGER_TRADER_KEY',spawned[0].env)
            self.assertNotIn('LEDGER_GUARDIAN_KEY',spawned[1].env)


if __name__=='__main__': unittest.main()

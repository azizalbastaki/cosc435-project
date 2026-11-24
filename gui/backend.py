"""Read-only dashboard data and explicit, bounded session controls."""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def events(role):
    p = ROOT / 'logs' / f'{role}.jsonl'
    if not p.exists():
        return []
    out = []
    for line in p.read_text(errors='replace').splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                out.append(value)
        except ValueError:
            continue  # an agent may still be writing the last line
    return out


def config(role):
    p = ROOT / role / 'config.json'
    return read_json(p if p.exists() else ROOT / role / 'config.example.json')


def credentials():
    values = {}
    for filename in ['private.env', '.env']:
        p = ROOT / filename
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if line.strip().startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            v = v.split(' #')[0].strip().strip('\"\'')
            if v and not v.startswith(('PASTE', 'PLACEHOLDER')):
                values[k.strip().removeprefix('export ')] = v
    allowed = {'LEDGER_TRADER_KEY', 'LEDGER_GUARDIAN_KEY'}
    allowed.update(config(r).get('model_adapter', {}).get('api_key_env', 'MODEL_API_KEY') for r in ['trading_agent', 'guardian'])
    for k in allowed:
        if os.environ.get(k):
            values[k] = os.environ[k]
    return {k: v for k, v in values.items() if k in allowed}


def readiness():
    keys = credentials()
    needed = ['LEDGER_TRADER_KEY', 'LEDGER_GUARDIAN_KEY']
    needed += [config(r).get('model_adapter', {}).get('api_key_env', 'MODEL_API_KEY') for r in ['trading_agent', 'guardian']]
    missing = [k for k in dict.fromkeys(needed) if not keys.get(k)]
    if missing:
        names = {'LEDGER_TRADER_KEY': 'Ledger trader key', 'LEDGER_GUARDIAN_KEY': 'Ledger Guardian key'}
        return False, 'Missing: ' + ', '.join(names.get(k, 'model key') for k in missing) + '. Add in Setup.'
    if keys['LEDGER_TRADER_KEY'] == keys['LEDGER_GUARDIAN_KEY']:
        return False, 'Trader and Guardian must have different role-scoped keys.'
    if any(not (ROOT / r / 'config.json').exists() for r in ['trading_agent', 'guardian']):
        return False, 'Save your configuration in Setup first.'
    return True, 'Configuration ready. Key validity is checked when connecting or starting.'


def save_setup(member, symbol, version, label, provider, model, pin, new_keys):
    from tools.configure import write_configs
    if not member.strip() or not label.strip() or not model.strip():
        raise ValueError('Enter your name, trader key label, and model name.')
    env_name = {'meta': 'MODEL_API_KEY', 'gemini': 'GEMINI_API_KEY', 'openai': 'OPENAI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY'}[provider]
    for value in new_keys.values():
        if value and (any(c.isspace() for c in value) or value.startswith(('PASTE', 'PLACEHOLDER'))):
            raise ValueError('Paste complete keys without spaces or line breaks.')
    existing = credentials()
    values = {**existing, **{k: v for k, v in new_keys.items() if v}}
    if values.get('LEDGER_TRADER_KEY') and values.get('LEDGER_TRADER_KEY') == values.get('LEDGER_GUARDIAN_KEY'):
        raise ValueError('Use different trader and Guardian keys.')
    old = config('trading_agent')
    # Never attach another member's account to old recovery state.
    if (ROOT / 'state/trading_agent_state.json').exists() and (
        symbol != old.get('symbol') or any(values.get(k) != existing.get(k) for k in ['LEDGER_TRADER_KEY', 'LEDGER_GUARDIAN_KEY'] if existing.get(k))
    ):
        raise ValueError('Use a separate copy of this project for another coin or account, so recovery state and results stay separate.')
    write_configs(symbol, int(version), label.strip(), provider, model.strip(), env_name, pin, member.strip())
    if any(new_keys.values()):
        p = ROOT / '.env'
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write('# Private credentials. Never include this file in a submission.\n')
            for key, value in values.items():
                f.write(f'{key}={value}\n')
        os.chmod(p, 0o600)


def overview():
    trader, guardian = events('trading_agent'), events('guardian')
    reviews = [e for e in guardian if e.get('event') == 'review']
    samples = [e for e in trader if e.get('event') == 'portfolio_sample' and e.get('total_value') is not None]
    sessions = []
    p = ROOT / 'logs/sessions.csv'
    if p.exists():
        with p.open() as f:
            sessions = list(csv.DictReader(f))
    return dict(trader=trader, guardian=guardian, reviews=reviews, samples=samples, sessions=sessions,
                decisions=Counter(e.get('decision') for e in reviews), snapshot=read_json(ROOT / 'state/dashboard_snapshot.json'))


def sync_account():
    """Explicit GET requests only. No proposals, reviews or model calls."""
    from common.ledger_client import LedgerClient
    keys = credentials()
    cfg = config('trading_agent')
    if not keys.get('LEDGER_TRADER_KEY'):
        raise ValueError('Add your trader key in Setup to connect.')
    client = LedgerClient(cfg['base_url'], keys['LEDGER_TRADER_KEY'])
    data = {'portfolio': client.portfolio(), 'policy': client.policy(), 'theory': client.theory(),
            'transactions': client.transactions(limit=200), 'guardian_logs': client.guardian_logs(limit=200),
            'quotes': client.quotes([cfg['symbol']]), 'fetched_at': time.time()}
    p = ROOT / 'state/dashboard_snapshot.json'
    p.parent.mkdir(exist_ok=True)
    temp = p.with_suffix('.tmp')
    temp.write_text(json.dumps(data, allow_nan=False))
    temp.replace(p)
    return data


def csv_bytes(rows):
    if not rows:
        return b''
    fields = list(dict.fromkeys(k for row in rows for k in row))
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        # Spreadsheet applications must treat untrusted text as text, not formulas.
        writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for k, v in row.items()})
    return output.getvalue().encode()


class SessionController:
    """A single controller shared by browser sessions; never launches on page load."""
    def __init__(self):
        self.process = None
        self.started = None
        self.minutes = 10
        self.unlimited = False
        self.lock = threading.Lock()
        self.stopped = False

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self, minutes, unlimited=False):
        with self.lock:
            if self.running:
                raise ValueError('A session is already running.')
            if not unlimited and not 1 <= minutes <= 10:
                raise ValueError('Choose a duration from 1 to 10 minutes.')
            ready, message = readiness()
            if not ready:
                raise ValueError(message)
            env = dict(os.environ)
            env.update(credentials())
            env['PYTHONUNBUFFERED'] = '1'
            (ROOT / 'logs').mkdir(exist_ok=True)
            with (ROOT / 'logs/dashboard_runner.out').open('a') as log:
                self.process = subprocess.Popen([sys.executable, str(ROOT / 'run_session.py')] + (['--unlimited'] if unlimited else ['--minutes', str(minutes)]),
                                                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            self.started, self.minutes, self.stopped, self.unlimited = time.time(), minutes, False, unlimited

    def stop(self):
        with self.lock:
            if self.running:
                self.stopped = True
                self.process.terminate()  # runner forwards termination to both agents

    def status(self):
        running = self.running
        elapsed = time.time() - self.started if running and self.started else 0
        remaining = 0 if self.unlimited else max(0, self.minutes * 60 - elapsed) if running and self.started else 0
        return dict(running=running, remaining=remaining, elapsed=elapsed, unlimited=self.unlimited, exit_code=self.process.poll() if self.process else None,
                    launched=self.process is not None, stopped=self.stopped)

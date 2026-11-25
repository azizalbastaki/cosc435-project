"""TradeSentry presentation tokens, shared by Streamlit, charts, and the flow iframe."""
from pathlib import Path

NAME = 'TradeSentry'
TAGLINE = 'Trade intelligently. Stay protected.'
DESCRIPTION = 'Autonomous trading with independent AI security supervision.'
COLORS = dict(midnight='#0B1220', slate='#151F30', emerald='#38E5AD', ice='#F4F7FB',
              steel='#94A3B8', graphite='#243247', approve='#38E5AD', modify='#60A5FA',
              escalate='#FBBF24', block='#F87171')
FONT = 'Inter, sans-serif'
MONO = 'JetBrains Mono, monospace'
ASSETS = Path(__file__).with_name('assets')
ICON = (ASSETS / 'logo.svg').read_text()
WORDMARK = (ASSETS / 'wordmark.svg').read_text()


def css():
    variables = ';'.join(f'--{name}:{value}' for name, value in COLORS.items())
    return ("@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');"
            f':root{{{variables};--font:{FONT};--mono:{MONO};color-scheme:dark;}}')


def decision_style(value):
    color = COLORS.get(str(value).lower())
    return f'color: {color}; font-weight: 600' if color else ''

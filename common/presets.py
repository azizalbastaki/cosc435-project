"""Team presets: the validated theory settings per coin (research of 24 Nov 2026) and model providers.

The trading agent pins these numbers by default: if the theory submitted on Ledger has different
settings, the agent HOLDs and asks you to re-validate (or to configure with --no-pin)."""

COIN_THEORY = {   # strategy = mean_reversion, lookback 1440 for every coin
    "BTC":  {"lookback_minutes": 1440, "threshold_pct": 4.5,  "position_fraction": 0.09, "stop_loss_pct": 3.5, "take_profit_pct": 12},
    "ETH":  {"lookback_minutes": 1440, "threshold_pct": 6.5,  "position_fraction": 0.09, "stop_loss_pct": 3.5, "take_profit_pct": 20},
    "DOGE": {"lookback_minutes": 1440, "threshold_pct": 11,   "position_fraction": 0.09, "stop_loss_pct": 5,   "take_profit_pct": 30},
    "XMR":  {"lookback_minutes": 1440, "threshold_pct": 3.75, "position_fraction": 0.09, "stop_loss_pct": 3,   "take_profit_pct": 10},
    "XRP":  {"lookback_minutes": 1440, "threshold_pct": 7.25, "position_fraction": 0.09, "stop_loss_pct": 4.5, "take_profit_pct": 20},
    "SOL":  {"lookback_minutes": 1440, "threshold_pct": 9,    "position_fraction": 0.09, "stop_loss_pct": 5,   "take_profit_pct": 30},
}

# daily low / high of each coin's 24 h wave (UTC); trade sessions ~0-2 h around them
COIN_WINDOWS_UTC = {"BTC": ("09:13", "21:13"), "ETH": ("06:32", "18:32"), "DOGE": ("03:52", "15:52"),
                    "XMR": ("01:12", "13:12"), "XRP": ("22:31", "10:31"), "SOL": ("19:51", "07:51")}

PROVIDERS = {   # provider -> (default model, key environment variable, where to get a key)
    "meta":      ("muse-spark-1.3", "MODEL_API_KEY", "https://dev.meta.ai  (Meta Model API -> API keys)"),
    "gemini":    ("gemini-3.8-flash", "GEMINI_API_KEY", "https://aistudio.google.com  (Get API key; free tier)"),
    "openai":    ("", "OPENAI_API_KEY", "https://platform.openai.com/api-keys"),
    "anthropic": ("", "ANTHROPIC_API_KEY", "https://console.anthropic.com"),
}

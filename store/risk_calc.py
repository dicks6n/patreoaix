# store/risk_calc.py
"""
Position size for a signal: "With your $200 balance use 0.02 lots".

lots = balance × risk% ÷ (what 1 lot loses at the stop), rounded down to the 0.01 step.
The loss per lot comes from the instrument's contract size (mt5_service.calc_profit), so gold, indices, crypto and
JPY pairs are all sized correctly.
"""
import math

from . import mt5_service as mt5

BALANCES = (100, 500, 1000)


def risk_per_lot(symbol, direction, entry, stop):
    try:
        return abs(float(mt5.calc_profit(symbol, direction, 1, entry, stop))) or None
    except Exception:
        return None


def lots_for(balance, risk_pct, per_lot, volume_min=0.01):
    """→ (lots, money at risk) or (None, money at risk) when even the minimum lot risks more than allowed."""
    risk = float(balance) * float(risk_pct) / 100
    if not per_lot:
        return None, risk
    lots = math.floor(risk / per_lot * 100) / 100
    return (lots if lots >= volume_min else None), risk


def caption_line(sig, risk_pct=1):
    """One line for Telegram: '🧮 1% risk: $100 → 0.01 lots · $500 → 0.05 · $1,000 → 0.10'."""
    per_lot = risk_per_lot(sig.symbol, sig.direction, sig.entry, sig.stop_loss)
    if not per_lot:
        return ''
    parts = []
    for b in BALANCES:
        lots, _ = lots_for(b, risk_pct, per_lot)
        parts.append(f"${b:,} → {lots:.2f}" if lots else f"${b:,} → too small")
    return f"🧮 {risk_pct:g}% risk lot size: " + ' · '.join(parts)

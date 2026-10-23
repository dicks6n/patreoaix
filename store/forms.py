# store/forms.py
from django import forms

class CreateBotForm(forms.Form):
    BOT_STRATEGIES = [
        ('volatility_hunter', 'AI Volatility Hunter v2.1'),
        ('vix_basis', 'VIX Futures Basis Arbitrage'),
        ('volatility_carry', 'Volatility Carry Strategy'),
        ('digit_switcher', 'Deriv Digit Frequency Bot'),
    ]

    bot_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-white text-xs', 'placeholder': 'My Custom Bot'})
    )
    strategy = forms.ChoiceField(
        choices=BOT_STRATEGIES,
        widget=forms.Select(attrs={'class': 'w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-white text-xs'})
    )
    trading_pair = forms.CharField(
        max_length=20,
        initial='R_100',
        widget=forms.TextInput(attrs={'class': 'w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-white text-xs'})
    )
    stake_amount = forms.DecimalField(
        max_digits=10, decimal_places=2, initial=10.00,
        widget=forms.NumberInput(attrs={'class': 'w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-white text-xs'})
    )
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
import logging

logger = logging.getLogger(__name__)

class DerivMultiContractAIEngine:
    """
    Comprehensive AI Engine for Deriv Synthetic & Volatility Indices.
    Supports Rise/Fall, Even/Odd, Over/Under, and Matches/Differs contract predictions.
    """

    SUPPORTED_INDICES = [
        'R_100', 'R_75', 'R_50', 'R_25', 'R_10',
        '1HZ100V', '1HZ75V', '1HZ50V', '1HZ25V', '1HZ10V',
        'JD10', 'JD25', 'JD50', 'JD75', 'JD100', 'stpRNG'
    ]

    # Real (non-synthetic) markets: Deriv's forex/metals feed using their
    # standard "frx" symbol prefix. These are genuine market prices, so the
    # last digit is NOT uniformly random the way it is on synthetic indices -
    # only Rise/Fall (direction) predictions are statistically meaningful here.
    REAL_MARKET_SYMBOLS = [
        'frxXAUUSD',  # Gold vs USD
        'frxXAGUSD',  # Silver vs USD
        'frxEURUSD',
        'frxUSDJPY',
        'frxGBPUSD',
        'frxAUDUSD',
        'frxUSDCAD',
        'frxUSDCHF',
        'frxNZDUSD',
    ]

    @classmethod
    def is_real_market(cls, symbol: str) -> bool:
        """True for real forex/metals symbols (Gold, EUR/USD, USD/JPY, etc.)."""
        return symbol in cls.REAL_MARKET_SYMBOLS

    @classmethod
    def is_synthetic(cls, symbol: str) -> bool:
        """True for Deriv synthetic/volatility indices."""
        return symbol in cls.SUPPORTED_INDICES

    def __init__(self):
        self.rf_models = {}  # Rise/Fall ML models per symbol
        self.eo_models = {}  # Even/Odd ML models per symbol

    @staticmethod
    def extract_last_digit(price) -> int:
        """Extracts the exact last digit from a tick price."""
        price_str = f"{float(price):.4f}".rstrip('0').rstrip('.')
        if '.' in price_str:
            return int(price_str[-1])
        return int(str(int(float(price)))[-1])

    def build_feature_matrix(self, tick_prices: list) -> pd.DataFrame:
        """Engineers feature matrix for digit patterns and price trends."""
        if len(tick_prices) < 15:
            return pd.DataFrame()

        prices = pd.Series(tick_prices)
        digits = [self.extract_last_digit(p) for p in tick_prices]
        parities = [1 if d % 2 == 0 else 0 for d in digits]

        df = pd.DataFrame({
            'price': prices,
            'digit': digits,
            'parity': parities,
            'price_change': prices.diff(),
            'direction': np.where(prices.diff() > 0, 1, 0)
        })

        # Feature Set A: Direction & Momentum (Rise/Fall)
        df['ema_5'] = df['price'].ewm(span=5, adjust=False).mean()
        df['ema_10'] = df['price'].ewm(span=10, adjust=False).mean()
        df['ema_diff'] = df['ema_5'] - df['ema_10']
        df['momentum_3'] = df['price'] - df['price'].shift(3)

        # Feature Set B: Digit Lags & Streaks (Even/Odd & Over/Under)
        for i in range(1, 6):
            df[f'lag_digit_{i}'] = df['digit'].shift(i)
            df[f'lag_parity_{i}'] = df['parity'].shift(i)

        df['even_ratio_5'] = df['parity'].rolling(window=5).mean()
        df['even_ratio_10'] = df['parity'].rolling(window=10).mean()

        # Digit Frequency Distribution (Last 15 Ticks)
        for d in range(10):
            df[f'freq_digit_{d}'] = df['digit'].rolling(15).apply(lambda s: (s == d).sum(), raw=True)

        return df.dropna().reset_index(drop=True)

    # -----------------------------------------------------------------
    # 1. RISE / FALL PREDICTION ENGINE (CALL / PUT)
    # -----------------------------------------------------------------
    def predict_rise_fall(self, df: pd.DataFrame, tick_prices: list) -> dict:
        """Predicts price direction: CALL (Rise) or PUT (Fall)."""
        recent_prices = tick_prices[-10:]
        price_diffs = np.diff(recent_prices)
        up_ticks = np.sum(price_diffs > 0)
        down_ticks = np.sum(price_diffs < 0)
        total = len(price_diffs)

        ema_5 = df['ema_5'].iloc[-1]
        ema_10 = df['ema_10'].iloc[-1]
        last_price = tick_prices[-1]

        # Calculate directional probability
        base_call_prob = (up_ticks / total) if total > 0 else 0.5
        trend_boost = 0.15 if ema_5 > ema_10 and last_price > ema_5 else (-0.15 if ema_5 < ema_10 else 0)
        call_prob = np.clip(base_call_prob + trend_boost, 0.05, 0.95)

        if call_prob >= 0.5:
            contract = 'CALL'
            confidence = call_prob * 100
        else:
            contract = 'PUT'
            confidence = (1 - call_prob) * 100

        return {
            'contract': contract,
            'confidence': f"{confidence:.1f}%",
            'raw_confidence': round(float(confidence), 1)
        }

    # -----------------------------------------------------------------
    # 2. EVEN / ODD PREDICTION ENGINE (DIGITEVEN / DIGITODD)
    # -----------------------------------------------------------------
    def predict_even_odd(self, df: pd.DataFrame, tick_prices: list) -> dict:
        """Predicts digit parity: DIGITEVEN or DIGITODD."""
        digits = [self.extract_last_digit(p) for p in tick_prices[-20:]]
        even_count = sum(1 for d in digits if d % 2 == 0)
        even_ratio = even_count / len(digits)

        # Incorporate rolling parity features
        even_ratio_5 = df['even_ratio_5'].iloc[-1]
        combined_prob = (even_ratio * 0.4) + (even_ratio_5 * 0.6)

        if combined_prob >= 0.5:
            contract = 'DIGITEVEN'
            confidence = combined_prob * 100
        else:
            contract = 'DIGITODD'
            confidence = (1 - combined_prob) * 100

        return {
            'contract': contract,
            'confidence': f"{confidence:.1f}%",
            'raw_confidence': round(float(confidence), 1)
        }

    # -----------------------------------------------------------------
    # 3. OVER / UNDER PREDICTION ENGINE (DIGITOVER / DIGITUNDER)
    # -----------------------------------------------------------------
    def predict_over_under(self, tick_prices: list) -> dict:
        """Determines best barrier digit and predicts DIGITOVER or DIGITUNDER."""
        digits = [self.extract_last_digit(p) for p in tick_prices[-25:]]
        
        # Test default middle barriers (Over 4 / Under 5)
        over_4_prob = sum(1 for d in digits if d > 4) / len(digits)
        under_5_prob = sum(1 for d in digits if d < 5) / len(digits)

        if over_4_prob >= under_5_prob:
            contract = 'DIGITOVER'
            barrier = 4
            confidence = over_4_prob * 100
        else:
            contract = 'DIGITUNDER'
            barrier = 5
            confidence = under_5_prob * 100

        return {
            'contract': contract,
            'barrier': barrier,
            'confidence': f"{confidence:.1f}%",
            'raw_confidence': round(float(confidence), 1)
        }

    # -----------------------------------------------------------------
    # 4. MATCHES / DIFFERS PREDICTION ENGINE (DIGITMATCH / DIGITDIFF)
    # -----------------------------------------------------------------
    def predict_matches_differs(self, tick_prices: list) -> dict:
        """Identifies most probable target digit (MATCH) and least probable digit (DIFF)."""
        digits = [self.extract_last_digit(p) for p in tick_prices[-30:]]
        counts = pd.Series(digits).value_counts()

        most_frequent_digit = int(counts.idxmax())
        least_frequent_digit = int(counts.idxmin())

        match_prob = counts[most_frequent_digit] / len(digits)
        differ_prob = 1.0 - (counts[least_frequent_digit] / len(digits))

        return {
            'match': {
                'contract': 'DIGITMATCH',
                'target_digit': most_frequent_digit,
                'confidence': f"{(match_prob * 100):.1f}%",
                'raw_confidence': round(float(match_prob * 100), 1)
            },
            'differ': {
                'contract': 'DIGITDIFF',
                'differ_digit': least_frequent_digit,
                'confidence': f"{(differ_prob * 100):.1f}%",
                'raw_confidence': round(float(differ_prob * 100), 1)
            }
        }

    # -----------------------------------------------------------------
    # 5. REAL MARKET PREDICTION ENGINE (GOLD / FOREX - RISE/FALL ONLY)
    # -----------------------------------------------------------------
    def predict_real_market(self, symbol: str, tick_prices: list) -> dict:
        """
        Predicts direction only, for real (non-synthetic) markets such as
        Gold or forex pairs. Digit-based contracts are deliberately not
        offered here - see REAL_MARKET_SYMBOLS docstring above for why.
        """
        if not tick_prices or len(tick_prices) < 15:
            return {'error': 'Insufficient tick price data provided'}

        last_price = float(tick_prices[-1])
        df = self.build_feature_matrix(tick_prices)
        if df.empty:
            return {'error': 'Failed to build feature matrix'}

        rise_fall = self.predict_rise_fall(df, tick_prices)

        return {
            'symbol': symbol,
            'last_price': last_price,
            'predictions': {'rise_fall': rise_fall},
            'top_signal': {
                'category': 'rise_fall',
                'contract_type': rise_fall['contract'],
                'barrier': None,
                'confidence': rise_fall['confidence'],
                'raw_confidence': rise_fall['raw_confidence'],
            }
        }

    # -----------------------------------------------------------------
    # MAIN PREDICTION ENTRY POINT (ALL CONTRACT TYPES)
    # -----------------------------------------------------------------
    def predict_all_contracts(self, symbol: str, tick_prices: list) -> dict:
        """
        Generates comprehensive AI predictions for ALL Deriv trade contracts.
        Routes real markets (Gold/forex) to the Rise/Fall-only engine, since
        digit-based contracts don't apply to them.
        """
        if self.is_real_market(symbol):
            return self.predict_real_market(symbol, tick_prices)

        if not tick_prices or len(tick_prices) < 15:
            return {'error': 'Insufficient tick price data provided'}

        last_price = float(tick_prices[-1])
        last_digit = self.extract_last_digit(last_price)

        df = self.build_feature_matrix(tick_prices)
        if df.empty:
            return {'error': 'Failed to build feature matrix'}

        # Calculate individual predictions
        rise_fall = self.predict_rise_fall(df, tick_prices)
        even_odd = self.predict_even_odd(df, tick_prices)
        over_under = self.predict_over_under(tick_prices)
        matches_differs = self.predict_matches_differs(tick_prices)

        # Evaluate candidate signals to determine the single strongest trade
        candidates = [
            {'category': 'rise_fall', 'contract': rise_fall['contract'], 'confidence': rise_fall['raw_confidence'], 'barrier': None},
            {'category': 'even_odd', 'contract': even_odd['contract'], 'confidence': even_odd['raw_confidence'], 'barrier': None},
            {'category': 'over_under', 'contract': over_under['contract'], 'confidence': over_under['raw_confidence'], 'barrier': over_under['barrier']},
            {'category': 'matches_differs', 'contract': matches_differs['differ']['contract'], 'confidence': matches_differs['differ']['raw_confidence'], 'barrier': matches_differs['differ']['differ_digit']},
        ]

        top_signal = max(candidates, key=lambda c: c['confidence'])

        return {
            'symbol': symbol,
            'last_price': last_price,
            'last_digit': last_digit,
            'predictions': {
                'rise_fall': rise_fall,
                'even_odd': even_odd,
                'over_under': over_under,
                'matches_differs': matches_differs
            },
            'top_signal': {
                'category': top_signal['category'],
                'contract_type': top_signal['contract'],
                'barrier': top_signal['barrier'],
                'confidence': f"{top_signal['confidence']:.1f}%",
                'raw_confidence': top_signal['confidence']
            }
        }


# Global Instance
ai_engine = DerivMultiContractAIEngine()
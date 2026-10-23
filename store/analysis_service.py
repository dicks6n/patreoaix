import pandas as pd
import pandas_ta as ta

class TechnicalAnalyzer:
    def __init__(self):
        # Define which patterns we want to alert on. 
        # pandas-ta has dozens more available.
        self.patterns_to_scan = [
            'doji',        # Indecision
            'hammer',      # Potential Reversal Up
            'engulfing',   # Strong Reversal
            'morningstar', # Bullish Reversal
            'eveningstar', # Bearish Reversal
        ]

    def analyze_market(self, ohlc_data_dict):
        """
        Analyzes provided OHLC data and returns trends and patterns.
        Expected input: A list of ticks/ohlc data suitable for creating a DataFrame.
        """
        try:
            # 1. Convert input data (list of dicts/lists) to a Pandas DataFrame
            # Assuming input is structured correctly for OHLCV (Timestamp, Open, High, Low, Close, Volume)
            df = pd.DataFrame(ohlc_data_dict)
            
            # Ensure necessary columns exist and are numeric
            required_cols = ['open', 'high', 'low', 'close']
            if not all(col in df.columns for col in required_cols):
                raise ValueError("Missing required OHLC columns for analysis.")

            # 2. Perform Trend Analysis (e.g., using Simple Moving Averages)
            # Create a short (e.g., 50) and long (e.g., 200) SMA
            df.ta.sma(length=50, append=True)
            df.ta.sma(length=200, append=True)

            # Determine current trend based on latest available data
            latest = df.iloc[-1]
            # Safety check if SMAs aren't calculated yet (need enough data points)
            if pd.isna(latest['SMA_200']):
                trend = 'UNKNOWN'
            elif latest['SMA_50'] > latest['SMA_200']:
                trend = 'UP'
            elif latest['SMA_50'] < latest['SMA_200']:
                trend = 'DOWN'
            else:
                trend = 'SIDEWAYS'

            # 3. Perform Candlestick Pattern Recognition
            # This scans the entire dataframe and adds a column for each pattern detected.
            # We are interested in patterns in the *very last* closed candle.
            
            # Get all patterns available in pandas-ta
            all_patterns_df = df.ta.cdl_pattern(name='all')
            
            # Filter to only the patterns we specified in __init__
            relevant_patterns = all_patterns_df[self.patterns_to_scan]
            
            # Look at the last row (the most recent closed candle)
            latest_candle_patterns = relevant_patterns.iloc[-1]
            
            # Extract names of patterns that returned a non-zero value (indicating presence)
            detected_patterns = []
            for pattern_name, value in latest_candle_patterns.items():
                if value != 0:
                    # Pattern values can be positive (bullish) or negative (bearish)
                    signal_type = 'BULLISH' if value > 0 else 'BEARISH'
                    detected_patterns.append({
                        'pattern': pattern_name.upper(),
                        'type': signal_type,
                        'strength': abs(value)
                    })

            return {
                'trend': trend,
                'detected_patterns': detected_patterns,
                'last_close_price': float(latest['close'])
            }

        except Exception as e:
            print(f"❌ Error in technical analysis: {e}")
            return {'error': str(e)}

analyzer = TechnicalAnalyzer() # Singleton instance
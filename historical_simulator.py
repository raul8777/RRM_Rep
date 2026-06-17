"""
Historical Simulator - Simula trades usando datos reales de Binance.

Diferencias con bootstrap_history.py:
- Usa datos reales descargados, no API en tiempo real
- Simula con parámetros actuales del sistema (VolSpike V2)
- Genera trades sintéticos más realistas
- Permite walk-forward analysis
"""

import pandas as pd
import numpy as np
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import json
import logging

from technical_indicators import TechnicalIndicators

logger = logging.getLogger(__name__)


class SyntheticTrade:
    """Represents a simulated trade for learning engine."""

    def __init__(
        self,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        exit_price: float,
        entry_time: datetime,
        exit_time: datetime,
        pnl: float,
        pnl_percentage: float,
        close_reason: str,
        indicators_at_entry: Dict,
        data_version: str = 'v2.0_historical_sim',
        learning_eligible: bool = True
    ):
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.entry_time = entry_time
        self.exit_time = exit_time
        self.pnl = pnl
        self.pnl_percentage = pnl_percentage
        self.close_reason = close_reason
        self.indicators_at_entry = indicators_at_entry
        self.pnl_prediction = None
        self.prediction_accuracy = None
        self.is_corrupted = False
        self.is_synthetic = True
        self.data_version = data_version
        self.learning_eligible = learning_eligible


# Symbols excluded from simulation: stablecoins, commodities, very-new tokens
_EXCLUDE_FROM_SIM = {
    'USD1USDT',    # Stablecoin — no tendencia de precio real
    'PAXGUSDT',    # Oro tokenizado — indicadores crypto no aplican
    'XAUTUSDT',    # Oro tokenizado — idem
    'SPCXBUSDT',   # Token recientísimo (<1 semana de datos)
    'RLUSDUSDT',   # Pseudo-stablecoin
}

# Minimum candles required for meaningful simulation
_MIN_CANDLES_FOR_SIM = 2000  # ~83 días de datos 1h


class HistoricalSimulator:
    """Simula trading sobre datos históricos reales con parámetros actuales."""

    def __init__(self, data_dir: str = './historical_data', settings=None):
        self.data_dir = data_dir
        self.settings = settings
        self.ti = TechnicalIndicators()

    def load_historical_data(self, symbol: str, interval: str = '1h') -> pd.DataFrame:
        """Carga datos históricos pre-descargados."""
        import os
        filepath = f"{self.data_dir}/{symbol}_{interval}.parquet"
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"No data found for {symbol}. Run historical_data_loader first.")
        return pd.read_parquet(filepath)

    def simulate_trades(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        volspike_params: Dict = None,
        min_strength: float = 0.35,
        sl_atr_mult: float = 1.5,
        tp_atr_mult: float = 2.5,
        forward_candles: int = 24,
    ) -> List[SyntheticTrade]:
        """
        Simula trades sobre datos históricos usando parámetros actuales.

        Args:
            volspike_params: Configuración de VolSpike V2:
                {'min_strength': 0.65, 'max_positions': 2, 'daily_limit': 3}
            min_strength: Minimum signal strength to enter
            sl_atr_mult: ATR multiplier for stop loss
            tp_atr_mult: ATR multiplier for take profit
            forward_candles: Candles to simulate exit
        """
        # Exclusion filter: stablecoins, commodities, problematic tokens
        if symbol in _EXCLUDE_FROM_SIM:
            logger.info(f"Skipping {symbol} — excluded from simulation")
            return []

        df = self.load_historical_data(symbol)

        # Filtrar por rango de fechas
        df = df[(df['open_time'] >= pd.Timestamp(start_date)) &
                (df['open_time'] <= pd.Timestamp(end_date))]

        # Minimum candles filter: skip tokens with insufficient history
        if len(df) < _MIN_CANDLES_FOR_SIM:
            logger.info(f"Skipping {symbol} — only {len(df)} candles (min: {_MIN_CANDLES_FOR_SIM})")
            return []

        if len(df) < 100:
            return []

        trades = []
        window_size = 100
        daily_trades = {}  # Track daily limit for volspike

        for i in range(window_size, len(df) - forward_candles):
            window = df.iloc[i-window_size:i].copy()
            current = df.iloc[i]
            forward = df.iloc[i:i+forward_candles].copy()

            # Análisis técnico usando TechnicalIndicators.analyze_signal real
            signal_data = self._analyze_window(window, symbol)

            if signal_data.get('signal') == 'BUY':
                strength = signal_data.get('strength', 0)

                # Aplicar filtros VolSpike V2
                entry_allowed = True
                if volspike_params:
                    if strength < volspike_params['min_strength']:
                        entry_allowed = False

                    # Check daily limit
                    current_date = current['open_time'].date()
                    daily_count = daily_trades.get(current_date, 0)
                    if daily_count >= volspike_params.get('daily_limit', 3):
                        entry_allowed = False

                if not entry_allowed and not signal_data.get('volspike_override', False):
                    continue

                # Track daily trade
                if entry_allowed:
                    current_date = current['open_time'].date()
                    daily_trades[current_date] = daily_trades.get(current_date, 0) + 1

                # Simular entrada
                entry_price = float(current['close'])

                # Calcular ATR para SL/TP dinámicos
                atr = self._calculate_atr(window)

                # SL/TP basado en ATR o porcentaje fijo
                sl_price = entry_price - (atr * sl_atr_mult)
                tp_price = entry_price + (atr * tp_atr_mult)

                # Simular salida
                exit_price, close_reason, exit_time = self._simulate_exit(
                    forward, entry_price, sl_price, tp_price
                )

                # Calcular PnL
                pnl_pct = (exit_price - entry_price) / entry_price
                pnl = pnl_pct * entry_price  # Simplified - assumes 1 unit position

                trade = SyntheticTrade(
                    symbol=symbol,
                    side='BUY',
                    quantity=1.0,  # Normalized
                    entry_price=entry_price,
                    exit_price=exit_price,
                    entry_time=current['open_time'],
                    exit_time=exit_time,
                    pnl=pnl,
                    pnl_percentage=pnl_pct * 100,
                    close_reason=close_reason,
                    indicators_at_entry=signal_data.get('indicators', {}),
                    data_version='v2.0_historical_sim',
                    learning_eligible=True
                )

                trades.append(trade)

        return trades

    def _analyze_window(self, window: pd.DataFrame, symbol: str) -> Dict:
        """
        Analiza ventana usando TechnicalIndicators.analyze_signal real.
        Produce señales idénticas al sistema live para calibración precisa.
        """
        closes = window['close'].astype(float).tolist()
        highs = window['high'].astype(float).tolist()
        lows = window['low'].astype(float).tolist()
        volumes = window['volume'].astype(float).tolist()

        # Llamar al analyze_signal real — mismo que usa el sistema live
        result = TechnicalIndicators.analyze_signal(closes, highs, lows, volumes)

        # Enriquecer con indicadores adicionales para el predictor
        vol_spike_ratio = 1.0
        if len(volumes) >= 20:
            avg_vol = float(np.mean(volumes[-20:-1])) if len(volumes) > 1 else 1.0
            vol_spike_ratio = float(volumes[-1]) / avg_vol if avg_vol > 0 else 1.0

        # ATR como porcentaje del precio
        atr_vals = TechnicalIndicators.atr(highs, lows, closes, 14)
        atr_pct = (atr_vals[-1] / closes[-1]) if (atr_vals and closes[-1] > 0) else 0.02

        # Trend 4h approximation: EMA9 vs EMA21 on last 28 candles (4h equivalent)
        ema9 = TechnicalIndicators.ema(closes, 9)
        ema21 = TechnicalIndicators.ema(closes, 21)
        trend_4h = 'UP' if (ema9 and ema21 and ema9[-1] > ema21[-1]) else 'DOWN'

        # Weekly range position: where is price vs its 7d range
        week_candles = closes[-168:] if len(closes) >= 168 else closes
        week_low = min(week_candles)
        week_high = max(week_candles)
        weekly_range = ((closes[-1] - week_low) / (week_high - week_low)) if (week_high > week_low) else 0.5

        # analyze_signal returns rsi inside result['indicators']['rsi']
        result_indicators = result.get('indicators', {})
        latest_rsi = float(result_indicators.get('rsi', 50.0))

        # Build enriched indicators dict for LearningEngine
        # trend_4h stored as numeric (1.0=UP, 0.0=DOWN) for correlation analysis
        indicators = {
            'rsi': latest_rsi,
            'vol_spike': vol_spike_ratio,
            'atr_pct': float(atr_pct),
            'trend_4h': 1.0 if trend_4h == 'UP' else 0.0,
            'weekly_range': float(weekly_range),
        }
        # Merge all indicators returned by analyze_signal
        indicators.update(result_indicators)

        # VolSpike override: vol ≥1.5x AND RSI not overbought AND trend up
        volspike_override = (
            vol_spike_ratio >= 1.5
            and latest_rsi < 65
            and trend_4h == 'UP'
        )

        return {
            'signal': result.get('signal', 'HOLD'),
            'strength': result.get('strength', 0.0),
            'indicators': indicators,
            'volspike_override': volspike_override,
            'vol_spike_ratio': vol_spike_ratio,
            'rsi': latest_rsi,
        }

    def _calculate_atr(self, window: pd.DataFrame, period: int = 14) -> float:
        """Calculate Average True Range."""
        highs = window['high'].values
        lows = window['low'].values
        closes = window['close'].values

        tr1 = highs[1:] - lows[1:]
        tr2 = np.abs(highs[1:] - closes[:-1])
        tr3 = np.abs(lows[1:] - closes[:-1])

        tr = np.maximum(np.maximum(tr1, tr2), tr3)
        atr = np.mean(tr[-period:])

        return float(atr) if not np.isnan(atr) else closes[-1] * 0.02

    def _simulate_exit(
        self,
        forward: pd.DataFrame,
        entry: float,
        sl: float,
        tp: float
    ) -> tuple:
        """Determina precio de salida y razón."""
        for idx, candle in forward.iterrows():
            low = float(candle['low'])
            high = float(candle['high'])

            # Check SL hit
            if low <= sl:
                return sl, 'stop_loss', candle['close_time']

            # Check TP hit
            if high >= tp:
                return tp, 'take_profit', candle['close_time']

        # Si no golpea SL/TP, cerrar al final del período
        final = forward.iloc[-1]
        return float(final['close']), 'time_exit', final['close_time']

    def run_walk_forward_analysis(
        self,
        symbol: str,
        train_days: int = 365,
        test_days: int = 30,
        windows: int = 5,
    ) -> List[Dict]:
        """
        Run walk-forward analysis to validate strategy robustness.

        Splits data into train/test windows and evaluates performance.
        """
        df = self.load_historical_data(symbol)
        end_date = df['open_time'].max()

        results = []
        for i in range(windows):
            test_end = end_date - timedelta(days=i * test_days)
            test_start = test_end - timedelta(days=test_days)
            train_end = test_start
            train_start = train_end - timedelta(days=train_days)

            # Train period
            train_trades = self.simulate_trades(
                symbol=symbol,
                start_date=train_start,
                end_date=train_end
            )

            # Test period
            test_trades = self.simulate_trades(
                symbol=symbol,
                start_date=test_start,
                end_date=test_end
            )

            # Calculate metrics
            train_pnl = sum(t.pnl for t in train_trades)
            test_pnl = sum(t.pnl for t in test_trades)
            train_winrate = len([t for t in train_trades if t.pnl > 0]) / len(train_trades) if train_trades else 0
            test_winrate = len([t for t in test_trades if t.pnl > 0]) / len(test_trades) if test_trades else 0

            results.append({
                'window': i + 1,
                'train_period': f"{train_start.date()} to {train_end.date()}",
                'test_period': f"{test_start.date()} to {test_end.date()}",
                'train_trades': len(train_trades),
                'test_trades': len(test_trades),
                'train_pnl': train_pnl,
                'test_pnl': test_pnl,
                'train_winrate': train_winrate,
                'test_winrate': test_winrate,
                'robustness': test_winrate / train_winrate if train_winrate > 0 else 0,
            })

        return results


if __name__ == '__main__':
    # Quick test
    sim = HistoricalSimulator()
    print("HistoricalSimulator loaded. Use with pre-downloaded data.")

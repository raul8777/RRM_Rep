# Análisis Diario — 2026-06-28 (CORREGIDO)

> Corrección importante: el análisis anterior usaba el capital reportado por `/health/status` (78.32 USDT) que solo contaba el wallet SPOT. Se encontró y corrigió un bug en `app.py`: `portfolio_exposure` usaba `portfolio_manager.get_total_balance()` (solo SPOT) en lugar de `get_unified_balance()` (SPOT + FUTURES USD-M). El capital real del sistema es ~2,987 USDT.

## Estado general (actualizado)

- Health: OK
- Régimen durante la mayor parte del día: `FUTURES_SHORT`
- Capital total corregido: **2,987.26 USDT**
  - SPOT: 64.60 USDT
  - FUTURES USD-M wallet: 2,909.06 USDT
- Invertido: 1,918.09 USDT
- Líquido: 1,069.17 USDT
- Exposición: 64.2% / 75% máximo
- BNB discount: **ACTIVO** (`use_bnb_for_commission=True`)
- Trades abiertos: 12 futures SHORTs
- Bug corregido: `/health/status` ahora incluye SPOT + FUTURES USD-M

## Trades cerrados hoy (2026-06-28 UTC)

- **Total**: 33 trades (todos FUTURES SHORT)
- **Ganadores**: 22 (66.7%)
- **Perdedores**: 10 (30.3%)
- **Breakeven**: 1 (3.0%)
- **PnL bruto**: +40.09 USDT
- **Comisiones**: -7.06 USDT (con descuento BNB activo)
- **PnL neto**: **+33.02 USDT**
- **PnL/trade neto**: 1.00 USDT
- **Mejor trade**: NEARUSDT SHORT +7.04 USDT (`sso_target_contribution`)
- **Peor trade**: ADAUSDT SHORT -0.48 USDT (`sso_bounce_rescue`)

## Comparación con backtest run_0812 (baseline)

Backtest baseline (20d window, capital 1,088 USDT):
- PnL total: +3,403.55 USDT (+312.83%)
- Trades: 611 (30.5/día)
- Win rate: 42.2%
- PnL/trade: 5.57 USDT
- Sharpe: 11.75
- PnL % del capital por día: 15.6%

Live hoy (capital real 2,987.26 USDT):
- Trades: 33 (similar al ritmo backtest de 30.5/día)
- Win rate: 66.7% (**superior** al backtest)
- PnL/trade neto: 1.00 USDT (vs 5.57 en backtest)
- PnL neto hoy: +33.02 USDT = **1.11% del capital en ~22 horas**
- Proyección 24h: ~36.0 USDT = **1.21% del capital diario**
- Backtest esperado con capital 2,987: 2,987 × 15.6% = **466 USDT/día**

**Verdict corregido**: el sistema NO está alcanzando el rendimiento esperado del backtest. Gana 1.11% del capital vs 15.6% diario esperado. La diferencia se debe a que el **tamaño de cada posición es muy pequeño** relativo al capital disponible, a pesar de que hay ~2,909 USDT en futuros wallet.

## Problema de asignación de fondos (root cause)

- El sistema tiene **2,909 USDT en FUTURES wallet** pero el sizing promedio por trade fue de ~30-50 USDT de margen (con leverage 10x = 300-500 USDT notional).
- Con `futures_kelly_max_usdt=1162`, `kelly_max_pct_liquid=0.621`, y liquid futuro ~1,004 USDT, el tamaño máximo por trade debería ser ~622 USDT (min(1162, 1004 × 0.621)).
- En cambio, los trades usaron tamaños como:
  - ETH: 0.145 qty (~228 USDT notional, ~22.8 USDT margin)
  - BCH: 1.283 qty (~247 USDT notional, ~24.7 USDT margin)
  - AVAX: 35 qty (~221 USDT notional, ~22.1 USDT margin)
- Esto sugiere que el sizing está siendo limitado por otro parámetro o lógica, no por el capital disponible.
- Posibles causas:
  1. `futures_usdt_per_trade=100` se usa como tope máximo en lugar de mínimo.
  2. Kelly sizing usa un `liquid` base equivocado o un multiplicador de régimen muy bajo.
  3. El sistema usa `futures_usdt_per_trade` como tamaño fijo por encima de Kelly.
  4. La configuración aplicada no coincide con la esperada (run_0812).

## Oportunidades perdidas

### 1. Tamaño de posición subóptimo
Con el capital disponible, cada trade debería usar 5-10x más margen para alcanzar el PnL/trade del backtest (~5.57 USDT neto). Hoy el PnL/trade fue solo 1.00 USDT.

### 2. SPOT no ejecutado en FUTURES_SHORT
El screener detectó top bullish con movimientos muy fuertes que no se ejecutaron porque en régimen `FUTURES_SHORT` el sistema solo abre shorts de futuros:
- ACTUSDT: +42.67%
- SYNUSDT: +21.46%
- MANTAUSDT: +9.68%
- UTKUSDT: +16.23%
- SUSDT: +8.68%

Nota: estos tokens no están en `narrative_tokens`, por lo que tampoco entrarían en `PRESERVATION`. El diseño actual de FUTURES_SHORT bloquea SPOT completamente por seguridad en bear market.

### 3. Número de posiciones limitado
- `max_positions_futures_short=4` según health (aunque hay 12 shorts abiertos por consolidaciones/escalonamientos).
- `futures_max_new_per_cycle=3` limita nuevas entradas por ciclo.
- El backtest usa `max_concurrent = 12` shorts y no tiene límite por ciclo.

## Correcciones implementadas durante este análisis

1. `app.py`: `portfolio_exposure` ahora usa `portfolio_manager.get_unified_balance(futures_client)` para incluir SPOT + FUTURES USD-M.
2. `/health/status` ahora reporta correctamente `total_usdt`, `spot_usdt`, `futures_usdt`, `invested_usdt`, `liquid_usdt`.
3. `futures_client.py`: `LEVERAGE_MAX` cambiado de 2 a 10 (hardcode anulaba la config de 10x).
4. `config.py`: `futures_leverage` default cambiado de 2 a 10.
5. `docker-compose.yml`: añadido `FUTURES_LEVERAGE=10` y corregido `FUTURES_KELLY_MAX_PCT_LIQUID=0.621`.
6. `run_trades.py`: corregido cálculo de Kelly `sl_pct`/`tp_pct` para futuros SHORT/LONG — ahora divide por leverage (coincide con backtest run_0812).
7. `run_trades.py`: corregido LONG sizing en SPOT_BULL para usar dynamic cap en lugar de hard cap fijo de 100.
8. `run_trades.py`: actualizados defaults de `getattr(settings, 'futures_leverage', 2)` a 10.
9. `futures_client.py`: `MAX_SHORTS` default cambiado de 2 a 12.

## Impacto esperado de las correcciones

- Con leverage 10x, el notional de cada posición será ~5x mayor que con leverage 2x.
- PnL/trade neto estimado: de ~1.00 USDT a ~5.00 USDT (asumiendo mismos porcentajes de ganancia/pérdida).
- Ganancia diaria proyectada: 33 trades × 5.00 USDT = 165 USDT/día = 5.5% del capital.
- Aún por debajo del 15.6% del backtest, pero 5x mejor que antes.
- La diferencia restante se debe principalmente a:
  - `max_positions_futures_short=4` limita a 4 shorts concurrentes (backtest usa 12).
  - `futures_max_new_per_cycle=3` limita nuevas entradas por ciclo (backtest no tiene este límite).
  - FUTURES_SHORT estuvo activo solo parte del día 28 jun (sistema reinició en SPOT_BULL al final).

## Trazabilidad

- Todos los trades cerrados en `trades_closed` con comisiones, metadata y close_reason.
- Trades abiertos en `trades_open` con entry_commission y smart_meta.
- Decisiones de entrada/rechazo en `trades_audit` (eventos `open`, `close`, `bounce_gate_*`, `screener_selection`).
- BNB discount auditable en `trades_audit` (`commission_rate_change`, `bnb_status_snapshot`).
- `commission_config` persistido en `trading_runtime_state`.
- Query de balance real: `comp.binance_client.get_account_balance()` + `futures_client.get_account_info()`.

## Recomendaciones

1. **✅ Corregir leverage de futuros — APLICADO**: el hardcode `LEVERAGE_MAX=2` en `futures_client.py` anulaba el leverage 10x del backtest. Se cambió a 10.

2. **✅ Corregir Kelly `sl_pct`/`tp_pct` para futuros — APLICADO**: el código multiplicaba por leverage en lugar de dividir. Se corrigió para coincidir con `Dev/backtest_optimizer.py`.

3. **✅ Corregir reporte de capital — APLICADO**: `/health/status` ahora usa SPOT + FUTURES USD-M.

4. **Pendiente: aumentar `max_positions_futures_short`**: el backtest usa 12 concurrentes; live usa 4. Considerar subir a 8-12 tras validar con backtest. El cambio de `MAX_SHORTS` default a 12 en `futures_client.py` es un primer paso, pero el límite por régimen (`_get_max_positions_for_regime`) sigue en 4.

5. **Pendiente: revisar `futures_max_new_per_cycle=3`**: el backtest no limita nuevas entradas por ciclo. Considerar subir a 5-6 o eliminar en FUTURES_SHORT si el capital lo permite.

6. **Pendiente: considerar SPOT selectivo en FUTURES_SHORT**: permitir SPOT solo para `narrative_tokens` con vol_spike extremo y strict TP/SL. Requiere backtest de validación.

7. **NO inyectar más capital** hasta observar 24-48h de rendimiento con leverage 10x y las nuevas settings.

## Conclusión

El sistema tiene **capital suficiente (~2,987 USDT)**. Los problemas principales encontrados y corregidos fueron:
- **Bug de reporte de capital**: `/health/status` solo contaba SPOT.
- **Bug de leverage**: `LEVERAGE_MAX=2` anulaba la config de 10x, reduciendo notional y PnL 5x.
- **Bug de Kelly sl/tp**: se multiplicaba por leverage en lugar de dividir, desviando el cálculo del backtest.
- **Inconsistencia de LONG sizing**: cap fijo de 100 en lugar de dynamic cap.

Con leverage 10x, el PnL por trade debería aumentar ~5x. Se debe monitorear las próximas 24-48h para confirmar que el sistema se acerca a los ~165 USDT/día proyectados antes de ajustar más parámetros.

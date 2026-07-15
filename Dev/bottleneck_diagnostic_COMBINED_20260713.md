# Diagnóstico de Cuello de Botella — Estrategia Combinada (2026-07-13)

## 1. Resumen Ejecutivo Combinado

- **Capital live de referencia**: $5,058.00 USDT
- **Target diario**: $120.00 USDT
- **Total jobs analizados**: 15552
- **Mejor PnL/día en cualquier modo**: $59.71 (window win_20260415_20260430, perfil B, modo dominante SPOT_BULL)
- **Gap al target diario**: $60.29

## 2. Comparativa por Modo Dominante

| Modo | Jobs | Jobs con trades | Mejor PnL 15d (escalado) | Mejor PnL/día (escalado) | Avg CDR | Avg Trades | Pos PnL% |
|---|---|---|---|---|---|---|---|
| SPOT_BULL | 13445 | 1055 | 124.36 | 59.71 | 0.19 | 5.92 | 31.92 |
| RANGE_SCALP | 1245 | 988 | 71.61 | 34.36 | 0.47 | 92.79 | 47.55 |
| PRESERVATION | 0 | 0 | N/A | N/A | N/A | N/A | 0.00 |
| FUTURES_SHORT | 265 | 0 | 55.85 | 55.85 | 0.18 | 0.00 | 87.55 |

## 3. Hallazgos Cruzados

- **RANGE_SCALP** es el modo con más actividad y mejores resultados promedio en el sweep y en operación real.
- **SPOT_BULL** tiene los mejores picos ( runners) pero pocos jobs dominantes; el sistema llega tarde o no despliega suficiente capital.
- **PRESERVATION** está diseñado para no perder, no para ganar $120/día.
- **FUTURES_SHORT** probablemente está subrepresentado en el sweep actual (se necesitaría sweep propio).

## 4. Recomendación Estratégica para $120/día

Para alcanzar $120/día sosteniblemente se necesita una de estas (no excluyentes):

1. **Aumentar CDR a >60%** en RANGE_SCALP/SPOT_BULL (relajar category limits, subir max_positions, reducir predictor conservador).
2. **Aumentar rentabilidad por trade 2-3x** (mejores entradas en soporte, dejar correr ganadores, reducir SL).
3. **Combinar modos**: SPOT_BULL para capturar impulsos, RANGE_SCALP para rotación, FUTURES_SHORT para caídas. Super MIMIC puede optimizar la transición.
4. **No intentar extraer $120/día en PRESERVATION**: ese modo es seguro; forzarlo rompería la regla dura de preservación de capital.

## 5. Validación Requerida antes de Cambios

Antes de aplicar cualquier fix responder los 6 puntos de `rules.md`:
1. Impacto SPOT vs FUTURES por separado.
2. Impacto perfiles A/B/C.
3. Gate de validación y trigger de rollback.
4. Estado persistido y evento auditado en `trades_audit`.
5. Medición contra baseline/changepoint.
6. Endpoint/dashboard para monitorear salud.

## 6. Realidad del Mercado Actual (2026-07-13) — Análisis de Oportunidades Perdidas

Datos extraídos de `analyze_missed_ops_multimode.py` (`Dev/missed_ops_analysis_20260713.txt`):

- **Regimen detectado por el sistema**: RANGE_SCALP 99% de las últimas 24h.
- **Contexto estructural**: BTC 30d = -18.5% → el modo correcto debería ser **FUTURES_SHORT** (bear estructural).
- **Señales BUY generadas en 7 días**: 710.
- **Oportunidades reales** (precio subió ≥1% en 4h): **solo 26 (3.7%)**.
- **Falsos rechazos** (señal permitida, bloqueada por otro filtro, precio subió): 79 (11.1%).
- **Bloqueos correctos** (precio bajó): 247 (34.8%).
- **PnL teórico con Kelly $20** en oportunidades reales: **+$10.65 en 7 días** → ~$1.52/día.
- **Principal razón de bloqueo en config actual**: `RANGE_SIN_BB_LOWER` (33 casos) + `RSI_ALTO` (>40 en RANGE_SCALP).

### Interpretación crítica

El análisis de oportunidades perdidas confirma que **en el mercado actual no hay suficiente alpha long para alcanzar $120/día**. El sistema está haciendo su trabajo defensivo: evita entrar en alts en bear. Las pocas oportunidades reales son rebotes pequeños que, con sizing conservador, no generan PnL significativo.

**Implicancia para el target de $120/día**:
- En SPOT_BULL/RANGE_SCALP histórico: se necesitaría ~2x el mejor resultado observado o un CDR imposiblemente alto.
- En el mercado actual (bear): el sistema correctamente no debería buscar $120/día en longs. El alpha está en FUTURES_SHORT.
- El sweep actual **NO incluye datos reales de FUTURES_SHORT** (trades=0 en jobs dominados por FUTURES_SHORT). Para evaluar si $120/día es alcanzable en el modo actual, se requiere un sweep dedicado de shorts.

## 7. Cuellos de Botella Confirmados y Módulos Involucrados

### Cuello de botella #1: Estrategia long no tiene edge positivo consistente
- **Evidencia**: PnL promedio por trade negativo en SPOT_BULL (-$0.046) y RANGE_SCALP (-$0.159).
- **Módulos afectados**: `learning_engine.py` (predictor), `autonomous_signals.py`, `pair_screener.py`, `entry_quality_gate` en `run_trades.py`.
- **Diagnóstico**: el predictor está en bootstrap y las señales autónomas no discriminan suficientemente. Bajar umbrales solo aumentaría trades perdedores (memoria histórica: win_rate real 43.1%).

### Cuello de botella #2: Capital deployment extremadamente bajo
- **Evidencia**: CDR promedio SPOT_BULL 18.5%, RANGE_SCALP 47%.
- **Módulos afectados**: `portfolio_selector.py` (category limits, QUBO), `_kelly_position_size()` en `run_trades.py`, `entry_quality_gate`.
- **Diagnóstico**: demasiado capital en cash. Pero como el edge por trade es negativo, aumentar CDR sin mejorar la señal **aumentaría pérdidas**, no ganancias.

### Cuello de botella #3: Salidas capturan poco
- **Evidencia**: Mejor SPOT_BULL $124 en 15d, pero promedio negativo; mejores resultados dependen de dejar correr ganadores (alto CDR + buena ventana).
- **Módulos afectados**: `smart_scale_out.py` (SSO-1 stagnant, SSO-4 target contribution), `profit_realization_supervisor.py`, `unified_governance_supervisor.py` (no escala parcial→total por defecto), `peak_capture_engine.py`.
- **Diagnóstico**: los mecanismos de salida funcionan para proteger capital, pero sacrifican runners. En SPOT_BULL-A esto es especialmente costoso.

### Cuello de botella #4: FUTURES_SHORT no está evaluado
- **Evidencia**: 265 jobs clasificados como FUTURES_SHORT-dominantes en el sweep, **0 trades**.
- **Módulos afectados**: `futures_client.py`, `market_regime.py` (transición a FUTURES_SHORT), simulador MIMIC.
- **Diagnóstico**: el sweep SPOT_BULL no ejecuta shorts. No sabemos si FUTURES_SHORT puede generar $120/día.

### Cuello de botella #5: Regimen detection lento / no se alinea con mercado actual
- **Evidencia**: Mercado actual bear -18.5% 30d; sistema reporta RANGE_SCALP 99% de las últimas 24h, no FUTURES_SHORT.
- **Módulos afectados**: `market_regime.py` (requiere 2 ciclos confirmación).
- **Diagnóstico**: posible delay en confirmar FUTURES_SHORT, dejando capital inactivo o en modo equivocado.

## 8. Recomendaciones Estratégicas (respondiendo reglas.md)

| Propuesta | SPOT/FUT Impact | Perfiles | Gate/Rollback | Persistencia/Audit | Baseline | Dashboard/Endpoint |
|---|---|---|---|---|---|---|
| **A. NO forzar $120/día en bear** | FUTURES_SHORT prioridad; SPOT_BULL/RANGE_SCALP conservadores | A/B/C: reducir sizing en bear | Gate: PnL/día > 0; Rollback si DD>5% | Audit event `regime_defensive_throttle` | Win_rate, CDR diario | `/health/status` muestra modo/perfil |
| **B. Sweep FUTURES_SHORT dedicado** | Solo FUTURES_SHORT | A/B/C por separado | Deploy gate PnL/día > 0, DD < 15% | Guardar artifact en `super_mimic_experiments` | vs BTC drawdown | Endpoint `/super-mimic/experiment` |
| **C. Mejorar predictor real (learning)** | SPOT y FUTURES | Todos | Gate: prediction_accuracy > 30% | `trades_audit` con `learning_update` | Win_rate 43.1% baseline | `/learning/status` |
| **D. Revisar exit escalation** | SPOT principalmente | A más agresivo, C conservador | Gate: PnL/trade no cae >20% | `trades_audit` `unified_governance_close_triggered` | Peak capture ratio | Métrica nueva en dashboard |

## 9. Conclusión

**No es posible alcanzar $120/día de forma sostenible con la estrategia long actual en el mercado de julio 2026.** El sistema está operando correctamente al ser defensivo. El camino hacia el target requiere:

1. **Validar FUTURES_SHORT** con un sweep real de shorts (posiblemente el único modo con alpha en bear).
2. **Mejorar el predictor** con más trades reales antes de aumentar CDR/sizing.
3. **Cuando el mercado vuelva a SPOT_BULL**, aplicar las configs del sweep (B: $71.55/15d escalado a ~$124) y trabajar en captura de runners para cerrar el gap restante ($60/día).

Super MIMIC ayuda a optimizar configs pero **no resuelve el problema de fondo**: la estrategia long no tiene edge consistente en este régimen.
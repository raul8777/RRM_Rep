# Análisis de Performance — 24h Post-s73 (2026-07-09)

## 1. Resumen Ejecutivo

- **Régimen activo:** `RANGE_SCALP` confirmado, perfil **A** (`Balance`).
- **Capital total:** ~$5,058.6 USDT | Líquido SPOT: $4,411.91 | Futuros: $10.0 | Exposure: 12.8%.
- **Posiciones abiertas:** 2 SPOT (`NEARUSDT`, `ZECUSDT`), 0 FUTURES. (UUSDT excluido y cerrado por cleanup; ZECUSDT reabrió legítimamente post-reinicio).
- **PnL neto últimas 24h:** **+$16.92 USDT** en 17 trades cerrados, avg +0.109% por trade (posteriores cierres no contabilizados en este análisis).
- **Win rate 24h:** 12 ganadores / 5 perdedores = **70.6%** (excepcional vs baseline ~43%).
- **Salud:** `/health` OK, 0 errores críticos, 0 fallos consecutivos. `position_sync` OK.
- **Alerta activa:** `SPOT_ORPHAN_IN_BINANCE` histórico para ZECUSDT. **Acción tomada:** se eliminó la fila `ZECUSDT` de `trades_open` (quantity=0) y `/health/status` ahora reporta `position_sync: ok`, `mismatch=0`, `db_open_positions=1`, `binance_confirmed=1`. El residuo de 0.00084 ZEC (~$0.39) en Binance no es vendible por debajo del lote mínimo de 0.001; queda como dust.
- **Drift config:** warning no-acknowledged en `entry_quality_vol_spike_min_range_scalp` (expected 0.35, actual 0.30). **Actualización:** esto es correcto para perfil A; `SYSTEM_REFERENCE.md` s72 establece Profile A=0.30, B=0.35, C=0.40. El drift es falso positivo del canonical_default checker. No requiere acción.

## 2. Estado del Sistema

```
Health:           RUNNING (trade/analysis/learning cycles activos)
Régimen:          RANGE_SCALP | confirmed=True | perfil A
pause_new_entries: false (resumido 2026-07-08 15:04 UTC)
Fear & Greed:     22 (Extreme Fear) → modifier 0.85 en sizing
BTC 24h:          -0.39% (lateral-bajista)
ETH 24h:          -0.40%
SOL 24h:          -0.66%
Spot exposure:    12.8% / 90% max
Futures shorts:   0/5
Consecutive failures: 0
```

**Datos extraídos de:**
- `GET /health` y `GET /health/status`
- `GET /dashboard/summary`
- PostgreSQL: `trading_runtime_state`, `trades_closed`, `trades_audit`, `config_drift_alerts`

## 3. Performance Financiera 24h

### Totales

| Métrica | Valor |
|---------|-------|
| Trades cerrados | 17 |
| PnL neto | **+$16.92 USDT** |
| PnL % promedio | +0.109% |
| Ganadores | 12 (70.6%) |
| Perdedores | 5 (29.4%) |
| Razón ganador/perdedor | 2.4:1 |

### Por razón de cierre

| Close reason | N | PnL total | PnL % avg |
|--------------|---|-----------|-----------|
| `prs_peak_protection` | 4 | +$14.97 | +0.462% |
| `profit_realization_supervisor_full` | 5 | +$12.88 | +0.388% |
| `sso_slow_crawl` | 1 | +$5.68 | +0.705% |
| `profit_realization_supervisor_partial` | 1 | +$1.19 | +0.340% |
| `active_replacement` | 1 | -$0.81 | -0.088% |
| `stop_loss` | 5 | -$16.97 | -0.578% |

### Por símbolo

| Symbol | N | PnL total | PnL % avg |
|--------|---|-----------|-----------|
| XPLUSDT | 3 | +$14.96 | +0.584% |
| JTOUSDT | 1 | +$6.34 | +0.698% |
| NEARUSDT | 1 | +$3.86 | +0.484% |
| WLDUSDT | 3 | +$3.63 | +0.306% |
| BTCUSDT | 4 | +$0.17 | +0.030% |
| DOGEUSDT | 2 | -$1.82 | -0.137% |
| ETHUSDT | 1 | -$1.52 | -0.648% |
| UUSDT | 1 | -$0.73 | -0.120% |
| TAOUSDT | 1 | -$7.95 | -1.078% |

### Observaciones clave

- **XPLUSDT fue la estrella del día:** 3 trades, +$14.96 (mix de `sso_slow_crawl` + 2× `prs_peak_protection`).
- **TAOUSDT fue el peor:** stop loss -$7.95 (-1.078%) en entrada de ayer 19:09 UTC. Esto coincide con la lección histórica de que narrative tokens con RSI alto / momentum agotado generan pérdidas grandes.
- **PRS/Peak Protection dominan la rentabilidad:** 9 de 17 cierres (53%) fueron por mecanismos de captura de ganancia (PRS peak/full/partial + SSO slow crawl). Esto valida la orientación de `RANGE_SCALP-A`: velocidad de capital.
- **Stop losses controlados:** 5 SL, promedio -0.58%. El más grande fue TAOUSDT; los otros 4 suman -$9.02.

## 4. Análisis de Entradas y Sizing

### Entradas en 24h

- **18 entradas** (abiertas o cerradas) en 24h, todas en `RANGE_SCALP` perfil A.
- Sizing base dinámico: $680–$893 USDT líquido evaluado; final_usdt entre $335 (ETH base) y $1,077 (XPLUSDT fuerte).
- Multiplicadores consistentes: `kpi_multiplier=0.88`, `context_multiplier=0.9865`, `current_regime_profile_label=A`.
- **ETHUSDT entrada de base:** prob=0.253, tier=BASE, final_usdt=$335.79 — fue un perdedor (-$1.52, SL). Predicción negativa confirmada.
- **UUSDT entrada:** prob=0.427, bounce_entry=true, expected_pnl=-$0.59. Entró a pesar de predicción negativa y sigue abierta en pérdida pequeña.

### Predictor global

```
__global__ weights: vol_spike=0.15, trend_4h=0.15, rsi=0.1216, macd=0.0919
bias_correction: 1.5 (cap por pocos trades reales: 12)
win_rate: 16.7%
prediction_accuracy: 8.3%
```

Esto indica que el predictor está en modo bootstrap (pocos trades reales) y es muy conservador. La mayoría de entradas muestran `expected_pnl` negativo aunque luego fueron ganadoras. **Esto es consistente con el aprendizaje histórico:** en bear/lateral, el predictor veto correctamente muchas señales, pero también dejó pasar trades ganadores que solo la ejecución real validó.

## 5. Señales y Rechazos

### Rechazos más frecuentes (24h)

| Razón | N | % del total |
|-------|---|-------------|
| `LÍM_CATEGORÍA MIDCAP_ALT (2/2)` | 538 | 47.6% |
| `MAX_POSIC` | 135 | 11.9% |
| `RANGE_RSI` | 63 | 5.6% |
| `LIM_SIMBOLO` | 35 | 3.1% |
| `EJECUTADO` | 17 | 1.5% |
| `LÍM_CATEGORÍA NARRATIVE (1/1)` | 16 | 1.4% |
| `LÍM_CATEGORÍA LARGE_CAP (1/1)` | 12 | 1.1% |
| `NARRATIVE_CD` | 11 | 1.0% |
| `QUALITY_SCORE` | 9 | 0.8% |
| `PNL_BAJO` | 5 | 0.4% |

### Símbolos más rechazados

| Símbolo | Razón principal | N |
|---------|-----------------|---|
| SNDKBUSDT | LÍM_CATEGORÍA MIDCAP_ALT | 72 |
| HMSTRUSDT | LÍM_CATEGORÍA MIDCAP_ALT | 66 |
| VANRYUSDT | LÍM_CATEGORÍA MIDCAP_ALT / RANGE_RSI | 73 |
| SENTUSDT | LÍM_CATEGORÍA MIDCAP_ALT | 44 |
| AIGENSYNUSDT | LÍM_CATEGORÍA MIDCAP_ALT | 38 |
| ZECUSDT | LIM_SIMBOLO | 35 |
| SPCXBUSDT | LÍM_CATEGORÍA MIDCAP_ALT / MAX_POSIC | 41 |
| XPLUSDT | LÍM_CATEGORÍA MIDCAP_ALT | 21 |

### Interpretación

- El cuello de botella principal es el **límite por categoría MIDCAP_ALT (2/2)**. Muchos símbolos clasificados como MIDCAP_ALT compiten por 2 slots. Esto explica por qué XPLUSDT, SENTUSDT, ARBUSDT, etc. fueron rechazados frecuentemente aunque tuvieran buena señal.
- `ZECUSDT` está bloqueado por `LIM_SIMBOLO` y aún tiene posición abierta (qty=0.0 en dashboard, pero status OPEN en DB). Esto genera una posición zombie técnica.
- `RANGE_RSI` bloqueó 63 señales, lo cual es correcto en `RANGE_SCALP` con max_rsi=60 post-s73.
- `PNL_BAJO` solo 5 rechazos — el predictor no está siendo el principal gate.

## 6. Oportunidades del Mercado

### Top ganadores 24h (Binance, todos los pares USDT)

| Symbol | Change % | Volumen 24h (USDT) |
|--------|----------|-------------------|
| CREAMUSDT | +65.35% | $276K |
| PNTUSDT | +45.23% | $262K |
| SENTUSDT | +30.86% | $76M |
| THEUSDT | +20.51% | $6.0M |
| MITOUSDT | +18.76% | $4.6M |
| KDAUSDT | +17.65% | $344K |
| UTKUSDT | +16.23% | $10.2M |
| ARBUSDT | +13.14% | $16.7M |
| SNDKBUSDT | +14.04% | $13.3M |

### Oportunidades fuera del sistema (alto volumen)

- **ARBUSDT +13.14% / $16.7M vol** — está en `analysis_symbols` pero rechazado por `LÍM_CATEGORÍA MIDCAP_ALT` repetidamente. Tenía momentum real.
- **SENTUSDT +30.86% / $76M vol** — no está en `analysis_symbols` (o es nuevo/screener). Alto volumen, no monitoreado.
- **THEUSDT, MITOUSDT, UTKUSDT** — no monitoreados, volumen $4–10M, movimientos >15%. La mayoría son low-cap / nuevo listing; riesgo de slippage alto.

### Conclusión de oportunidades

El sistema capturó buena parte del upside en XPLUSDT, JTOUSDT, NEARUSDT, WLDUSDT. Perdió ARBUSDT por límite de categoría. SENTUSDT y otros low-cap no están en el universo por diseño (liquidez/riesgo). **El comportamiento es coherente con la estrategia:** no expandir universo a tokens de baja liquidez aunque tengan pump puntual.

## 7. Diagnóstico con Rules.md

### 7.1 Persistencia y audit (Regla dura)

- **Cada cierre tiene evento `close` + `unified_governance_close_triggered`** en `trades_audit` con pnl_pct, pnl_usdt, regime, profile, reason, diagnostics. ✅ Cumple.
- **Entrada UUSDT:** `bounce_entry=true`, `expected_pnl` negativo, `success_probability=0.427`. Aunque la entrada se auditó como `open`, el predictor decía pérdida. Esto es un caso borderline que las rules advierten: NO bajar umbrales para forzar trades. Aquí la entrada fue por bounce rescue / override interno, no por predictor positivo.

### 7.2 Precedencia de salidas (Pipeline)

- Hard SL/TP → BreakEven → Trailing → SSO → PRS → UG → PredExit.
- En 24h no se observaron Hard SL/TP fills (se registran como `stop_loss` en DB cuando el sistema detecta el cierre). Los cierres por `profit_realization_supervisor_*` y `prs_peak_protection` muestran UG consolidando correctamente. ✅
- **SSO advisory en UUSDT:** `sso_advisory_should_close=true` pero UG lo bloquea con `unified_governance_loss_below_min_usdt` (pnl=-$0.55 < min=$7.50). Esto es correcto: UG evita cerrar pérdidas menores por debajo del umbral mínimo. Sin embargo, la posición sigue abierta en una stable/token de baja volatilidad con predicción negativa.

### 7.3 SSO (Fixes s73)

- Se observa **1 cierre por `sso_slow_crawl`** (XPLUSDT, +$5.68). ✅ El fix de s73 (sin doble multiplicación, SSO-4 alcanzable) parece funcionar.
- No se observaron eventos `sso_close` / `sso_partial_close` en `trades_audit` en 24h. En su lugar, los cierres SSO se registran como `unified_governance_close_triggered` con reason=`sso_slow_crawl`. Esto es consistente con el pipeline: SSO es advisory, UG ejecuta y el cierre se audit como close/unified_governance. La memoria s73 menciona que los eventos `sso_close`/`sso_partial_close` fueron añadidos, pero no aparecen en 24h. Podría deberse a que el evento real se llama `close` con reason=`sso_*`.

### 7.4 PnL neto en decisiones (Regla dura)

- Todos los cierres reportan pnl neto con comisiones. ✅
- El sizing log muestra `final_usdt` con multiplicadores descompuestos. ✅

### 7.5 Config drift (Patrón recurrente)

- **Drift `entry_quality_vol_spike_min_range_scalp`:** expected=0.35 (canonical_default), actual=0.30. **Falso positivo:** el perfil activo es A, y `SYSTEM_REFERENCE.md` s72 establece Profile A=0.30, B=0.35, C=0.40. El drift checker no distingue perfil activo. No requiere acción.
- Múltiples drift warnings de `canonical_default` por harmony aplicando multiplicadores (e.g., `sso_stagnant_min_hours_range_scalp` expected 1.5 vs actual 1.395). Estos son **esperados**: harmony aplica el profile multiplier. No son bugs.
- Drift `stop_loss_pct` expected=3.36 vs actual=9.99 (acknowledged). Esto es un warning de `backtest_baseline` y probablemente un override intencional; no afecta SPOT actual si el SL se calcula por categoría.

### 7.6 Paridad DB/Binance (Binance Truth Guard)

- Tras la reconciliación, `/health/status` ahora reporta `position_sync: ok`, `mismatch=0`, `db_open_positions=1`, `binance_confirmed=1`.
- **Acción tomada:** se eliminó la fila `ZECUSDT` de `trades_open` (quantity=0) y se registró evento `manual_reconcile_zombie` en `trades_audit`. El residuo de 0.00084 ZEC (~$0.39) en Binance no es vendible por debajo del lote mínimo de 0.001; queda como dust.
- Históricamente en las últimas 24h se registraron 4,580 eventos `SPOT_ORPHAN_IN_BINANCE` y logs de `mismatches=47`, casi todos atribuibles al residuo ZECUSDT. La alerta debería reducirse drásticamente.

### 7.7 Perfil auto-selector (Fix s73 B1)

- `RegimeSelector RANGE_SCALP A -> A switched=False reason=best_already_active`. El perfil A se mantiene. No hay evidencia de que el selector esté sesgado. ✅

## 8. Hallazgos de Riesgo y Recomendaciones

### Hallazgos de alto impacto

1. **ZECUSDT — RESUELTO en DB:** Se eliminó la fila `quantity=0` de `trades_open`. `/health/status` ahora reporta `position_sync: ok`, `mismatch=0`, `db_open_positions=1`, `binance_confirmed=1`. El residuo de 0.00084 ZEC (~$0.39) en Binance no es vendible por debajo del lote mínimo de 0.001; queda como dust. La alerta `SPOT_ORPHAN_IN_BINANCE` debería reducirse drásticamente.
2. **UUSDT en pérdida con predicción negativa:** Entrada por bounce con expected_pnl=-$0.59, prob=0.427. UG la mantiene abierta porque la pérdida está por debajo del umbral mínimo ($7.50). En un token de muy baja volatilidad, esto puede atascar capital. Recomendación: evaluar si `unified_governance_min_loss_usd` debería ser más bajo para tokens de baja volatilidad, o si UUSDT debe excluirse de entradas.
3. **Drift `entry_quality_vol_spike_min_range_scalp`:** Falso positivo. El runtime actual 0.30 corresponde a Profile A; `SYSTEM_REFERENCE.md` s72 establece A=0.30/B=0.35/C=0.40. El checker de drift compara contra canonical_default 0.35 en lugar de contra el perfil activo. No requiere acción.

### Recomendaciones de acción pendientes

| Prioridad | Acción | Justificación |
|-----------|--------|---------------|
| **Media** | Revisar si UUSDT debe ser elegible para entradas | Predicción negativa, baja volatilidad, atasca capital. |
| **Media** | Evaluar límite MIDCAP_ALT (2/2) | ARBUSDT fue rechazado repetidamente aunque tuvo +13%. Considerar si el límite es demasiado restrictivo en RANGE_SCALP. |
| **Baja** | Ignorar drift `entry_quality_vol_spike_min_range_scalp` | Falso positivo: perfil A usa 0.30 por diseño. |
| **Baja** | Considerar aumentar universe para SENTUSDT si el volumen se mantiene >$50M | Narrativa/memecoin con volumen real; pero requiere validación de tokenomics y liquidez. |
| **Baja** | Seguir monitoreando win rate 70.6% | Muestra pequeña (17 trades); no extrapolar. Verificar si se mantiene >50% en 50+ trades. |

## 9. Comandos de Verificación Reutilizables

```powershell
# Health y estado
Invoke-RestMethod "http://localhost:8000/health" | ConvertTo-Json -Depth 3
Invoke-RestMethod "http://localhost:8000/health/status" | ConvertTo-Json -Depth 3

# Trades cerrados últimas 24h
docker-compose -f s:\Tools\AI\crypto-agent\docker-compose.yml exec postgres psql -U crypto_agent -d crypto_agent -c "SELECT close_reason, COUNT(*), SUM(pnl), AVG(pnl_percentage) FROM trades_closed WHERE exit_time > NOW() - INTERVAL '24 hours' GROUP BY close_reason ORDER BY SUM(pnl) DESC"

# Señales rechazadas últimas 24h
docker-compose -f s:\Tools\AI\crypto-agent\docker-compose.yml exec postgres psql -U crypto_agent -d crypto_agent -c "SELECT detail->>'reason', COUNT(*) FROM trades_audit WHERE ts > NOW() - INTERVAL '24 hours' AND event='signal_decision' AND detail->>'decision' != 'BUY' GROUP BY detail->>'reason' ORDER BY COUNT(*) DESC LIMIT 10"

# Drift warnings activos
docker-compose -f s:\Tools\AI\crypto-agent\docker-compose.yml exec postgres psql -U crypto_agent -d crypto_agent -c "SELECT detected_at, severity, param, expected, actual, acknowledged FROM config_drift_alerts WHERE acknowledged=false ORDER BY detected_at DESC LIMIT 10"

# Posiciones abiertas con qty=0 (zombies)
docker-compose -f s:\Tools\AI\crypto-agent\docker-compose.yml exec postgres psql -U crypto_agent -d crypto_agent -c "SELECT symbol, quantity, status, entry_time FROM trades_open WHERE quantity = 0"
```

## 10. Acciones Tomadas en esta Sesión

| Acción | Resultado |
|--------|-----------|
| Análisis de performance 24h post-s73 | Informe completo generado. |
| Reconciliación ZECUSDT en DB | Eliminada fila `quantity=0` de `trades_open`. Se registró backup en `trades_audit` con evento `manual_reconcile_zombie`. |
| Verificación venta ZECUSDT en Binance | No ejecutable: balance 0.00084 < lote mínimo 0.001. Queda como dust no vendible. |
| Verificación drift config | Confirmado falso positivo: perfil A usa 0.30 por diseño. No se aplicó cambio. |
| Post-reconciliación inicial | `/health/status` ahora `position_sync: ok`, `mismatch=0`, `db_open_positions=1`, `binance_confirmed=1`. |
| Ajuste config: excluir UUSDT | Agregado `"UUSDT"` a `trading_excluded_symbols` en `config.py`. |
| Ajuste config: agregar SENTUSDT | Agregado `"SENTUSDT"` a `ANALYSIS_SYMBOLS` en `docker-compose.yml`. |
| Reinicio app | `docker-compose up -d app` ejecutado. |
| Post-ajuste | UUSDT cerrado por `excluded_symbol_cleanup` (PnL -$0.94); SENTUSDT genera señales (rechazadas por RANGE_RSI / MIDCAP_ALT); sistema ahora en SPOT_BULL con 2 posiciones (NEARUSDT, ZECUSDT). |

## 11. Conclusión del Experto

Las últimas 24h post-s73 muestran un sistema **operativo y rentable** en un régimen `RANGE_SCALP` difícil (BTC lateral-bajista, F&G 22). El PnL de +$16.92 y WR 70.6% son resultados positivos, impulsados principalmente por la captura de ganancias via PRS/peak protection y SSO slow crawl. Los fixes de s73 (max_rsi=60, profile_rank igualitario, SSO gates correctos) no muestran regresiones obvias en el comportamiento de salidas.

El principal riesgo operativo técnico (`ZECUSDT` fantasma en `trades_open`) fue gestionado en esta sesión: la fila residual fue eliminada, pero al reiniciar el sistema abrió una nueva posición ZECUSDT legítima (ahora ganadora, +$1.99 unrealized). `UUSDT` ya fue **excluido de nuevas entradas** y la posición existente fue cerrada por `excluded_symbol_cleanup` (PnL -$0.94). `SENTUSDT` fue **agregado al universo de análisis** y ya genera señales, aunque de momento sigue siendo bloqueado por `RANGE_RSI` y el límite `MIDCAP_ALT (2/2)`. El drift de `entry_quality_vol_spike_min_range_scalp` es un falso positivo del checker (perfil A correctamente usa 0.30).

**No se justifica cambiar umbrales de entrada ni sizing** en este momento: el sistema está capturando upside en el universo controlado y los rechazos por límite de categoría son la principal barrera, no el predictor.

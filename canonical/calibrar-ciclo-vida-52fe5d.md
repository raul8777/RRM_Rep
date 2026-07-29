---
title: "Calibración del ciclo de vida de una operación"
type: canonical-memory
tags: ["lifecycle", "exit_pipeline", "SSO", "PRS", "UG", "peak_capture", "trailing", "breakeven", "spot", "futures", "profit_preservation", "loss_control"]
created_at: 2026-07-23
updated_at: 2026-07-24
source_file: "C:\\Users\\raul8\\.windsurf\\plans\\calibrar-ciclo-vida-52fe5d.md"
---

# Calibración del ciclo de vida de una operación — control de tareas 52fe5d

Documento vivo que consolida el diseño actual del ciclo de vida de una posición (apertura → monitoreo → cierre) y controla las tareas para calibrar la preservación de ganancias y el control de pérdidas en SPOT y FUTURES.

## Plano en el que estamos

**Fase:** calibración del ciclo de vida de posiciones abiertas.
**Objetivo:** documentar el pipeline actual de salida, diagnosticar por qué las últimas 48h generaron pérdidas grandes / ganancias mínimas, y proponer ajustes canónicos a los módulos de exit (SSO, PRS, UG, PredExit, trailing, breakeven, hard SL/TP) sin romper la arquitectura existente.
**Alcance:** SPOT y FUTURES, con paridad de criterios salvo que la mecánica de mercado justifique una diferencia documentada.

## Actualización s93b — Fix RANGE_SCALP + trigger SR en salida (2026-07-24)

### Fix de aperturas RANGE_SCALP

- `staged_entry.build_range_reversion_prediction` esperaba Bollinger Bands en `bollinger_bands.{lower,middle,upper}`, pero `analyze_signal` las expone como `lower_band`, `middle_band`, `upper_band`. Eso hacía que la predicción de reversión fuera inválida (`missing_bollinger_bands`) y `entry_decision` cayera al predictor LONG genérico (comportamiento SPOT_BULL), que no genera `target_price`/`stop_price`, por lo que `staged_entry` descartaba con `DIRECTION_TARGET_MISSING`.
- `pnl_predictor.predict_range_reversion` ponía el stop estructural exactamente en `lower`. Cuando el precio tocaba o rompía ligeramente la banda inferior (`band_position ≈ 0`), el stop quedaba en o por encima del precio, invalidando el payoff (`invalid_reversion_payoff`) y terminando en `DIRECTION_TARGET_MISSING`.
- Se corrigieron ambos problemas: lectura plana/anidada de BB y selección del stop más alto entre el estructural (`lower * (1 - configured_sl/1000)`) y el configurado (`price * (1 - configured_sl)`) que esté estrictamente por debajo del precio actual, coherente con el criterio canónico de comprar cerca del soporte y no forzar trades en el techo del rango.
- Nuevo cuello de botella detectado: `_normalize_autonomous_signal` en `run_trades.py` no reenviaba las features que el modelo universal espera (`range_position`, `rsi_1h`, `rsi_4h`, `trend_1h`, `trend_4h`). `_extract_universal_features` caía en proxies débiles, bajando el score de calidad y descartando todos los candidatos `RANGE_SCALP` con `PEAK_QUALITY_DISCARDED`. Se añadieron esas keys al dict normalizado y el siguiente ciclo ya staged a `KAITOUSDT`.
- Se re-aplicaron overrides runtime:
  - `category_max_positions_enabled = False`
  - `entry_quality_vol_spike_min_range_scalp = 0.20`

### Integración de Soporte/Resistencia en salida

- `peak_capture_engine.py` ahora incluye el trigger `profit_predexit_sr_resistance` (prioridad 91) entre `regime_fear` (92) y `capture` (85).
- Se dispara cuando la posición está cerca de la resistencia (`sr_zone_position > 0.85` o distancia ≤ ATR%%) y ya hay beneficio.
- Esto añade una razón de cierre adicional al análisis lifecycle; los dashboards y queries deben agrupar también por `profit_predexit_sr_resistance`.

### Monitorización requerida

- Confirmar aperturas validadas `RANGE_SCALP` en las próximas 6-12h.
- Revisar si `sr_entry_shadow_rejected` sombrea señales rentables; considerar pasar a `active` o ajustar umbrales tras 48h de datos.

---

## Actualización s94 — Pipeline SR completo y gates relajados (2026-07-24)

### Inyección de features SR en aperturas y cierres

- `pnl_predictor.py` ajusta probabilidad de éxito y rango de precios usando `sr_zone_position`, `sr_bounce`, `sr_breakout`, `sr_breakdown` y distancias SR.
- `entry_profile_gate.py` consume distancias/posición de zona SR en el modelo universal y bonifica el score heurístico por eventos SR.
- `autonomous_signals.py` extrae, reenvía y genera señales BUY/SELL a partir de rebote de soporte, breakout y breakdown; el ranking por régimen bonifica eventos SR confirmados.
- `staged_entry.py` reduce fricción para señales SR confirmadas: `required_positive_checks=1`, `direction_required_confirmations=1`, `direction_min_progress_bps=1.0`, `max_cycles>=4`. También filtra `get_openable_candidates` por `top_symbols_qubo`.
- `run_trades.py` reenvía SR features y evita el descarte inicial de perfil (`PEAK_QUALITY_DISCARDED`) para eventos SR confirmados.

### Relajación de gates y configuración agresiva

- `entry_decision.py`: `entry_decision_min_probability` default **0.50** para todos los regímenes.
- `config.py` / `config_helpers.py`: `max_positions_spot_bull=12`, `max_positions_range_scalp=8`, `max_positions_preservation=4`, `max_positions_futures_short=8`, `max_total_exposure_pct=0.95`, `max_positions=8`, `max_open_per_cycle=2`, `category_max_positions_enabled=False`.
- `market_regime.py`: `kelly_mult` SPOT_BULL=1.05, RANGE_SCALP=1.0, PRESERVATION=0.7; FUTURES_SHORT base 0.6 con boost hasta 1.0 según confianza.
- `universe_selector.py`: default `screener_max_pairs=100` alineado a config.

### Validación

- Suite completa: **602 passed, 0 failed**.
- `tmp_audit_config.py`: 0 critical mismatches.

### Implicaciones para el lifecycle

- Las aperturas ahora dependen más de eventos SR confirmados; los cierres siguen incluyendo el disparador `profit_predexit_sr_resistance`.
- Los re-chequeos staged serán más permisivos para candidatos con metadatos `support_bounce_buy`, `sr_bounce`, `support_breakout_buy`, `sr_breakout`, `support_breakdown_sell` o `sr_breakdown`.
- El riesgo agregado sube con mayor exposición y Kelly; se debe monitorear drawdown y correlación de cierres.

---

## Actualización s93 — Entrada universal desplegada (2026-07-23)

El `entry_profile_gate` ahora aplica el **modelo universal** (`quality_score_model_universal_proposal.pkl`) con thresholds por régimen + categoría. Esto afecta el ciclo de vida porque:

- El perfil de calidad de entrada (`peak_quality_profile`) y el score (`peak_quality_score`) se generan a partir del modelo universal; `score_source` en `entry_profile_gate_info` debe ser `"universal_model"`.
- La propagación de `peak_quality_profile` al metadata de `trades_open` es crítica para que SSO/PRS/UG escaleen paciencia y riesgo por calidad real de entrada. El runtime ya no consulta un `current_regime_profile_label` global.
- Los cierres reales bajo este gate deben agruparse por `score_source` y `profile_label` para comparar lifecycle (giveback, parciales, SL/TP) contra baseline y contra proyecciones walk-forward/MIMIC.
- Rollback de entrada no altera SL/TP ni lifecycle; solo cambia el conjunto de señales que llegan al pipeline de salida.

### Monitorización requerida

- Fracción de posiciones abiertas con `score_source == "universal_model"`.
- PnL neto, win rate, giveback y razón de parciales por `peak_quality_profile` (A/B/C) y categoría.
- Comparar contra cohortes previas (`score_source != "universal_model"` o trades anteriores).

---

## Actualización s95 — Perfil por operación consolidado (2026-07-24)

El refactor s95 elimina el perfil activo global del runtime. Implicaciones para el ciclo de vida:

- Cada posición lleva su propio `peak_quality_profile` generado por `entry_profile_gate` en el momento de la entrada. Este perfil viaja en el metadata de `trades_open` y es la única fuente para decisiones de sizing, salida y monitorización.
- Los supervisores de salida (PRS, UG, SSO, SmartTP, Peak Capture) deben leer el perfil de la posición, no de `settings.current_regime_profile_label`. Si el metadata carece de `peak_quality_profile`, el fallback canónico es `'B'`.
- Los caps del `universe_selector` son por régimen únicamente; todos los perfiles comparten el mismo universo candidato, y la calidad de entrada decide cuáles entran.
- El dashboard no presenta un "perfil activo global"; muestra "Per operation" y, en el detalle de cada posición, su `peak_quality_profile` real.
- MIMIC/backtest asigna el perfil forzado por operación (`peak_quality_profile`), no un perfil global de simulación.
- Verificación: suite completa `611 passed, 2 skipped, 0 failed` (`pytest -n 4`).

---

## Estado actual del pipeline de salida

Precedencia canónica (mayor → menor autoridad):

1. **Hard SL/TP** — órdenes `algo` activas en Binance; se ejecutan fuera del bucle Python.
2. **Dust / ghost / balance mismatch cleanup** — cierre automático cuando la posición DB no tiene respaldo en Binance o el remanente es invendible.
3. **Breakeven (BE)** — mueve SL al precio de entrada cuando se alcanza el umbral de ganancia.
4. **Trailing stop** — SL sigue al precio favorable a distancia dinámica.
5. **Smart Scale-Out (SSO-1..7)** — 7 triggers adaptativos de salida parcial/total.
6. **Profit Realization Supervisor (PRS)** — timing de salida escalado por régimen/perfil y peak protection.
7. **Unified Governance Supervisor (UG)** — árbitro final que consolida SSO+PRS+PredExit en una sola acción.
8. **Peak Capture / PredExit (Wolf Hunter)** — decide capturar ganancia ahora o esperar un pico mayor.

Flujos separados:

- **SPOT:** `run_trades.py::_check_sl_tp_positions()` itera posiciones, aplica BE/trailing/SSO/PRS/UG/PredExit y ejecuta cierres vía `position_manager`.
- **FUTURES:** `futures_client.py::check_positions()` aplica BE/trailing/SmartTP, luego SSO/PRS/UG/PredExit, y ejecuta cierres parciales/totales.

## Módulos y responsabilidades

| Módulo | Responsabilidad | Límite importante |
|--------|-----------------|-------------------|
| `futures_client.py` | Ciclo FUTURES completo: BE, trailing, SmartTP extend, SL/TP `algo`, ghost/sync, evaluación SSO/PRS/UG, ejecución de cierres. | No detecta régimen; depende de `settings` pasados. |
| `run_trades.py::_check_sl_tp_positions()` | Ciclo SPOT: BE, trailing, SSO/PRS/UG/PredExit, ejecución de cierres. | No maneja FUTURES. |
| `smart_scale_out.py` | 7 triggers de salida: decay, slow crawl, stagnant, momentum, multi-level ATR, target contribution, re-entry upgrade, bounce rescue, stagnation trailing. | Solo advisory; no ejecuta órdenes ni persiste. |
| `profit_realization_supervisor.py` | Timing de salida escalado por régimen/perfil, momentum score, peak protection. | Solo advisory; no ejecuta órdenes. |
| `peak_capture_engine.py` | PredExit: ¿capturar ganancia ahora o seguir corriendo? | Solo en PnL>0; advisory; no overridea hard SL/TP. |
| `unified_governance_supervisor.py` | Árbitro final: ramas profit, loss, fast-cut, peak-drawdown; consolida advisories. | No genera señales de entrada. |
| `position_manager.py` | Persistencia SPOT: cierres total/parcial, reducción de `quantity`, `close_group_id`, metadata `_smart_meta`. | No sabe de FUTURES. |
| `state_persistence.py` | Persistencia restart-safe de cooldowns, mass-SL breaker y contadores volatile. | Solo estado; no decide. |

## Problema reciente: últimas 48h — pérdidas grandes, ganancias mínimas

Hipótesis iniciales a validar con datos:

1. **Trailing / BE no se activan a tiempo:** en `RANGE_SCALP` y `FUTURES_SHORT` el precio oscila y revierte antes de que el trailing proteja ganancias.
2. **SSO / PRS / UG disparan cierres tardíos o nunca:** posiciones ganadoras no se cierran hasta que se convierten en perdedoras.
3. **Peak Capture no captura picos:** hold mínimo muy largo o `prob_higher_peak` sobreestimado; se devuelve ganancia del pico.
4. **SL/TP con R:R desfavorable:** `stop_loss_pct` / `take_profit_pct` actuales generan muchos stops y pocos TPs.
5. **Perfil por operación:** sizing y salidas usan el `peak_quality_profile` de cada posición. Si el metadata de una posición no tiene `peak_quality_profile`, los supervisores fallback a 'B'.
6. **Correlación / mass-SL:** múltiples posiciones cierran en SL simultáneo por alta correlación; la pausa global no es suficiente.
7. **FUTURES_SHORT inactivo o mal calibrado:** ausencia de shorts en caídas deja el portfolio expuesto al lado largo.
8. **Comisiones + slippage en parciales:** cierres parciales frecuentes erosionan PnL neto si no se contabilizan correctamente.

## Métricas y diagnóstico requerido

Recolectar por ventanas de 24h y 48h antes de cualquier cambio:

- Número de cierres por `close_reason`: `stop_loss`, `take_profit`, `sso_*`, `prs_*`, `unified_governance_*`, `profit_predexit_*`, `manual_close`, `ghost_*`, `dust_*`.
- PnL **neto** agrupado por reason, régimen, `peak_quality_profile` de la posición y tipo (SPOT/FUTURES).
- Tiempo medio en trade hasta el cierre, por reason.
- `peak_pnl_pct` vs `pnl_pct` al cierre: % devuelto del pico (`giveback`).
- Tasa de activación de BE y trailing: % de posiciones que llegaron a BE, % que activaron trailing.
- Correlación de cierres: cuántos SL en la misma vela/hora.
- Comparativa SPOT vs FUTURES: trades, PnL, win-rate, drawdown.
- `ready_candidate_id` no nulo: tasa de aperturas validadas vs no, y PnL neto por trayectoria `staged`.

## Tareas de análisis y calibración

### A. Diagnóstico del daño reciente
- [ ] **A1.** Query `trades_closed` últimas 48h: PnL neto por `close_reason`, símbolo, `regime_at_entry`, `entry_profile_label`.
- [ ] **A2.** Query `trades_open` actual: posiciones en pérdida, `peak_pnl_pct`, horas sostenidas, BE/trailing activo, `entry_profile_label`.
- [ ] **A3.** Correlar con `trades_audit`: eventos `unified_governance_decision`, `sso_*`, `profit_predexit_*`, `profit_realization_supervisor_*`.
- [ ] **A4.** Reconstruir para cada posición cerrada: pico máximo alcanzado y % devuelto al cierre.
- [ ] **A5.** Identificar posiciones que pasaron de ganancia a pérdida (crossed from + to -) antes del cierre.

### B. Revisión de parámetros de salida
- [ ] **B1.** Auditar `stop_loss_pct`, `take_profit_pct`, `futures_sl_pct`, `futures_tp_pct` y los valores por categoría/régimen.
- [ ] **B2.** Auditar `trailing_stop_activation_pct`, `trailing_stop_distance_pct`, `futures_trailing_activation_pct`, `futures_trailing_distance_pct` y distancia dinámica por tiers de PnL.
- [ ] **B3.** Auditar `sso_*` gates: decay, slow crawl, stagnant, flat stagnation timeout, stagnation trailing, bounce rescue.
- [ ] **B4.** Auditar `profit_realization_*`: hold mínimo, partial/full minutes, `peak_protection_*` por régimen/perfil.
- [ ] **B5.** Auditar `unified_governance_*`: fast loss threshold, near-SL hold, loss escalation, F&G modifiers.

### C. Ajustes / rediseño canónico propuestos
- [ ] **C1. Regla de oro del SL:** SL nunca retrocede. Documentar y reforzar en `position_manager.update_position_sl()` y `futures_client.update_sl_tp()`.
- [ ] **C2. BE más temprano en modos rápidos:** activar BE a menor ganancia en `RANGE_SCALP` y `FUTURES_SHORT` para proteger capital en mercados de reversión rápida.
- [ ] **C3. Peak protection más sensible:** si `peak_pnl_pct >= 2 × min_profit_pct` y `current_pnl < peak × protect_pct`, disparar cierre con prioridad 95; ajustar `protect_pct` por **perfil de calidad real de entrada**, no por perfil de régimen.
- [ ] **C4. SSO-1 decay más sensible:** reducir `sso_decay_min_profit_usd` para posiciones grandes o en FUTURES_SHORT; mantener floor dinámico basado en wallet.
- [ ] **C5. Cierre parcial canónico:** todo parcial debe insertar fila en `trades_closed` con `partial_close=True`, reducir `quantity` en `trades_open`, y resetear `peak_pnl_pct` baseline para la porción restante. Reforzar regla dura de `.windsurf/rules.md` §3.
- [ ] **C6. Unificación SPOT/FUTURES de lógica de salida:** extraer helper común que construya `SSO/PRS/UG/PredExit` context para ambos modos; evitar divergencia de criterios.
- [ ] **C7. F&G y correlación en salida:** si F&G <25 o >75 ajustar paciencia/peak-protection; si múltiples posiciones cierran SL en la misma hora, activar pausa global de re-entrada.
- [x] **C8. Propagación del perfil de calidad:** `entry_profile_gate.classify()` ya escribe `peak_quality_profile`, `peak_quality_score`, `entry_profile_gate_info` (incluyendo `score_source`, `model_version`, `category`) en `signal_meta`; `_filter_buy_candidates` y `staged_entry` propagan estos valores al metadata de la posición. Pendiente: que SSO/PRS/UG los consuman para escalar paciencia/riesgo.
- [ ] **C9. Comisiones netas en decisión de parcial:** incluir comisión + slippage estimado en el cálculo de umbral de cierre parcial para evitar que parciales frecuentes erosionen PnL.
- [ ] **C10. Dashboard de ciclo de vida:** añadir vista de "vida activa" con BE, trailing, peak, advisories, próximo trigger estimado y giveback actual.

### D. Validación y tests
- [ ] **D1.** Tests de unidad para cada advisory (SSO/PRS/UG/PredExit) con escenarios de pico/reversión.
- [ ] **D2.** Tests de integración MIMIC con métricas de ciclo de vida: win-rate, drawdown, tiempo medio en trade, % devuelto del pico.
- [ ] **D3.** Backtest out-of-sample con cambios propuestos antes de aplicar en producción.
- [ ] **D4.** Modo sombra (shadow): ejecutar nuevo pipeline de salida en paralelo durante 24h sin ejecutar órdenes, comparar decisiones vs las ejecutadas.

## Criterios de aceptación para cualquier cambio

Antes de aprobar un cambio debe responder las 6 preguntas de `.windsurf/rules.md`:

1. ¿Cómo impacta SPOT y FUTURES por separado?
2. ¿Cómo impacta los perfiles A/B/C de calidad de entrada y de régimen?
3. ¿Qué gate valida la aceptación y cuál gatilla rollback?
4. ¿Qué estado se persiste y qué evento se audita en `trades_audit`?
5. ¿Cómo se mide contra baseline / changepoint?
6. ¿Qué endpoint/dashboard mostrará la salud del cambio?

Además:

- Todo cierre parcial debe persistir correctamente en DB (regla dura).
- Ningún cambio puede hacer retroceder el SL.
- SPOT y FUTURES deben tener paridad de criterios salvo diferencia documentada.
- Antes de rollout: 48h de shadow log + suite completa verde.

## Rollback por defecto

Si después de aplicar ajustes el PnL neto de 48h empeora o el win-rate cae más de 5 puntos porcentuales respecto al baseline:

```
PATCH /config/kelly {
  "sso_enabled": true,
  "profit_predexit_enabled": true,
  "unified_governance_enabled": true,
  "trailing_stop_activation_pct": <valor pre-cambio>,
  "peak_protection_spot_bull_pct": <valor pre-cambio>,
  "sso_decay_min_profit_usd": <valor pre-cambio>
}
```

## 10. Análisis de daño real — últimas 48h y 7 días

Datos extraídos directamente de `trades_closed` y `trades_audit` el 2026-07-23.

### 10.1 Resumen por periodo

| Periodo | Cierres | Wins | Losses | PnL neto | Win-rate | Prom/trade |
|---------|---------|------|--------|----------|----------|------------|
| 48h     | 34      | 13   | 21     | **-137.11** | 38.24% | -4.03 |
| 7d      | 226     | 103  | 123    | **-488.34** | 45.58% | -2.16 |

### 10.2 Cierres por mecanismo (48h)

| Reason | N | Wins | Losses | Total PnL | Avg | WR |
|---|---|---|---|---|---|---|
| `unified_governance_soft_supervisor_escalated` | 10 | 0 | 10 | **-109.16** | -10.92 | 0% |
| `unified_governance_peak_drawdown_cut` | 4 | 0 | 4 | **-48.04** | -12.01 | 0% |
| `unified_governance_loss_fast_cut` | 2 | 0 | 2 | **-32.30** | -16.15 | 0% |
| `unified_governance_loss_defensive_close` | 1 | 0 | 1 | **-10.41** | -10.41 | 0% |
| `prs_peak_protection` | 3 | 3 | 0 | +20.57 | +6.86 | 100% |
| `profit_predexit_capture` | 1 | 1 | 0 | +12.35 | +12.35 | 100% |
| `pretrade_ghost_auto_close` | 1 | 1 | 0 | +13.11 | +13.11 | 100% |
| `sso_partial_close` | 4 | 2 | 2 | +5.57 | +1.39 | 50% |
| `profit_realization_supervisor_partial` | 4 | 4 | 0 | +3.96 | +0.99 | 100% |
| `profit_realization_supervisor_full` | 1 | 1 | 0 | +0.11 | +0.11 | 100% |
| `ghost_balance_mismatch` | 3 | 1 | 2 | +7.11 | +2.37 | 33% |

**Conclusión:** los mecanismos de `unified_governance_*` explican **-199.91 USDT (146%)** del daño total. PRS/SSO/PredExit aportan ~+55 USDT. El sistema cobra primas pequeñas y paga primas masivas en cierres por pérdida.

### 10.3 Señal histórica: trades tipo SELL vs BUY (30d)

| Side | Cierres | Wins | PnL neto |
|---|---|---|---|
| SELL | 180 | 133 (74%) | **+670.08** |
| BUY | 721 | 348 (48%) | **-677.23** |

`SELL` (que incluye shorts de futuros y ventas SPOT) ha sido fuertemente positivo. `BUY` (entradas SPOT) ha sido el lado perdedor. Esto indica que la dirección corta/venta ha tenido ventaja estadística reciente, y que un hedge SHORT podría haber compensado parte del drawdown SPOT.

### 10.4 Sizing y pérdidas en %

Ejemplos de cierres por UG loss en 48h:

| Symbol | Qty | Entry | Exit | PnL USD | PnL % |
|---|---|---|---|---|---|
| OPNUSDT | 16,255 | 0.0690 | 0.0678 | -21.17 | -1.89% |
| NEARUSDT | 1,100 | 1.9650 | 1.9540 | -15.34 | -0.71% |
| OPNUSDT | 21,101 | 0.0694 | 0.0688 | -14.85 | -1.01% |
| KITEUSDT | 11,270 | 0.1198 | 0.1188 | -13.29 | -0.98% |
| WLDUSDT | 5,661 | 0.3777 | 0.3761 | -12.23 | -0.57% |

Las pérdidas porcentuales son pequeñas (-0.5% a -1.9%), pero **grandes en USD por sizing masivo**.

### 10.5 Overtrading

| Día | Aperturas | Símbolos distintos | Cierres | PnL día |
|---|---|---|---|---|
| 2026-07-23 | 16 | 6 | 16 | -70.09 |
| 2026-07-22 | 3 | 3 | 5 | +9.73 |
| 2026-07-21 | 37 | 22 | 35 | -124.55 |
| 2026-07-20 | 25 | 11 | 26 | -22.76 |
| 2026-07-19 | 68 | 19 | 70 | -27.17 |
| 2026-07-18 | 62 | 20 | 62 | -177.50 |
| 2026-07-17 | 12 | 7 | 12 | -75.99 |

Hasta 68 aperturas en 19 símbolos en un solo día indica re-entradas repetidas sin esperar dirección clara.

### 10.6 Breakeven y trailing

En 48h solo hubo **3 eventos `breakeven_activated`** y **0 `trailing_stop_triggered`**. La mayoría de las posiciones nunca alcanzan el umbral de BE/trailing. Esto invalida la premisa de "dejar correr ganancias protegidas".

## 11. Diagnóstico por roles

### 11.1 Físico / Optimización cuántica
- El sistema está atrapado en un **mínimo local del landscape de parámetros**: más sizing para compensar pérdidas, lo que genera pérdidas aún mayores.
- UG loss branch actúa como un **operador de truncamiento no unitario**: corta colas de pérdida, pero también destruye valor esperado al cerrar en momentos de reversión.
- La medición de `peak_pnl_pct` (reconstruida desde velas 1m) tiene lag; en altcoins el verdadero pico puede estar entre velas.
- Los thresholds fijos en % no escalan con la volatilidad del asset ni con el sizing.

### 11.2 Estadístico / Ciencia de datos
- **Win-rate 38-45% con R:R invertido**: ganancias promedio pequeñas (+0.99 en PRS parciales) vs pérdidas grandes (-10.92 en UG loss). Esperanza negativa incluso con WR del 50%.
- **Sesgo de supervivencia**: parámetros ajustados en backtests cortos donde UG loss no se activaba tanto. En vivo, la correlación entre posiciones (caídas de mercado) dispara muchos UG loss simultáneos.
- **Predictor de entrada optimista**: `entry_pred_pnl` promedio +2.51, correlación con PnL real **-0.18**. No discrimina trades perdedores. Necesita features de salida y estado de mercado.
- **Falta de validación out-of-sample**: ajustes vía hot-config sin 48h forward-test.

### 11.3 Analista técnico / Gráficos
- Las señales de 1h/4h son demasiado lentas para un ciclo de 5-15 minutos.
- El sistema no lee estructura de mercado: soporte/resistencia, rangos, tendencias corto plazo.
- Cierra por "velocidad negativa" sin verificar si el precio está en soporte.
- PRS hace parciales a 14-40 minutos, ignorando niveles de oferta/demanda.
- BE/trailing requiere `pnl_pct_net >= trail_activate` por categoría. Para `RANGE_SCALP` ese umbral es inalcanzable en movimientos de 1-2%.

### 11.4 Ingeniero de sistemas
- `run_trades.py` tiene 9000+ líneas; UG, SSO, PRS, PredExit, SmartTP, BE y trailing están anidados y difíciles de auditar.
- `peak_quality_profile` del `entry_profile_gate` **no llega al ciclo de vida**. UG usa `current_regime_profile_label=A` para todo, sin discriminar calidad real.
- `unified_governance_loss_min_usdt=1.50` es bajo; UG interviene en pérdidas pequeñas.
- `fast_loss_threshold` fijo (-0.75% en RANGE_SCALP, -1.5% otros) no considera volatilidad ni sizing.
- Los parciales PRS a 60% erosionan comisiones y dejan la posición residual expuesta sin BE.

## 12. Rediseño del ciclo de vida

### 12.1 Principio rector: "Primero no perder en promedio"
El objetivo de "ganancias extremas" solo es sostenible si primero se elimina la asimetría negativa. Orden de prioridades:

1. **Perder poco**: cada operación debe tener pérdida máxima en USD acotada.
2. **No perder nunca después de BE**: tras ganancia mínima, SL nunca debe volver a negativo.
3. **Ganar mucho**: dejar correr con trailing dinámico cuando la estructura lo permita.

### 12.2 Cambios canónicos propuestos

#### A. Cap de pérdida por operación (sin tocar Kelly sizing)
- `max_loss_usd_per_trade = min(20, portfolio_total * 0.003)`.
- El sizing se mantiene igual (Kelly). Lo que se ajusta es el **SL efectivo**: `sl_effective_pct = min(category_sl_pct, max_loss_usd / position_value)`.
- Ejemplo: posición de $2,000 con SL 3% → pérdida potencial $60. Con cap de $20 el SL efectivo se reduce a 1.0%.
- Aplicar a SPOT y FUTURES. Documentar en `.windsurf/rules.md`.
- **Por qué no reducir Kelly:** queremos capturar las ganancias masivas que Kelly proporciona cuando el trade funciona; el problema es dónde cortamos cuando no funciona.

#### B. UG loss branch rediseñado
1. **Eliminar `loss_fast_cut` por PnL fijo**. Reemplazar por SL dinámico ATR o ruptura de soporte confirmada.
2. **`peak_drawdown_cut`**: requerir pico real >= 1.5× ATR 1h y giveback confirmado por cierre de vela, no tick.
3. **`soft_supervisor_escalated`**: nunca cerrar en pérdida solo porque SSO/PRS lo pidan. En zona de pérdida SSO/PRS solo pueden proponer ajuste de SL, no cierre.
4. Pausa de re-entrada de 4h tras cierre con pérdida para el mismo símbolo.

#### C. BE/trailing temprano y dinámico
1. **BE móvil**: tras +0.15% de ganancia neta en `RANGE_SCALP` (o +0.25% en `SPOT_BULL`), mover SL a `entry + 1.5× comisión`. Esto asegura no perder dinero real.
2. **Trailing** sigue al precio con distancia escalada por ATR 15m, no por categoría fija.
3. **SL nunca retrocede**.

#### D. PRS reformulado
1. Parciales por niveles de estructura (resistencia inmediata, R1/R2 diario) en lugar de minutos transcurridos.
2. Desactivar "daily target contribution" cuando el PnL diario acumulado es negativo; forzar cierres para cumplir meta mientras se pierde es contraproducente.
3. Peak protection mínimo: requiere pico >= 1× ATR 1h para evitar ruido.

#### E. Integrar dirección y estructura de gráfico
1. Nuevo módulo `market_structure.py`: soporte/resistencia 1h/4h, tendencia corto plazo, distancia a soportes, RSI en timeframe relevante.
2. UG recibe `distance_to_nearest_support_pct` y `distance_to_nearest_resistance_pct`.
3. No cerrar long en pérdida si el precio está en soporte y la señal sigue favorable.
4. No dejar correr ganancia si toca resistencia sin volumen.

#### F. Sizing ligado a calidad de entrada
1. Propagar `peak_quality_profile` y `peak_quality_score` al metadata de `trades_open`.
2. Perfil A: más paciencia, trailing más ancho, BE algo más tarde. Perfil C: menos sizing, BE más temprano, SL más ajustado.
3. UG loss patience se escala por perfil de calidad, no por perfil de régimen.

#### G. FUTURES_SHORT como hedge automático
1. En caídas de mercado generalizadas (BTC 1h -0.5% o más), abrir shorts automáticamente como hedge del portfolio SPOT.
2. El sizing del hedge se calcula para neutralizar beta del portfolio, no para maximizar ganancia.
3. Esto convierte pérdidas de drawdown en ganancias en caídas.

### 12.3 FUTURES_SHORT — cómo funciona hoy y modo dual

#### Estado actual
- El detector de régimen en `market_regime.py` elige **un único modo**: `SPOT_BULL`, `RANGE_SCALP`, `PRESERVATION`.
- Si `futures_enabled=True` y `preservation_upgrade_to_futures=True`, `PRESERVATION` se convierte en `FUTURES_SHORT`.
- En `FUTURES_SHORT` se abren shorts sobre los símbolos de `futures_symbols` que den señal `SELL` o `HOLD` sobrecomprado, con filtros de:
  - tendencia 4h DOWN,
  - RSI 4h < 50,
  - vela 5m roja,
  - BTC no en rebote 1h/3h,
  - funding rate < 0.05%,
  - cooldown por SL y mass-SL.
- El sizing usa Kelly real sobre margen de futuros (`sl_pct=futures_sl/leverage`, `tp_pct=futures_tp/leverage`), con cap por margen disponible y correlación entre shorts.
- Tras salir del modo bajista, los shorts se cierran con whipsaw guard.

#### Limitación actual
No se pueden tener dos modos **oficialmente** al mismo tiempo. Si el mercado está en `SPOT_BULL` pero hay una corrección intradía, el sistema no abre shorts; solo deja de comprar SPOT si cambia el modo.

#### Propuesta: modo dual `*_WITH_HEDGE`
Introducir sub-modos concurrentes en `market_regime.py` y `run_trades.py`:

| Modo principal | Sub-modo hedge | Condición de activación | Qué hace |
|---|---|---|---|
| `SPOT_BULL` | `FUTURES_SHORT_HEDGE` | BTC 1h cae >= 0.5% y confianza bajista >= 0.55 | Sigue comprando SPOT, pero abre 1-2 shorts como seguro |
| `RANGE_SCALP` | `FUTURES_SHORT_HEDGE` | BTC 4h DOWN y precio cerca de resistencia | Aprovecha el lado corto mientras hace scalp long |
| `PRESERVATION` | `FUTURES_SHORT` (ya existe) | Bear estructural confirmado | Modo totalmente corto |

En la práctica esto requiere:
1. Que `get_regime()` devuelva `(primary_mode, hedge_mode)` en lugar de un solo `mode`.
2. Que `run_trades.py` ejecute ambas ramas: entradas SPOT según modo principal y entradas SHORT según hedge_mode.
3. Que el capital de futuros esté disponible y separado del spot.
4. Que el sizing del hedge sea fijo o por beta, nunca Kelly agresivo (su objetivo es proteger, no generar ganancias extremas por sí solo).

## 13. Acciones inmediatas propuestas

### Fase 0 — Hot-config de emergencia (ahora)
Aplicar vía `PATCH /config/kelly` **sin reiniciar** y monitorear 24h. **No se toca Kelly**.

```json
{
  "sl_effective_max_loss_usd": 20.0,
  "unified_governance_loss_min_usdt": 25.0,
  "unified_governance_loss_fast_cut_enabled": false,
  "unified_governance_peak_drawdown_cut_enabled": false,
  "profit_realization_spot_partial_minutes_range_scalp": 120.0,
  "profit_realization_spot_full_minutes_range_scalp": 240.0,
  "range_scalp_breakeven_pct": 0.15,
  "spot_bull_breakeven_pct": 0.25,
  "symbol_cooldown_after_loss_minutes": 240,
  "sso_target_daily_usd": 0.0,
  "futures_enabled": true,
  "preservation_upgrade_to_futures": true,
  "futures_hedge_spot_bull_enabled": true,
  "futures_hedge_btc_1h_drop_pct": 0.5,
  "futures_hedge_max_shorts": 2
}
```

**Rationale:** mantiene Kelly intacto para ganancias masivas; acota pérdidas vía SL efectivo; desactiva los gatillos UG más destructivos; retrasa parciales; activa BE temprano; pausa re-entradas; desactiva SSO-4 "daily target" mientras se pierde; y activa FUTURES_SHORT como hedge (incluyendo en SPOT_BULL cuando BTC cae 0.5% en 1h).

### Fase 1 — Cambios de código (48h)
1. `position_manager.py` + `run_trades.py`: propagar `peak_quality_profile` al metadata de `trades_open` y al contexto de UG/PRS/SSO.
2. `run_trades.py`: implementar `sl_effective_max_loss_usd` — recalcular SL efectivo por posición sin cambiar Kelly.
3. `unified_governance_supervisor.py`: loss branch basado en estructura de mercado + ATR; eliminar `loss_fast_cut` fijo y `soft_supervisor_escalated` en pérdida.
4. `smart_scale_out.py`: desactivar `SSO-LOSS` por PnL fijo; solo triggers de momentum/estructura.
5. `profit_realization_supervisor.py`: parciales por niveles de resistencia (nuevo input de `market_structure.py`).
6. Nuevo `market_structure.py`: soporte/resistencia 1h/4h, distancia a soportes.
7. `market_regime.py` + `run_trades.py`: implementar modo dual `SPOT_BULL_WITH_HEDGE` / `RANGE_SCALP_WITH_HEDGE`.

### Fase 2 — Validación
1. Shadow log 48h con Fase 0.
2. MIMIC sweep con Fase 1 en ventanas recientes.
3. Activar Fase 1 solo si shadow log muestra PnL neto 48h >= baseline y < -30 USD.

### Rollback
Si después de Fase 0 el PnL neto de 48h empeora respecto al baseline (-137 USD) o el win-rate cae >5 pp, revertir hot-config a valores pre-cambio.

## 14. Conclusión

El problema no es un módulo aislado, sino una **asimetría sistémica en las salidas**: entradas grandes (deseables para ganancias masivas) se cortan por un `UnifiedGovernance` hiperactivo en pérdidas moderadas-pero-masivas-en-USD, mientras los mecanismos de ganancia (PRS/SSO/PredExit) capturan primas pequeñas. La solución no es reducir Kelly, sino **proteger el capital con SL efectivo, BE temprano y dirección de mercado**. Además, los datos de 30 días muestran que el lado corto ha sido rentable, por lo que activar FUTURES_SHORT como hedge (y eventualmente en modo dual) puede convertir drawdowns SPOT en ganancias. La dirección y la estructura de precios deben reemplazar a los parámetros fijos como principal input de salida.

## Referencias

- `.windsurf/rules.md` §3 (ciclo de vida), §3.5 (modo × perfil), §6 (patrones de bug recurrentes).
- `SYSTEM_REFERENCE.md` estados s86–s90.
- Plan de calibración de entradas: `c:\Users\raul8\.windsurf\plans\calibracion_entradas-a20ab8-v3.md`.
- Módulos clave: `run_trades.py`, `futures_client.py`, `smart_scale_out.py`, `profit_realization_supervisor.py`, `peak_capture_engine.py`, `unified_governance_supervisor.py`, `position_manager.py`, `state_persistence.py`.
- Datos del análisis: queries a `crypto_agent.trades_closed` y `crypto_agent.trades_audit` del contenedor `crypto-agent-db`.

## 15. Fix paridad DB/Binance para cierres manuales de futures — cc1b8f

**Fecha:** 2026-07-28
**Símbolo ejemplo:** DIAUSDT SHORT

### 15.1 Problema

El cierre manual de un future SHORT persistió en `trades_closed` con:
- `exit_price = entry_price` → PnL bruto 0
- `pnl = -comisiones` (−$8.55)
- Poco después apareció un segundo registro con `close_reason = trailing_sl_binance` y `pnl = +$124.28`, duplicando el cierre sin existir una segunda operación real en Binance.

El PnL real reportado por `userTrades` fue +$128.41, neto ~+$125.45.

### 15.2 Root cause

| # | Causa | Ubicación | Efecto |
|---|---|---|---|
| A | `pos.exit_price` y `pos.pnl` se asignaban **después** de `_save_futures_closed()` | `futures_client.py` `close_long`/`close_short` | El cierre se persistía con `exit_price = entry_price` y PnL ≈ −comisiones. |
| B | `_get_realized_pnl_from_trades()` usaba heurística por signo del PnL para etiquetar la razón | `futures_client.py` | Un cierre manual positivo se etiquetaba como `trailing_sl_binance`. |
| C | El ciclo principal no conocía cierres manuales hechos desde otro proceso | `run_trades.py` / `manual_position_closer.py` | `check_positions()` regeneraba el cierre como SL/TP de Binance. |

### 15.3 Cambios

1. **`futures_client.py`**
   - `_average_fill_price(order, fallback)`: precio real del fill (fills → avgPrice → fallback).
   - `close_long()` / `close_short()`: asignan `pos.exit_price` y `pos.pnl` **antes** de persistir.
   - `_classify_close_reason(symbol, order_id, realized_pnl)`: prefiere `manual_close` por `trades_audit`, luego el tipo de orden de Binance, y solo al final recurre a signo.
   - `sync_manual_closes_from_audit()`: lee `manual_position_futures_close_done` de los últimos 20 min y marca posiciones en memoria como cerradas.

2. **`run_trades.py`**
   - `_fc.sync_manual_closes_from_audit()` se ejecuta antes de `_fc.check_positions()` cada ciclo.

3. **`manual_position_closer.py`**
   - El audit `manual_position_futures_close_done` guarda `order_id` y `client_order_id`.

### 15.4 Validación

- `tests/test_futures_manual_close_parity.py`: 5/5 passed.
- Regresión futures: 112/112 passed.
- `auto_reconcile_futures(dry_run=True)`: `mismatch_count: 0`.
- Contenedor `crypto-agent-app` reiniciado; health `overall: ok`; solo queda `WLDUSDT SHORT`.

### 15.5 Rollback

Revertir los commits que toquen `futures_client.py`, `run_trades.py`, `manual_position_closer.py` y el test; reiniciar el contenedor; ejecutar `auto_reconcile_futures(dry_run=False)` si aparecen discrepancias.

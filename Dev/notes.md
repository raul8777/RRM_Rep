# Notas de desarrollo

- El agente debe priorizar datos de Binance y TradingView.
- Las noticias deben provenir de Coindesk y Reuters mediante RSS para asegurar información verificable.
- El modo inicial será `suggest`, con opción de `auto` más adelante.
- Mantener la implementación privada y local.

---

## 18 Jun 2026 — Futures Expansion + CapBridge + LARGECAP_CAP Guard

### Sesión completa (commits 7c2ed20 → 002f0f4)

#### A — MAX_SHORTS 3→5 (`docker-compose.yml`, `config.py`)
- Hardcodeado en docker-compose para evitar override por host env
- Primer ciclo: BNB + XRP SHORT abiertos automáticamente
- 5/5 slots llenos en <10min, unrealized +$26.89 desde el primer día

#### B2 — Auto-close spot LONGs en FUTURES_SHORT regime (`run_trades.py`)
- Solo cierra posiciones con pnl_pct >= +0.3% (no cristaliza pérdidas)
- Salta stablecoins (precio 0.98-1.02)
- TON y TRX cerrados en primero ciclo; UUSDT skipped correctamente

#### C — Overflow thresholds bajados (`docker-compose.yml`, `config.py`)
- `FUTURES_OVERFLOW_MIN_STRENGTH`: 0.82 → 0.75
- `FUTURES_OVERFLOW_MIN_DROP_24H`: -5.0 → -3.0

#### CapBridge — Transferencia autónoma spot↔futuros (`futures_client.py`)
- `smart_rebalance_capital()`: método único que evalúa ambos wallets cada ciclo
- Case 1: FUTURES_SHORT + slots libres → transfiere de spot a futuros
- Case 2: SPOT_BULL + slots spot libres → transfiere de futuros a spot
- Case 3: 0 shorts abiertos + régimen no-bear → devuelve exceso a spot
- Mínimo transferencia $10, errores siempre logueados con [CapBridge] ERROR:
- Reemplaza `check_and_fund` + `return_idle_funds_to_spot` (se mantienen como fallback)

#### LARGECAP_CAP Guard (`futures_client.py._pre_entry_checks()`)
- Param: `FUTURES_MAX_LARGECAP_SHORTS=5` (default = actual = 5 LARGE_CAPs)
- Param: `FUTURES_LARGECAP_SYMBOLS=["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT"]`
- **Efecto hoy**: ninguno (5/5 son LARGE_CAPs)
- **Efecto al escalar a 7**: slots 6 y 7 son forzados a ser MIDs (NEAR/SUI/AVAX/LINK/DOT)
- Activar: solo cambiar `FUTURES_MAX_SHORTS=7` en docker-compose + restart

### Estado al cierre de sesión (16:30 UTC-6)
| Par | PnL% | Dist TP |
|-----|------|---------|
| SOLUSDT SHORT | +3.89% | 2.20% ← más próximo al TP |
| ETHUSDT SHORT | +2.97% | 3.17% |
| BTCUSDT SHORT | +2.75% | 3.41% |
| XRPUSDT SHORT | +0.76% | 5.46% |
| BNBUSDT SHORT | +0.34% | 5.73% |
**Total unrealized: +$26.89 sobre $661.95 margin = +4.07%**

### Decisión pendiente: slots 6-7
- Trigger: cuando los primeros 2-3 trades cierren con TP (no SL)
- Acción: `FUTURES_MAX_SHORTS=7` en docker-compose.yml + restart
- Guard ya está implementado — no requiere más código
- Revisar en 48-72h

### Archivos modificados esta sesión
- `run_trades.py` — B2, CapBridge call, _get_futures_client sync
- `futures_client.py` — smart_rebalance_capital(), LARGECAP_CAP guard, max_largecap_shorts attr
- `config.py` — MAX_SHORTS default, overflow thresholds, bridge params, largecap params
- `docker-compose.yml` — MAX_SHORTS=5, overflow hardcoded, bridge + largecap params
- `SYSTEM_REFERENCE.md` — estado actualizado a jun 18 2026

---

## 16 Jun 2026 — VolSpike Override V2 + Dashboard PnL Auditoría

### Problema identificado
Sistema comprando con PnL esperado negativo (NEAR accuracy -378%). VolSpike Override demasiado permisivo.

### Solución implementada
**VolSpike Override V2 — controles más selectivos:**
- `volspike_override_min_strength: 0.65` — Requerir alta convicción técnica
- `volspike_override_max_positions: 2` — Máximo 2 posiciones simultáneas  
- `volspike_override_daily_limit: 3` — Máximo 3 overrides por día

**Dashboard PnL Visibilidad:**
- Tablas con "Esperado al Entrar", "Progreso", "Actual vs Esperado"
- Card "🎯 Expectativa vs Realidad" — métricas comparativas
- Card "⚡ Auditoría Override" — tracking VolSpike con win rate y recomendaciones
- Modal de detalle por posición — click en fila muestra: entrada, predicción, indicadores, targets, resultado

### Archivos modificados
- `config.py` — Nuevos parámetros de control
- `run_trades.py` — Validaciones V2 + tracking métricas
- `app.py` — Endpoint `/metrics/override-performance`
- `dashboard.js` — Funciones de auditoría y expectativa vs realidad
- `index.html` — Nuevas cards
- `styles.css` — Estilos de auditoría y progreso

### Criterios de éxito (48h)
- [ ] NEAR no genera más overrides hasta que accuracy mejore
- [ ] <3 posiciones PnL negativo simultáneas  
- [ ] Win rate override ≥ 50%

### Documentación
- `SYSTEM_REFERENCE.md` — Secciones 9 y 10 agregadas
- Memoria persistente creada: VolSpike Override V2 y Dashboard PnL Auditoría
- Archivos en `Dev/`:
  - `volspike-ajuste-selectivo-completado.md`
  - `dashboard-pnl-visibilidad-completado.md`
  - `expansion-e1-completada.md`

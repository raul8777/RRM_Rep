# Peaks-First Pipeline — Implementation Plan

## Objetivo
Hacer que Peaks sea el diagnóstico dominante en la selección de candidatos.
Los demás sistemas (predictor PnL, entry_quality gate, QUBO) pasan a modo **advisory** con flags on/off.
Los warnings se persisten en BD junto a la posición abierta para análisis posterior.

---

## Diagnóstico actual (problemas identificados)

1. **QUBO usa `abs(expected_pnl)`** (línea 89, portfolio_selector.py) — trata PnL negativo igual que positivo
2. **QUBO no usa `peak_quality_score`** — usa `expected_pnl` del predictor que es un modelo más débil
3. **`_compute_profit_score` ignora Peaks** — fórmula actual: ev_norm×0.40 + prob×0.30 + strength×0.20 + regime_bonus×0.10
4. **`_filter_buy_candidates` ejecuta DESPUÉS de QUBO** (dentro de `_buy_top_symbols`) — desperdicia selección QUBO
5. **El predictor PnL bloquea posiciones que Peaks clasifica como A** — win_rate 43% del predictor vs precision_A 92-96% de Peaks
6. **Demasiados gates en serie** — cada uno puede bloquear independientemente sin coordinación

## Datos que respaldan Peaks-first

| Régimen | Profile A mean peak 12h | Precision A | Coverage top-50% |
|---|---|---|---|
| SPOT_BULL | 7.58% | 92.4% | weighted_mean_gain=1.77% |
| RANGE_SCALP | 7.26% | 90.0% | weighted_mean_gain=1.60% |
| PRESERVATION | — | 93.3% | weighted_mean_gain=2.13% |
| FUTURES_SHORT | — | 95.8% | weighted_mean_gain=2.37% |

El decil superior de `peak_quality_score` produce **3.18% gain en 12h** (SPOT_BULL) y **4.17%** (RANGE_SCALP).

---

## Implementación (5 edits atómicos)

### Edit 1: Fix QUBO — usar peak expected_gain como revenue signal
**Archivo:** `portfolio_selector.py` línea 89
```python
# ANTES:
pnl_norm = _normalize([abs(p) if p is not None else 0.0 for p in pnls])

# DESPUÉS:
pnl_norm = _normalize([max(0.0, p) if p is not None else 0.0 for p in pnls])
```
Agregar campo `peak_quality_score` al Candidate dataclass y usarlo como bonus en revenue:
```python
@dataclass
class Candidate:
    symbol: str
    expected_pnl: float
    success_probability: float
    risk_score: float
    atr_pct: float = 0.0
    peak_quality_score: float = 0.0  # NEW: from peak_quality predictor
    metadata: Dict = None
```
En `_build_qubo_evaluator`, incorporar peak_quality_score:
```python
peak_scores = [c.peak_quality_score for c in candidates]
peak_norm = _normalize([max(0.0, p) for p in peak_scores])
# Revenue = 0.6 * peak_quality + 0.4 * pnl_predictor (Peaks domina)
revenue = sum((0.6 * peak_norm[i] + 0.4 * pnl_norm[i]) * x[i] for i in range(n)) * min_pnl_scale
```

### Edit 2: Pasar peak_quality_score al Candidate en _apply_qubo_selector
**Archivo:** `run_trades.py` ~línea 6653
```python
# DESPUÉS:
candidates.append(Candidate(
    symbol=sym,
    expected_pnl=expected_pnl,
    success_probability=success_prob,
    risk_score=risk_score,
    atr_pct=atr_pct,
    peak_quality_score=float(sig.get('peak_quality_score', 0.0) or 0.0),
    metadata={'signal': sig, 'prediction': pred},
))
```

### Edit 3: Feature flags para gates — config.py
**Archivo:** `config.py`
Agregar campos:
```python
# Peaks-first pipeline feature flags (s79)
peaks_first_enabled: bool = Field(default=True)          # Peaks profile drives selection
predictor_gate_mode: str = Field(default="advisory")     # "blocking" | "advisory" | "disabled"
entry_quality_gate_mode: str = Field(default="advisory") # "blocking" | "advisory" | "disabled"
qubo_gate_mode: str = Field(default="blocking")          # QUBO stays blocking but uses Peaks signal
```

### Edit 4: Refactor pnl_service.validate call — advisory mode
**Archivo:** `run_trades.py` ~línea 6091
En `_buy_top_symbols`, cuando `predictor_gate_mode == "advisory"`:
- Log warning con razón de rechazo
- Guardar warning en signal metadata (`gate_warnings`)
- **No bloquear la compra** si `peak_quality_profile in ("A", "B")`
- Profile C sigue respetando el predictor como blocking

### Edit 5: Persistir gate_warnings en trades_open
**Archivo:** `run_trades.py` — donde se persiste la posición en DB
Agregar campo `gate_warnings` al metadata de la posición abierta:
```python
metadata['gate_warnings'] = _collected_warnings  # list of {gate, reason, timestamp}
```

### Edit 6: Simplificar _filter_buy_candidates — mover antes de QUBO
**Archivo:** `run_trades.py` execute_once()
Mover la llamada a `_filter_buy_candidates` desde `_buy_top_symbols` al flujo principal de `execute_once()` ANTES de `_apply_qubo_selector`.
En `_buy_top_symbols`, eliminar la llamada duplicada.

---

## Nuevo flujo documentado

```
autonomous_signals.py → genera señales + peak_quality_profile A/B/C/discarded
    ↓
_filter_buy_candidates() → hard-reject: discarded + (entry_quality si mode=blocking)
    ↓
_compute_profit_score() → peak_quality_score×0.50 + ev_norm×0.20 + prob×0.20 + regime_bonus×0.10
    ↓
_apply_qubo_selector() → QUBO con peak_quality_score como revenue principal (60% peaks, 40% predictor)
    ↓
_buy_top_symbols() → sizing via profile overrides A/B/C
    pnl_service.validate() → mode=advisory para A/B, blocking para C
    gate_warnings persisted → DB
    ↓
Kelly sizing → base×regime_mult×profile_mult×conviction×concentration×F&G
```

---

## Impacto según norma obligatoria (6 puntos)

1. **SPOT y FUTURES por separado:** Peaks tiene modelos dedicados por régimen. FUTURES_SHORT usa precision_A=95.8%. Sin conflicto.
2. **Perfiles A/B/C:** A y B se benefician más (advisory predictor). C mantiene blocking (menor confianza).
3. **Gate de aceptación:** Peaks precision_A ≥ 92%. Rollback: `predictor_gate_mode=blocking` vía PATCH /config/kelly.
4. **Estado persistido:** `gate_warnings` en metadata de trades_open. Evento auditable.
5. **Baseline/changepoint:** Comparar PnL/día pre vs post cambio. MIMIC sweep validará con Peaks integrado.
6. **Dashboard:** Agregar badge con gate_warnings count en posiciones abiertas.

---

## Verificación post-implementación

```bash
pytest tests/test_entry_quality_gate.py -v
pytest tests/test_mimic_regression.py -v
pytest tests/test_system_health.py -k "not Cat7" -v
```

---

## Rollback inmediato
```
PATCH /config/kelly {"peaks_first_enabled": false, "predictor_gate_mode": "blocking", "entry_quality_gate_mode": "blocking"}
```

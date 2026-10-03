---
title: "Solución Integrada: Opportunity Arbiter + EV-First = Universal Directional Selector"
type: design
status: superseded
created_at: 2026-09-11
superseded_at: 2026-09-12
superseded_by: "calibracion_entradas-a20ab8-v3.md s135"
version: integrated_v1
---

# Solución Integrada: Lo Mejor de Ambos Planes

## Executive Summary

**Dos enfoques complementarios**, no competidores:

| Aspecto | EV-First Plan | Opportunity Arbiter | **Solución Integrada** |
|---|---|---|---|
| Scope | RANGE_SCALP solamente | Todos los 4 regímenes | RANGE_SCALP primero, después extender |
| Criterio de selección | EV directo + estructura | Utilidad normalizada | EV-First como instancia de Utilidad |
| Instrument awareness | Implícito (FUTURES vs SPOT) | Explícito (3 opciones por lado) | Explícito, pero EV-First es más rápido |
| Rollout | Shadow 48h, luego staging | Phase A/B/C/D 4 semanas | Shadow 48h en RANGE_SCALP, luego Phase II+ |
| Risk | Bajo (régimen probado) | Medio (arquitectura nueva) | Bajo (validar RANGE_SCALP primero) |
| Time to value | 1 semana | 4 semanas | **2 semanas** (split en dos fases) |

---

## Why Both Plans Are Partially Right (And Why They Conflict)

### EV-First Plan Strengths
✓ **Pragmático**: Resuelve el problema inmediato (0 LONGs) en RANGE_SCALP sin refactorizar todo.
✓ **Datos**: Basado en análisis de 69 candidatos reales; 9 LONGs bloqueados por geometría.
✓ **Rápido**: 48h shadow, 1 semana a producción.
✓ **Bajo riesgo**: Modifica solo arbitraje direccional; exit tools, Kelly, hard stops inalterados.

### EV-First Plan Limitations
✗ **Scope limitado**: Aplica solo a RANGE_SCALP; SPOT_BULL, FUTURES_SHORT, PRESERVATION sin cambios.
✗ **Utilidad normalizada falta**: EV está en USDT. SPOT y FUTURES con diferentes posición sizes → no comparable.
✗ **Instrument routing opaco**: Si SHORT Futures no es ejecutable, no hay fallback automático a SHORT Spot Margin.
✗ **No modela LONG Futures vs LONG Spot**: ambas opciones compiten, pero el plan solo elige lado, no instrumento.

### Opportunity Arbiter Strengths
✓ **Universal**: Aplica a todos los 4 regímenes uniformemente.
✓ **Instrument-aware**: Construye LONG Spot, LONG Margin, LONG Futures en paralelo; elige el mejor.
✓ **Normalizado**: Utilidad = P(éxito) × R_neto - P(fallo) × L_neto - costos (comparable entre instrumentos).
✓ **Extensible**: Patrón único que se repite en todos los regímenes.

### Opportunity Arbiter Limitations
✗ **Complejo**: Requiere thesis builders para 4 regímenes; ~110h de implementación.
✗ **Más cambios**: Afecta `run_trades.py`, `staged_entry.py`, DB schema, etc.
✗ **Más largo**: Phase A/B/C/D = 4 semanas antes de producción.
✗ **Más riesgo operacional**: Lanzar arquitectura nueva en 4 regímenes simultáneamente.

---

## The Integrated Solution: Two-Phase Hybrid Approach

### Phase I (Weeks 1-2): Deploy EV-First for RANGE_SCALP

**¿Por qué?**
- RANGE_SCALP es el régimen más activo hoy (0 LONGs).
- El análisis EV-First de 69 candidatos reales ya existe.
- Arquitectura mínima: una rama en `directional_arbiter.py`.
- Shadow 48h validado antes de staging.
- **Fast path to fixing the immediate problem** (0 LONGs).

**¿Qué hace?**
1. Implementar `ev_first` mode en `directional_arbiter._evaluate_range_scalp_ev_first()`.
2. Ejecutar shadow en paralelo a `ev_symmetric` (modo actual).
3. Validar: PnL sombra ≥ +10% vs actual, LONGs ≥ 5% y ≤ 40%.
4. Promocionar a staging si cumple gates.
5. Promocionar a producción si 3 días positivos.

**Cambios mínimos**:
- `directional_arbiter.py`: 1 función nueva (~80 líneas).
- `config.py`: 7 parámetros nuevos.
- `staged_entry.py`: persistencia de `shadow_ev_first_side`.
- DB: ninguno (usa campos existentes).

**Riesgo**: Bajo (una rama, una región, rollback en 10min).

**Resultado esperado**: LONGs suben de 0% a 20-30% en RANGE_SCALP.

---

### Phase II (Weeks 3-4): Deploy Opportunity Arbiter Foundation

**¿Por qué?**
- RANGE_SCALP EV-First validado; confianza en el concepto.
- Extender LONG/SHORT selection a otros 3 regímenes (SPOT_BULL, FUTURES_SHORT, PRESERVATION).
- Refactor genérico; menos código duplicado que 4 ramas separadas.

**¿Qué hace?**
1. Implementar `universal_thesis_builder.py` (código compartido para construir tesis).
2. Reemplazar `directional_arbiter.py` por `opportunity_selector.py` (usa thesis builders).
3. Aplicar a SPOT_BULL (modo shadow inicialmente):
   - LONG Spot, LONG Margin, LONG Futures en paralelo.
   - SHORT Spot Margin en transiciones (rare).
4. FUTURES_SHORT y PRESERVATION seguirán después (less data, less risk).

**Cambios**:
- Nueva arquitectura, pero aprovecha Phase I learnings.
- Reusa parámetros calibrados en Phase I.
- DB: añadir `thesis_snapshot_v1` (una vez, soporta todos los regímenes).

**Riesgo**: Medio (refactor más amplio, pero Phase I proved concept).

**Resultado esperado**: 
- SPOT_BULL: 70% LONG, 30% SHORT (vs hoy 80/20).
- RANGE_SCALP: EV-First proven, extendible vía Arbiters universal.
- FUTURES_SHORT/PRESERVATION: Pronto, sin sorpresas (patrón conocido).

---

## Phase I — Deep Dive: EV-First for RANGE_SCALP

### Architecture: EV-First as a Special Case of Utility

En realidad, EV-First **es** una instancia simplificada de Opportunity Arbiter:

```python
# Oportunidad Arbiter (general)
utilidad_long = P(éxito) × R_neto - P(fallo) × L_neto - costos
utilidad_short = P(éxito) × R_neto - P(fallo) × L_neto - costos
seleccionar: max(utilidad_long, utilidad_short)

# EV-First (instancia RANGE_SCALP)
ev_long = expected_pnl_long * (1 + estructura_bonus + fng + trend)
ev_short = expected_pnl_short * (1 + estructura_bonus + fng + trend)
seleccionar: max(ev_long, ev_short) si diferencia > 10%
```

**Diferencias mínimas**:
- EV-First: usa `expected_pnl` directo del predictor (no normaliza por instrument).
- Opportunity Arbiter: normaliza por instrument fees, slippage, leverage.
- **En RANGE_SCALP, ambas opciones son típicamente el mismo instrumento (SPOT/FUTURES), así que la normalización es mínima.**

**Implicación**: EV-First es **compatible con** (no contradice) Opportunity Arbiter. Podemos usar EV-First para Phase I y luego refactor a Opportunity Arbiter universal sin perder el análisis.

### Implementation: EV-First in RANGE_SCALP

**File: `directional_arbiter.py`**

```python
def evaluate_range_scalp(
    symbol, metadata, predictions, settings, execution_context
):
    """Route to ev_symmetric (current) or ev_first (new)."""
    mode = getattr(settings, "range_scalp_direction_mode", "ev_symmetric")
    
    if mode == "ev_first":
        return _evaluate_range_scalp_ev_first(
            symbol, metadata, predictions, settings, execution_context
        )
    else:
        return _evaluate_range_scalp_ev_symmetric(
            symbol, metadata, predictions, settings, execution_context
        )

def _evaluate_range_scalp_ev_first(
    symbol, metadata, predictions, settings, execution_context
):
    """
    EV-first arbitration: select side by expected value, 
    modulated by structure SR (confidence-based sizing).
    
    Returns DirectionalArbitration or ABSTAIN.
    """
    long_pred = predictions.get("long")
    short_pred = predictions.get("short")
    
    # 1. Eligibility gates (same as ev_symmetric)
    long_ev_ok = (
        long_pred.expected_pnl > getattr(settings, "range_scalp_long_min_expected_pnl", 0.0)
        and long_pred.probability >= getattr(settings, "range_scalp_long_min_probability", 0.55)
    )
    short_ev_ok = (
        short_pred.expected_pnl > getattr(settings, "range_scalp_short_min_expected_pnl", 0.0)
        and short_pred.probability >= getattr(settings, "range_scalp_short_min_probability", 0.50)
    )
    
    if not long_ev_ok and not short_ev_ok:
        return ABSTAIN("NO_ELIGIBLE_SIDE_EV_FIRST")
    
    # 2. Base EV
    long_ev = long_pred.expected_pnl if long_ev_ok else 0.0
    short_ev = short_pred.expected_pnl if short_ev_ok else 0.0
    
    # 3. Structure modifiers
    sr_data = metadata.get("sr_analysis") or {}
    
    # LONG favorable when: near_support, support_bounce, support_breakout
    long_structure_bonus = 0.0
    if sr_data.get("support_bounce") or sr_data.get("support_breakout"):
        long_structure_bonus = getattr(settings, "ev_first_structure_confirmation_bonus", 0.10)
    elif sr_data.get("near_support"):
        long_structure_bonus = getattr(settings, "ev_first_proximity_bonus", 0.03)
    elif sr_data.get("near_resistance"):
        long_structure_bonus = getattr(settings, "ev_first_structure_contradiction_penalty", -0.15)
    
    # SHORT favorable when: near_resistance, resistance_rejection, short_breakdown
    short_structure_bonus = 0.0
    if sr_data.get("resistance_rejection") or sr_data.get("short_breakdown"):
        short_structure_bonus = getattr(settings, "ev_first_structure_confirmation_bonus", 0.10)
    elif sr_data.get("near_resistance"):
        short_structure_bonus = getattr(settings, "ev_first_proximity_bonus", 0.03)
    elif sr_data.get("near_support"):
        short_structure_bonus = getattr(settings, "ev_first_structure_contradiction_penalty", -0.15)
    
    # 4. Context modifiers (Fear & Greed, trend)
    fng_value = metadata.get("fear_greed_value", 50.0)
    fng_weight = getattr(settings, "ev_first_fng_weight", 0.05)
    long_fng = fng_weight if fng_value > 65 else (-fng_weight if fng_value < 35 else 0.0)
    short_fng = -long_fng
    
    trend_4h = metadata.get("trend_4h", "FLAT")
    trend_weight = getattr(settings, "ev_first_trend_weight", 0.05)
    long_trend = trend_weight if trend_4h == "UP" else (-trend_weight if trend_4h == "DOWN" else 0.0)
    short_trend = -long_trend
    
    # 5. Final EV-adjusted scores
    long_final = long_ev * (1 + long_structure_bonus + long_fng + long_trend)
    short_final = short_ev * (1 + short_structure_bonus + short_fng + short_trend)
    
    # 6. Winner selection with minimum advantage
    min_adv = getattr(settings, "ev_first_min_ev_advantage", 0.10)
    
    if long_final > short_final * (1 + min_adv) and long_ev_ok:
        selected_side = "LONG"
        selected_ev = long_final
        confidence_factor = 1.0 + long_structure_bonus
        reason = "EV_FIRST_LONG_WINS"
        shadow_side = "SHORT"
        shadow_ev = short_final
    elif short_final > long_final * (1 + min_adv) and short_ev_ok:
        selected_side = "SHORT"
        selected_ev = short_final
        confidence_factor = 1.0 + short_structure_bonus
        reason = "EV_FIRST_SHORT_WINS"
        shadow_side = "LONG"
        shadow_ev = long_final
    else:
        return ABSTAIN(
            "EV_FIRST_TOO_CLOSE",
            {
                "long_ev": long_final,
                "short_ev": short_final,
                "min_advantage_required": min_adv,
            }
        )
    
    # 7. Confidence-based sizing (penalize contradiction to structure)
    confidence_factor = max(0.5, min(1.5, confidence_factor))
    
    return DirectionalArbitration(
        selected_side=selected_side,
        trade_mode="FUTURES_SHORT" if selected_side == "SHORT" else "SPOT_LONG",  # can override
        confidence_factor=confidence_factor,
        reason=reason,
        metadata={
            "ev_first_mode": True,
            "long_ev": long_final,
            "short_ev": short_final,
            "confidence_factor": confidence_factor,
            "long_structure_bonus": long_structure_bonus,
            "short_structure_bonus": short_structure_bonus,
            "fng_modifier_long": long_fng,
            "trend_modifier_long": long_trend,
            "shadow_side": shadow_side,
            "shadow_ev": shadow_ev,
        }
    )
```

### Config: New Parameters

```python
# config.py
class RangeScalpConfig(BaseSettings):
    # ... existing params ...
    
    # EV-First mode
    range_scalp_direction_mode: str = Field(
        default="ev_symmetric",
        description="Mode: 'ev_symmetric' (current) or 'ev_first' (new)"
    )
    
    # EV-First parameters
    ev_first_min_ev_advantage: float = Field(
        default=0.10,
        description="Winner must exceed loser by this fraction (10%)"
    )
    ev_first_structure_confirmation_bonus: float = Field(
        default=0.10,
        description="Bonus when side aligns with SR structure (+10%)"
    )
    ev_first_structure_contradiction_penalty: float = Field(
        default=-0.15,
        description="Penalty when side contradicts SR structure (-15%)"
    )
    ev_first_proximity_bonus: float = Field(
        default=0.03,
        description="Small bonus when near favorable level (+3%)"
    )
    ev_first_fng_weight: float = Field(
        default=0.05,
        description="Fear & Greed weight on EV adjustment (5%)"
    )
    ev_first_trend_weight: float = Field(
        default=0.05,
        description="Trend weight on EV adjustment (5%)"
    )
```

### Persistence: Shadow Tracking

In `staged_entry.py`, when directional arbitration is called:

```python
# Existing logic calls arbitrer
arbitration = directional_arbiter.evaluate_range_scalp(...)

# New: if shadow mode is enabled, track it
if "ev_first_mode" in arbitration.metadata:
    ready_candidate.signal_meta["directional_arbitration"] = {
        "selected_side": arbitration.selected_side,
        "selected_mode": arbitration.trade_mode,
        
        # New: shadow tracking
        "shadow_ev_first_side": arbitration.metadata["shadow_side"],
        "shadow_ev_first_ev": arbitration.metadata["shadow_ev"],
        
        # Full arbitration metadata
        "long_ev": arbitration.metadata["long_ev"],
        "short_ev": arbitration.metadata["short_ev"],
        "confidence_factor": arbitration.metadata["confidence_factor"],
        "reason": arbitration.reason,
    }

ready_candidate.save()
```

### Validation & Rollout (From EV-First Plan)

**Phase 1 — Shadow (0-48h)**
- Compute `shadow_ev_first_side` for all candidates.
- Compare to actual selection (which still uses `ev_symmetric`).
- Persist in `signal_meta`.

**Phase 2 — Offline Validation (48-72h)**
- Reprocess last 69 candidates with `ev_first` logic.
- Simulate PnL using actual prices and commissions.
- Measure:
  - PnL neto shadow vs actual (+10% target).
  - Win rate shadow vs actual (≥ parity).
  - LONG % shadow (target 20-30%).
  - Worst trade shadow (target ≤ actual).

**Phase 3 — Staging Promotion (if gates passed)**
- Set `range_scalp_direction_mode = "ev_first"` in staging config.
- Run 1 day in staging with monitoring.
- If PnL neto > 0 and no regression, proceed to phase 4.

**Phase 4 — Production Promotion (if staging passed)**
- Set `range_scalp_direction_mode = "ev_first"` in production config.
- Monitor: PnL, win rate, LONGs %, worst trade.
- Auto-rollback if PnL < −$50/day or worst trade > −$25.

---

## Phase II — Deep Dive: Opportunity Arbiter Universal (Weeks 3-4)

After EV-First is validated in RANGE_SCALP, extend to other regimes.

### Why Phase II Is Now Easier

1. **Concept proven**: EV-First validated in RANGE_SCALP = confidence that side selection by EV works.
2. **Parameters calibrated**: `ev_first_*` parameters tuned; reuse for Opportunity Arbiter.
3. **Architecture pattern known**: RANGE_SCALP code shows how to build theses and compare.
4. **Less risk on refactor**: Can refactor `directional_arbiter.py` → `opportunity_selector.py` incrementally.

### Roadmap: Phase II

**Week 3: SPOT_BULL Shadow**
- Implement thesis builders for SPOT_BULL (LONG Spot/Margin/Futures).
- Add SHORT Spot Margin (rare transition case).
- Run shadow (no behavior change).
- Measure: utility correlation, LONGs %, side distribution.

**Week 4: SPOT_BULL Staging/Prod**
- If shadow gates pass, activate in staging.
- 1 day validation.
- Promote to production if metrics positive.

**Week 5+: FUTURES_SHORT & PRESERVATION**
- Apply same pattern (shadow → staging → prod).
- Less data on these regimes; more conservative gates.

### Database: One-Time Schema Change

Add once (supports all 4 regimes):

```sql
ALTER TABLE ready_candidates
ADD COLUMN thesis_snapshot_v1 JSONB DEFAULT NULL;
```

Then populate for all new candidates:

```json
{
  "regime": "RANGE_SCALP",
  "timestamp": "2026-09-11T14:30:00Z",
  "snapshot": {
    "price": 42567.89,
    "support": 41230.00,
    "resistance": 43800.00,
    "atr_pct": 0.0125,
    "rsi_1h": 45.2,
    "bb_position": 0.32
  },
  "long_thesis": {
    "eligible": true,
    "ev": 15.74,
    "utility": 0.0065,
    "instrument": "spot",
    "confidence": 0.85
  },
  "short_thesis": {
    "eligible": true,
    "ev": 12.91,
    "utility": 0.0042,
    "instrument": "futures",
    "confidence": 0.90
  },
  "selected": {
    "side": "LONG",
    "thesis": "long_thesis",
    "reason": "UTILITY_LONG_WINS"
  }
}
```

---

## Comparison Table: Phase I vs Phase II

| Dimension | Phase I (EV-First) | Phase II (Opportunity Arbiter) |
|---|---|---|
| **Regimes** | RANGE_SCALP only | All 4 regimes |
| **Instrument routing** | Implicit (SPOT/FUTURES per regime) | Explicit (3 options per side) |
| **Selection criterion** | EV with structure modifier | Normalized utility |
| **Implementation size** | ~80 lines | ~500 lines + refactor |
| **Testing** | RANGE_SCALP, proven instruments | New architecture, all regimes |
| **Timeline** | 1 week (shadow + 3 days staging) | 2 weeks (shadowing + staging) |
| **Rollback effort** | 10 minutes | 1 hour (more complex) |
| **Data available** | 69 candidates analyzed | Depends on staging duration |
| **Risk** | Low | Medium |
| **Expected LONG %** | RANGE_SCALP 20-30% | SPOT_BULL 70%, RANGE_SCALP 50% |

---

## Alignment with Rules and Canonicals

### From `rules.md`

> "Toda revisión periódica de pérdidas usa **AIC — Análisis Inteligente de Cierres** y debe separar A) no abrir, B) proteger después de abrir y C) reparar/calibrar configuración existente."

**Solución integrada aborda C)**: Rebalancea la configuración de dirección (B ya está OK gracias a s113 SR thesis check).

> "La única salida automática es 100% por hard risk, emergencia o tesis estructural rota/estancada."

**No cambia exits**: Solo entrada. Mismo hard stop, mismo exit tools.

> "Cualquier gate se promueve sin reportar pérdidas evitadas, ganancias sacrificadas, mejora neta."

**Medibles en Phase I**:
- Pérdidas evitadas: 9 SHORTs que perdieron donde LONG tenía mejor EV.
- Ganancias sacrificadas: 0 (LONGs no abrían antes, así que no hay ganancia real sacrificada).
- Mejora neta: PnL shadow 69 candidatos, con EV-first vs ev_symmetric.

### From `calibracion_entradas-a20ab8-v3.md`

> "Arbitro direccional, en todos los regímenes y mercados: evalúa ambos bordes S/R y las predicciones LONG Spot, LONG Futures y SHORT ejecutable."

**Solución integrada implements this exactly**:
- Phase I: RANGE_SCALP arbitraje direccional por EV (S/R como modulador).
- Phase II: Extiende a todos los regímenes con thesis builders (LONG Spot/Margin/Futures vs SHORT explícitamente).

---

## Risk Mitigation

### Phase I Risks (EV-First RANGE_SCALP)

| Risk | Likelihood | Mitigation |
|---|---|---|
| Predictor LONG sobreestima EV | Medium | Gate `min_probability ≥ 0.55` + shadow validation 48h. |
| Apertura de LONGs débiles cerca de resistencia | Low | Penalty −15% en EV + reducción de tamaño a ≤0.85×. |
| Perder ventaja de SHORT actual | Low | Structure bonus +10% a SHORT con breakdown; SHORTs fuertes siguen ganando. |
| Worst trade > −$25 | Low | Size reduction por confidence factor ya aplicado. |

### Phase II Risks (Opportunity Arbiter all regimes)

| Risk | Likelihood | Mitigation |
|---|---|---|
| Instrument routing selection error | Medium | Test thesis builders offline con 48h de datos simulados primero. |
| Utilidad predictions inaccurate across instruments | Medium | Shadow mode 48-72h con PnL simulado antes de activar. |
| Complexity of Phase II breaks Phase I gains | Low | Refactor incremental; RANGE_SCALP stays in Phase I code if Phase II rollback needed. |

---

## Timeline (Total 3 Weeks)

```
Week 1: Phase I (EV-First RANGE_SCALP)
├── Day 1-2: Implement ev_first in directional_arbiter.py (80 lines)
├── Day 3: Shadow mode activated, 48h collection
├── Day 4: Offline validation (PnL simulation, gates)
├── Day 5: Staging promotion (1 day validation)
└── Day 6-7: Production promotion if gates pass

Week 2: Phase I Production + Phase II Foundation
├── Day 8-10: Monitor Phase I in production (daily KPIs)
├── Day 10-14: Implement Opportunity Arbiter foundation
│   ├── universal_thesis_builder.py (~300 lines)
│   ├── refactor directional_arbiter → opportunity_selector
│   └── DB schema (thesis_snapshot_v1)

Week 3: Phase II (SPOT_BULL Shadow + Staging)
├── Day 15-16: SPOT_BULL shadow (24-48h collection)
├── Day 17: Offline validation (gates)
├── Day 18: Staging promotion (1 day)
├── Day 19-21: Production promotion if valid; FUTURES_SHORT/PRESERVATION shadow start

Post-Week 3: FUTURES_SHORT & PRESERVATION (1-2 weeks each, less critical)
```

---

## Implementation Checklist

### Phase I Checklist (EV-First RANGE_SCALP)

- [ ] Implement `_evaluate_range_scalp_ev_first()` in `directional_arbiter.py`
- [ ] Add 7 new config parameters for EV-First
- [ ] Add `shadow_ev_first_side` persistence to `signal_meta`
- [ ] Add shadow metrics to `/metrics/shadow-readiness`
- [ ] Test locally: 69 historical candidates, compare shadow vs actual
- [ ] Offline validation: PnL simulation, win rate, LONG %, worst trade
- [ ] Deploy to staging with `range_scalp_direction_mode = "ev_symmetric"` (shadow only)
- [ ] Collect 48h shadow data
- [ ] Gates pass? Promote: set `range_scalp_direction_mode = "ev_first"` in staging
- [ ] Staging: 1 day production-like monitoring
- [ ] Gates pass? Promote to production with auto-rollback triggers
- [ ] Production: Daily monitoring for 5-7 days

### Phase II Checklist (Opportunity Arbiter Foundation)

- [ ] Design `universal_thesis_builder.py` (shared code for all regimes)
- [ ] Implement SPOT_BULL thesis builders (LONG Spot/Margin/Futures + SHORT Margin)
- [ ] Implement `opportunity_selector.py` (replaces directional_arbiter for selected regimes)
- [ ] Add `thesis_snapshot_v1` to DB schema
- [ ] Integrate with `run_trades.py` and `staged_entry.py`
- [ ] Shadow: SPOT_BULL for 48h (no behavior change)
- [ ] Offline validation
- [ ] Staging: 1 day
- [ ] Production: if valid

---

## Success Criteria

### Phase I Success (End of Week 1)
- ✅ LONGs increase from 0% to 20-30% in RANGE_SCALP.
- ✅ PnL neto shadow ≥ +10% vs actual (69 candidates).
- ✅ Win rate shadow ≥ win rate actual.
- ✅ Worst trade shadow ≤ worst trade actual.
- ✅ Production: 3 days positive PnL without regression.

### Phase II Success (End of Week 3)
- ✅ SPOT_BULL: 70% LONG (vs 80% SHORT today).
- ✅ Utility correlation with realized PnL > 0.55.
- ✅ No regression in any regime.
- ✅ Thesis_snapshot_v1 populated for 100% of candidates.
- ✅ FUTURES_SHORT/PRESERVATION ready for Phase III.

---

## Fallback & Rollback

### Phase I Rollback (Instant)
```bash
# In config.py or environment:
range_scalp_direction_mode = "ev_symmetric"
# Restart app
docker compose restart app
# Time to revert: 10 minutes
```

### Phase II Rollback (If needed)
```bash
# Revert to Phase I (EV-First still active for RANGE_SCALP)
# Or full rollback to pre-Phase I:
git checkout HEAD~20  # (or last known good commit)
docker compose restart app
# Time to revert: 1 hour
```

---

## Key Differences vs Original Plans

### vs Original EV-First Plan
- **Better**: Positions EV-First as Phase I of broader Opportunity Arbiter, not isolated change.
- **Faster**: Phase I de 1 semana es validación para Phase II.
- **Safer**: Phase II only starts if Phase I succeeds.

### vs Original Opportunity Arbiter Plan
- **Faster**: Phase I (1 week) solves immediate problem (0 LONGs).
- **Incremental**: Refactor happens gradually; RANGE_SCALP proven first.
- **Less complex**: Phase I is 80 lines; Phase II builds on lessons.
- **Reduced risk**: If Phase II fails, Phase I still works.

---

## Conclusion

**Solución integrada = EV-First (Phase I) + Opportunity Arbiter (Phase II)**

**Best of both**:
- ✅ Pragmatismo de EV-First (1 semana a producción).
- ✅ Universalidad de Opportunity Arbiter (4 regímenes).
- ✅ Bajo riesgo (validar RANGE_SCALP primero).
- ✅ Extensibilidad (Phase II predecible después de Phase I).


# Ajuste Selectivo VolSpike Override — Implementación Completada

**Fecha:** 16 Jun 2026  
**Objetivo:** Reducir exposición a trades con PnL esperado negativo manteniendo oportunidades de breakout

---

## ✅ Cambios Implementados

### 1. Nuevos Parámetros de Control (config.py)

```python
# V2: Controles adicionales para hacer override más selectivo (jun 16 2026)
volspike_override_min_strength: float = 0.65      # Requerir alta convicción técnica
volspike_override_max_positions: int = 2            # Máx 2 posiciones override simultáneas
volspike_override_daily_limit: int = 3             # Máx 3 overrides por día
```

### 2. Validaciones Adicionales (run_trades.py)

```python
# Check 1: Strength mínimo requerido
if strength < 0.65:
    print(f"VolSpike OVERRIDE {symbol}: rechazado - strength {strength:.2f} < 0.65")
    continue

# Check 2: Límite de posiciones override simultáneas  
if len(_VOLSPIKE_POSITIONS) >= 2:
    print(f"VolSpike OVERRIDE {symbol}: rechazado - límite de 2 posiciones alcanzado")
    continue

# Check 3: Límite diario
if _today_count >= 3:
    print(f"VolSpike OVERRIDE {symbol}: rechazado - límite diario de 3 alcanzado")
    continue
```

### 3. Tracking de Métricas (run_trades.py)

```python
# Variables de tracking
_VOLSPIKE_POSITIONS: set = set()           # Symbols con override activo
_OVERRIDE_DAILY_COUNT: Dict[str, int] = {}  # date_str -> count
_OVERRIDE_PERFORMANCE: list = []            # Lista de resultados

# Al cerrar posición, registrar performance:
if symbol in _VOLSPIKE_POSITIONS:
    _OVERRIDE_PERFORMANCE.append({
        'symbol': symbol,
        'expected_pnl': _pnl_expected,
        'actual_pnl': _pnl_actual,
        'accuracy_pct': _accuracy,
        'close_reason': close_reason,
        'duration_hours': _duration_hours,
    })
```

### 4. Endpoint de Métricas (app.py)

```python
@app.get("/metrics/override-performance")
async def get_override_performance() -> Dict:
    # Retorna:
    # - summary: total_trades, win_rate, avg_accuracy, total_pnl
    # - by_symbol: métricas por símbolo
    # - recent_trades: últimos 10 trades
    # - recommendations: alertas automáticas
```

### 5. Dashboard Auditoría (dashboard.js + index.html)

- **Nueva card:** "⚡ Auditoría Override (VolSpike)"
- **Visualización:**
  - Win rate global con indicador de estado (✅/⚡/🚨)
  - Límites activos (posiciones: X/2, hoy: Y/3)
  - Tabla por símbolo con win rate y PnL
  - Últimos 5 overrides con accuracy
  - Recomendaciones automáticas

### 6. Estilos CSS (styles.css)

- Estilos para header de auditoría
- Tabla de símbolos con colores por performance
- Lista de trades recientes
- Sección de recomendaciones destacada

---

## 📊 Flujo de Decisiones Actual

```
Señal NARRATIVE entrante
         │
    ┌────▼────┐
    │ VolSpike│
    │ ≥ 1.5x  │── No ──▶ Rechazado (sigue flujo normal)
    └────┬────┘
         │ Sí
    ┌────▼──────────────┐
    │ RSI 4h < 65       │── No ──▶ Rechazado
    └────┬──────────────┘
         │ Sí
    ┌────▼──────────────┐
    │ Trend UP          │── No ──▶ Rechazado
    └────┬──────────────┘
         │ Sí
    ┌────▼──────────────┐
    │ Strength ≥ 0.65   │── No ──▶ Rechazado (NUEVO)
    └────┬──────────────┘
         │ Sí
    ┌────▼──────────────────────┐
    │ Posiciones override < 2   │── No ──▶ Rechazado (NUEVO)
    └────┬──────────────────────┘
         │ Sí
    ┌────▼──────────────────────┐
    │ Overrides hoy < 3         │── No ──▶ Rechazado (NUEVO)
    └────┬──────────────────────┘
         │ Sí
         ▼
    ✅ APROBADO
    Registra en tracking
    Ejecuta compra
```

---

## 🎯 Resultado Esperado

| Métrica | Antes | Después (48h) |
|---------|-------|---------------|
| Trades PnL negativo/día | ~5-8 | ~2-3 |
| Win rate override | ~40% | ~55%+ |
| Exposición simultánea | Ilimitada | Máx 2 pos |
| Overrides diarios | Ilimitado | Máx 3/día |
| Transparencia | Baja | Alta (dashboard) |

---

## 📈 Criterios de Éxito (48h)

- [ ] NEAR no genera más overrides hasta que accuracy mejore
- [ ] <3 posiciones con PnL esperado negativo simultáneas
- [ ] Win rate de overrides ≥ 50%
- [ ] Dashboard muestra métricas actualizadas
- [ ] Al menos 1 recomendación automática generada

---

## 🔄 Plan de Rollback

Si métricas empeoran después de 48h:

1. **Opción 1:** Subir `volspike_override_max_positions` a 3
2. **Opción 2:** Bajar `volspike_override_min_strength` a 0.60
3. **Opción 3:** Desactivar override temporalmente (set `volspike_override_enabled=false` en .env)

---

## 📝 Archivos Modificados

| Archivo | Líneas | Cambios |
|---------|--------|---------|
| `config.py` | +4 | Nuevos parámetros de control |
| `run_trades.py` | ~50 | Validaciones + tracking métricas |
| `app.py` | ~100 | Endpoint `/metrics/override-performance` |
| `dashboard.js` | ~100 | Función `renderVolSpikeAudit()` |
| `index.html` | +7 | Nueva card auditoría |
| `styles.css` | ~100 | Estilos card auditoría |

---

## 🚀 Docker Rebuild

✅ **Completado** — Contenedor reconstruido y ejecutándose

---

**Estado:** ✅ IMPLEMENTADO | **Revisión:** 48h | **Objetivo:** Maximizar ganancias reduciendo exposición a override de baja calidad

# Dashboard PnL Visibilidad — Implementación Completada

**Fecha:** 16 Jun 2026  
**Objetivo:** Mayor visibilidad de expectativas vs realidad en trades, explicación de "PnL Pred.", y métricas de accuracy del predictor.

---

## ✅ Cambios Implementados

### 1. Tablas de Posiciones Mejoradas

#### Posiciones Abiertas (`renderOpenPositions`)
- **Nuevas columnas:**
  - "Esperado al Entrar" — PnL predicho por IA al momento de compra
  - "Progreso" — Barra visual de avance hacia Take Profit
  - "Actual vs Esperado" — Comparación lado a lado con desviación
- **Interacción:** Click en fila abre modal de detalle
- **Indicadores:** 🎯 badge si tiene predicción, ⚡OVF si es overflow

#### Posiciones Cerradas (`renderClosedPositions`)
- **Nuevas columnas:**
  - "Esperado" — PnL que la IA predijo al entrar
  - "Accuracy" — Badge visual: 🎯 Preciso (≥80%), ⚡ Bueno (50-80%), ❌ Falló (<50%)
  - "Desvío" — Diferencia en USDT y porcentaje vs esperado
- **Entrada → Salida** — Precios de entrada y salida en una columna
- **Duración** — Horas totales de la posición

### 2. Nueva Card: "Expectativa vs Realidad"

**Ubicación:** Entre Posiciones Abiertas y Futuros

**Contenido:**
- **Stats (4 métricas):**
  - 🎯 Predicciones — Total con subcontador de ganando
  - 💰 Esperado — Suma de PnL esperado
  - 📈 Real — Suma de PnL real obtenido
  - 🎯 Accuracy — Precisión promedio del modelo
- **Tabla:** Últimos 6 trades (abiertos + cerrados) con:
  - Símbolo + estado (🟢 abierto / ⚫ cerrado)
  - PnL esperado vs real
  - Barra de progreso visual
  - Diferencia con flecha ↑↓
- **Explicación:** Texto explicando qué es "PnL Pred."

### 3. Modal de Detalle por Posición

**Trigger:** Click en cualquier fila de posición

**Secciones:**
1. **📍 ENTRADA** — Precio, cantidad, invertido, fecha
2. **🎯 PREDICCIÓN** — PnL esperado, probabilidad, rango de precio, modo (entrenado/aprendizaje)
3. **📊 INDICADORES** — RSI, EMA9>EMA21, Vol Spike, BB Position al entrar
4. **🛡️ TARGETS** — SL y TP con porcentajes
5. **💰 RESULTADO** — Precio actual/salida, PnL real, vs esperado, accuracy
6. **🔚 CIERRE** (solo cerradas) — Razón y fecha

### 4. Nuevos Helpers JavaScript

| Función | Descripción |
|---------|-------------|
| `fmtPrediction(pred, showDetails)` | Formatea predicción con badge de bootstrap |
| `fmtAccuracy(actual, expected)` | Calcula y muestra accuracy con iconos |
| `fmtAccuracyFromField(accuracyPct)` | Muestra accuracy desde campo pre-calculado |
| `fmtProgressBar(current, entry, target)` | Barra de progreso visual hacia TP |
| `fmtExpectedVsActual(actual, expected)` | Comparación esperado vs real con desviación |
| `renderExpectativaVsRealidad(data)` | Renderiza card consolidada |
| `showPositionDetailModal(symbol, id, status)` | Abre modal con detalle completo |

---

## 📁 Archivos Modificados

| Archivo | Líneas | Cambios |
|---------|--------|---------|
| `frontend/dashboard.js` | ~400 | Nuevos helpers, mejoras en tablas, nueva card, modal |
| `frontend/styles.css` | ~260 | Estilos para progress bars, accuracy badges, modal, card |
| `frontend/index.html` | +8 | Nueva card "Expectativa vs Realidad" |

---

## 🎨 Nuevos Estilos CSS

```css
/* Progress bars hacia TP */
.progress-container, .progress-bar, .progress-tp

/* Accuracy badges */
.accuracy-precise (🎯 verde), .accuracy-good (⚡ naranja), .accuracy-missed (❌ rojo)

/* Card Expectativa */
.expectativa-stats, .stat-box, .expectativa-table, .mini-progress

/* Modal detalle */
.modal-detail, .modal-overlay, .modal-content, .modal-section, .detail-grid
```

---

## 📊 ¿Qué significa "PnL Pred."?

**Respuesta corta:** Es lo que el modelo de IA esperaba ganar/perder al momento de comprar.

**Fórmula:**
```
expected_pnl = (prob_success × ganancia_potencial) - (prob_loss × pérdida_potencial) - comisiones
```

**Componentes:**
- `expected_pnl` — Ganancia/pérdida esperada en USDT
- `success_probability` — Confianza del modelo (0-100%)
- `price_range` — Rango de precios estimado para el ciclo
- `bootstrap_mode` — True si aún no ha aprendido del símbolo

**Interpretación:**
- 🟢 PnL Pred. > 0 y Resultado > 0: Predicción correcta
- 🟡 PnL Pred. > 0 pero Resultado < 0: Predicción falló
- 🟡 PnL Pred. < 0 pero Resultado > 0: Sorpresa positiva
- 🔴 PnL Pred. < 0 y Resultado < 0: Predicción correcta (evitó pérdida)

---

## 🎯 Accuracy del Predictor

| Badge | Rango | Significado |
|-------|-------|-------------|
| 🎯 Preciso | ≥80% | El modelo acertó casi exactamente |
| ⚡ Bueno | 50-80% | Diferencia moderada, aceptable |
| ❌ Falló | <50% | Predicción lejos de la realidad |

**Cálculo:**
```
accuracy = (PnL_real / PnL_esperado) × 100
```

---

## 🔍 Cómo Usar

1. **Ver expectativas:** Tablas ahora muestran "Esperado al Entrar" en cada posición
2. **Ver progreso:** Barra visual indica qué tan cerca estás del TP
3. **Comparar:** Columna "Actual vs Esperado" muestra desviación en tiempo real
4. **Ver detalle:** Click en cualquier posición abre modal completo
5. **Ver consolidado:** Card "Expectativa vs Realidad" resume últimos trades
6. **Evaluar accuracy:** Badge en posiciones cerradas indica precisión del modelo

---

## ✅ Verificación Post-Implementación

```bash
# Dashboard debe mostrar:
# - [x] Columna "Esperado al Entrar" en posiciones abiertas
# - [x] Barra de progreso hacia TP
# - [x] Card "Expectativa vs Realidad" entre abiertas y futuros
# - [x] Click en posición abre modal con detalle
# - [x] Badge de accuracy en posiciones cerradas
# - [x] Explicación de "PnL Pred." en card consolidada
```

---

## 📈 Próximas Mejoras (Opcional)

1. **Histórico accuracy:** Gráfico de precisión del predictor por semana
2. **Calibración:** Ajuste automático de bias por símbolo
3. **Alertas:** Notificación cuando accuracy cae <40% por 5 trades seguidos
4. **Export:** CSV de expectativas vs realidad para análisis externo

---

**Estado:** ✅ COMPLETADO | **Docker:** Rebuild aplicado | **Commit:** dashboard-pnl-visibilidad

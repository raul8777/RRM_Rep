# Notas de desarrollo

- El agente debe priorizar datos de Binance y TradingView.
- Las noticias deben provenir de Coindesk y Reuters mediante RSS para asegurar información verificable.
- El modo inicial será `suggest`, con opción de `auto` más adelante.
- Mantener la implementación privada y local.

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

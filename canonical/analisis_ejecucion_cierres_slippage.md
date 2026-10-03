# Análisis de ejecución de cierres y mitigación de slippage

## Estado de implementación en Staging — 12 sep 2026

La política está desplegada activamente en Staging, sin modo shadow decisional. Los cierres no urgentes usan `LIMIT FOK` full-size cuando el libro permite calcular un VWAP dentro del límite; hard-risk/emergencia usan `MARKET` inmediato y cualquier FOK incompleto escala a `MARKET`. La contabilidad sigue dependiendo del fill real de Binance.

La instrumentación registra precio esperado contra precio real, VWAP, slippage, PnL, costes, tipo de orden, reintentos, escalamiento, profundidad y timestamps. El dashboard expone esta evidencia en `close_execution_active`. La muestra posterior al despliegue queda pendiente porque todavía no hubo cierres nuevos desde el reinicio de `app_stg`; los conteos históricos no se usan para declarar cobertura del cambio.

## Objetivo

Definir el comportamiento recomendado del sistema cuando el precio observado al decidir un cierre difiere del precio promedio realmente ejecutado por Binance. Este documento parte del caso OPUSDT en Binance Futures Testnet y debe utilizarse como insumo de diseño; no representa por sí mismo una autorización para cambiar producción.

## Mecánica correcta de ejecución

La contraparte depende del lado de la posición:

- Abrir un SHORT: el sistema vende contra compradores disponibles en el `bid`.
- Cerrar un SHORT: el sistema compra contra vendedores disponibles en el `ask`.
- Cerrar un LONG: el sistema vende contra compradores disponibles en el `bid`.

Una orden MARKET garantiza prioridad de ejecución, no garantiza precio. Si no existe profundidad suficiente en el mejor nivel, Binance consume sucesivos niveles del libro. Cada fragmento resultante es un `fill`; varios fills no implican varias aperturas ni varias operaciones.

## Evidencia: OPUSDT

Posición:

- Instrumento: OPUSDT Futures Testnet.
- Dirección: SHORT.
- Cantidad: `20,725.3 OP`.
- Entrada: `0.0964`.
- Cierre: 100%.
- `partial_close=false`.

Al decidir el cierre, aproximadamente a las 13:40:42 México:

- Precio observado: `0.0960`.
- PnL estimado: `+7.4926 USDT`.
- Motivo: `profit_realization_supervisor_full`.
- Fracción solicitada: `close_pct=1.0`.

Binance ejecutó la compra MARKET de cierre aproximadamente tres segundos después mediante estos fills:

| Cantidad | Precio |
|---:|---:|
| 341.8 | 0.0962 |
| 184.8 | 0.0962 |
| 20,198.7 | 0.0963 |

Precio promedio ponderado real:

```text
0.096297459
```

Resultado:

```text
PnL estimado al decidir:  +7.4926 USDT
PnL realizado Binance:    +2.1252 USDT
PnL neto tras comisiones: +0.5277 USDT
Slippage aproximado:       0.31%
Beneficio consumido:       ~93%
```

Binance actuó correctamente según el libro disponible. La reconciliación y la contabilidad también reflejaron los fills reales. El problema de diseño es que la política de cierre no protegió suficientemente el beneficio frente al impacto de mercado.

## Estado actual del sistema

Los cierres normales de Futures usan órdenes MARKET:

- `FuturesClient.close_long`: SELL MARKET.
- `FuturesClient.close_short`: BUY MARKET.

El sistema cuenta con filtros de liquidez y slippage para evaluar entradas, pero presentan estas limitaciones:

1. Utilizan volumen de 24 horas, no profundidad real del libro.
2. Suponen un spread conservador fijo de aproximadamente `0.05%`.
3. En Testnet se omite el filtro de volumen porque ese volumen no representa la liquidez de producción.
4. No calculan el VWAP esperado para ejecutar la cantidad completa.
5. No gobiernan específicamente la ejecución de cierres MARKET.
6. La reserva configurada de slippage es aproximadamente `0.052%`, mientras OPUSDT experimentó cerca de `0.31%`.

El contrato económico ya contempla fees, slippage y funding, pero el slippage es una estimación configurada y no una simulación del libro en tiempo real.

## Restricciones canónicas

La implementación debe respetar las reglas vigentes:

- `exit_fractionation_mode=disabled` prohíbe salidas parciales automáticas.
- Una salida automática debe cerrar el 100% por hard risk, emergencia o tesis estructural rota/estancada.
- Hard risk tiene precedencia sobre la optimización de precio.
- El PnL Futures contractual es `(mark-entry)*qty` para LONG y `(entry-mark)*qty` para SHORT; nunca se multiplica nuevamente por leverage.
- La unidad de análisis es la tesis `(symbol, entry_time)`, no cada fill del exchange.

Los fills parciales internos de una orden completa no deben presentarse como aperturas separadas, escalonamientos o tomas parciales de profit.

## Diagnóstico

No todo está incorrecto:

### Correcto

- Las órdenes MARKET se ejecutan conforme al libro disponible.
- Los fills de Binance son la fuente de verdad del precio promedio y PnL realizado.
- La posición OPUSDT se cerró completamente.
- El PnL reconciliado fue correcto.
- MARKET evitó dejar exposición residual.

### Incompleto o no óptimo

- Una única política MARKET se utiliza para cierres con urgencias económicas muy diferentes.
- No existe un límite explícito de impacto de mercado antes de un cierre no urgente.
- No se calcula el VWAP ejecutable para el 100% de la cantidad.
- No se compara el beneficio neto posterior al fill estimado contra el beneficio que se intenta proteger.
- No existe escalamiento controlado de orden limitada a MARKET según urgencia y tiempo.

## Política recomendada por nivel de urgencia

### Nivel A: hard risk y emergencia

Ejemplos:

- Hard stop.
- Pérdida máxima por trade.
- Riesgo de liquidación.
- Error de paridad con Binance.
- Tesis violentamente invalidada.

Comportamiento:

```text
MARKET o STOP_MARKET inmediato.
Aceptar slippage.
Priorizar eliminación del riesgo.
```

Una orden limitada podría no ejecutarse y dejar una exposición peligrosa abierta. No debe introducirse una protección de precio que debilite hard risk.

### Nivel B: invalidación estructural no crítica

Comportamiento recomendado:

1. Consultar `bookTicker` y profundidad del libro.
2. Calcular el VWAP para cerrar el 100%.
3. Intentar una orden LIMIT FOK dentro de una tolerancia de slippage.
4. Si falla, refrescar el libro y reintentar durante una ventana breve.
5. Si el precio se deteriora, aumenta la urgencia o expira el plazo, escalar a MARKET.

### Nivel C: toma de beneficio o cierre blando

Antes de ordenar, calcular:

```text
beneficio_neto_post_ejecucion =
    pnl_al_vwap_estimado
    - comisiones_pendientes
    - funding
    - reserva_de_slippage_adicional
```

Si la ejecución estimada consume el beneficio mínimo protegido:

- No enviar MARKET inmediatamente.
- Intentar LIMIT FOK dentro del límite económico.
- Revaluar durante una ventana corta.
- Ejecutar MARKET únicamente si la tesis se invalida o aumenta la urgencia.

## Compatibilidad con la prohibición de parciales

Tipos de orden:

- GTC: puede llenarse parcialmente y permanecer abierta; no es la opción preferida.
- IOC: puede ejecutar una fracción y cancelar el resto; entra en conflicto con la regla de salida total.
- FOK: ejecuta el 100% o nada; es el tipo más compatible con `exit_fractionation_mode=disabled`.
- MARKET: busca cerrar el 100%, pero no limita el precio.

Secuencia recomendada para cierres no críticos:

```text
LIMIT FOK -> relectura del libro -> reintento/reprecio -> MARKET si aumenta la urgencia
```

Para hard risk:

```text
MARKET inmediato
```

## Cálculos previos obligatorios

Antes de un cierre no urgente se debería obtener:

- Mejor bid y ask.
- Spread actual en bps.
- Profundidad acumulada por nivel.
- VWAP esperado para la cantidad completa.
- Slippage esperado en bps y USDT.
- Fees de salida.
- Funding pendiente.
- PnL neto esperado después de ejecución.
- Distancia al hard stop.
- Tiempo máximo permitido para intentar precio limitado.

Para cerrar un SHORT debe calcularse un precio máximo de compra que preserve el mínimo resultado neto requerido. Para cerrar un LONG debe calcularse un precio mínimo de venta con el mismo objetivo.

## Guard económico propuesto

Un cierre blando puede usar una condición conceptual como:

```text
execute_soft_exit =
    expected_net_after_fill >= minimum_locked_profit
    OR urgency_escalated
```

Donde:

```text
expected_net_after_fill =
    gross_pnl_at_full_size_vwap
    - verified_or_estimated_fees
    + funding
```

El guard no debe bloquear hard risk.

## Control por símbolo y venue

Mantener estadísticas rodantes por `symbol`, `trade_type` y venue:

- Slippage estimado versus realizado.
- Spread promedio y percentiles.
- Profundidad disponible para el notional utilizado.
- Tiempo hasta fill.
- Reintentos FOK.
- PnL perdido por impacto.

Si un símbolo presenta slippage excesivo repetidamente:

- Reducir sizing futuro.
- Exigir mayor profundidad antes de entrar.
- Aumentar el beneficio mínimo para cierres blandos.
- Aplicar cooldown.
- Bloquear nuevas entradas cuando los costos ejecutables invaliden el EV.

## Observabilidad requerida

Cada cierre debe registrar:

- Precio observado al decidir.
- Timestamp de decisión.
- Mejor bid/ask.
- Snapshot o resumen de profundidad.
- VWAP estimado para el 100%.
- Tipo de orden seleccionado.
- Precio límite y tolerancia.
- Número de reintentos.
- Timestamp de envío y fill.
- Precio promedio real.
- Slippage estimado y real.
- PnL antes de ejecución.
- PnL realizado bruto.
- Comisiones, funding y PnL neto.
- Motivo de escalamiento a MARKET, si ocurrió.

El dashboard debe distinguir:

- Precio de decisión.
- Precio ejecutado promedio.
- Slippage real.
- Fills del exchange.
- Aperturas/escalonamientos reales.
- Cierre total versus parcial.

Los fills de una sola orden no deben activar el control de expansión de múltiples aperturas.

## Testnet versus producción

Binance Futures Testnet puede presentar:

- Menor profundidad.
- Spreads artificiales.
- Libros discontinuos.
- Slippage considerablemente mayor que Mainnet.

Por tanto:

- OPUSDT demuestra una vulnerabilidad real de la política de ejecución.
- El `0.31%` observado no debe extrapolarse directamente a producción.
- Los umbrales de producción deben calibrarse con profundidad y fills de Mainnet.
- Testnet debe seguir utilizándose para verificar comportamiento ante libros adversos.

## Diseño recomendado

Implementar un ejecutor adaptativo de cierres con esta interfaz conceptual:

```text
execute_close(
    symbol,
    side,
    full_quantity,
    reason,
    urgency,
    minimum_locked_profit,
    max_slippage_bps,
    deadline_ms
)
```

Responsabilidades:

1. Validar la cantidad real en Binance.
2. Clasificar la urgencia.
3. Simular el fill completo contra profundidad.
4. Elegir MARKET o LIMIT FOK.
5. Reintentar dentro del deadline.
6. Escalar a MARKET cuando corresponda.
7. Confirmar posición final igual a cero.
8. Consultar `userTrades`.
9. Persistir precio promedio y PnL verificados.
10. Auditar estimación, ejecución y desviación.

## Criterios de aceptación

Una implementación futura debe demostrar:

1. Hard risk nunca queda bloqueado esperando mejor precio.
2. Ningún cierre automático deja una posición residual por diseño.
3. Los cierres blandos no usan MARKET cuando el impacto estimado consume el beneficio protegido, salvo escalamiento documentado.
4. El precio promedio persistido coincide con `userTrades`.
5. El PnL persistido coincide con realized PnL, fees y funding de Binance.
6. Los fills no se interpretan como múltiples operaciones.
7. La lógica LONG y SHORT es simétrica.
8. Todas las decisiones y reintentos quedan auditados.
9. Existen pruebas de FOK exitoso, FOK rechazado, escalamiento a MARKET, hard risk y libro insuficiente.
10. La evaluación se realiza primero en staging y se reportan pérdidas evitadas, ganancias sacrificadas, mejora neta y tamaño de muestra antes de promoción.

## Recomendación final

No reemplazar indiscriminadamente todas las órdenes MARKET por LIMIT. Eso cambiaría slippage por riesgo de no ejecución.

La política recomendada es:

| Tipo de cierre | Orden inicial | Escalamiento |
|---|---|---|
| Hard risk o emergencia | MARKET/STOP_MARKET | Inmediato, sin espera |
| Invalidación estructural | LIMIT FOK | Reprecio breve y MARKET por deadline/urgencia |
| Toma de beneficio | LIMIT FOK con límite económico | Revaluación; MARKET solo si aumenta la urgencia |
| Manual | Preview de impacto | Elección explícita del operador |

La contabilidad verificada por Binance es correcta. El área a mejorar es la calidad de ejecución: profundidad real, VWAP para tamaño completo, límites económicos y selección adaptativa de orden según urgencia.

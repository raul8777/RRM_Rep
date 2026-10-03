# Análisis: los SHORTs de RANGE_SCALP fallan por dirección, no por gestión de salida

> **Actualización s140 — 2026-09-28:** el diagnóstico de dirección se mantiene, pero la revalidación de cualquier candidata reutilizada ahora exige evidencia fresca. El snapshot histórico identifica la oportunidad; no autoriza reutilizar indicadores, predicción, tesis, contrato, target/stop ni tamaño. Ver `canonical/calibracion_entradas-a20ab8-v3.md`.

## Contexto

Durante la calibración Super MIMIC del sistema de trading se detectó una brecha entre el PnL contractual proyectado y el realizado. Al instrumentar salidas reales (SL/TP intrabar + time stop 2–4h) se obtuvo la primera medición válida del comportamiento de entrada.

Este documento resume el hallazgo para revisión por Copilot antes de implementar cambios.

## Datos observados

Población analizada: 1,154 entradas SHORT registradas en los resultados persistidos de Entry (ventanas development de Super MIMIC, seeds 20260920/20260921).

```text
Tesis dominante: SHORT_RESISTANCE_REJECTION  → 1,142 (99%)
                 SHORT_SUPPORT_BREAKDOWN     →    12

Primer nivel tocado (intrabar, velas 1h):
  SL: 1,119 (97%)
  TP:    35 (3%)

Excursión adversa mediana (MAE): +5.19% contra la posición
Excursión favorable mediana (MFE): +0.85% a favor
```

Resultado económico observado en simulación con salidas reales:

```text
SHORT: 1,024 cierres | −$6,995 | win rate 7.5%
LONG:     65 cierres |  +$58   | win rate 44.6%
```

## Hipótesis descartadas

### "El stop está demasiado cerca"

No. De los 1,119 shorts liquidados por SL, solo **17** habrían alcanzado el TP dentro de las 24h siguientes. Ampliar el stop convierte 97% de pérdidas en pérdidas más grandes. La geometría no es el problema.

### "Es culpa del lifecycle"

No. El análisis es contrafactual sobre datos de entrada pura, sin intervención de lifecycle. La dirección elegida era incorrecta desde la apertura.

## La evidencia direccional

Flip-test sobre las mismas 1,154 entradas:

| Escenario | Resultado |
|---|---|
| SHORT real (stop +0.73%, target −1.75%) | SL=1,119 / TP=35 → ≈ −$6,892 |
| LONG espejo simétrico (stop −0.73%, target +1.75%) | SL=954 / TP=200 → ≈ +$4,005 |
| LONG scalping corto (TP +0.73%, SL −1.75%) | TP=1,119 / SL=35 → 97% WR |

Interpretación: el precio **sube hacia arriba** tras la señal de "rechazo de resistencia" en la gran mayoría de casos. Estas rupturas alcistas se catalogaron erróneamente como rechazos. La información direccional existe — el sistema la aplica con el signo equivocado.

## Causa raíz en el código

En `run_trades.py` (~7874–7898):

```python
if support_context:
    long_prediction        = predict_range_reversion(..., trade_side="LONG")
    futures_long_prediction = predict_range_reversion(..., trade_side="LONG")
elif resistance_context and short_executable:
    rejection       = predict_range_reversion(..., trade_side="SHORT")
    short_prediction = rejection
```

Y en `directional_arbiter.py` (~113):

```python
long_thesis  = {"SUPPORT": "LONG_SUPPORT_BOUNCE",   "RESISTANCE": "LONG_RESISTANCE_BREAKOUT"}
short_thesis = {"SUPPORT": "SHORT_SUPPORT_BREAKDOWN","RESISTANCE": "SHORT_RESISTANCE_REJECTION"}
```

El árbitro **sí contempla** la tesis `LONG_RESISTANCE_BREAKOUT`. El problema está antes: en `resistance_context` solo se **genera** el candidato SHORT. El LONG llega al árbitro con `long_prediction=None` → contrato inválido → SHORT gana por defecto sin competencia.

La evidencia de rechazo además llega sistemáticamente vacía:

```text
rejection_evidence: confirmed=false, explicit_rejection=false, breakout_pressure=false
```

y el contrato sigue siendo ejecutable — la proximidad a la resistencia basta.

Conclusión: el sistema abre shorts en rupturas alcistas porque es la única carta sobre la mesa en esa zona. No elige mal entre dos opciones; nunca le presentan la opción LONG.

## Propuesta de corrección

1. **Generar ambos candidatos en cada zona**:
   - `resistance_context` → evaluar `SHORT_RESISTANCE_REJECTION` **y** `LONG_RESISTANCE_BREAKOUT`.
   - `support_context` → evaluar `LONG_SUPPORT_BOUNCE` **y** `SHORT_SUPPORT_BREAKDOWN`.

2. El árbitro decide por EV contractual — el mecanismo ya existe y no requiere gate direccional ni quotas.

3. `rejection_evidence` y `breakout_pressure` se convierten en moduladores del EV (un short con breakout_pressure alto pierde EV naturalmente frente al breakout long).

4. Recalibrar la geometría LONG en ruptura (stop bajo la zona, target en el siguiente nivel) con los contrafactuales ya construidos — el mirror mostró que 0.73% es ruido intrabar.

## Estimación de impacto

De 1,154 shorts, ~1,119 eran rupturas alcistas. Si la comparación simétrica hubiera existido y el modelo de probabilidad capturara el sesgo alcista, la cohorte −$6,892 podría haberse transformado aproximadamente en +$4,000 a geometría espejo — más con geometría LONG recalibrada. Swing potencial ≈ $10,900 sobre la muestra.

## Caveats

- La estimación usa el precio de entrada idéntico; slippage real del flip no está modelado.
- El outcome LONG-espejo usa la geometría del contrato short — la geometría correcta del breakout puede ser mejor.
- El predictor puede calibrar distinto LONG vs SHORT; el resultado final depende de `predict_range_reversion` llamado en ambas direcciones.
- Solo 17/1,119 se salvarían con stops más anchos — la geometría es secundaria a la dirección.
- No se han usado ventanas OOS en ningún análisis.

## Corrección s140 — no reutilizar evidencia caducada

La observación operativa posterior al despliegue bilateral mostró que `ready`/`validated` podía permanecer activo y ser reintentado con metadata de una validación anterior. En la última hora previa a esta corrección hubo cuatro ciclos completos con candidatos seleccionados pero cero ejecuciones; los bloqueos únicos fueron 14 candidatos por recompensa/riesgo stale, uno por refresh de precio fallido, tres por falta de retorno bilateral positivo y dos por contrato Spot stale. Los checks repetidos no representan oportunidades independientes.

La implementación ahora vuelve a ejecutar el predictor desde análisis fresco cuando una candidata no reaparece en el top del ciclo, persiste `fresh_signal_meta` y `fresh_prediction`, y hace que FastPath y el camino normal consuman ese snapshot. La re-arbitración vuelve a comparar LONG/SHORT y venues con el contexto actual; si no puede formar evidencia/contrato completo, bloquea con razón auditable. La prueba específica de predictor fresco y la suite de lifecycle/arbitraje quedaron verdes; el cambio se desplegó solo en Staging.

## Preguntas abiertas para Copilot

1. ¿`predict_range_reversion` produce `success_probability` calibrable para LONG en zona de resistencia? Necesitamos que la rama LONG_RESISTANCE_BREAKOUT sea evaluable.
2. ¿`resistance_context`/`zone` usa niveles que pueden estar desactualizados respecto a breakout ya confirmado?
3. ¿El orden `elif` entre `support_context` y `resistance_context` puede ocultar el caso mixto (precio en medio del rango)?
4. ¿Se mantiene simetría de sizing — `short_size` hoy fijado a `range_scalp_short_usdt_per_trade`?

<!-- COPILOT_UPDATE_START: 2026-09-26 -->
<!-- Autor: GitHub Copilot | Propósito: propuesta de máxima ganancia neta y mayor cobertura -->
## [COPILOT UPDATE 2026-09-26] Decisión orientada a máxima ganancia neta y mayor número de posiciones

### Principio rector

El objetivo no debe ser maximizar el número bruto de aperturas. Debe ser maximizar:

```text
ganancia neta esperada del portfolio
    = número de oportunidades ejecutadas
      × EV neto por oportunidad
      × probabilidad de fill correcto
      − drawdown − coste de capital − errores de dirección
```

Por tanto, la estrategia correcta es **abrir el mayor número de posiciones que sigan teniendo EV neto positivo después de fees, slippage, funding, riesgo y correlación**. Un gate que solo bloquea candidatos debe demostrar que evita pérdidas; un gate que solo aumenta cobertura debe demostrar que no convierte cobertura en volumen perdedor.

### Cambio prioritario: mercado bilateral, no sesgo de zona

La unidad de decisión debe ser una pareja de contratos direccionales por símbolo y evento:

| Contexto | Contrato LONG | Contrato SHORT |
|---|---|---|
| Resistencia | `LONG_RESISTANCE_BREAKOUT` | `SHORT_RESISTANCE_REJECTION` |
| Soporte | `LONG_SUPPORT_BOUNCE` | `SHORT_SUPPORT_BREAKDOWN` |

Cada contrato debe tener su propia predicción, target, stop, coste, sizing y probabilidad calibrada. La zona solo debe activar la competición; no debe determinar el lado. El árbitro debe seleccionar el mayor `expected_net_pnl` ajustado por riesgo y, si ninguno supera el umbral, abstenerse.

Esto corrige el defecto observado en `run_trades.py`: el árbitro es bilateral, pero la preparación de predicciones todavía recalibra solo una rama mediante `elif`. La corrección conceptual es generar ambos lados antes de arbitrar, incluyendo casos donde soporte y resistencia estén simultáneamente activos.

### Cómo aumentar aperturas sin degradar el edge

1. **Eliminar el monopolio de SHORT en resistencia.** Toda resistencia debe producir dos hipótesis evaluables. El caso alcista no puede competir usando únicamente un predictor LONG genérico si existe un predictor específico de breakout.
2. **Usar `ABSTAIN` solo cuando ambos contratos fallen.** No bloquear por falta de confirmación secundaria si el contrato tiene geometría válida, EV neto positivo y suficiente probabilidad.
3. **Separar elegibilidad de preferencia.** `spot_executable`, `futures_executable`, capital, slots y tamaño mínimo determinan si un contrato puede ejecutarse; no deben convertir una preferencia SHORT en una prohibición de LONG.
4. **Priorizar por EV neto por unidad de capital y por unidad de riesgo.** El número de posiciones debe crecer mediante candidatos independientes, no mediante tamaños mayores en señales correlacionadas.
5. **Permitir posiciones simultáneas solo hasta el límite de correlación y capital.** El QUBO debe seguir siendo el control de concentración; no sustituirlo por un límite fijo de posiciones.
6. **Mantener sizing asimétrico cuando la evidencia lo justifique.** SHORT futures, LONG futures y SPOT LONG no tienen los mismos costes, leverage, funding ni riesgo de liquidación. El sizing común solo debe ser el techo; cada contrato debe reducirlo por su riesgo real.
7. **No esperar confirmaciones staged innecesarias.** La confirmación direccional debe impedir entradas prematuras, no retrasar una oportunidad ya validada. Si una candidata ya está `validated` y no esperó por capital o slots, debe pasar al preflight inmediatamente según s110.

### Reglas por instrumento

#### SPOT

- Mantener LONG como vía principal cuando el breakout de resistencia tenga presión, volumen y momentum positivos.
- No abrir SHORT spot-margin solo para aumentar el conteo si no supera a LONG después de costes y riesgo.
- Si no hay futures executable, permitir igualmente `SPOT_LONG` cuando el contrato LONG tenga EV positivo.
- Medir la ganancia incremental de cada nueva posición contra el coste de capital inmovilizado y la correlación con posiciones SPOT existentes.

#### FUTURES

- Permitir `FUTURES_SHORT` únicamente cuando la predicción SHORT sea superior a LONG con costes de funding y riesgo de squeeze incluidos.
- Permitir `FUTURES_LONG` en resistencia cuando el breakout tenga mayor retorno neto, sin imponer una preferencia estructural por SHORT.
- Mantener filtros de BTC, funding, momentum y vela de confirmación; son controles de riesgo de derivados, no obstáculos a eliminar para subir el número de operaciones.
- Medir por separado el PnL neto de `FUTURES_LONG` y `FUTURES_SHORT`; un resultado combinado puede ocultar que una dirección destruye capital.

### Política por perfiles A/B/C

- **Perfil A:** máxima cobertura dentro de contratos con EV neto claramente positivo. Puede aceptar menor probabilidad si el retorno por riesgo compensa, pero nunca una geometría inválida.
- **Perfil B:** criterio base para rollout. Requiere calibración estable y balance entre cobertura, profit factor y drawdown.
- **Perfil C:** no debe apagarse; debe funcionar como modo defensivo con menos operaciones y mayor exigencia de probabilidad, liquidez y confirmación.

La optimización no debe seleccionar siempre A por ofrecer más operaciones. El selector debe comparar rendimiento real por régimen y perfil. La expansión de cobertura se acepta solo si mejora el portfolio, no si mejora únicamente el conteo.

### Qué modificaría primero, en orden

1. **Generación bilateral de contratos** en resistencia y soporte.
2. **Predicciones específicas por tesis**, especialmente `LONG_RESISTANCE_BREAKOUT`.
3. **Resolución de contexto mixto**: no usar `elif`; conservar ambos eventos y dejar que la evidencia y la economía decidan.
4. **Calibración separada LONG/SHORT y SPOT/FUTURES**, con costes reales y outcomes netos.
5. **Ranking de candidatos por retorno neto marginal del portfolio**, no por score bruto ni por cantidad de señales.
6. **Ajuste de thresholds por perfil**, empezando por B y después A/C, sin cambiar todavía stops ni leverage.
7. **Expansión gradual de `max_open_per_cycle`** únicamente después de comprobar que la cola no está monopolizada por candidatos fallidos y que el capital se distribuye entre contratos viables.

No cambiaría primero el stop, el leverage, el multiplicador de Kelly ni el filtro de liquidez. Esos cambios amplifican tanto el edge como el error de dirección y harían imposible atribuir la mejora.

### Gates de aceptación

El cambio bilateral debe pasar todos estos gates antes de activar capital real:

1. **Gate direccional:** en una ventana OOS, la dirección seleccionada debe superar al lado contrario en EV neto realizado y no solo en probabilidad prevista.
2. **Gate económico:** PnL neto, profit factor y retorno sobre riesgo deben mejorar después de fees, slippage y funding.
3. **Gate de cobertura:** aumentar el número de aperturas sin que el PnL por operación y el drawdown empeoren más allá del límite definido.
4. **Gate de portfolio:** el incremento de posiciones no puede aumentar materialmente la correlación ni consumir slots con candidatos de baja viabilidad.
5. **Gate de ejecución:** toda apertura debe pasar preflight y fill confirmado; nunca persistir `trades_open` antes del fill.
6. **Gate de muestra:** medir por régimen, dirección, instrumento y perfil; no aprobar por el agregado de una sola ventana.

### Rollback

La generación bilateral debe tener un flag independiente y reversible. El rollback debe restaurar la ruta anterior de arbitraje sin modificar posiciones abiertas, historial ni parámetros de salida. Si mejora la cobertura pero cae el EV neto, se debe reducir el universo o subir el umbral de aceptación, no ampliar stops para maquillar el resultado.

### Persistencia y auditoría obligatoria

Cada decisión debe conservar en `ready_candidates.data` y en la metadata de la posición:

- ambos contratos evaluados;
- tesis, lado, instrumento y tamaño solicitado;
- probabilidad, EV bruto y EV neto;
- stop, target, fees, slippage y funding;
- evidencia de rechazo o breakout;
- contrato seleccionado, contrato alternativo y razón de selección;
- `ready_candidate_id` y estado de confirmación direccional.

Debe insertarse un evento en `trades_audit` para la comparación bilateral y otro para la ejecución/fill. El dashboard debe mostrar seleccionadas, descartadas, abstenciones, aperturas efectivas y el EV que tenía la alternativa no seleccionada.

### Validación requerida antes de tocar código

La siguiente investigación debe ejecutarse sobre ventanas development y OOS, con resolución intrabar suficiente:

- reconstruir los 1,154 casos con ambos contratos;
- excluir o resolver explícitamente velas con stop y target ambiguos;
- separar los 65 casos aún no cerrados;
- comparar SHORT actual, LONG genérico, LONG breakout específico y `ABSTAIN`;
- medir SPOT LONG, FUTURES LONG y FUTURES SHORT por separado;
- segmentar por A/B/C y por régimen;
- repetir con costes 1x, 1.5x y 2x;
- comparar contra baseline 3y/60000 y contra un holdout temporal no usado en calibración.

La recomendación final es **aumentar el número de posiciones mediante cobertura bilateral y selección económica, no mediante relajación indiscriminada de gates**. El primer cambio debe permitir que el sistema vea todas las direcciones plausibles; el segundo debe asegurar que solo abra las que tengan mayor ganancia neta esperada y capacidad real de ejecución.

<!-- COPILOT_UPDATE_END: 2026-09-26 -->

<!-- DEVIN_UPDATE_START: 2026-09-27 -->
## [DEVIN UPDATE 2026-09-27] Correcciones del diagnóstico con datos post-verificación

La investigación posterior reveló que el diagnóstico inicial era parcialmente incorrecto y el mecanismo real de fracaso es más profundo:

### Corrección 1: el árbitro SÍ era bilateral

Los `contract_diagnostics` muestran que LONG y SHORT sí compiten en ambas zonas:

```text
Selección del árbitro (300 muestras):
  RESISTANCE → LONG ganó 137 | SHORT ganó 33
  SUPPORT    → SHORT ganó 123 | LONG ganó 7

Cuando SHORT gana: median short_ev $15.78 vs long_ev $5.13
                   short_p 0.693 vs long_p 0.745
```

El problema no es la ausencia del candidato LONG — es **por qué el SHORT gana**.

### Corrección 2: mecanismo real — geometría con stops sub-ruido

```text
Distancia stop efectiva mediana: 0.28% (0.26×ATR 1h)
Target mediano:                  3.9%  (~3×ATR)
ATR(14,1h) mediano:              1.32%
```

Tasa de SL-hit por ratio stop/ATR en las 1,154 entradas:

| Stop/ATR | SL rate | n |
|---|---:|---:|
| <0.5 | 95% | 704 |
| 0.5–1 | 100% | 380 |
| 1–1.5 | 100% | 41 |

**El stop mediano es un cuarto del rango de una vela 1h.** El modelo EV `p×reward − (1−p)×risk` ve risk pequeño + predictor confiado (~0.80) → `expected_return_on_risk` inflado → SHORT gana arbitraje → el mercado lo liquida en ~1 vela por ruido puro.

### Corrección 3: ambas direcciones están invertidas

Deriva real 24h después de cada señal (161 entradas únicas):

```text
Tras señal SHORT (resistencia): precio SUBE  +1.67% media
Tras señal LONG  (soporte):     precio BAJA  −1.37% media
```

Flip-test con geometría ATR-calibrada (entradas únicas, dirección opuesta a la señalada):

```text
SHORT-señal → LONG:  EV positivo en 8/8 celdas (mejor: tp1.5×ATR/sl2.0×ATR, +1.03%, WR 87%)
LONG-señal  → SHORT: EV positivo en 8/8 celdas (mejor: tp1.5×ATR/sl2.0×ATR, +2.07%, WR 90%)
```

### Interpretación

La señal de **ubicación** (precio en nivel S/R + volatilidad) es buena — detecta eventos reales. La asunción de **reversión** está sistemáticamente equivocada en este régimen: los "rechazos de resistencia" eran rupturas alcistas y los "rebotes de soporte" eran rupturas bajistas. El sistema fada movimientos que continúan.

> Nota metodológica: la deduplicación (1,154 → 161 puntos únicos) revela que el 87% de los "trades" eran la misma decisión de entrada repetida en jobs paralelos con distintos parámetros — no re-entradas productivas del sistema. Esto no invalida el análisis (el patrón por entrada única es el mismo), pero las magnitudes agregadas ($−6,892, +$4,005) estaban infladas ~8×.

### Corrección propuesta (Entry stage)

1. **Noise floor bilateral** (`range_scalp_min_stop_atr_multiple`): un stop más cercano que N×ATR no es ejecutable — el EV binario no puede expresar hit-rates de ~95% por ruido. Ya implementado, default 0 (off), calibrable en grid {1.0, 1.5, 2.0}.

2. **Penalización por evidencia contradictoria**: cuando `rejection_evidence.breakout_pressure=true`, la probabilidad del contrato SHORT debe reducirse (factor `range_scalp_rejection_breakout_penalty`), dejando que el LONG breakout gane el arbitraje por mérito propio.

3. **La comparación correcta no es "short vs long del mismo tipo"** — es "reversión vs continuación". En resistencia, las dos hipótesis son `SHORT_RESISTANCE_REJECTION` (fade) vs `LONG_RESISTANCE_BREAKOUT` (follow). El árbitro ya las compara; el problema era que el SHORT llegaba con probabilidad inflada y risk artificialmente pequeño.

4. **No flip ciego**: invertir toda señal sería overfitting a 3 ventanas. La corrección estructural es que la evidencia de momentum/breakout module la probabilidad del lado "fade" — exactamente el propósito de `rejection_evidence` que hoy se registra pero no usa.

### Calibración pendiente (para el próximo Entry dirigido)

| Parámetro | Grid sugerido | Justificación |
|---|---|---|
| `range_scalp_min_stop_atr_multiple` | {1.0, 1.5, 2.0} | Datos: incluso 1.5×ATR muestra 100% SL — el umbral real puede ser ≥2 |
| `range_scalp_rejection_breakout_penalty` | {0.3, 0.5, 0.7} | Cuánto castigar SHORT con breakout_pressure |
| `entry_time_stop_hours` | {2, 4} | Ya en grid |
| Geometría tp/sl por ATR | tp {0.75, 1.5}×ATR, sl {1.0, 1.5}×ATR | Celdas con EV positivo en flip-test |

<!-- DEVIN_UPDATE_END: 2026-09-27 -->

<!-- COPILOT_UPDATE_START: 2026-09-27 -->
<!-- Autor: GitHub Copilot | Alcance: solo entradas; lifecycle deliberadamente fuera de alcance -->
## [COPILOT UPDATE 2026-09-27] Análisis de los nuevos hallazgos: corregir entradas antes de aumentar cobertura

### Veredicto actualizado

Los nuevos datos corrigen el diagnóstico anterior. El problema principal **no es que falte el candidato LONG**: el árbitro ya compara LONG y SHORT. El problema de entrada es una combinación de tres fallos:

1. **Geometría de riesgo inválida para el horizonte de decisión.** Un stop mediano de `0.28%`, equivalente a `0.26×ATR(1h)`, permite que el ruido normal liquide la entrada. El modelo calcula un riesgo artificialmente pequeño y puede inflar `expected_return_on_risk` aunque la probabilidad de acertar la dirección sea mala.
2. **Sesgo de reversión contra un mercado que continúa.** En resistencia, el sistema interpreta continuación alcista como rechazo; en soporte, interpreta continuación bajista como rebote. La señal de ubicación parece útil, pero el signo de la tesis está invertido.
3. **Evidencia repetida entre jobs.** Los `1,154` registros no equivalen a `1,154` oportunidades independientes. La unidad estadística provisional es `161` entradas únicas. Las magnitudes agregadas deben tratarse como evidencia de patrón, no como PnL desplegable esperado.

El orden correcto para maximizar ganancias es: **hacer que el contrato económico sea honesto, corregir la dirección y solo después aumentar el número de entradas**. Aumentar cobertura antes de esas dos correcciones amplificaría el error.

### Qué acepto y qué no acepto de la propuesta actual

**Acepto como hipótesis de entrada:**

- un noise floor basado en ATR para evitar stops sub-ruido;
- penalizar la evidencia de breakout contra la tesis de rechazo;
- comparar continuación contra reversión con contratos separados;
- calibrar geometría y probabilidad por lado e instrumento;
- deduplicar oportunidades antes de calcular cobertura y PnL.

**No aceptaría todavía como decisión final:**

- fijar `2.0×ATR` como umbral definitivo solo porque `1–1.5×ATR` falló en esta muestra;
- invertir ciegamente todas las señales SHORT a LONG y LONG a SHORT;
- usar el flip-test como PnL esperado de producción;
- multiplicar `max_open_per_cycle` antes de comprobar que el nuevo ranking no selecciona más contratos correlacionados;
- aplicar la penalización a toda señal SHORT sin distinguir breakout confirmado, presión alcista débil y rechazo genuino.

El resultado de `1.0–1.5×ATR` con SL alto no prueba que el umbral correcto sea exactamente `2.0×ATR`: la muestra es pequeña en esos buckets y la dirección sigue siendo una variable confundida. El grid debe tratar el noise floor como candidato a gate, no como verdad calibrada.

### Plan de entradas por fases

#### Fase E1: hacer válido el contrato económico

Antes de cualquier ampliación de cobertura, cada candidato debe cumplir:

- stop y target con geometría válida para su lado;
- distancia stop mínima parametrizada respecto a ATR del mismo timeframe de decisión;
- riesgo económico calculado con fees, slippage y funding;
- `expected_return_on_risk` limitado por un riesgo que no sea sub-ruido;
- tamaño reducido si el stop estructural obliga a exceder `max_loss_usd_per_trade`.

Si ningún valor del grid conserva EV positivo OOS, el sistema debe abstenerse. No se debe compensar un contrato inválido aumentando leverage o número de posiciones.

#### Fase E2: separar continuación y reversión

En resistencia, evaluar independientemente:

- `SHORT_RESISTANCE_REJECTION`: requiere evidencia de rechazo y no debe recibir una probabilidad alta cuando existe presión de breakout;
- `LONG_RESISTANCE_BREAKOUT`: requiere evidencia de continuación y su propia geometría LONG.

En soporte, evaluar independientemente:

- `LONG_SUPPORT_BOUNCE`: requiere rebote real;
- `SHORT_SUPPORT_BREAKDOWN`: requiere evidencia de continuación bajista.

La ubicación S/R genera el conjunto de hipótesis, pero no decide el signo. El contrato ganador debe ser el de mayor EV neto después de costes y riesgo, no el de mayor probabilidad aislada ni el que tenga el stop más pequeño.

#### Fase E3: aumentar entradas buenas

Solo después de E1 y E2:

1. ampliar la cobertura de candidatos que pasan el contrato económico;
2. ordenar por EV neto marginal del portfolio y riesgo marginal, no por volumen bruto de señales;
3. permitir más posiciones cuando haya capital y slots libres y la correlación incremental sea aceptable;
4. conservar `ABSTAIN` cuando ambos lados sean malos;
5. medir cuántas oportunidades nuevas son realmente únicas y cuántas son duplicados del mismo evento.

Así se busca el mayor número de entradas **rentables**, no el mayor número de tickets.

### Impacto separado por instrumento

**SPOT:** debe favorecer `SPOT_LONG` ante continuación alcista en resistencia y permitirlo aunque no exista ruta FUTURES. No se debe habilitar short-margin solo para aumentar conteo. La validación debe medir capital inmovilizado y correlación con el resto del portfolio.

**FUTURES:** `FUTURES_SHORT` necesita superar a `FUTURES_LONG` después de funding, slippage y riesgo de squeeze; no basta con que tenga un stop nominal menor. `FUTURES_LONG` debe competir en resistencia cuando la continuación tenga mejor EV neto. Funding, momentum BTC y elegibilidad del instrumento siguen siendo gates de entrada.

### Impacto por perfiles A/B/C

- **A:** mayor cobertura de contratos con EV neto positivo, pero no acepta stop sub-ruido ni geometría inválida.
- **B:** perfil de validación principal; exige que la mejora sobreviva OOS con equilibrio entre cobertura, PF y drawdown.
- **C:** menos entradas, mayor exigencia de confirmación y liquidez; sirve para comprobar que la mejora no depende únicamente de asumir riesgo agresivo.

No se debe cambiar el perfil para ocultar el fallo de dirección. Cada perfil debe conservar su identidad y reportar cobertura y PnL neto por separado.

### Gates de aceptación, persistencia y rollback

- **Gate de aceptación:** mejora OOS del PnL neto y profit factor, con mayor o igual cobertura útil, sin empeorar el drawdown ni la correlación marginal.
- **Gate de dirección:** la tesis seleccionada debe superar a su tesis contraria con la geometría propia, no con el stop artificialmente pequeño.
- **Gate de ejecución:** solo contar una entrada después de preflight y fill confirmado.
- **Persistencia:** guardar en `ready_candidates.data` ambos contratos, ATR, stop/target, ratio stop/ATR, probabilidad calibrada, EV neto, penalty aplicada, dedupe key y contrato seleccionado.
- **Auditoría:** registrar en `trades_audit` la comparación bilateral, la razón de rechazo del lado contrario y el evento de ejecución/fill.
- **Rollback:** cada modificación de entrada debe poder desactivarse por separado: noise floor, penalty de breakout y geometría ATR. El rollback vuelve a la ruta anterior de selección sin tocar lifecycle ni posiciones abiertas.

La salud debe aparecer en el dashboard o métrica de readiness con: oportunidades únicas, candidatos por lado, ratio stop/ATR, contratos bloqueados por noise floor, dirección seleccionada, fills y PnL neto por SPOT/FUTURES y A/B/C.

### Validación mínima antes de implementar

La próxima corrida debe usar una unidad por entrada única y separar explícitamente:

- development frente a OOS temporal;
- resistencia frente a soporte;
- continuación frente a reversión;
- SPOT LONG, FUTURES LONG y FUTURES SHORT;
- perfiles A/B/C;
- ratios stop/ATR `{0.5, 1.0, 1.5, 2.0}`;
- penalizaciones `{0.0, 0.3, 0.5, 0.7}`;
- costes 1x, 1.5x y 2x;
- velas ambiguas donde stop y target aparecen en el mismo intervalo.

La métrica primaria será PnL neto OOS por oportunidad única y por unidad de riesgo. Las métricas secundarias serán cobertura útil, profit factor, hit rate, drawdown, correlación incremental y porcentaje de candidatos descartados por cada gate. El baseline debe ser el comportamiento actual y la comparación de largo plazo 3y/60000; no se debe declarar éxito por el flip-test de estas tres ventanas.

### Conclusión de alcance

La recomendación queda limitada a la **entrada**: contrato, dirección, geometría inicial, probabilidad, selección, sizing inicial, deduplicación y ejecución confirmada. No se modifica ni se evalúa ahora el lifecycle, los exits, trailing, SSO, PRS, PredExit ni UG. Esos componentes quedan explícitamente fuera de alcance para esta investigación.

<!-- COPILOT_UPDATE_END: 2026-09-27 -->

<!-- COPILOT_UPDATE_START: 2026-09-27-02 -->
<!-- Autor: GitHub Copilot | Alcance: auditoría de gaps del diagnóstico y de la lógica de entrada -->
## [COPILOT UPDATE 2026-09-27-02] Gaps adicionales antes de implementar cambios de entrada

### Correcciones históricas que deben considerarse superseded

Las afirmaciones iniciales de que el árbitro no recibía LONG o de que SHORT era la única opción en resistencia ya no deben tratarse como causa raíz vigente. La evidencia posterior y `rules.md` indican que el arbitraje bilateral ya está activo. Esas afirmaciones quedan como trazabilidad histórica, no como tareas de implementación.

El problema vigente es por qué un contrato con riesgo subestimado puede ganar el arbitraje y llegar a ejecución.

### Gap 1: se optimiza ratio de riesgo, no ganancia neta

`opportunity_contract.py` calcula `expected_net_pnl_usdt`, pero `directional_arbiter.py` selecciona por `expected_return_on_risk` tanto para la competición LONG/SHORT como para la elección SPOT/FUTURES LONG.

Esto reproduce el incentivo defectuoso detectado en los datos: un stop muy pequeño reduce el denominador y puede hacer ganador a un contrato cuya ganancia absoluta, probabilidad real o robustez de ejecución son peores.

La prioridad de selección de entradas debería ser:

1. contrato ejecutable y con geometría válida;
2. riesgo económico dentro del límite;
3. `expected_net_pnl_usdt` positivo después de costes;
4. ranking por EV neto marginal del portfolio;
5. retorno sobre riesgo como restricción secundaria o desempate.

No debe declararse corregido el problema mientras el selector continúe maximizando únicamente `expected_return_on_risk`.

### Gap 2: el noise floor existe, pero está apagado y puede fallar abierto

Los parámetros `range_scalp_min_stop_atr_multiple` y `range_scalp_rejection_breakout_penalty` existen en `config.py`, pero sus defaults son `0.0` y `1.0`. En esos valores no bloquean stops sub-ruido ni penalizan el rechazo frente a breakout.

Además, en `opportunity_contract.py`, si falta `atr` y falta `atr_pct`, el cálculo produce un ATR efectivo de cero y `stop_above_noise_floor=True`. Eso convierte la ausencia de volatilidad en un bypass silencioso.

Antes de activar el gate hay que decidir y medir una política fail-closed:

- ATR válido del timeframe de decisión: se puede evaluar el ratio;
- ATR ausente, ambiguo o con unidades inválidas: bloquear o enviar a `ABSTAIN`;
- fallback ATR: solo si está versionado, auditado y validado OOS.

También hay que comprobar que el ATR usado en el contrato sea el mismo que se usa para el stop real y el sizing. Si el contrato calcula con un stop sub-ruido y la ejecución lo reemplaza después por un stop ATR, el ranking sigue estando mal aunque la orden final parezca protegida.

### Gap 3: la evidencia contradictoria tiene cobertura incompleta

La penalización solo se aplica cuando `rejection_evidence.breakout_pressure` es verdadero. Esto no cubre necesariamente:

- momentum alcista sin flag formal de breakout;
- volumen expansivo sin `sr_breakout`;
- ruptura producida entre el snapshot y el submit;
- evidencia con otro nombre o procedente de otra capa;
- soporte roto frente a una tesis LONG de rebote.

La próxima validación debe medir la matriz completa de evidencia: disponibilidad, missingness, frescura, fuente, dirección y resultado. Un `False` por ausencia de dato no puede interpretarse igual que un `False` observado y confirmado.

### Gap 4: posible doble conteo de señales

`sr_breakout`, `sr_breakdown`, momentum, volumen y tendencia participan en distintas capas del pipeline. El predictor puede usar esas features para producir `success_probability` y el contrato vuelve a usarlas para penalizar o favorecer el lado.

Hay que separar:

- features usadas para calibrar la probabilidad;
- evidencia usada como veto o penalty;
- features usadas únicamente para auditoría.

Si la misma evidencia se usa para elevar `p` y después para justificar la selección del contrato, la probabilidad puede estar inflada y dejar de ser calibrada. Se necesita un ablation test por feature y por capa.

### Gap 5: deduplicación insuficiente para inferencia

Reducir `1,154` registros a `161` entradas únicas es necesario, pero no demuestra que `N=161` sea independiente. Eventos del mismo símbolo y ventana de mercado siguen correlacionados.

La validación debe:

- definir una `dedupe_key` reproducible;
- agrupar por símbolo, evento y timestamp de entrada;
- usar bootstrap por cluster o por ventana temporal;
- reportar el número efectivo de clusters;
- separar jobs paralelos de oportunidades reales.

Las cifras agregadas del flip-test deben presentarse como contrafactuales, no como PnL esperado de producción.

### Gap 6: el flip-test no es un resultado ejecutable

El flip-test conserva el precio de entrada y cambia el lado, pero no reconstruye toda la decisión de entrada. No incluye necesariamente:

- probabilidad recalibrada para la tesis opuesta;
- target y stop propios del lado opuesto;
- fees, slippage y funding específicos;
- elegibilidad SPOT/FUTURES;
- capital, slots y correlación con el resto de candidatos;
- latencia y orden intrabar.

Debe usarse para detectar asimetría direccional, no para estimar directamente el beneficio de producción.

### Gap 7: riesgo de look-ahead e intrabar ambiguity

La señal, los niveles S/R, el ATR y la evaluación de primer nivel tocado deben estar congelados en el instante real de entrada. Hay que verificar que no se usen datos de cierre o niveles recalculados después del evento.

Cuando una vela toca stop y target en el mismo intervalo, el resultado debe marcarse como ambiguo o resolverse con datos de 1m/5m. No se debe elegir implícitamente el orden que favorezca la tesis.

La validación debe informar tres escenarios:

- orden intrabar observado con datos de mayor resolución;
- peor caso;
- mejor caso.

### Gap 8: configuración y trazabilidad operativa

El acceso a nuevos parámetros debe seguir `get_param(settings, key)` y mantener paridad entre `config.py`, defaults canónicos, aplicación por régimen, hot-config y drift checks. El hecho de que el helper sincronice defaults dinámicamente no sustituye la trazabilidad de qué valor estuvo activo en cada decisión.

Cada contrato debe persistir:

- valor configurado y fuente del valor;
- ATR, unidad y timeframe;
- stop contractual y stop efectivo;
- ratio stop/ATR;
- probabilidad original y probabilidad después de penalty;
- EV neto y retorno sobre riesgo;
- razón de descarte o selección.

El evento `trades_audit` debe distinguir comparación bilateral, bloqueo por contrato inválido y ejecución con fill confirmado.

### Plan de validación corregido

Antes de cambiar capital real, ejecutar una matriz que compare el baseline actual contra cada variante, con selección por EV neto y por ratio solo como control:

- deduplicación por cluster;
- development y OOS temporal;
- SPOT LONG, FUTURES LONG y FUTURES SHORT;
- tesis de continuación y reversión;
- perfiles A/B/C;
- stop/ATR `{0.5, 1.0, 1.5, 2.0}`;
- penalty `{0.0, 0.3, 0.5, 0.7}`;
- ATR presente frente a ATR ausente;
- costes 1x, 1.5x y 2x;
- resolución de velas ambiguas.

La métrica primaria será PnL neto OOS por oportunidad única y por unidad de capital. Las secundarias serán cobertura útil, profit factor, drawdown, calibración de probabilidad, correlación marginal, ratio de candidatos inválidos y porcentaje de fills confirmados.

### Decisión de rollout

No conviene activar todavía un penalty global ni fijar `2.0×ATR` como verdad. La primera comprobación debe demostrar que el selector deja de premiar riesgo artificialmente pequeño y que el stop contractual coincide con el stop ejecutado.

El rollout deberá tratar cada cambio como bandera independiente:

- selección por EV neto;
- noise floor;
- penalty de evidencia;
- política ATR ausente;
- geometría ATR.

El gate de aceptación será mejora OOS neta por oportunidad única sin deterioro material de drawdown, correlación o fill rate. El rollback desactiva solo la bandera fallida. No se modifica lifecycle, exits, trailing, SSO, PRS, PredExit ni UG en esta fase.

<!-- COPILOT_UPDATE_END: 2026-09-27-02 -->

<!-- DEVIN_UPDATE_START: 2026-09-27-02 -->
## [DEVIN UPDATE 2026-09-27-02] Implementación aplicada y cobertura real por ruta de entrada

Acepto la auditoría de Copilot y corregí los dos gaps críticos identificados. Estado actual del código (staging activo, producción intacta por defaults):

### Implementado y activo en staging

| Mecanismo | Config | Estado |
|---|---|---|
| Noise floor ATR (fail-closed) | `range_scalp_min_stop_atr_multiple=1.5` | activo |
| Ranking por EV neto (ROR como desempate) | `range_scalp_ranking_mode=ev_net` | activo |
| Penalización por breakout pressure | `range_scalp_rejection_breakout_penalty=0.5` | activo |

### Cobertura real por ruta de entrada (auditoría completa)

| Ruta | Instrumento | Cobertura |
|---|---|---|
| Árbitro RANGE_SCALP | SPOT LONG / FUTURES LONG / FUTURES SHORT / SPOT_MARGIN SHORT | ✅ gate en `build_executable_opportunity` (los 3 contratos) |
| FUTURES_SHORT directo | FUTURES SHORT | ✅ `stop_noise_gate` inline en `run_trades.py` antes de `open_short` |
| SR Pool shorts | FUTURES SHORT | ✅ mismo gate en la ruta del pool |
| Predictive short via `selected_contract` | FUTURES SHORT | ✅ hereda la validación del contrato |
| Shorts ATR-anclados (`_atr_anchor_sl_pct`) | FUTURES SHORT | ✅ ya estructuralmente anclados a ATR |
| SPOT LONGs directos (SPOT_BULL / preservation) | SPOT LONG | ⚠️ no aplicable — no llevan stop contractual a la entrada; el stop lo gestiona lifecycle. Si se requiere protección equivalente, necesita un gate de sanity distinto, no este mecanismo |

Perfiles A/B/C: el árbitro corre idéntico para todos — los gates aplican igual.

### Fixes aplicados tras la auditoría de Copilot

1. **Gap 1 (ranking por ROR)**: `evaluate_range_scalp` ahora ordena por `expected_net_pnl_usdt` primero y `expected_return_on_risk` como desempate cuando `ranking_mode=ev_net`. El selector ya no premia stops artificialmente pequeños.

2. **Gap 2 (fail-open ATR)**: con el gate activo, ATR ausente o inválido → contrato **no ejecutable** + `atr_available=false` en `validation_evidence`. Ausencia de dato ya no es bypass silencioso.

3. **Fail-closed aplica también a los gates directos**: `stop_noise_gate()` devuelve `ok=False` si falta ATR cuando `min_stop_atr_multiple > 0`.

### Cómo se observa en operación

- Rechazos nuevos: razón `STOP_INSIDE_NOISE_FLOOR` en logs y `signal_decision`.
- Contratos: `validation_evidence.stop_above_noise_floor`, `atr_frac`, `stop_distance_pct`, `atr_available`.
- Resultados de sweep: filas nuevas marcadas `entry_flags.generation="v2_entry_fix"` vs baseline sin el campo.
- Sweep reanudado desde 115/280 — los jobs v2 corren ya con los flags activos en el contenedor staging.

### Nota metodológica final

El flip-test (+EV en todas las celdas) se reporta como diagnóstico de asimetría direccional, no como PnL esperado — la confirmación ejecutable vendrá de la corrida dirigida A/B y después OOS. Rollback: cada flag es independiente y default-off en producción.

<!-- DEVIN_UPDATE_END: 2026-09-27-02 -->

<!-- DEVIN_UPDATE_START: 2026-09-27-03 -->
## [DEVIN UPDATE 2026-09-27-03] Extensión del noise floor a todas las rutas de entrada

Tras la pregunta del usuario sobre cobertura universal, el gate se extendió más allá del árbitro:

### Helper compartido

```python
# opportunity_contract.py
def stop_noise_gate(*, entry_price, stop_price, indicators, min_stop_atr_multiple)
    -> (ok, stop_distance_frac, atr_frac)
```

Fail-closed cuando el gate está activo: si ATR falta o es inválido → `ok=False` (la ausencia de volatilidad no es bypass). Es reutilizable por cualquier ruta de entrada — no requiere el contrato completo.

### Sitios cableados

- Árbitro RANGE_SCALP: los 3 contratos (`build_executable_opportunity`).
- `run_trades.py` ~9863: FUTURES_SHORT directo — short con `sl_pct` flat queda bloqueado si `stop_dist < 1.5×ATR`, razón `STOP_INSIDE_NOISE_FLOOR`.
- `run_trades.py` ~12271: SR Pool shorts — mismo gate antes de `open_short`.
- Predictive short via `selected_contract`: hereda la validación del contrato ya gated.
- Shorts `_atr_anchor_sl_pct`: ya estructuralmente anclados a ATR.

### No cubierto (por diseño)

SPOT LONGs directos (SPOT_BULL / preservation) no llevan stop contractual a la entrada — el stop lo gestiona lifecycle post-apertura. Protegerlos requiere un gate de sanity de entrada distinto (fuera del alcance de este fix).

### Estado

```text
Staging: activo con los 3 flags + código bind-mounted reiniciado
Sweep:   reanudado desde 115/280, filas nuevas marcadas generation="v2_entry_fix"
Tests:   111 (rango/contrato/dirección) + 649 (suite completa) pasan
```

<!-- DEVIN_UPDATE_END: 2026-09-27-03 -->

<!-- COPILOT_UPDATE_START: 2026-09-27-04 -->
<!-- Autor: GitHub Copilot | Alcance: implementación de anti-ghost y trazabilidad de unidades -->
## [COPILOT UPDATE 2026-09-27-04] Fix anti-ghost transversal desplegado en staging

### Causa raíz confirmada

`get_binance_sell_qty()` comparaba la posición DB con `get_account_balance()`, que informa saldo libre, no el total Spot libre+bloqueado. Además, una excepción se convertía en `balance_qty=0`, idéntico a una respuesta válida de balance vacío. Los exits SSO/PRS/SL/TP y el autofix pretrade podían borrar `trades_open` con esa única lectura; después Binance Truth veía el activo como orphan. Una rama de reconciliación adicional cerraba todas las unidades ante balance diminuto aunque quedara residuo.

### Cambios implementados en el workspace staging

1. `BinanceClient.get_account_balance_details()` entrega `free`, `locked` y `total=free+locked`; `get_wallet_balance()` utiliza ese desglose y ya no infiere total desde un `walletBalance` opcional.
2. `trade_utils.get_binance_asset_balance()` devuelve estado tipado. Error o breakdown inconsistente queda como `api_error`/unknown, no como cantidad cero. Ghost compara total; la orden solo puede usar free.
3. `run_trades.py` ya no cierra SSO, PRS, SL/TP ni autofix pretrade por un snapshot aislado de ghost: difiere y audita para reconciliación confirmada.
4. `PositionManager` reconcilia free+locked. Si queda cantidad positiva, recorta solo el exceso; si hay más unidades en Binance que en DB, crea una posición adicional idempotente de coste desconocido. El cierre total por ausencia requiere lecturas válidas consecutivas **y evidencia de SELL fills recientes que cubran la cantidad rastreada**; sin esa evidencia (posible transferencia a Earn/Funding), conserva y alerta. Error de API no incrementa la confirmación.
5. `hard_risk_monitor.py`, `manual_position_closer.py`, cleanup de polvo y `/positions/close-all` preservan la posición ante error, ausencia no confirmada o saldo bloqueado. El ghost manual exige dos balances cero más prueba SELL suficiente. `/positions/close-all` y los partial closes persisten solo cantidad de fills confirmados; si la persistencia falla, no hay fallback de mutación de quantity sin fila de cierre.
6. `/positions/open` expone asset base, base quantity, valor de entrada en USDT, mark value, lineage/source, balance free/locked/total y procedencia del coste. El dashboard muestra USDT como valor primario, cantidad y unidad base debajo; reconciliaciones sin fills muestran `base de coste desconocida`.
7. La importación de balance no fabrica comisión ni adquisición: conserva mark de referencia, marca `cost_basis_status=unknown`, fuente y lineage estable. Futures conserva su ruta independiente basada en `positionAmt` y confirmación de cierre; no usa balances Spot.

### Cobertura y alcance

El contrato se aplica a SPOT en las rutas `run_trades` (SSO/PRS/SL-TP), hard-risk, manual, position sync, Binance Truth/autofix, close-all y limpieza de polvo. Futures permanece instrument-specific y se valida por `positionAmt`, `positionSide`, fills y residual. Perfiles A/B/C no cambian la verdad de saldo ni los gates de persistencia; ningún perfil puede convertir error de API en ghost.

### Gates, auditoría y rollback

- **Aceptación:** cero falsos cierres ante free=0/locked>0 o API error; excedentes y residuos coinciden con el total Spot por unidad base; cada cierre real tiene fill y qty exactos; ausencia sin fill requiere confirmación repetida válida.
- **Rollback:** mantener desactivado el auto-close de snapshot único. Si la reconciliación causa regresión, desactivar solo autofix/import automático; no borrar ni dejar de mostrar posiciones existentes.
- **Persistencia/auditoría:** conservar `position_id`/`lineage_id`, symbol, base asset/qty, free/locked/total, mark USDT, coste conocido/desconocido, fuente, timestamps y estado de confirmación en metadata y `trades_audit` (`ghost_close_deferred`, `reconcile_qty_drift`, `reconcile_unknown_context`, fill de close).
- **Dashboard/API:** `/positions/open`, `/dashboard/summary` y estado Binance Truth deben mostrar cantidad delta, free/locked/total, valor USDT, coste desconocido y orphan/ghost unresolved.
- **Comparación:** validar en staging contra el baseline/changepoint vigente (3y/60000) para accounting y outcomes; para el incidente operativo, usar además snapshots contemporáneos de exchange/DB, no los agregados históricos.

### Validación y despliegue

Tests focalizados cubren saldo bloqueado, error de API, importación idempotente de excedente, dos snapshots válidos de total cero, prueba SELL suficiente, cierre manual ghost, `close-all` sin fill, API de posiciones y paridad Futures. Se reinició únicamente `app_stg`; PostgreSQL, Redis, volúmenes y producción no se modificaron.

Verificación de implementación (2026-09-27, 18:43 UTC): suite focalizada **57 passed**; análisis del editor sin errores; `git diff --check` focalizado sin errores (solo advertencia LF/CRLF). El startup sync restauró `NEARUSDT` como una posición abierta: `13.9 NEAR`, free `13.9`, locked `0`, total `13.9`; valor de referencia `74.921 USDT`, mark actual `74.8098 USDT`, `lineage_id` persistido y `cost_basis_status=unknown`. `entry_price=5.39` es el mark de importación, no coste de adquisición verificado; entry commission queda en cero porque no hay fill recuperable. Binance Truth Spot validó DB=1/Binance=1, cero mismatches; el primer ciclo terminó a las 18:43:41 UTC sin error y el dashboard muestra `Spot: 1` con las unidades y la etiqueta de coste desconocido. No se envió ninguna orden de compra o venta durante la restauración.

<!-- COPILOT_UPDATE_END: 2026-09-27-04 -->

## [COPILOT UPDATE 2026-09-27-05] Selección bilateral por venue y capital con autorización JIT

### Implementación directa en staging (sin shadow de política)

1. `directional_arbiter.py` compara los contratos Spot LONG y Futures LONG, y Futures SHORT y Spot Margin SHORT cuando Margin está habilitado y el símbolo es ejecutable. El resultado conserva las matrices `long_venue_contracts`/`short_venue_contracts`, el contrato ganador y la alternativa; el ranking configurado (`ev_net` en staging) no admite override direccional por `routing_intent`.
2. `_range_scalp_direction_decision()` calcula predicciones independientes por venue SHORT. Los candidatos SR Pool LONG/SHORT entran al flujo bilateral; los helpers legacy de apertura SHORT y las aperturas Futures LONG heurísticas solo se permiten con `directional_policy_mode=disabled` (rollback explícito). El FastPath ya no cambia de Futures LONG fallido a Spot genérico y conserva contrato, decision_id, SL/TP y tamaño del contrato SPOT_LONG ganador.
3. CapBridge es incondicionalmente observacional; ni siquiera `execute_transfers=True` puede mover fondos. La compra/movimiento automático de BNB desde ciclo y GET de comisión está deshabilitado; reporta necesidades/reservas sin comprar ni transferir.
4. Los movimientos JIT ocurren solo tras contrato elegido, gates de ejecución y refresh de precio; se transfiere el déficit al centavo superior, protegiendo reservas BNB independientes de Spot/Futures. Se requiere balance de origen verificable, persistencia previa de `capital_transfer_authorized` y resultado `capital_transfer_result` con `decision_id`; error de balance, reserva o auditoría falla cerrado. La base SAPI sigue el endpoint Demo Spot de staging y no `api.binance.com`.
5. Spot BUY compara coste con USDT libre después de una sola aplicación de reserva BNB. No se reduce silenciosamente un contrato después de su selección; si funding verificado no cubre el tamaño y fee requeridos, se rechaza la entrada.

### Gates y estado operativo

- **Aceptación técnica:** matriz completa de venues, mismo ganador hasta submit/fill, cero transferencias predecisión, transferencias JIT dentro de ambas reservas BNB, evento de auditoría con decisión y resultado verificable. Tests de selección/transferencia y regresión anti-ghost: **114 passed**; Pylance sin errores.
- **A/B/C:** ninguna regla de perfil altera orden del arbitraje ni puede saltar gates; los perfiles solo alimentan sizing/evidencia vigente. No se cambió la calibración de entradas para aislar el allocator.
- **Rollback:** fijar `directional_policy_mode=disabled` vuelve a habilitar executors legacy; CapBridge permanece bloqueado siempre y no es ruta de rollback para transferencias. Reanudar nuevas entradas requiere quitar `pause_new_entries` después de corregir la red y validar balance.
- **Despliegue:** solo se reinició `app_stg`; PostgreSQL y Redis conservaron sus contenedores/volúmenes. El endpoint `/control/status` confirma `RUNNING_ENTRIES_PAUSED`, motivo `allocator_deploy_and_balance_error_investigation`, TTL 180 min. El ciclo registra `monitor only` para SL/TP; no hay posiciones abiertas. NEAR aparece en cierres con `stop_loss` a 5.426, no como posición ghost abierta.
- **Bloqueante operativo:** logs del proceso staging reportan `NameResolutionError` resolviendo `demo-api.binance.com` y rechazos de orden de testnet por balance insuficiente. Las últimas comprobaciones post-pausa no muestran nuevas órdenes ni transferencias. No reanudar entradas hasta recuperar DNS/endpoint y reconciliar saldos de testnet.
- **Comparación causal:** mantener congelados candidate tape, config/model hash, universo, sizing y costes; comparar solo allocator con el mismo conjunto LONG/SHORT/venue, además del baseline 3y/60000 y OOS/changepoint por generación. No inferir mejora de PnL de la prueba técnica ni de fills simulados.

## [COPILOT UPDATE 2026-09-27-06] Contrato normativo del pipeline de entrada

El detalle canónico paso a paso está en `canonical/calibracion_entradas-a20ab8-v3.md`, sección “Lógica operativa de entrada s139”. Este resumen fija cómo leer la evidencia y qué debe ocurrir antes de cualquier apertura:

1. La comparación es por candidato/símbolo: `portfolio_selector` prioriza la cartera entre símbolos; después, para cada candidato elegible, el árbitro obtiene el mejor LONG y el mejor SHORT entre sus venues habilitados y compara esos dos finalistas. No es un único ranking cross-symbol de todos los contratos.
2. Cerca de soporte compiten LONG bounce y SHORT breakdown; cerca de resistencia compiten SHORT rejection y LONG breakout. S/R habilita tesis, no preselecciona lado. La predicción, geometría, costes, riesgo, liquidez y ejecutabilidad pertenecen a cada contrato/venue.
3. En Staging el modo `ev_net` ordena primero por `expected_net_pnl_usdt` positivo; ROR desempata. Ruido ATR, target/stop inválidos, probabilidad mínima, pérdida máxima, costes, balance y demás gates pueden hacer una alternativa no ejecutable. Sin alternativa válida, se abstiene.
4. `ready`/`validated` describen lifecycle y checks configurados, pero no congelan dirección. Ciclo normal y FastPath refrescan datos y re-arbitran justo antes del routing/submit. Un ganador que falla no puede convertirse silenciosamente en otra dirección o venue.
5. CapBridge y el manejo automático BNB son solo observación/auditoría antes de una decisión. Tras seleccionar y aprobar una entrada, el JIT puede mover el mínimo shortfall, preservando las reservas BNB independientes; requiere balance verificable, auditoría con `decision_id`, refresh posterior y nueva comprobación de suficiencia.
6. La orden debe corresponder al contrato ganador. Solo fill confirmado crea una apertura persistida con qty/precio/costes reales, `decision_id`, `ready_candidate_id` y auditoría. Transferencia, submit aceptado o señal no son fills.

El comparador de ranking en la versión activa usa EV neto, no `expected_net_pnl / risk_usdt` como objetivo primario. Las frases anteriores de s135/s137 que declaren ROR como objetivo quedan supersedidas para Staging por esta precisión y por s139. El historial estadístico Super MIMIC sigue siendo evidencia in-sample de dirección/ruido; no demuestra que el nuevo allocator mejore PnL realizado.

En la última verificación, los tests enfocados dieron **114 passed**. `app_stg` está desplegado, pero `pause_new_entries` permanece activo por 180 minutos debido a fallos observados de DNS al resolver Binance Demo Spot y a rechazos de balance en testnet. La app mantiene monitorización/cierres; no reanudar entradas hasta corregir conectividad y reconciliar los saldos.

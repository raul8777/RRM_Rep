# Peak Dataset — Fase 1: Análisis de muestra representativa (dataset corregido)

- **Total de registros en parquet:** 1,638,639
- **Registros muestreados:** 199,941
- **Estratos únicos (símbolo × tipo × dirección × régimen):** 690
- **Columnas:** 114
- **Archivo de muestra:** `s:\Tools\AI\crypto-agent\Dev\benchmarks\peaks_dataset\sample_phase1.parquet`
- **Dataset:** `s:\Tools\AI\crypto-agent\Dev\benchmarks\peaks_dataset\peaks_all.parquet`

## Distribución de picos por estrato (muestra, top 20)

| symbol | peak_type | direction | s_mode_confirmed | Count |
|---|---|---|---|---|
| ADAUSDT | forward_4h | SHORT | RANGE_SCALP | 802 |
| ADAUSDT | forward_4h | LONG | RANGE_SCALP | 802 |
| ADAUSDT | forward_2h | LONG | RANGE_SCALP | 802 |
| ADAUSDT | forward_2h | SHORT | RANGE_SCALP | 802 |
| XLMUSDT | forward_4h | SHORT | RANGE_SCALP | 802 |
| XLMUSDT | forward_4h | LONG | RANGE_SCALP | 802 |
| XLMUSDT | forward_2h | SHORT | RANGE_SCALP | 802 |
| XLMUSDT | forward_2h | LONG | RANGE_SCALP | 802 |
| UNIUSDT | forward_2h | LONG | RANGE_SCALP | 802 |
| UNIUSDT | forward_2h | SHORT | RANGE_SCALP | 802 |
| UNIUSDT | forward_4h | SHORT | RANGE_SCALP | 802 |
| UNIUSDT | forward_4h | LONG | RANGE_SCALP | 802 |
| BCHUSDT | forward_4h | SHORT | RANGE_SCALP | 802 |
| BCHUSDT | forward_4h | LONG | RANGE_SCALP | 802 |
| BCHUSDT | forward_2h | LONG | RANGE_SCALP | 802 |
| BCHUSDT | forward_2h | SHORT | RANGE_SCALP | 802 |
| SOLUSDT | forward_4h | SHORT | RANGE_SCALP | 802 |
| BNBUSDT | forward_2h | LONG | RANGE_SCALP | 802 |
| BTCUSDT | forward_4h | SHORT | RANGE_SCALP | 802 |
| BTCUSDT | forward_2h | SHORT | RANGE_SCALP | 802 |

## Estadísticas de `peak_return` (muestra)

| Métrica | Valor |
|---|---|
| count | 199941.000000 |
| mean | 0.000091 |
| std | 0.017128 |
| min | -0.388850 |
| p10 | -0.014509 |
| p25 | -0.005798 |
| p50 | 0.000000 |
| p75 | 0.005506 |
| p90 | 0.014437 |
| max | 0.752625 |

### Por dirección

| direction | count | mean | std | median |
|---|---|---|---|---|
| LONG | 99954 | 0.006164 | 0.016961 | 0.002933 |
| SHORT | 99987 | -0.005979 | 0.015011 | -0.003215 |

### Por tipo de pico

| peak_type | count | mean | std | median |
|---|---|---|---|---|
| forward_2h | 97542 | 0.000004 | 0.011760 | 0.000000 |
| forward_4h | 97541 | 0.000108 | 0.016457 | 0.000000 |
| local_extrema | 4858 | 0.001511 | 0.062113 | -0.003130 |

### Por régimen (mode_confirmed en start)

| s_mode_confirmed | count | mean | std | median |
|---|---|---|---|---|
| PRESERVATION | 40501 | -0.000047 | 0.019676 | 0.000000 |
| RANGE_SCALP | 98239 | 0.000157 | 0.015960 | 0.000000 |
| SPOT_BULL | 61201 | 0.000078 | 0.017127 | 0.000000 |

### Por dirección × tipo

| direction | peak_type | count | mean | std | median |
|---|---|---|---|---|---|
| LONG | forward_2h | 48771 | 0.003150 | 0.011572 | 0.001581 |
| LONG | forward_4h | 48770 | 0.007241 | 0.015642 | 0.004031 |
| LONG | local_extrema | 2413 | 0.045314 | 0.050267 | 0.030995 |
| SHORT | forward_2h | 48771 | -0.003142 | 0.011086 | -0.001799 |
| SHORT | forward_4h | 48771 | -0.007024 | 0.013973 | -0.004370 |
| SHORT | local_extrema | 2445 | -0.041719 | 0.037544 | -0.031841 |

## Top símbolos por volumen de picos

| Símbolo | Count |
|---|---|
| XLMUSDT | 6,659 |
| BCHUSDT | 6,658 |
| UNIUSDT | 6,657 |
| ETHUSDT | 6,656 |
| BTCUSDT | 6,655 |
| NEARUSDT | 6,655 |
| BNBUSDT | 6,654 |
| SOLUSDT | 6,654 |
| ADAUSDT | 6,652 |
| FETUSDT | 6,551 |
| AAVEUSDT | 6,550 |
| SUIUSDT | 6,550 |
| TAOUSDT | 6,549 |
| WLDUSDT | 6,549 |
| XRPUSDT | 6,549 |

## Correlaciones con `peak_return` (muestra)

| Feature | Correlación |
|---|---|
| e_momentum_3h | 0.706 |
| e_return_1h | 0.705 |
| e_return_4h | 0.663 |
| e_momentum_6h | 0.552 |
| e_bb_position | 0.486 |
| e_rsi_1h | 0.433 |
| e_btc_return_1h | 0.418 |
| e_momentum_12h | 0.400 |
| e_btc_return_4h | 0.399 |
| e_max_run_up_24h | 0.355 |
| e_max_drawdown_24h | -0.336 |
| e_return_24h | 0.299 |
| e_btc_day_pct | 0.244 |
| e_market_breadth | 0.207 |
| e_range_position | 0.198 |

## Valores faltantes (features con >80% de nulos en la muestra)

| Feature | % nulos |
|---|---|
| s_btc_dominance | 100.0 |
| e_btc_dominance | 100.0 |
| s_funding_rate | 93.1 |
| e_funding_rate | 93.1 |

## Notas / próximos pasos

- El dataset corregido ahora incluye `btc_return_1h/4h/24h`, `btc_atr_pct`, `ema9_vs_ema21`, `bb_bandwidth` y `btc_correlation_24h`.
- `funding_rate` y `ticker_*` quedan nulos en los períodos sin datos externos disponibles.
- `btc_dominance` está 100% nulo porque el archivo fuente solo contiene un snapshot.
- La Fase 2 procesará el dataset completo para validar que la muestra representa bien la población.
- La Fase 3 generará gráficos e informe final.

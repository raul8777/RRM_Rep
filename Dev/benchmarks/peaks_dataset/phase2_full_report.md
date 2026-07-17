# Peak Dataset — Fase 2: Análisis del dataset completo

- **Dataset:** `s:\Tools\AI\crypto-agent\Dev\benchmarks\peaks_dataset\peaks_all.parquet`
- **Total de registros:** 1,638,639
- **Workers:** 12
- **Tiempo de procesamiento:** 0.0 minutos
- **Columnas:** 114
- **Estratos únicos (símbolo × tipo × dirección × régimen):** 690

## Distribución global de `peak_return`

| count | mean | std | min | p10 | p25 | p50 | p75 | p90 | max |
|---|---|---|---|---|---|---|---|---|---|
| 1,638,639 | -0.000047 | 0.017444 | -0.454213 | -0.014599 | -0.005772 | 0 | 0.005396 | 0.014245 | 1.383420 |

## Picos por régimen y dirección — `mode_confirmed` (población completa)

| regime | direction | count | mean | std | min | max | p10 | p25 | p50 | p75 | p90 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| PRESERVATION | LONG | 165,953 | 0.006605 | 0.019446 | -0.254902 | 0.885446 | -0.008123 | -0.001827 | 0.003017 | 0.011494 | 0.024578 |
| PRESERVATION | SHORT | 165,983 | -0.008001 | 0.019361 | -0.454213 | 0.197060 | -0.027105 | -0.013433 | -0.004184 | 0.000836 | 0.007093 |
| RANGE_SCALP | LONG | 402,472 | 0.005786 | 0.015853 | -0.227302 | 0.752625 | -0.005791 | -0.001043 | 0.002841 | 0.009362 | 0.019652 |
| RANGE_SCALP | SHORT | 402,506 | -0.005516 | 0.014532 | -0.406130 | 0.337091 | -0.019398 | -0.009494 | -0.002885 | 0.001017 | 0.005682 |
| SPOT_BULL | LONG | 250,784 | 0.006019 | 0.017037 | -0.167254 | 1.383420 | -0.006390 | -0.001280 | 0.002945 | 0.009930 | 0.020906 |
| SPOT_BULL | SHORT | 250,941 | -0.005830 | 0.014673 | -0.399022 | 0.251634 | -0.020536 | -0.010212 | -0.003145 | 0.001057 | 0.006088 |

## Picos por régimen y dirección — `mode_futures` (población completa)

`mode_futures` es `mode_confirmed` con `PRESERVATION` renombrado a `FUTURES_SHORT`.

| regime | direction | count | mean | std | min | max | p10 | p25 | p50 | p75 | p90 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| FUTURES_SHORT | LONG | 165,953 | 0.006605 | 0.019446 | -0.254902 | 0.885446 | -0.008123 | -0.001827 | 0.003017 | 0.011494 | 0.024578 |
| FUTURES_SHORT | SHORT | 165,983 | -0.008001 | 0.019361 | -0.454213 | 0.197060 | -0.027105 | -0.013433 | -0.004184 | 0.000836 | 0.007093 |
| RANGE_SCALP | LONG | 402,472 | 0.005786 | 0.015853 | -0.227302 | 0.752625 | -0.005791 | -0.001043 | 0.002841 | 0.009362 | 0.019652 |
| RANGE_SCALP | SHORT | 402,506 | -0.005516 | 0.014532 | -0.406130 | 0.337091 | -0.019398 | -0.009494 | -0.002885 | 0.001017 | 0.005682 |
| SPOT_BULL | LONG | 250,784 | 0.006019 | 0.017037 | -0.167254 | 1.383420 | -0.006390 | -0.001280 | 0.002945 | 0.009930 | 0.020906 |
| SPOT_BULL | SHORT | 250,941 | -0.005830 | 0.014673 | -0.399022 | 0.251634 | -0.020536 | -0.010212 | -0.003145 | 0.001057 | 0.006088 |

## Muestra vs población: diferencia de media de `peak_return`

Muestra de Fase 1 vs población completa por régimen/dirección (`mode_confirmed`).

| regime | direction | count | mean | mean_sample | diff_mean |
|---|---|---|---|---|---|
| PRESERVATION | SHORT | 165,983 | -0.008001 | -0.007291 | 0.000709 |
| PRESERVATION | LONG | 165,953 | 0.006605 | 0.007198 | 0.000594 |
| SPOT_BULL | SHORT | 250,941 | -0.005830 | -0.005870 | -0.000039 |
| RANGE_SCALP | LONG | 402,472 | 0.005786 | 0.005821 | 0.000035 |
| SPOT_BULL | LONG | 250,784 | 0.006019 | 0.006030 | 0.000011 |
| RANGE_SCALP | SHORT | 402,506 | -0.005516 | -0.005506 | 0.000010 |

## Top 20 estratos por cantidad

| symbol | peak_type | direction | regime | count | mean | std |
|---|---|---|---|---|---|---|
| UNIUSDT | forward_2h | SHORT | RANGE_SCALP | 6,571 | -0.003245 | 0.011468 |
| UNIUSDT | forward_4h | LONG | RANGE_SCALP | 6,571 | 0.007613 | 0.015101 |
| UNIUSDT | forward_4h | SHORT | RANGE_SCALP | 6,571 | -0.007461 | 0.014572 |
| ETHUSDT | forward_2h | LONG | RANGE_SCALP | 6,571 | 0.002285 | 0.008117 |
| ETHUSDT | forward_4h | LONG | RANGE_SCALP | 6,571 | 0.005136 | 0.010459 |
| ETHUSDT | forward_2h | SHORT | RANGE_SCALP | 6,571 | -0.002018 | 0.008310 |
| UNIUSDT | forward_2h | LONG | RANGE_SCALP | 6,571 | 0.003313 | 0.011558 |
| ETHUSDT | forward_4h | SHORT | RANGE_SCALP | 6,571 | -0.004825 | 0.010756 |
| BTCUSDT | forward_4h | SHORT | RANGE_SCALP | 6,571 | -0.002888 | 0.006098 |
| BTCUSDT | forward_4h | LONG | RANGE_SCALP | 6,571 | 0.003293 | 0.006557 |
| BTCUSDT | forward_2h | LONG | RANGE_SCALP | 6,571 | 0.001482 | 0.004999 |
| BTCUSDT | forward_2h | SHORT | RANGE_SCALP | 6,571 | -0.001206 | 0.004844 |
| BNBUSDT | forward_4h | SHORT | RANGE_SCALP | 6,571 | -0.003728 | 0.008415 |
| BNBUSDT | forward_4h | LONG | RANGE_SCALP | 6,571 | 0.004044 | 0.007782 |
| BNBUSDT | forward_2h | SHORT | RANGE_SCALP | 6,571 | -0.001569 | 0.006402 |
| ADAUSDT | forward_2h | LONG | RANGE_SCALP | 6,571 | 0.003135 | 0.013498 |
| NEARUSDT | forward_2h | SHORT | RANGE_SCALP | 6,571 | -0.003403 | 0.011600 |
| NEARUSDT | forward_4h | LONG | RANGE_SCALP | 6,571 | 0.007909 | 0.014741 |
| NEARUSDT | forward_4h | SHORT | RANGE_SCALP | 6,571 | -0.007791 | 0.014636 |
| BNBUSDT | forward_2h | LONG | RANGE_SCALP | 6,571 | 0.001819 | 0.006063 |

## Features con >0.5% de valores nulos

| Feature | % nulos |
|---|---|
| s_btc_dominance | 100.0 |
| e_btc_dominance | 100.0 |
| s_funding_rate | 93.3 |
| e_funding_rate | 93.2 |
| window_id | 80.2 |
| window_regime | 80.2 |
| s_ticker_24h_volume | 61.8 |
| s_ticker_last_price | 61.8 |
| s_ticker_quote_volume | 61.8 |
| s_ticker_price_change_percent | 61.8 |
| s_ticker_weighted_avg_price | 61.8 |
| e_ticker_last_price | 61.8 |
| e_ticker_24h_volume | 61.8 |
| e_ticker_quote_volume | 61.8 |
| e_ticker_weighted_avg_price | 61.8 |
| e_ticker_price_change_percent | 61.8 |
| s_btc_30d_pct | 4.9 |
| e_btc_30d_pct | 4.8 |
| s_bb_bandwidth | 1.2 |
| e_bb_bandwidth | 1.2 |
| s_btc_7d_pct | 1.1 |
| e_btc_7d_pct | 1.1 |
| s_rsi_4h | 0.6 |
| e_rsi_4h | 0.5 |

## Hallazgos y notas

- **Regímenes:** la población tiene estratos bien balanceados; `RANGE_SCALP` es el más frecuente (~49%), seguido de `SPOT_BULL` (~31%) y `PRESERVATION` (~20%).
- **Long vs Short:** LONG tiene media positiva y SHORT negativa, consistente con la dirección del retorno.
- **Muestra representativa:** la muestra de Fase 1 (~200k) reproduce las medias poblacionales por régimen/dirección con desviaciones pequeñas (ver tabla "Muestra vs población").
- **Valores faltantes:** `s_btc_dominance` / `e_btc_dominance` son 100% nulos (archivo fuente con 1 solo snapshot); `s_funding_rate` / `e_funding_rate` y `ticker_*` tienen muchos nulos en el periodo previo a la disponibilidad de datos.
- **Próximo paso:** Fase 3 — visualizaciones y reporte final con hallazgos accionables.

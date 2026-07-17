# Tablas de calibración A/B/C por régimen
Clasificación por pico a 12h: A ≥ 5%, B 2.5-5%, C 1-2.5%, Rechazo < 1%.
## Régimen: RANGE_SCALP
Total señales: 11161
### Distribución de picos por clase
| clase | count | % | mean peak_12h | p25 peak_12h | p50 peak_12h | p75 peak_12h | mean peak_4h | p75 peak_4h |
|---|---|---|---|---|---|---|---|---|
| A | 993 | 8.9 | 0.0726 | 0.0558 | 0.0646 | 0.0796 | 0.0363 | 0.0511 |
| B | 2803 | 25.1 | 0.0348 | 0.0291 | 0.0337 | 0.0396 | 0.0197 | 0.0276 |
| C | 3719 | 33.3 | 0.0168 | 0.0131 | 0.0165 | 0.0202 | 0.0113 | 0.0154 |
| REJECT | 3646 | 32.7 | 0.0048 | 0.0021 | 0.0048 | 0.0072 | 0.0040 | 0.0062 |

### Percentiles de features por clase
| feature | clase | p10 | p25 | p50 | p75 | p90 |
|---|---|---|---|---|---|---|
| expected_pnl | A | -0.0936 | 0.1892 | 0.6567 | 1.2295 | 1.7602 |
| expected_pnl | B | -0.1779 | 0.0662 | 0.3939 | 0.7832 | 1.2328 |
| expected_pnl | C | -0.2787 | -0.0786 | 0.2245 | 0.5873 | 0.9741 |
| expected_pnl | REJECT | -0.3189 | -0.1474 | 0.1690 | 0.5191 | 0.8799 |
| success_probability | A | 0.5892 | 0.6050 | 0.6293 | 0.6574 | 0.6836 |
| success_probability | B | 0.5887 | 0.6023 | 0.6275 | 0.6544 | 0.6840 |
| success_probability | C | 0.5857 | 0.5964 | 0.6214 | 0.6492 | 0.6778 |
| success_probability | REJECT | 0.5855 | 0.5961 | 0.6182 | 0.6469 | 0.6778 |
| confluence | A | 1.2491 | 1.4701 | 1.8108 | 2.2040 | 2.5702 |
| confluence | B | 1.2422 | 1.4327 | 1.7852 | 2.1612 | 2.5759 |
| confluence | C | 1.2000 | 1.3500 | 1.7000 | 2.0886 | 2.4897 |
| confluence | REJECT | 1.1969 | 1.3460 | 1.6545 | 2.0561 | 2.4892 |
| range_score | A | 0.0000 | 0.0000 | 0.1000 | 0.4444 | 0.7960 |
| range_score | B | 0.0000 | 0.0000 | 0.3500 | 0.5052 | 0.7905 |
| range_score | C | 0.0000 | 0.0000 | 0.3500 | 0.5164 | 0.7965 |
| range_score | REJECT | 0.0000 | 0.0000 | 0.3500 | 0.4870 | 0.7907 |
| range_position | A | 0.0727 | 0.1208 | 0.2030 | 0.3429 | 0.5269 |
| range_position | B | 0.0825 | 0.1507 | 0.2345 | 0.3554 | 0.4725 |
| range_position | C | 0.0958 | 0.1764 | 0.2702 | 0.3925 | 0.5141 |
| range_position | REJECT | 0.1037 | 0.1801 | 0.2811 | 0.4144 | 0.5323 |
| rsi_4h | A | 31.0498 | 38.0132 | 45.4721 | 54.0000 | 61.1489 |
| rsi_4h | B | 30.0553 | 36.2041 | 44.6927 | 53.9813 | 60.2769 |
| rsi_4h | C | 32.3772 | 38.4615 | 46.3636 | 55.1847 | 61.8541 |
| rsi_4h | REJECT | 32.3686 | 38.2561 | 45.7143 | 54.7379 | 61.3297 |
| vol_spike | A | 0.4176 | 0.5895 | 0.8376 | 1.2080 | 1.7704 |
| vol_spike | B | 0.4082 | 0.5695 | 0.8306 | 1.2774 | 1.8644 |
| vol_spike | C | 0.4172 | 0.5669 | 0.8325 | 1.2230 | 1.7721 |
| vol_spike | REJECT | 0.3879 | 0.5483 | 0.8063 | 1.2279 | 1.8136 |
| short_term_score | A | 0.0000 | 0.1822 | 0.7124 | 1.5309 | 2.3961 |
| short_term_score | B | 0.0000 | 0.0581 | 0.4771 | 0.9614 | 1.6886 |
| short_term_score | C | 0.0000 | 0.0000 | 0.2204 | 0.7365 | 1.3425 |
| short_term_score | REJECT | 0.0000 | 0.0000 | 0.1533 | 0.6304 | 1.1649 |
| atr_pct | A | 0.0111 | 0.0144 | 0.0186 | 0.0241 | 0.0290 |
| atr_pct | B | 0.0094 | 0.0111 | 0.0147 | 0.0186 | 0.0221 |
| atr_pct | C | 0.0069 | 0.0094 | 0.0123 | 0.0161 | 0.0197 |
| atr_pct | REJECT | 0.0039 | 0.0075 | 0.0114 | 0.0149 | 0.0182 |
| peak_quality | A | -0.4826 | -0.0556 | 0.6034 | 1.4038 | 2.1066 |
| peak_quality | B | -0.7067 | -0.4272 | 0.1079 | 0.7159 | 1.2440 |
| peak_quality | C | -0.9200 | -0.6919 | -0.2246 | 0.3513 | 0.8668 |
| peak_quality | REJECT | -1.1887 | -0.8810 | -0.3921 | 0.2708 | 0.7571 |

## Régimen: SPOT_BULL
Total señales: 4372
### Distribución de picos por clase
| clase | count | % | mean peak_12h | p25 peak_12h | p50 peak_12h | p75 peak_12h | mean peak_4h | p75 peak_4h |
|---|---|---|---|---|---|---|---|---|
| A | 315 | 7.2 | 0.0758 | 0.0556 | 0.0644 | 0.0804 | 0.0328 | 0.0476 |
| B | 849 | 19.4 | 0.0341 | 0.0285 | 0.0324 | 0.0386 | 0.0193 | 0.0271 |
| C | 1281 | 29.3 | 0.0166 | 0.0127 | 0.0159 | 0.0203 | 0.0111 | 0.0148 |
| REJECT | 1927 | 44.1 | 0.0039 | 0.0007 | 0.0035 | 0.0065 | 0.0033 | 0.0055 |

### Percentiles de features por clase
| feature | clase | p10 | p25 | p50 | p75 | p90 |
|---|---|---|---|---|---|---|
| expected_pnl | A | 0.0356 | 0.4151 | 0.7085 | 1.2796 | 1.8517 |
| expected_pnl | B | 0.0105 | 0.2599 | 0.6142 | 0.9890 | 1.4307 |
| expected_pnl | C | -0.1338 | 0.1127 | 0.5586 | 0.8811 | 1.3120 |
| expected_pnl | REJECT | -0.3922 | -0.2776 | 0.2774 | 0.7246 | 1.1362 |
| success_probability | A | 0.6196 | 0.6235 | 0.6413 | 0.6663 | 0.6905 |
| success_probability | B | 0.6169 | 0.6239 | 0.6426 | 0.6627 | 0.6917 |
| success_probability | C | 0.6154 | 0.6226 | 0.6438 | 0.6655 | 0.6892 |
| success_probability | REJECT | 0.6144 | 0.6225 | 0.6406 | 0.6616 | 0.6870 |
| confluence | A | 1.5944 | 1.6467 | 1.8837 | 2.2173 | 2.5398 |
| confluence | B | 1.5586 | 1.6526 | 1.9015 | 2.1690 | 2.5560 |
| confluence | C | 1.5390 | 1.6348 | 1.9167 | 2.2066 | 2.5226 |
| confluence | REJECT | 1.5259 | 1.6327 | 1.8750 | 2.1549 | 2.4936 |
| range_score | A | 0.0000 | 0.0000 | 0.0000 | 0.4884 | 0.8473 |
| range_score | B | 0.0000 | 0.0000 | 0.2500 | 0.6496 | 0.8886 |
| range_score | C | 0.0000 | 0.0000 | 0.2500 | 0.6733 | 0.8889 |
| range_score | REJECT | 0.0000 | 0.0000 | 0.2500 | 0.6961 | 0.8971 |
| range_position | A | 0.1411 | 0.2083 | 0.3145 | 0.4912 | 0.6605 |
| range_position | B | 0.1158 | 0.2068 | 0.2896 | 0.3980 | 0.6136 |
| range_position | C | 0.0903 | 0.2084 | 0.2985 | 0.3939 | 0.5954 |
| range_position | REJECT | 0.1601 | 0.2500 | 0.3083 | 0.4527 | 0.6250 |
| rsi_4h | A | 30.9381 | 38.5164 | 45.1923 | 51.5788 | 58.1816 |
| rsi_4h | B | 23.4667 | 35.4331 | 43.6214 | 49.5575 | 56.4233 |
| rsi_4h | C | 22.7273 | 34.1564 | 43.2432 | 48.5839 | 54.7945 |
| rsi_4h | REJECT | 26.0870 | 37.1429 | 44.3182 | 49.4621 | 54.5672 |
| vol_spike | A | 0.5200 | 0.7456 | 1.0588 | 1.6616 | 2.4115 |
| vol_spike | B | 0.4797 | 0.6762 | 1.0787 | 1.6838 | 2.6570 |
| vol_spike | C | 0.5213 | 0.7509 | 1.1396 | 1.7437 | 2.5610 |
| vol_spike | REJECT | 0.5380 | 0.8434 | 1.3794 | 1.9962 | 2.9145 |
| short_term_score | A | 0.0491 | 0.4678 | 0.9516 | 1.6258 | 2.4591 |
| short_term_score | B | 0.0131 | 0.3062 | 0.7796 | 1.3551 | 2.0609 |
| short_term_score | C | 0.0000 | 0.1276 | 0.7094 | 1.1950 | 1.8475 |
| short_term_score | REJECT | 0.0000 | 0.0000 | 0.3146 | 0.9916 | 1.6264 |
| atr_pct | A | 0.0082 | 0.0114 | 0.0165 | 0.0231 | 0.0289 |
| atr_pct | B | 0.0079 | 0.0103 | 0.0137 | 0.0174 | 0.0217 |
| atr_pct | C | 0.0067 | 0.0086 | 0.0117 | 0.0152 | 0.0197 |
| atr_pct | REJECT | 0.0002 | 0.0041 | 0.0085 | 0.0127 | 0.0166 |
| peak_quality | A | -0.6862 | -0.2123 | 0.5738 | 1.1967 | 1.8219 |
| peak_quality | B | -0.6701 | -0.3742 | 0.3121 | 0.7541 | 1.1968 |
| peak_quality | C | -0.8444 | -0.5805 | 0.1693 | 0.5665 | 1.0676 |
| peak_quality | REJECT | -1.3149 | -1.0685 | -0.4616 | 0.3463 | 0.7821 |

## Umbrales propuestos (borrador)
Basados en p25 de A, p25 de B y p10 de C usando `peak_quality`.

### RANGE_SCALP
| perfil | peak_quality min | recomendación percentil |
|---|---|---|
| A | -0.0556 | score >= p85 |
| B | -0.4272 | score p50-p85 |
| C | -0.92 | score p15-p50 |
| REJECT | < -0.92 | score < p15 |

### SPOT_BULL
| perfil | peak_quality min | recomendación percentil |
|---|---|---|
| A | -0.2123 | score >= p85 |
| B | -0.3742 | score p50-p85 |
| C | -0.8444 | score p15-p50 |
| REJECT | < -0.8444 | score < p15 |

## Notas
- `peak_quality` combina `atr_pct`, `expected_pnl`, categoría, calidad por símbolo y momentum condicionado a régimen.
- A es la clase más pequeña (~10% de señales) y representa los picos ≥5% a 12h.
- El score separa mejor la media de pico que la clase exacta; se usa para rankear y concentrar capital.

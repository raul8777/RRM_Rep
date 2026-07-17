# Peak Calibration Dataset

Self-contained historical peak dataset for A/B/C profile calibration.

## Files

- `peaks_all.parquet` — flat DataFrame with one row per detected peak.
- `peaks_all.jsonl` (optional) — JSON Lines version, generated with `--format jsonl`.
- `peaks_summary.json` — overall metadata.
- `peaks_by_regime.json` — return distribution by `mode` and `direction`.

## Columns

Metadata: `symbol`, `direction`, `peak_type`, `peak_start_ts`, `peak_end_ts`,
`peak_return`, `window_id`, `window_regime`.

Asset start features are prefixed with `s_` and end features with `e_`.
Examples: `s_return_1h`, `e_rsi_1h`, `s_vol_spike`, `e_btc_close`.

Market/regime features (prefixed `s_`/`e_` as well):
`btc_close`, `btc_return_1h/4h/24h`, `btc_atr_pct`, `btc_7d_pct`, `btc_30d_pct`,
`btc_day_pct`, `vol_ratio`, `bb_bandwidth`, `ema9_vs_ema21`, `mode_raw`,
`mode_confirmed`, `mode_futures`, `regime_label`, `confidence`, `fear_greed_value`,
`fear_greed_classification`, `btc_dominance`, `market_breadth`, `funding_rate`,
`ticker_last_price`, `ticker_price_change_percent`, `ticker_24h_volume`,
`ticker_quote_volume`, `ticker_weighted_avg_price`.

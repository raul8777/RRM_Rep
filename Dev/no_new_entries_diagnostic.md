# No New Entries Diagnostic

Generated: 2026-07-14T16:57:50.334691+00:00

## Dominant Categories
- last_2h: pausa_operativa
- context: excepcion_de_ciclo
- last_2h (current window): falta_estructural_de_candidatos
- context (current window): excepcion_de_ciclo

## last_2h Counters
- paused: 18
- empty_candidates: 19
- nonempty_candidates: 0
- prediction_ok: 0
- prediction_zero: 147
- cycle_error: 3
- entry_blocked: 6
- entry_executed: 0

## context Counters
- paused: 0
- empty_candidates: 5
- nonempty_candidates: 5
- prediction_ok: 1
- prediction_zero: 22
- cycle_error: 8
- entry_blocked: 10
- entry_executed: 0

## Evidence Samples (last_2h)
- paused:
  - [execute_once] pause_new_entries=True ÔÇö running SL/TP monitor only
  - [execute_once] monitor_only=True (system PAUSED) ÔÇö running SL/TP monitor only
  - [execute_once] pause_new_entries=True ÔÇö running SL/TP monitor only
- empty_candidates:
  - Top buy symbols: []
  - Top buy symbols: []
  - Top buy symbols: []
- nonempty_candidates:
  - (none)
- prediction_ok:
  - (none)
- prediction_zero:
  - Prediction for ETHUSDT: 0.00 USDT
  - Prediction for SOLUSDT: 0.00 USDT
  - Prediction for SUIUSDT: 0.00 USDT
- cycle_error:
  - Commission refresh error (non-fatal): name '_get_futures_client' is not defined
  - Commission refresh error (non-fatal): name '_get_futures_client' is not defined
  - Commission refresh error (non-fatal): name '_get_futures_client' is not defined
- entry_blocked:
  - [BNB] spot=0.0000 futures=0.0000 price=561.87 use_bnb=False actions=[spot:buy_skipped_insufficient_usdt, futures:none]
  - [BNB] spot=0.0000 futures=0.0000 price=561.02 use_bnb=False actions=[spot:buy_skipped_insufficient_usdt, futures:none]
  - [BNB] spot=0.0000 futures=0.0000 price=561.62 use_bnb=False actions=[spot:buy_skipped_insufficient_usdt, futures:none]
- entry_executed:
  - (none)

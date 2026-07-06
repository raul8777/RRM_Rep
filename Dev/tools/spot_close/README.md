# Spot Close Tools

Herramientas operativas para cierre manual de posiciones SPOT.

## Scripts Python

- close_if_non_negative.py: cierra solo si el PnL neto estimado no es negativo.
- force_close.py: cierra forzado por market sell si el exchange lo permite.
- force_close_dust.py: resuelve dust con top-up para cumplir min_notional y luego vende todo.

## Wrappers PowerShell

Ejecutar desde la raiz del workspace o desde crypto-agent.

- .\Dev\tools\spot_close\run_close_if_non_negative.ps1
- .\Dev\tools\spot_close\run_force_close.ps1
- .\Dev\tools\spot_close\run_force_dust_close.ps1

## Ejemplos

```powershell
.\Dev\tools\spot_close\run_close_if_non_negative.ps1 -Symbols "ENAUSDT,ADAUSDT"
.\Dev\tools\spot_close\run_force_close.ps1 -Symbols "ENAUSDT,ADAUSDT,BTCUSDT"
.\Dev\tools\spot_close\run_force_dust_close.ps1 -Symbols "ENAUSDT,ADAUSDT" -BufferUsdt 0.30
```

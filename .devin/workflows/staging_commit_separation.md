---
description: Como separar en staging cada cambio entre "listo para produccion" y "solo simulacion/staging"
---

# Separacion de commits en staging

## Objetivo

Todo cambio en `S:\Tools\AI\crypto-agent-staging` debe quedar clasificado para que,
el dia que se migre a produccion, solo baje lo productivo y nunca el codigo de
simulacion spot-margin ni los experimentos no validados.

## Convencion de commits

Antes de hacer un commit en staging, decidir:

- `[PROD]` — cambio validado, no depende de `spot_margin_simulation_enabled`,
  `DRY_RUN_*`, endpoints de testnet, belief-driven exits, SR discovery
  experimental, ni assets ilquidos de testnet. Puede migrarse a produccion.
- `[SIM]` — cambio exclusivo de simulacion, infraestructura de staging,
  experimentos sin evidencia fuera de muestra, overrides de testnet/demo.
  NO puede migrarse a produccion.

## Uso del helper

// turbo
```powershell
# Ejecutar SIEMPRE desde S:\Tools\AI\crypto-agent-staging
powershell -ExecutionPolicy Bypass -File tmp_staging_commit_helper.ps1 -Type prod -Message "descripcion del cambio"
```

Para cambios de simulacion:

```powershell
powershell -ExecutionPolicy Bypass -File tmp_staging_commit_helper.ps1 -Type sim -Message "ajuste capital simulado"
```

El helper:

1. Verifica que estes en la rama `staging`.
2. Crea (si no existe) la rama `staging-prod-safe` desde `redesign-lifecycle-20260713`.
3. Commitea con el prefijo correcto.
4. Si es `[PROD]`, hace `cherry-pick` a `staging-prod-safe` y corre el guarda.
5. Aborta si el guarda detecta patrones prohibidos en `staging-prod-safe`.

## Hook de commit-msg

El worktree de staging tiene activado `core.hooksPath=.githooks`.
Eso significa que **todo commit manual** (sin el helper) tambien debe llevar el
prefijo. Si no, el hook rechaza el commit y muestra:

```
ERROR: el mensaje de commit en staging debe comenzar con [PROD] o [SIM].
```

Para saltarselo a sabiendas:

```bash
git commit -m "[SIM] ..." --no-verify
```

## Verificar que la rama limpia sigue siendo productiva

// turbo
```powershell
powershell -ExecutionPolicy Bypass -File S:\Tools\AI\crypto-agent\tmp_guard_staging_prod_merge.ps1 -Branch staging-prod-safe
```

Si devuelve exit code 0, la rama `staging-prod-safe` solo contiene codigo
productivo. Si devuelve 1, algun commit [PROD] arrastro codigo de simulacion y
debe corregirse.

## Migrar a produccion (solo cuando se autorice)

1. Asegurar que `staging-prod-safe` pase el guarda.
2. En `S:\Tools\AI\crypto-agent`, crear rama de migracion desde
   `redesign-lifecycle-20260713`.
3. Fusionar o cherry-pick `staging-prod-safe`.
4. Correr tests y validar en el stack productivo con precaucion.

## Reglas duras

- Nunca hacer `git merge staging` completo a produccion.
- Nunca marcar un cambio como `[PROD]` si contiene:
  - `spot_margin_simulation_enabled` / `SpotMarginClient`
  - `DRY_RUN_SIMULATED_CAPITAL_USDT`, `DRY_RUN_MAX_POSITION_NOTIONAL_USDT`,
    `DRY_RUN_FUTURES_MARGIN_FRACTION`
  - `binance_spot_demo_url`, `demo-fapi.binance.com`, `demo-fstream.binance.com`,
    `testnet.binancefuture.com`
  - `position_belief_enabled`, `position_exit_decision_loop_enabled`
  - `sr_discovery_source_enabled`, `range_scalp_intelligent_hold`,
    `evaluate_thesis`, `belief_state`, `belief_last`, `belief_exhausted`,
    `belief_running_strong`
  - `_EXCLUDED_BALANCE_ASSETS`

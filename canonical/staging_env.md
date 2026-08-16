# Staging real — configuración operativa

**Estado:** activo  
**Última actualización:** 2026-08-16

## ACTUALIZACIÓN s130 (16 ago 2026) — aislamiento de código real

**Hallazgo crítico:** hasta el 16 ago 2026, `docker-compose.staging.yml` vivía en el mismo directorio que producción (`S:\Tools\AI\crypto-agent`) y montaba `.:/app` — el mismo working tree que producción. Esto significaba que staging y producción ejecutaban **el mismo código** en todo momento; solo la base de datos/Redis/puerto estaban aislados. Cualquier cambio sin commit aplicado en el working tree quedaba activo en ambos entornos simultáneamente. Este fue el origen de que cambios "en prueba en staging" ya estuvieran corriendo en producción.

**Fix aplicado:**
1. Se consolidó el working tree de producción como commit base (`redesign-lifecycle-20260713` @ `2c3c67f`), incluyendo módulos que estaban sin trackear pero ya en uso real: `ug_loss_policy.py`, `sr_pool_scanner.py`, `support_resistance.py`, `technical_graph_analyst.py`, `side_utils.py`, `dashboard_truth.py`, `docker-compose.staging.yml`, migraciones `023`-`025`.
2. Se creó la rama `staging` desde ese mismo commit.
3. Se creó un **git worktree separado** en `S:\Tools\AI\crypto-agent-staging`, checked out en la rama `staging` — un directorio físicamente distinto con su propio `.git` (worktree), no una copia manual.
4. `docker-compose.staging.yml` **real y autoritativo** ahora vive en `S:\Tools\AI\crypto-agent-staging\docker-compose.staging.yml` y monta el `.` de ese directorio.
5. El archivo homónimo en `S:\Tools\AI\crypto-agent` (producción) quedó como stub de documentación — no debe usarse para levantar staging.
6. El volumen de Postgres de staging (`crypto-agent_staging_postgres_data`) se preservó declarándolo `external: true` en el nuevo compose file, para no perder datos al mover el stack de directorio.

**Flujo de promoción staging → producción:**
```bash
# 1. Desarrollar y probar en el worktree de staging
cd S:\Tools\AI\crypto-agent-staging
# editar código, correr tests, levantar el stack staging, validar

git add -A && git commit -m "..."

# 2. Revisar el diff antes de promover
cd S:\Tools\AI\crypto-agent
git diff redesign-lifecycle-20260713..staging

# 3. Fusionar a producción
git merge staging

# 4. Reiniciar el contenedor de producción para aplicar el código fusionado
docker compose restart app   # o rebuild si cambiaron dependencias
```

**Importante:** producción y staging ahora son directorios de filesystem distintos. Editar archivos directamente en `S:\Tools\AI\crypto-agent` sigue afectando producción de inmediato al reiniciar ese contenedor — la disciplina de "todo cambio pasa primero por staging" depende de que el desarrollo ocurra en `S:\Tools\AI\crypto-agent-staging`, no en la carpeta de producción.

## ACTUALIZACIÓN s130b (16 ago 2026) — paridad de configuración exacta con producción

**Regla dura:** `docker exec <container> printenv` **NO es suficiente** para verificar paridad de config. `config.py::_resolve_env_file()` hace que pydantic-settings cargue `.env` (producción) o `.env.staging.example` (staging) como *dotenv fallback* directamente desde disco para cualquier campo que no esté seteado como variable de entorno explícita — esos valores nunca aparecen en `printenv` pero sí afectan el `Settings()` real. Se encontraron 8 divergencias reales (`sr_pool_enabled`, `sr_pool_trade_enabled`, `sr_pool_intra_cycle_enabled`, `btc_dominance_altcoin_threshold`, `cycle_range_scalp`, `max_rebalance_per_cycle`, `trailing_stop_activation_pct`, `trailing_stop_distance_pct`) que un diff de `printenv` no detectó.

**Método correcto de verificación:**
```bash
docker exec crypto-agent-app python -c "
from config import get_settings; import json
s = get_settings(); d = s.model_dump()
print(json.dumps({k: v for k, v in sorted(d.items())}, default=str))
" > prod_settings.json

docker exec crypto-agent-app_STG python -c "... misma línea ..." > stg_settings.json

# diff programático (Python) de ambos JSON completos — no de env vars
```

**Fix aplicado en `docker-compose.staging.yml` (rama `staging`, commit `edef562`):**
1. Eliminado `env_file: [.env, .env.staging.example]` — producción nunca usa `env_file`; cargarlo en staging filtraba claves legacy del `.env` compartido (`BEAR_MARKET_*`, `TRAILING_STOP_*`, `FUTURES_ENABLED`, etc.) sin equivalente real en producción.
2. El bloque `environment:` de staging ahora declara ~130 variables explícitas, espejo exacto del `Settings()` efectivo de producción (dump en vivo del 16 ago 2026).
3. Único conjunto de excepciones permitidas: `DRY_RUN=true`, `ALLOW_REAL_TRADING=false`, y los 3 parámetros de simulación de capital (`DRY_RUN_SIMULATED_CAPITAL_USDT`, `DRY_RUN_MAX_POSITION_NOTIONAL_USDT`, `DRY_RUN_FUTURES_MARGIN_FRACTION`) que son no-op en producción y solo dan una base de capital simulado a Kelly sizing en staging.

**Verificación final (16 ago 2026):** diff completo de `Settings().model_dump()` entre `crypto-agent-app` y `crypto-agent-app_STG` → 0 campos faltantes, 0 campos extra, 3 mismatches (todos los DRY_RUN intencionales). ~490 campos de configuración idénticos.

**Mantenimiento:** si se agrega un parámetro nuevo a `docker-compose.yml`/`docker-compose.override.yml` de producción, replicarlo también en el `environment:` de `S:\Tools\AI\crypto-agent-staging\docker-compose.staging.yml`, y re-correr el diff de `Settings()` completo — no confiar en `printenv`.

## Objetivo

Garantizar que el sistema disponga de un entorno real de staging, separado del productivo, con su propia base de datos, su propio Redis y su propio puerto de dashboard para evaluar cambios de gobernanza, lifecycle y shadow-readiness sin mezclar datos ni trazas con producción.

## Regla de verdad

El código que implementa la lógica de staging existe, pero un entorno de staging solo es válido si:

1. corre en un stack separado,
2. usa su propia base de datos,
3. tiene un puerto distinto,
4. expone `APP_ENV=staging`,
5. presenta el dashboard con etiqueta `STAGING`,
6. no comparte Datasource ni persistence con producción,
7. mantiene `DRY_RUN=true` y `ALLOW_REAL_TRADING=false`.

Staging puede consumir precios y datos de cuenta de Binance para simular con
verdad de mercado, pero nunca puede enviar órdenes reales. La paridad con
producción aplica a sizing y política Kelly, no al permiso de ejecución.

## Infraestructura

### Stack productivo
- servicio `app` en puerto `8000`
- `postgres` en red interna
- `redis` en red interna
- base de datos `crypto_agent`

### Stack staging
- servicio `app_stg` / contenedor `crypto-agent-app_STG` en puerto `8001`
- servicio `postgres_stg` / contenedor `crypto-agent-db_STG` en red aislada
- servicio `redis_stg` / contenedor `crypto-agent-redis_STG` en red aislada
- base de datos `crypto_agent_stg` por defecto

## Archivos

- `docker-compose.staging.yml`: stack real de staging
- `.env.staging.example`: plantilla de entorno de staging
- `app.py`: detecta `APP_ENV` y expone la etiqueta de entorno
- `frontend/index.html` y `frontend/dashboard.js`: muestran `PRODUCTION` o `STAGING`

## Perfil canónico de sizing STG

Desde 2026-08-16, STG reproduce la política Kelly efectiva de PROD para que
los resultados de lifecycle sean comparables. La seguridad de ejecución sigue
siendo exclusivamente simulada.

| Parámetro | Valor STG | Propósito |
|---|---:|---|
| `FUTURES_LEVERAGE` | `2` | Igualar leverage operativo |
| `KELLY_BASE_USDT` | `150` | Baseline SPOT |
| `KELLY_MAX_USDT` | `2500` | Máximo Kelly SPOT |
| `KELLY_DYNAMIC_BASE_PCT` | `0.22` | Base dinámica sobre líquido |
| `KELLY_MAX_PCT_LIQUID` | `0.90` | Cap líquido SPOT |
| `FUTURES_KELLY_BASE_USDT` | `200` | Baseline FUTURES |
| `FUTURES_KELLY_MAX_USDT` | `2500` | Máximo Kelly FUTURES |
| `FUTURES_KELLY_MAX_PCT_LIQUID` | `0.90` | Cap líquido FUTURES |
| `FUTURES_KELLY_CAP_PCT` | `0.90` | Cap sobre margen disponible |
| `DRY_RUN_FUTURES_MARGIN_FRACTION` | `1.0` | Evita el doble recorte histórico de 30% en simulación |
| `DRY_RUN_SIMULATED_CAPITAL_USDT` | `2324` | Snapshot del wallet FUTURES PROD al 2026-08-16 |
| `DRY_RUN_MAX_POSITION_NOTIONAL_USDT` | `5000` | Permite hasta `$2500` de margen a 2x |

`DRY_RUN_SIMULATED_CAPITAL_USDT` es un snapshot operativo, no un default
permanente. Debe recertificarse contra `totalWalletBalance` de PROD cuando el
capital cambie materialmente. No se debe usar `availableBalance` como capital
base porque cae al comprometer margen en posiciones abiertas y haría que STG
copiara accidentalmente la ocupación actual de PROD.

El saldo simulado disponible se define como:

$$
	ext{simulated available} = \text{simulated capital} - \text{committed simulated margin}
$$

## Arranque

```bash
docker compose --env-file .env.staging.example -f docker-compose.staging.yml up -d --build
```

## URL visible

```text
http://localhost:8001/frontend/index.html
```

## Verificación operativa

- `GET /status` debe devolver `environment=staging`
- `GET /dashboard/config` debe devolver `environment_label=STAGING`
- `GET /config/kelly` debe devolver FUTURES `base_usdt=200`, `max_usdt=2500`,
	`cap_pct_liquid=0.90` y `cap_pct_margin=0.90`
- la cabecera del dashboard debe mostrar `STAGING`
- el backend no debe depender de `postgres` del stack productivo
- `docker inspect crypto-agent-app_STG` debe confirmar `DRY_RUN=true` y
	`ALLOW_REAL_TRADING=false`
- las primeras aperturas posteriores al despliegue deben compararse por
	`base_usdt`, `sized_usdt`, `final_usdt`, capital líquido y multiplicadores;
	no sirven posiciones seed/reconciliadas como evidencia de paridad

Validación del despliegue 2026-08-16:

- `/status`: `status=ok`, `environment=staging`
- `/config/kelly`: perfil FUTURES `200 / 2500 / 0.90 / 0.90`
- tests focales de capital simulado y guard USD: `7 passed`
- diagnósticos de archivos modificados: sin errores

## Rollback

Para dejar de usar staging:

```bash
docker compose -f docker-compose.staging.yml down
```

## Importante

A diferencia de la suposición previa, el puerto 8001 no era un staging real: era solo otra instancia de la app intentado arrancarse sin infraestructura propia. El stack de staging real se crea con `docker-compose.staging.yml` y se valida contra su propia base de datos.

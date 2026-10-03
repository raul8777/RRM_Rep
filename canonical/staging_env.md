# Staging real — configuración operativa

**Estado:** activo  
**Última actualización:** 2026-08-16

## Objetivo

Garantizar un entorno operacional sandbox de Staging, separado de Producción, con DB, Redis y puerto propios. Staging envía órdenes de API únicamente a Binance Spot Demo y USD-M Futures Testnet para probar hipótesis antes de Producción; no es una fase `shadow`/no-submit.

## Regla de verdad

El código que implementa la lógica de staging existe, pero un entorno de staging solo es válido si:

1. corre en un stack separado,
2. usa su propia base de datos,
3. tiene un puerto distinto,
4. expone `APP_ENV=staging`,
5. presenta el dashboard con etiqueta `STAGING`,
6. no comparte Datasource ni persistence con producción,
7. mantiene `BINANCE_USE_TESTNET=true`, `DRY_RUN=false` y `ALLOW_REAL_TRADING=true`, con clientes apuntando exclusivamente a los hosts Spot Demo/Futures Testnet.

Staging usa fondos ficticios de Demo/Testnet y el ciclo real de órdenes/fills de Binance. La paridad con Producción aplica a sizing, selección y política de riesgo; la diferencia deliberada es el host/cuenta sandbox. `ALLOW_REAL_TRADING=true` no autoriza Producción: si el host efectivo no es Testnet/Demo, la preflight debe bloquear.

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
| `DRY_RUN_FUTURES_MARGIN_FRACTION` | `1.0` | Solo para ejecuciones explícitas `DRY_RUN=true`; no limita órdenes activas Testnet |
| `DRY_RUN_SIMULATED_CAPITAL_USDT` | `2324` (histórico) | Solo para balance envelope en `DRY_RUN`; no es saldo ni sizing activo de Staging |
| `DRY_RUN_MAX_POSITION_NOTIONAL_USDT` | `5000` (histórico) | Solo para `DRY_RUN`; no es el cap del canary Testnet |

Los controles `DRY_RUN_SIMULATED_*` solo entran en vigor cuando `DRY_RUN=true`; el wrapper de `run_trades.py` retorna sin modificar balances en el modo operacional actual. No copiar saldos de Producción a Testnet. El sizing activo usa balances sandbox observados y caps explícitos de riesgo/canary de Staging.

Cuando se ejecuta una prueba aislada con `DRY_RUN=true`, el saldo simulado disponible se define como:

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
- `docker inspect crypto-agent-app_STG` debe confirmar `APP_ENV=staging`, `BINANCE_USE_TESTNET=true`, `DRY_RUN=false`, `ALLOW_REAL_TRADING=true` y bases URL exclusivamente Demo/Testnet
- las primeras aperturas posteriores al despliegue deben compararse por
	`base_usdt`, `sized_usdt`, `final_usdt`, capital líquido y multiplicadores;
	no sirven posiciones seed/reconciliadas como evidencia de paridad

Validación histórica del despliegue 2026-08-16 (perfil dry-run; no describe el perfil operacional vigente):

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

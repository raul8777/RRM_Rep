# Staging real — configuración operativa

**Estado:** activo  
**Última actualización:** 2026-08-16

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

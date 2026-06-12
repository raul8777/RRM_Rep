# Binance Trading Agent

Proyecto local privado para un agente de trading en Binance Spot con análisis de TradingView y noticias reales.

## Objetivo
- Recibir alertas de TradingView.
- Analizar noticias de Coindesk y Reuters.
- Ejecutar órdenes en Binance Spot mediante testnet primero.
- Soportar modo `suggest` y modo `auto`.
- Evaluar la estrategia real contra benchmarks.

## Estructura
- `app.py` — servidor FastAPI y endpoints.
- `config.py` — configuración y variables de entorno.
- `binance_client.py` — cliente Binance con testnet/live.
- `news_client.py` — recuperación de noticias reales.
- `agent.py` — lógica del agente de trading.
- `strategy.py` — normalización de señales y construcción de órdenes.
- `tests/` — pruebas básicas.
- `Dev/` — notas y scripts de desarrollo.

## Configuración
Crea un archivo `.env` en el directorio raíz con estas variables:

```env
BINANCE_API_KEY=tu_api_key
BINANCE_API_SECRET=tu_api_secret
BINANCE_USE_TESTNET=true
TRADINGVIEW_WEBHOOK_SECRET=tu_secreto_webhook
AGENT_MODE=suggest
DEFAULT_TRADE_SIZE=10.0
DEFAULT_SYMBOL=BTCUSDT
NEWS_LOOKBACK_HOURS=24
```

### Seguridad adicional
- Usa un archivo de entorno fuera del repositorio cuando sea posible. Define `DOTENV_PATH` para cargar el `.env` desde una ruta segura fuera del repo.
- Puedes almacenar las claves sensibles en un archivo JSON seguro fuera del repositorio y usar `SECRETS_FILE_PATH`.
- No publiques `.env` ni compartas las claves.
- Asegúrate de que `.env` y los archivos de secretos estén en `.gitignore`.
- Para producción, usa variables de entorno del sistema o un gestor de secretos real en lugar de `.env`.

#### Ejemplo de archivo de secretos seguro
Guarda este contenido en un archivo fuera del repositorio y define `SECRETS_FILE_PATH` con su ruta:

```json
{
  "BINANCE_API_KEY": "tu_api_key",
  "BINANCE_API_SECRET": "tu_api_secret",
  "TRADINGVIEW_WEBHOOK_SECRET": "tu_secreto_webhook"
}
```

## Ejecutar

### Con Docker Compose (Recomendado)
Inicia todos los servicios (PostgreSQL, Redis, App):

```bash
docker-compose --env-file .env up -d
```

Detén todos los servicios:

```bash
docker-compose --env-file .env down
```

Verifica los logs:

```bash
docker-compose logs app
```

### Local (Sin Docker)
Instala dependencias:

```bash
pip install -r requirements.txt
```

Inicia el servidor local:

```bash
uvicorn app:app --reload
```

## Endpoints
- `GET /status` — estado del agente.
- `POST /webhook` — recibe alertas de TradingView.
- `POST /command` — cambia el modo entre `suggest` y `auto`.

## Alertas TradingView
El webhook acepta payload JSON con campos como `symbol`, `action`, `quantity`, `price`, `comment` y `message`.

Ejemplo mínimo:

```json
{
  "symbol": "BTCUSDT",
  "action": "buy",
  "quantity": 0.001
}
```

Ejemplo con mensaje libre:

```json
{
  "message": "BUY BTCUSDT 0.001 - breakout signal",
  "strategy": "breakout"
}
```

## Nota
El sistema está diseñado para un entorno privado y local. No publiques las claves API en GitHub.

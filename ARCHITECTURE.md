# Архитектура торгового бота Bybit V5

Статус: **Phase 1 — подключение к бирже**. Историческая доходность любой будущей стратегии не гарантирует прибыль.

## Цель

Инфраструктура для тестирования и исполнения стратегий: backtest → paper → testnet → mainnet. Стратегия заменяется без правок `exchange/`, `risk/`, `execution/`, `database/`, `monitoring/`.

## Режимы

| `system.mode` | Данные | Ордера | Условие |
|---|---|---|---|
| `backtest` | Исторические свечи | Симуляция | Phase 4 |
| `paper` | Realtime Bybit | Симуляция, без API-ордеров | Phase 6 |
| `testnet` | Bybit Testnet API | Реальные testnet-ордера | Phase 8 |
| `mainnet` | Bybit Mainnet API | Реальные ордера только при `LIVE_TRADING_CONFIRM=true` | Phase 10 |

`exchange.testnet` выбирает хост API (`api-testnet.bybit.com` / `api.bybit.com`). `MODE=testnet` требует `testnet: true`. `MODE=mainnet` требует `testnet: false`.

Bybit Demo Trading (`api-demo.bybit.com`) **не используется**: это четвёртая среда с отдельными ключами, её легко перепутать с Testnet.

## Поток данных (все фазы)

```
Bybit REST/WS  →  MarketData / AccountSnapshot
                      ↓
                 Strategy.generate_signal()   # LONG|SHORT|EXIT|HOLD
                      ↓
                 RiskManager.approve()        # размер = risk / расстояние до SL
                      ↓
                 ExecutionEngine              # paper | testnet | mainnet
                      ↓
                 OrderManager                 # idempotency, fill confirmation
                      ↓
                 Database + Logger + Telegram
```

Стратегия **не** отправляет ордера.

Look-ahead (backtest): сигнал по закрытию свечи N исполняется только по следующей доступной цене (открытие N+1 / bid-ask), никогда по close N.

## Структура

```
trading_bot/
  main.py                 CLI Phase 1: ping, instruments, klines, ticker, orderbook, account, stream
  config/                 YAML + overlay из .env
  core/                   ошибки, retry, kill switch, redaction, id событий
  exchange/               REST (pybit), rate limit, WebSocket, спецификация инструмента
  market/                 свечи, стакан, ticker
  account/                wallet / positions / orders / API key (source of truth = биржа)
  monitoring/             structlog + redaction секретов
  strategy/               Phase 3
  risk/                   Phase 5
  execution/              Phases 6–8
  backtest/               Phase 4
  paper/                  Phase 6
  database/               Phase 2+
config/config.yaml
config/.env.example
tests/unit
tests/integration
```

## Назначение модулей

| Модуль | Ответственность | Не делает |
|---|---|---|
| `config` | Параметры без правки кода, секреты только из env | Не хранит ключи в YAML |
| `exchange.bybit_client` | Единая точка REST V5 | Не содержит стратегии |
| `exchange.websocket` | Realtime + reconnect + pause торговли | Не шлёт ордера |
| `exchange.instruments` | tick/lot/minQty **с API** | Не хардкодит ограничения |
| `market` | Нормализация OHLCV/стакана | Не торгует |
| `account` | Снимок баланса/позиций/ордеров | Не кэширует как истину между рестартами |
| `strategy` | Сигнал | Не вызывает API ордеров |
| `risk` | Лимиты и sizing от стопа | Не обходит Kill Switch |
| `execution` | Исполнение + комиссии/slippage | Не считает сигнал |
| `backtest` | Event-driven симуляция | Не подглядывает в будущее |
| `monitoring` | Логи/алерты | Не пишет secret/token в лог |

## Библиотеки (Phase 1)

| Библиотека | Зачем |
|---|---|
| **pybit** | Официальный Python SDK Bybit V5 (подпись REST HMAC) |
| **websocket-client** | Собственный WS-клиент: backoff, heartbeat, pause |
| **pydantic / pydantic-settings** | Валидация конфигурации |
| **PyYAML** | `config.yaml`; float → `Decimal` |
| **python-dotenv** | `.env` |
| **structlog** | JSON/консоль, timestamp, event_id |
| **tenacity** | Конечный exponential backoff |
| **pytest** | Unit + integration |

Не используется в v1: pandas как ядро движка, CCXT, ML.

Деньги и цены — `decimal.Decimal`, не `float`.

## Kill Switch

При активации: новые позиции/ордера запрещены; политика по открытым позициям задаётся `kill_switch.position_policy` (`hold` по умолчанию, `flatten` — в фазе исполнения). Состояние пишется в лог с `event_id`.

## Противоречия ТЗ (решения)

1. **§37 «сначала архитектура, ждать подтверждения» vs «начни с Phase 1».** Сделана архитектура и сразу Phase 1, без Phases 2–10.
2. **Политика Kill Switch не задана.** Вынесена в конфиг: `hold` | `flatten`.
3. **`BYBIT_TESTNET` vs `system.mode`.** Режим работы и хост API — разные оси; валидатор запрещает несовместимые пары.
4. **Demo Trading vs Testnet.** Только настоящий Testnet и Mainnet.
5. **`availableToWithdraw` deprecated на UNIFIED.** Для доступных средств используется `totalAvailableBalance` (+ coin equity).
6. **Интеграционные private-тесты требуют ключи.** Без ключей пропускаются; public REST/WS гоняются всегда.
7. **Walk-forward для заведомо учебной EMA.** Инфраструктура WF появится в Phase 4; оптимизация под max profit запрещена ТЗ.

## Риски Bybit V5 API

- **Geo-block / CloudFront 403.** `api.bybit.com` и `stream.bybit.com` часто закрыты для IP из США и части облачных провайдеров («block access from your country»). Это не rate limit: бот поднимает `GeoRestrictedError` и **не** ретраит бесконечно. Запускайте с IP, который Bybit допускает, либо используйте VPS в разрешённом регионе. Альтернативный домен `bytick.com` задаётся `exchange.domain` / `BYBIT_DOMAIN`, но тоже может быть за CloudFront.
- **Unified Trading Account.** `wallet-balance` с `accountType=UNIFIED`. Классический аккаунт — другой путь.
- **recvWindow / 10002.** Часы сервера. Сверяем `/v5/market/time`. Не раздувать recvWindow бесконечно.
- **Rate limit.** Default linear ~10 req/s на UID; IP 600/5s; 403 «access too frequent» — бан ~10 минут. Realtime — через WS, REST централизован.
- **Kline:** newest-first, max 1000, незакрытая свеча в `close` = last trade. Не торговать по close незакрытой свечи.
- **instruments-info:** >500 linear, нужна пагинация; tick/lot/maxQty меняются. Всегда с API.
- **HTTP 200 ≠ fill.** `retCode` и фактический статус ордера (REST/WS) обязательны (Order Manager, Phase 7).
- **Stop-loss на бирже.** `stopLoss` / conditional. Если SL не подтверждён — CRITICAL + авария (Phase 7–8).
- **orderLinkId** для идемпотентности (дубли WS/retry/рестарт).
- **WS:** ping каждые 20с, иначе disconnect; reconnect + resubscribe; при обрыве — `Trading temporarily paused`.
- **Private WS HMAC:** `GET/realtime{expires}` — expires в ms.
- **Права ключа:** бот отказывается работать, если есть `Withdraw`.
- **Ключ без IP** истекает ~через 90 дней.
- **Funding** на perpetual — учитывать в paper/backtest.
- **Hedge vs one-way** (`positionIdx`) ломает netting, если не учесть.
- **Partial fills** и `orderStatus` Created/New ≠Filled.
- **Региональные хосты** (bybit.kz, bybit.eu, …) — v1 только `bybit.com`.

## Порядок фаз

1. **Phase 1 (эта ветка):** REST, public WS, market data, account snapshot, конфиг, логи, kill switch-состояние, тесты.
2. Phase 2: хранение свечей/сделок (SQLite).
3. Phase 3: интерфейс стратегии + EMA baseline.
4. Phase 4: event-driven backtest, метрики, OOS, walk-forward.
5. Phase 5: risk manager / position sizing.
6. Phase 6: paper engine.
7. Phase 7: order manager, idempotency, SL/TP на бирже.
8. Phase 8: testnet live loop + restore-on-start.
9. Phase 9: Telegram.
10. Phase 10: mainnet guards.

После каждой фазы — тесты. Стратегия не объявляется прибыльной.

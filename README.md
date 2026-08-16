# Модульный торговый бот для Bybit

Инфраструктура для **тестирования и исполнения** торговых стратегий через официальный Bybit V5 API.

**Исторические результаты (backtest / paper) не гарантируют будущую прибыль.** Базовая EMA-стратегия нужна только для проверки контура и не считается рабочей торговой системой.

Сейчас реализована **Phase 1**: подключение к API, рыночные данные, информация об аккаунте, WebSocket с reconnect.

Подробности архитектуры, библиотек и ограничений Bybit: [ARCHITECTURE.md](ARCHITECTURE.md).

## Возможности Phase 1

- REST через официальный SDK `pybit` (не скрейпинг).
- Public WebSocket: ticker, kline, orderbook; heartbeat, exponential backoff, пауза торговли при обрыве.
- Спецификация инструмента (tick size, lot size, min qty, leverage) **только с API**.
- Свечи с защитой от look-ahead: незакрытый бар по умолчанию отбрасывается.
- Снимок аккаунта: баланс, позиции, ордера, fee rate, проверка API-ключа (запрет Withdraw).
- Конфиг в YAML, секреты в `.env`. Secret не пишется в лог.
- Kill Switch как состояние (исполнение политики — в следующих фазах).
- Режимы `backtest | paper | testnet | mainnet`; на mainnet — баннер и флаг `LIVE_TRADING_CONFIRM`.

Ещё не реализовано (следующие фазы): стратегия, backtest, paper engine, боевые ордера, Telegram, Docker, SQLite-журнал сделок.

## Требования

- Python **3.11+**
- Сеть до `api.bybit.com` / `api-testnet.bybit.com` и `stream-testnet.bybit.com`
- Для private-команд: API-ключ **без права Withdraw**, желательно с IP whitelist

## Установка Python

```bash
python3 --version   # >= 3.11
# Ubuntu/Debian, если python3 слишком старый:
# sudo apt update && sudo apt install python3.12 python3.12-venv
```

## Установка зависимостей

```bash
cd bybit_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install -e .
```

## Создание `.env`

```bash
cp .env.example .env
# или
cp config/.env.example config/.env
```

Пример:

```
BYBIT_API_KEY=
BYBIT_API_SECRET=
BYBIT_TESTNET=true
MODE=paper
LIVE_TRADING_CONFIRM=false
```

Параметры стратегии, риска и символов — в `config/config.yaml`, не в коде.

Никогда не коммитьте `.env`. Бот не должен печатать `BYBIT_API_SECRET`, Telegram token и подпись запроса.

## Получение Bybit API key

1. Откройте [Bybit API Management](https://www.bybit.com/app/user/api-management) (mainnet) или [Testnet API](https://testnet.bybit.com/app/user/api-management).
2. Создайте **отдельный** ключ только для бота (не ключ от UI).
3. Права: чтение + торговля контрактами/спотом по необходимости.
4. **Не включайте Withdraw.**
5. Привяжите IP whitelist.
6. Ключ Testnet не работает на Mainnet и наоборот (ошибка `10003`).

Bybit Demo Trading (`api-demo.bybit.com`) в этом проекте не используется.

## Настройка Testnet

1. Регистрация: https://testnet.bybit.com/
2. Ключи с testnet-сайта.
3. В `.env`: `BYBIT_TESTNET=true` и `MODE=testnet`.
4. В `config.yaml`: `exchange.testnet: true`.
5. На testnet можно запросить тестовые средства в UI Bybit.

Проверка без ордеров:

```bash
python -m trading_bot ping
python -m trading_bot account
```

## Запуск Phase 1

Публичные команды ключей не требуют:

```bash
python -m trading_bot ping
python -m trading_bot instruments
python -m trading_bot klines --limit 10
python -m trading_bot ticker
python -m trading_bot orderbook
python -m trading_bot stream --seconds 15
```

Приватные:

```bash
python -m trading_bot account
```

На `MODE=mainnet` в stderr печатается:

```
WARNING:
REAL MONEY TRADING ENABLED
```

Без `LIVE_TRADING_CONFIRM=true` реальные ордера не будут отправляться (когда появится Order Manager).

## Backtest / Paper / Testnet / Mainnet

| Режим | Сейчас | Позже |
|---|---|---|
| `MODE=backtest` | конфиг валиден | Phase 4: `python -m trading_bot backtest` |
| `MODE=paper` | публичный рынок + WS | Phase 6: симуляция ордеров в БД |
| `MODE=testnet` | REST/WS testnet, `account` | Phase 8: боевой цикл с ордерами |
| `MODE=mainnet` | баннер + confirm | Phase 10: малый live |

## Telegram

Запланировано в Phase 9. Переменные уже зарезервированы:

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_ALLOWED_USER_IDS=
```

Команды `/status /positions /balance /pnl /start /stop /kill` будут доступны только whitelist user id.

## Docker

Запланировано после стабилизации testnet-контура (`Dockerfile` + `docker compose up -d`, секреты через env).

## Emergency shutdown

Сейчас: остановить процесс (`Ctrl+C` / `kill`). Kill Switch в коде блокирует новые ордера после `activate()`, но автоматического flatten ещё нет.

Когда появится исполнение:

1. Telegram `/kill` или локальный kill switch.
2. Новые ордера не отправляются.
3. Позиции — по `kill_switch.position_policy` (`hold` или `flatten`).
4. На бирже должны остаться защитные Stop Loss (требование Phase 7).

Если бот или VPS упал, полагайтесь на **биржевой** SL, не на Python-процесс.

## Тесты

```bash
pytest tests/unit
pytest tests/integration -m "not private"
# с ключами:
pytest tests/integration -m private
```

Public integration ходит в Bybit Testnet REST/WS. Если CloudFront отвечает 403 (geo-block типичных облачных IP), эти тесты **пропускаются**, а не считаются зелёными. Private тесты пропускаются без ключей.

Запускайте integration с машины/VPS в регионе, который Bybit не блокирует.

## Безопасность

- Секреты только в окружении.
- Ключ без Withdraw; иначе `account` завершится ошибкой.
- Не логируются secret, token, `X-BAPI-SIGN`.
- Mainnet-ордера — отдельный флаг подтверждения.

## Дисклеймер

Это не инвестиционная рекомендация. Проект строит **детерминированную инфраструктуру**. Прибыль стратегии не утверждается и не обещается.

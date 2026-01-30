# crypto_bot.py
# Telegram Crypto Bot (Binance) — отвечает на КАЖДОЕ сообщение новым сообщением,
# строит свечной график + RSI, умеет уведомления, и работает с ЛЮБЫМИ монетами,
# у которых есть пара SYMBOLUSDT на Binance.
#
# ENV (Render): BOT_TOKEN = токен бота из @BotFather

import os
import json
import time
import asyncio
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple
from io import BytesIO

import requests
import pandas as pd
import pytz

import matplotlib
matplotlib.use("Agg")  # важно для Render/серверов без экрана

import mplfinance as mpf

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ===================== НАСТРОЙКИ =====================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
MOSCOW_TZ = pytz.timezone("Europe/Moscow")

# Кнопки для быстрого выбора (но бот принимает любые тикеры)
TOP_COINS = ["BTC", "ETH", "BNB", "SOL", "ADA", "XRP"]

BINANCE_BASE = "https://api.binance.com"
ALERTS_FILE = "alerts.json"

CHECK_ALERTS_EVERY_SECONDS = 20
HTTP_TIMEOUT = 12

# КЕШ всех торговых пар Binance
BINANCE_SYMBOLS_CACHE: set[str] = set()
BINANCE_SYMBOLS_LAST_UPDATE = 0
BINANCE_SYMBOLS_TTL = 6 * 60 * 60  # 6 часов

# ===================== УВЕДОМЛЕНИЯ (МОДЕЛЬ) =====================

@dataclass
class PriceAlert:
    alert_id: int
    user_id: int
    symbol: str
    direction: str  # "above" или "below"
    target: float
    created_at: int
    is_active: bool = True


ALERTS: Dict[int, List[PriceAlert]] = {}
NEXT_ALERT_ID = 1


def load_alerts() -> None:
    global ALERTS, NEXT_ALERT_ID
    try:
        if not os.path.exists(ALERTS_FILE):
            return
        with open(ALERTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)

        ALERTS = {}
        max_id = 0
        for user_id_str, items in raw.get("alerts", {}).items():
            uid = int(user_id_str)
            ALERTS[uid] = []
            for it in items:
                a = PriceAlert(**it)
                ALERTS[uid].append(a)
                max_id = max(max_id, a.alert_id)

        NEXT_ALERT_ID = max_id + 1
    except Exception:
        ALERTS = {}
        NEXT_ALERT_ID = 1


def save_alerts() -> None:
    try:
        raw = {"alerts": {}}
        for uid, items in ALERTS.items():
            raw["alerts"][str(uid)] = [asdict(a) for a in items]
        with open(ALERTS_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_user_alerts(user_id: int) -> List[PriceAlert]:
    return ALERTS.get(user_id, [])


def add_alert(user_id: int, symbol: str, direction: str, target: float) -> PriceAlert:
    global NEXT_ALERT_ID
    a = PriceAlert(
        alert_id=NEXT_ALERT_ID,
        user_id=user_id,
        symbol=symbol.upper(),
        direction=direction,
        target=target,
        created_at=int(time.time()),
        is_active=True,
    )
    NEXT_ALERT_ID += 1
    ALERTS.setdefault(user_id, []).append(a)
    save_alerts()
    return a


def delete_alert(user_id: int, alert_id: int) -> bool:
    items = ALERTS.get(user_id, [])
    new_items = [a for a in items if a.alert_id != alert_id]
    if len(new_items) == len(items):
        return False
    ALERTS[user_id] = new_items
    save_alerts()
    return True


# ===================== BINANCE API =====================

def _binance_get(path: str, params: Optional[dict] = None):
    try:
        url = f"{BINANCE_BASE}{path}"
        r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
        data = r.json()
        # Binance ошибки тоже возвращает JSON вида {"code": -1121, "msg": "..."}
        if isinstance(data, dict) and "code" in data and "msg" in data:
            return None
        return data
    except Exception:
        return None


def refresh_binance_symbols(force: bool = False) -> None:
    """Качаем все торговые пары Binance и кешируем."""
    global BINANCE_SYMBOLS_CACHE, BINANCE_SYMBOLS_LAST_UPDATE

    now = int(time.time())
    if not force and BINANCE_SYMBOLS_CACHE and (now - BINANCE_SYMBOLS_LAST_UPDATE) < BINANCE_SYMBOLS_TTL:
        return

    data = _binance_get("/api/v3/exchangeInfo", params=None)
    if not isinstance(data, dict) or "symbols" not in data:
        return

    symbols = set()
    try:
        for s in data["symbols"]:
            if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True):
                sym = s.get("symbol")
                if sym:
                    symbols.add(sym)
    except Exception:
        return

    if symbols:
        BINANCE_SYMBOLS_CACHE = symbols
        BINANCE_SYMBOLS_LAST_UPDATE = now


def binance_pair_exists(base: str, quote: str = "USDT") -> bool:
    refresh_binance_symbols()
    return f"{base.upper()}{quote.upper()}" in BINANCE_SYMBOLS_CACHE


def get_price(symbol: str) -> Optional[float]:
    sym = symbol.upper().strip()
    if not binance_pair_exists(sym, "USDT"):
        return None

    data = _binance_get("/api/v3/ticker/price", params={"symbol": f"{sym}USDT"})
    if not isinstance(data, dict) or "price" not in data:
        return None
    try:
        return float(data["price"])
    except Exception:
        return None


def get_24h_change(symbol: str) -> Optional[float]:
    sym = symbol.upper().strip()
    if not binance_pair_exists(sym, "USDT"):
        return None

    data = _binance_get("/api/v3/ticker/24hr", params={"symbol": f"{sym}USDT"})
    if not isinstance(data, dict) or "priceChangePercent" not in data:
        return None
    try:
        return float(data["priceChangePercent"])
    except Exception:
        return None


def get_candles(symbol: str, interval: str, limit: int) -> Optional[pd.DataFrame]:
    sym = symbol.upper().strip()
    if not binance_pair_exists(sym, "USDT"):
        return None

    data = _binance_get(
        "/api/v3/klines",
        params={"symbol": f"{sym}USDT", "interval": interval, "limit": limit},
    )
    if not data or not isinstance(data, list):
        return None

    try:
        df = pd.DataFrame(
            data,
            columns=[
                "time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "c1",
                "c2",
                "c3",
                "c4",
                "c5",
                "c6",
            ],
        )
        df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert(MOSCOW_TZ)
        df.set_index("time", inplace=True)
        df = df[["open", "high", "low", "close"]].astype(float)
        return df
    except Exception:
        return None


# ===================== АНАЛИТИКА =====================

def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def rsi_text(rsi_value: float) -> str:
    if rsi_value >= 70:
        return "🔴 Перекуплен (возможна коррекция)"
    if rsi_value <= 30:
        return "🟢 Перепродан (возможен отскок)"
    return "🟡 Нейтральная зона"


def detect_trend(df: pd.DataFrame) -> Tuple[str, float]:
    first = float(df["close"].iloc[0])
    last = float(df["close"].iloc[-1])
    change = ((last - first) / first) * 100

    if change > 1:
        return "🟢 Восходящий (LONG)", change
    if change < -1:
        return "🔴 Нисходящий (SHORT)", change
    return "🟡 Флэт", change


# ===================== ГРАФИК (СВЕЧИ + RSI) =====================

def build_chart_with_rsi(df: pd.DataFrame, symbol: str, tf: str) -> Tuple[BytesIO, float]:
    df = df.copy()
    df["RSI"] = calculate_rsi(df).bfill()

    rsi_plot = mpf.make_addplot(df["RSI"], panel=1, ylabel="RSI")

    buf = BytesIO()

    mpf.plot(
        df,
        type="candle",
        style="charles",
        title=f"{symbol} — {tf} (МСК)",
        ylabel="USDT",
        addplot=[rsi_plot],
        panel_ratios=(3, 1),
        hlines=dict(
            hlines=[30, 70],
            colors=["green", "red"],
            linestyle="--",
            panel=1,
        ),
        volume=False,
        savefig=dict(fname=buf, dpi=120, bbox_inches="tight"),
    )

    buf.seek(0)
    return buf, float(df["RSI"].iloc[-1])


# ===================== UI / КНОПКИ =====================

def kb_main_menu() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(c, callback_data=f"COIN_{c}") for c in TOP_COINS]]
    rows.append([InlineKeyboardButton("📌 Мои уведомления", callback_data="MY_ALERTS")])
    return InlineKeyboardMarkup(rows)


def kb_timeframes() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🕐 1h", callback_data="TF_1h"),
                InlineKeyboardButton("🕓 4h", callback_data="TF_4h"),
                InlineKeyboardButton("📅 1d", callback_data="TF_1d"),
            ],
            [
                InlineKeyboardButton("🔔 Установить уведомление", callback_data="SET_ALERT"),
                InlineKeyboardButton("📌 Мои уведомления", callback_data="MY_ALERTS"),
            ],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="MENU")],
        ]
    )


def kb_after_chart() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔔 Установить уведомление", callback_data="SET_ALERT"),
                InlineKeyboardButton("📌 Мои уведомления", callback_data="MY_ALERTS"),
            ],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="MENU")],
        ]
    )


def kb_after_alert_created() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📌 Мои уведомления", callback_data="MY_ALERTS"),
                InlineKeyboardButton("🏠 Главное меню", callback_data="MENU"),
            ]
        ]
    )


def kb_alerts_list(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    alerts = get_user_alerts(user_id)
    for a in alerts:
        arrow = "≥" if a.direction == "above" else "≤"
        rows.append(
            [
                InlineKeyboardButton(
                    f"❌ #{a.alert_id} {a.symbol} {arrow} {a.target}",
                    callback_data=f"DEL_{a.alert_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="MENU")])
    return InlineKeyboardMarkup(rows)


# ===================== ВСПОМОГАТЕЛЬНОЕ =====================

def normalize_symbol(text: str) -> str:
    # Принимаем любые формы: "btc", "BTCUSDT", "BTC/USDT"
    s = (text or "").strip().upper()
    s = s.replace("/", "").replace("-", "").replace(" ", "")
    # Если человек ввёл BTCUSDT — оставим BTC
    if s.endswith("USDT") and len(s) > 4:
        s = s[:-4]
    return s


def parse_alert_price(text: str) -> Optional[Tuple[str, float]]:
    """
    Поддержка:
      50000  -> above
      >50000 -> above
      <50000 -> below
    """
    t = (text or "").strip().replace(" ", "")
    if not t:
        return None

    direction = "above"
    if t.startswith(">"):
        direction = "above"
        t = t[1:]
    elif t.startswith("<"):
        direction = "below"
        t = t[1:]

    try:
        price = float(t)
        if price <= 0:
            return None
        return direction, price
    except Exception:
        return None


def binance_symbol_hint(symbol: str) -> str:
    s = symbol.upper().strip()

    # Если USDT-пары нет, подскажем другие котировки
    quotes_to_try = ["USDC", "FDUSD", "BUSD", "TRY", "BTC", "ETH"]
    available = []
    for q in quotes_to_try:
        if binance_pair_exists(s, q):
            available.append(f"{s}{q}")

    if available:
        pairs = ", ".join(available[:6])
        return (
            f"❌ На Binance **нет пары {s}USDT**.\n\n"
            f"✅ Зато есть: {pairs}\n\n"
            f"👉 Сейчас бот строит графики по USDT-парам.\n"
            f"Выбери другую монету или напиши тикер, у которого есть USDT-пара."
        )

    return (
        f"❌ Не удалось получить данные по `{s}`.\n\n"
        f"Причины:\n"
        f"• пары `{s}USDT` нет на Binance\n"
        f"• временно недоступен Binance API\n\n"
        f"✅ Напиши любой тикер (например: BTC, ETH, SOL) или выбери кнопку ниже."
    )


# ===================== HANDLERS =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_alert_price"] = False
    context.user_data.pop("symbol", None)
    context.user_data.pop("alert_symbol", None)

    await update.message.reply_text(
        "Выберите криптовалюту или введите символ (например BTC):",
        reply_markup=kb_main_menu(),
    )


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "MENU":
        context.user_data["awaiting_alert_price"] = False
        context.user_data.pop("symbol", None)
        context.user_data.pop("alert_symbol", None)
        await query.message.reply_text(
            "Главное меню. Выберите монету или введите символ:",
            reply_markup=kb_main_menu(),
        )
        return

    if data == "MY_ALERTS":
        alerts = get_user_alerts(user_id)
        if not alerts:
            await query.message.reply_text(
                "📌 У вас пока нет уведомлений.\n\n"
                "Выберите монету → «Установить уведомление».",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🏠 Главное меню", callback_data="MENU")]]
                ),
            )
            return

        lines = ["📌 Ваши уведомления:"]
        for a in alerts:
            arrow = "≥" if a.direction == "above" else "≤"
            lines.append(f"• #{a.alert_id} {a.symbol} {arrow} {a.target}")
        await query.message.reply_text("\n".join(lines), reply_markup=kb_alerts_list(user_id))
        return

    if data.startswith("DEL_"):
        try:
            alert_id = int(data.replace("DEL_", ""))
        except ValueError:
            await query.message.reply_text(
                "❌ Ошибка: неверный ID уведомления.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🏠 Главное меню", callback_data="MENU")]]
                ),
            )
            return

        ok = delete_alert(user_id, alert_id)
        if ok:
            await query.message.reply_text(f"✅ Уведомление #{alert_id} удалено.", reply_markup=kb_alerts_list(user_id))
        else:
            await query.message.reply_text(f"❌ Не нашёл уведомление #{alert_id}.", reply_markup=kb_alerts_list(user_id))
        return

    if data.startswith("COIN_"):
        symbol = data.replace("COIN_", "").strip().upper()
        context.user_data["symbol"] = symbol
        context.user_data["awaiting_alert_price"] = False

        await query.message.reply_text(
            f"✅ Выбран {symbol}. Выберите таймфрейм:",
            reply_markup=kb_timeframes(),
        )
        return

    if data == "SET_ALERT":
        symbol = context.user_data.get("symbol")
        if not symbol:
            await query.message.reply_text("Сначала выберите монету 🙂", reply_markup=kb_main_menu())
            return

        # проверим, что у монеты есть USDT-пара
        if not binance_pair_exists(symbol, "USDT"):
            await query.message.reply_text(binance_symbol_hint(symbol), reply_markup=kb_main_menu())
            return

        context.user_data["awaiting_alert_price"] = True
        context.user_data["alert_symbol"] = symbol

        await query.message.reply_text(
            "🔔 Введите цену, при достижении которой уведомить.\n"
            "Примеры:\n"
            "• 50000  (уведомить когда цена станет ≥ 50000)\n"
            "• <50000 (уведомить когда цена станет ≤ 50000)\n\n"
            f"Монета: {symbol}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="MENU")]]),
        )
        return

    if data.startswith("TF_"):
        tf = data.replace("TF_", "")
        symbol = context.user_data.get("symbol")
        if not symbol:
            await query.message.reply_text("Сначала выберите монету 🙂", reply_markup=kb_main_menu())
            return

        await send_crypto_info(query.message, symbol, tf)
        return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Бот отвечает на ЛЮБОЕ сообщение.
    - если ждём цену для уведомления -> обработка цены
    - иначе -> считаем, что это тикер монеты
    """
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    # 1) ждём цену для уведомления
    if context.user_data.get("awaiting_alert_price"):
        parsed = parse_alert_price(text)
        symbol = context.user_data.get("alert_symbol") or context.user_data.get("symbol")

        if not symbol:
            context.user_data["awaiting_alert_price"] = False
            await update.message.reply_text("⚠️ Потерял выбранную монету. Выберите заново:", reply_markup=kb_main_menu())
            return

        if not parsed:
            await update.message.reply_text(
                "⚠️ Не понял цену.\nПример: 50000 или <50000",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="MENU")]]),
            )
            return

        direction, target = parsed

        cur = get_price(symbol)
        if cur is None:
            context.user_data["awaiting_alert_price"] = False
            await update.message.reply_text(binance_symbol_hint(symbol), reply_markup=kb_main_menu())
            return

        a = add_alert(user_id, symbol, direction, target)
        context.user_data["awaiting_alert_price"] = False

        arrow = "≥" if a.direction == "above" else "≤"
        await update.message.reply_text(
            "✅ Уведомление создано!\n"
            f"ID: #{a.alert_id}\n"
            f"Монета: {a.symbol}\n"
            f"Цель: {arrow} {a.target}\n"
            f"Текущая: {cur:.6f}",
            reply_markup=kb_after_alert_created(),
        )
        return

    # 2) обычный режим: тикер
    symbol = normalize_symbol(text)

    # Валидация
    if not (2 <= len(symbol) <= 15) or not symbol.isalnum():
        await update.message.reply_text(
            "⚠️ Это не похоже на тикер.\n"
            "Напиши, например: BTC, ETH, SOL, DOGE, PEPE\n"
            "или выбери кнопку ниже 👇",
            reply_markup=kb_main_menu(),
        )
        return

    context.user_data["symbol"] = symbol

    # Если монеты на Binance в USDT нет — сразу подскажем (и всё равно ответим)
    if not binance_pair_exists(symbol, "USDT"):
        await update.message.reply_text(binance_symbol_hint(symbol), reply_markup=kb_main_menu())
        return

    await update.message.reply_text(f"✅ Выбран {symbol}. Выберите таймфрейм:", reply_markup=kb_timeframes())


# ===================== ОСНОВНОЕ: ОТПРАВКА ДАННЫХ + ГРАФИК =====================

async def send_crypto_info(message, symbol: str, tf: str):
    """
    Требование: новый запрос -> новое сообщение от бота.
    Поэтому всегда reply_photo/reply_text (НЕ edit).
    """
    sym = symbol.upper().strip()

    if not binance_pair_exists(sym, "USDT"):
        await message.reply_text(binance_symbol_hint(sym), reply_markup=kb_main_menu())
        return

    price = get_price(sym)
    change24 = get_24h_change(sym)

    if tf == "1h":
        df = get_candles(sym, "1h", 120)
    elif tf == "4h":
        df = get_candles(sym, "4h", 120)
    else:
        df = get_candles(sym, "1d", 120)

    if price is None or change24 is None or df is None or df.empty:
        await message.reply_text(binance_symbol_hint(sym), reply_markup=kb_main_menu())
        return

    trend_text, trend_change = detect_trend(df)
    chart_buf, rsi_value = build_chart_with_rsi(df, sym, tf)

    caption = (
        f"💰 {sym}USDT: {price:.6f}\n"
        f"📉 24h: {change24:.2f}%\n"
        f"📊 Тренд: {trend_text}\n"
        f"📈 Движение (на TF): {trend_change:.2f}%\n"
        f"📉 RSI: {rsi_value:.2f} — {rsi_text(rsi_value)}\n"
        f"⏱ Таймфрейм: {tf} (МСК)\n\n"
        f"🔔 Уведомления: «Установить уведомление»"
    )

    await message.reply_photo(photo=chart_buf, caption=caption, reply_markup=kb_after_chart())


# ===================== ФОНОВАЯ ПРОВЕРКА УВЕДОМЛЕНИЙ =====================

async def alerts_loop(app):
    while True:
        try:
            # обновим кеш пар иногда (чтобы не устаревал)
            refresh_binance_symbols()

            for user_id, items in list(ALERTS.items()):
                if not items:
                    continue

                cache_price: Dict[str, Optional[float]] = {}

                for a in list(items):
                    if not a.is_active:
                        continue

                    # если внезапно пары USDT нет — пропускаем
                    if not binance_pair_exists(a.symbol, "USDT"):
                        continue

                    if a.symbol not in cache_price:
                        cache_price[a.symbol] = get_price(a.symbol)

                    cur = cache_price[a.symbol]
                    if cur is None:
                        continue

                    hit = False
                    if a.direction == "above" and cur >= a.target:
                        hit = True
                    if a.direction == "below" and cur <= a.target:
                        hit = True

                    if hit:
                        arrow = "≥" if a.direction == "above" else "≤"
                        text = (
                            "🔔 Сработало уведомление!\n"
                            f"#{a.alert_id} {a.symbol}USDT\n"
                            f"Условие: {arrow} {a.target}\n"
                            f"Текущая: {cur:.6f}"
                        )
                        try:
                            await app.bot.send_message(chat_id=user_id, text=text, reply_markup=kb_after_alert_created())
                        except Exception:
                            pass

                        delete_alert(user_id, a.alert_id)

        except Exception:
            pass

        await asyncio.sleep(CHECK_ALERTS_EVERY_SECONDS)


async def post_init(app):
    load_alerts()
    refresh_binance_symbols(force=True)
    app.create_task(alerts_loop(app))


# ===================== RUN =====================

def main():
    if not TOKEN:
        raise RuntimeError("❌ Не найден BOT_TOKEN. Добавь переменную окружения BOT_TOKEN в Render.")

    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(buttons))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("✅ Бот запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

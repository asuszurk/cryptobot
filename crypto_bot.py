import os
TOKEN = os.getenv("BOT_TOKEN")

import asyncio
from io import BytesIO

import requests
import pandas as pd
import pytz
import mplfinance as mpf

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== НАСТРОЙКИ ==================
#TOKEN = "ВАШ_TELEGRAM_TOKEN"  # <-- вставь реальный токен
TOP_COINS = ["BTC", "ETH", "BNB", "SOL", "ADA", "XRP"]
MOSCOW_TZ = pytz.timezone("Europe/Moscow")
CHECK_ALERTS_EVERY_SECONDS = 30
BINANCE_BASE = "https://api.binance.com"

# ================== PLACEHOLDER IMAGE (СТАБИЛЬНЫЙ JPEG) ==================
def make_placeholder_jpg(text: str = "Crypto Bot"):
    """
    Telegram иногда падает на 1x1 / прозрачных PNG.
    Делаем нормальный JPEG 800x450 (без прозрачности) => всегда проходит.
    """
    fig = plt.figure(figsize=(8, 4.5), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.text(0.5, 0.55, text, ha="center", va="center", fontsize=28)
    ax.text(0.5, 0.35, "Выберите монету / таймфрейм / уведомления", ha="center", va="center", fontsize=12)

    buf = BytesIO()
    fig.savefig(buf, format="jpeg", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    buf.name = "ui.jpg"
    return buf

PLACEHOLDER_BUF_BYTES = make_placeholder_jpg("Crypto Bot").getvalue()

def placeholder_image(name="ui.jpg") -> BytesIO:
    buf = BytesIO(PLACEHOLDER_BUF_BYTES)
    buf.name = name
    buf.seek(0)
    return buf

# ================== BINANCE ==================
def get_price(symbol: str):
    try:
        url = f"{BINANCE_BASE}/api/v3/ticker/price"
        data = requests.get(url, params={"symbol": f"{symbol}USDT"}, timeout=8).json()
        return float(data["price"])
    except:
        return None

def get_24h_change(symbol: str):
    try:
        url = f"{BINANCE_BASE}/api/v3/ticker/24hr"
        data = requests.get(url, params={"symbol": f"{symbol}USDT"}, timeout=8).json()
        return float(data["priceChangePercent"])
    except:
        return None

def get_candles(symbol: str, interval: str, limit: int):
    try:
        url = f"{BINANCE_BASE}/api/v3/klines"
        params = {"symbol": f"{symbol}USDT", "interval": interval, "limit": limit}
        data = requests.get(url, params=params, timeout=8).json()

        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "_","_","_","_","_","_"
        ])

        df["time"] = (
            pd.to_datetime(df["time"], unit="ms", utc=True)
              .dt.tz_convert(MOSCOW_TZ)
        )
        df.set_index("time", inplace=True)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        return df
    except:
        return None

# ================= RSI =================
def calculate_rsi(df: pd.DataFrame, period: int = 14):
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def rsi_state(rsi: float):
    if rsi <= 30:
        return "🟢 RSI: перепродан"
    elif rsi >= 70:
        return "🔴 RSI: перекуплен"
    else:
        return "🟡 RSI: нейтрально"

def rsi_signal(rsi: float):
    if rsi <= 30:
        return "🟢 LONG"
    elif rsi >= 70:
        return "🔴 SHORT"
    else:
        return "🟡 WAIT"

# ================= TREND =================
def detect_trend(df: pd.DataFrame):
    first = df["close"].iloc[0]
    last = df["close"].iloc[-1]
    change = ((last - first) / first) * 100

    if change > 1:
        return "🟢 Восходящий тренд", change
    elif change < -1:
        return "🔴 Нисходящий тренд", change
    else:
        return "🟡 Флэт", change

# ================= GRAPH =================
def build_chart(df: pd.DataFrame, symbol: str, tf: str):
    df = df.copy()
    df["RSI"] = calculate_rsi(df)

    rsi_plot = mpf.make_addplot(df["RSI"], panel=1, ylabel="RSI")

    fig, axes = mpf.plot(
        df,
        type="candle",
        style="charles",
        title=f"{symbol} — {tf} (МСК)",
        ylabel="USD",
        addplot=[rsi_plot],
        panel_ratios=(3, 1),
        volume=False,
        returnfig=True
    )

    rsi_ax = axes[-1]
    rsi_ax.axhline(30, linestyle="--")
    rsi_ax.axhline(70, linestyle="--")
    rsi_ax.set_ylim(0, 100)

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    buf.name = f"{symbol}_{tf}.png"
    return buf, float(df["RSI"].iloc[-1])

# ================= UI / KEYBOARDS =================
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(c, callback_data=f"COIN_{c}") for c in TOP_COINS]
    ])

def timeframe_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🕐 1h", callback_data="TF_1h"),
            InlineKeyboardButton("🕓 4h", callback_data="TF_4h"),
            InlineKeyboardButton("📅 1d", callback_data="TF_1d"),
        ],
        [
            InlineKeyboardButton("🔔 Установить уведомление", callback_data="ALERT_SET"),
            InlineKeyboardButton("📋 Мои уведомления", callback_data="ALERT_LIST"),
        ],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")]
    ])

def nav_full_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="BACK_LAST"),
            InlineKeyboardButton("📋 Мои уведомления", callback_data="ALERT_LIST"),
        ],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")]
    ])

def menu_only_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")]
    ])

def ui_ptr(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.user_data.get("ui_chat_id")
    msg_id = context.user_data.get("ui_message_id")
    if chat_id and msg_id:
        return chat_id, msg_id
    return None, None

async def set_ui_ptr(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int):
    context.user_data["ui_chat_id"] = update.effective_chat.id
    context.user_data["ui_message_id"] = message_id

async def ui_edit_screen(context: ContextTypes.DEFAULT_TYPE, photo_buf: BytesIO, caption: str, reply_markup=None):
    chat_id, msg_id = ui_ptr(context)
    if not chat_id or not msg_id:
        return None
    try:
        photo_buf.seek(0)
        media = InputMediaPhoto(media=photo_buf, caption=caption)
        return await context.bot.edit_message_media(
            chat_id=chat_id,
            message_id=msg_id,
            media=media,
            reply_markup=reply_markup
        )
    except:
        return None

async def ui_send_new_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, photo_buf: BytesIO, caption: str, reply_markup=None):
    photo_buf.seek(0)
    msg = await update.effective_chat.send_photo(
        photo=photo_buf,
        caption=caption,
        reply_markup=reply_markup
    )
    await set_ui_ptr(update, context, msg.message_id)
    return msg

# ================= ALERT STORAGE =================
def get_alerts_store(app):
    if "alerts" not in app.bot_data:
        app.bot_data["alerts"] = {}
    return app.bot_data["alerts"]

def next_alert_id(user_alerts: list):
    return max([a["id"] for a in user_alerts], default=0) + 1

def format_alert_line(a: dict) -> str:
    op = "≥" if a["direction"] == "up" else "≤"
    return f"#{a['id']} {a['symbol']} {op} {a['target']}"

def build_alerts_keyboard(user_alerts: list) -> InlineKeyboardMarkup:
    rows = []
    for a in sorted(user_alerts, key=lambda x: x["id"]):
        rows.append([InlineKeyboardButton(f"❌ Удалить {format_alert_line(a)}", callback_data=f"DEL_{a['id']}")])
    rows.append([
        InlineKeyboardButton("⬅️ Назад", callback_data="BACK_LAST"),
        InlineKeyboardButton("🏠 Главное меню", callback_data="menu"),
    ])
    return InlineKeyboardMarkup(rows)

def format_alerts_text(user_alerts: list) -> str:
    if not user_alerts:
        return "🔕 У тебя нет активных уведомлений.\n\nСоздай через кнопку 🔔."
    lines = ["🔔 Твои уведомления (удаление одним кликом):\n"]
    for a in sorted(user_alerts, key=lambda x: x["id"]):
        lines.append("• " + format_alert_line(a))
    return "\n".join(lines)

async def create_alert_for_user(app, user_id: int, symbol: str, target: float):
    current = await asyncio.to_thread(get_price, symbol)
    if current is None:
        return False, "❌ Не удалось получить цену. Проверь символ (BTC, ETH...)"

    direction = "up" if target > current else "down"
    store = get_alerts_store(app)
    user_alerts = store.get(user_id, [])
    aid = next_alert_id(user_alerts)

    user_alerts.append({"id": aid, "symbol": symbol, "target": target, "direction": direction})
    store[user_id] = user_alerts

    op_txt = "≥" if direction == "up" else "≤"
    return True, (
        f"✅ Уведомление создано #{aid}\n"
        f"🪙 {symbol}\n"
        f"🎯 Цель: {op_txt} {target}\n"
        f"💰 Текущая: {current:.2f}"
    )

# ================= ALERT CHECK =================
async def check_alerts_once(app):
    store = get_alerts_store(app)
    if not store:
        return

    symbols = set()
    for _uid, alist in store.items():
        for a in alist:
            symbols.add(a["symbol"])
    if not symbols:
        return

    prices = {}
    for sym in symbols:
        price = await asyncio.to_thread(get_price, sym)
        if price is not None:
            prices[sym] = price

    to_remove = []
    for uid, alist in store.items():
        for a in alist:
            sym = a["symbol"]
            if sym not in prices:
                continue

            price = prices[sym]
            target = a["target"]
            direction = a["direction"]
            triggered = (direction == "up" and price >= target) or (direction == "down" and price <= target)

            if triggered:
                op_txt = "≥" if direction == "up" else "≤"
                text = (
                    f"🔔 Сработало уведомление #{a['id']}\n"
                    f"🪙 {sym}\n"
                    f"🎯 Цель: {op_txt} {target}\n"
                    f"💰 Сейчас: {price:.2f}"
                )
                try:
                    await app.bot.send_message(chat_id=uid, text=text)
                except:
                    pass
                to_remove.append((uid, a["id"]))

    if to_remove:
        for uid, aid in to_remove:
            store[uid] = [a for a in store.get(uid, []) if a["id"] != aid]

async def alerts_loop(app):
    while True:
        await check_alerts_once(app)
        await asyncio.sleep(CHECK_ALERTS_EVERY_SECONDS)

async def post_init(app):
    if app.job_queue is not None:
        app.job_queue.run_repeating(lambda ctx: check_alerts_once(app), interval=CHECK_ALERTS_EVERY_SECONDS, first=10)
    else:
        app.create_task(alerts_loop(app))

# ================= SCREENS =================
async def screen_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = "Выберите криптовалюту или введите символ:"
    edited = await ui_edit_screen(context, placeholder_image(), caption, reply_markup=main_menu_kb())
    if edited is None:
        await ui_send_new_screen(update, context, placeholder_image(), caption, reply_markup=main_menu_kb())

async def screen_timeframes(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str):
    caption = f"Выбран {symbol}. Выберите таймфрейм:"
    edited = await ui_edit_screen(context, placeholder_image(), caption, reply_markup=timeframe_kb())
    if edited is None:
        await ui_send_new_screen(update, context, placeholder_image(), caption, reply_markup=timeframe_kb())

async def screen_alert_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str):
    caption = (
        f"🔔 Введите цену, при достижении которой уведомить.\n"
        f"Пример: 50000\n\n"
        f"Монета: {symbol}"
    )
    edited = await ui_edit_screen(context, placeholder_image(), caption, reply_markup=nav_full_kb())
    if edited is None:
        await ui_send_new_screen(update, context, placeholder_image(), caption, reply_markup=nav_full_kb())

async def screen_alerts_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = get_alerts_store(context.application)
    uid = update.effective_user.id
    user_alerts = store.get(uid, [])

    caption = format_alerts_text(user_alerts)
    if user_alerts:
        kb = build_alerts_keyboard(user_alerts)
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="BACK_LAST"),
             InlineKeyboardButton("🏠 Главное меню", callback_data="menu")]
        ])

    edited = await ui_edit_screen(context, placeholder_image(), caption, reply_markup=kb)
    if edited is None:
        await ui_send_new_screen(update, context, placeholder_image(), caption, reply_markup=kb)

async def screen_chart(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str, tf: str, menu_only: bool):
    price = await asyncio.to_thread(get_price, symbol)
    change24 = await asyncio.to_thread(get_24h_change, symbol)

    if tf == "1h":
        interval, limit = "1h", 80
    elif tf == "4h":
        interval, limit = "4h", 80
    else:
        interval, limit = "1d", 80
        tf = "1d"

    df = await asyncio.to_thread(get_candles, symbol, interval, limit)

    if price is None or df is None or change24 is None:
        await ui_edit_screen(context, placeholder_image(), "❌ Не удалось получить данные", reply_markup=nav_full_kb())
        return

    trend_text, trend_change = detect_trend(df)
    chart, rsi_value = await asyncio.to_thread(build_chart, df, symbol, tf)

    caption = (
        f"💰 {symbol}: ${price:.2f}\n"
        f"📈 24h: {change24:+.2f}%\n"
        f"📊 {trend_text}\n"
        f"📈 Движение: {trend_change:+.2f}%\n"
        f"{rsi_state(rsi_value)}\n"
        f"🤖 Сигнал: {rsi_signal(rsi_value)}\n"
        f"⏱ Таймфрейм: {tf} (МСК)\n"
        f"🔔 Нажми кнопку или введи цену после 🔔"
    )

    kb = menu_only_kb() if menu_only else nav_full_kb()

    edited = await ui_edit_screen(context, chart, caption, reply_markup=kb)
    if edited is None:
        await ui_send_new_screen(update, context, chart, caption, reply_markup=kb)

# ================= HANDLERS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # создаём UI экран (фото), потом редактируем
    await ui_send_new_screen(update, context, placeholder_image(), "Загрузка...", reply_markup=None)

    context.user_data["symbol"] = None
    context.user_data["last_tf"] = None
    context.user_data["awaiting_alert_price"] = False

    await screen_main_menu(update, context)

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await set_ui_ptr(update, context, query.message.message_id)

    data = query.data

    if data == "menu":
        context.user_data["awaiting_alert_price"] = False
        await screen_main_menu(update, context)
        return

    if data.startswith("COIN_"):
        symbol = data.replace("COIN_", "")
        context.user_data["symbol"] = symbol
        context.user_data["last_tf"] = None
        context.user_data["awaiting_alert_price"] = False
        await screen_timeframes(update, context, symbol)
        return

    if data.startswith("TF_"):
        tf = data.replace("TF_", "")
        symbol = context.user_data.get("symbol")
        if not symbol:
            await screen_main_menu(update, context)
            return
        context.user_data["last_tf"] = tf
        context.user_data["awaiting_alert_price"] = False
        await screen_chart(update, context, symbol, tf, menu_only=False)
        return

    if data == "BACK_LAST":
        symbol = context.user_data.get("symbol")
        last_tf = context.user_data.get("last_tf")
        if not symbol or not last_tf:
            await screen_main_menu(update, context)
            return
        await screen_chart(update, context, symbol, last_tf, menu_only=True)
        return

    if data == "ALERT_SET":
        symbol = context.user_data.get("symbol")
        if not symbol:
            await screen_main_menu(update, context)
            return
        context.user_data["awaiting_alert_price"] = True
        await screen_alert_prompt(update, context, symbol)
        return

    if data == "ALERT_LIST":
        context.user_data["awaiting_alert_price"] = False
        await screen_alerts_list(update, context)
        return

    if data.startswith("DEL_"):
        try:
            aid = int(data.replace("DEL_", ""))
        except:
            return

        store = get_alerts_store(context.application)
        uid = update.effective_user.id
        user_alerts = store.get(uid, [])
        store[uid] = [a for a in user_alerts if a["id"] != aid]

        await screen_alerts_list(update, context)
        return

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text.strip()
    symbol = context.user_data.get("symbol")

    # ввод цены для уведомления
    if context.user_data.get("awaiting_alert_price") and symbol:
        try:
            target = float(text.replace(",", "."))
        except:
            await ui_edit_screen(context, placeholder_image(), "❌ Введите число. Пример: 50000", reply_markup=nav_full_kb())
            return

        context.user_data["awaiting_alert_price"] = False
        ok, msg = await create_alert_for_user(context.application, update.effective_user.id, symbol, target)
        await ui_edit_screen(context, placeholder_image(), msg, reply_markup=nav_full_kb())
        return

    # ввод символа монеты
    entered = text.upper()
    context.user_data["symbol"] = entered
    context.user_data["last_tf"] = None
    context.user_data["awaiting_alert_price"] = False
    await screen_timeframes(update, context, entered)

# ================= ERROR HANDLER =================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # чтобы не было "No error handlers are registered"
    try:
        print("❌ Ошибка:", context.error)
    except:
        pass

# ================= RUN =================
def main():
    if not TOKEN or "ВАШ_TELEGRAM_TOKEN" in TOKEN:
        print("❌ Вставь реальный токен в переменную TOKEN (из @BotFather).")
        return

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.add_error_handler(error_handler)

    print("✅ Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()

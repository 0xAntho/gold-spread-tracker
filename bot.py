"""
bot.py
~~~~~~
Main entry point. The only file that talks to Telegram.
Aggregates alerts from variational_monitor and extended_monitor,
handles all commands, and runs the scheduler.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import variational_monitor
import extended_monitor

# ─── CONFIG ────────────────────────────────────────────────────────────────────

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID        = os.environ["CHAT_ID"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
PNL_THRESHOLD  = float(os.getenv("PNL_THRESHOLD", "10.0"))
PNL_MIN_HOURS  = float(os.getenv("PNL_MIN_HOURS", "24.0"))

# ─── LOGGING ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─── TRADE DATA MODEL ──────────────────────────────────────────────────────────

@dataclass
class Trade:
    id: int
    platform: str          # "variational" or "extended"
    long_ticker: str
    short_ticker: str
    entry_long: float
    entry_short: float
    size: float
    opened_at: datetime
    pnl_alerted: bool = False

    funding_long_accumulated: float = 0.0
    funding_short_accumulated: float = 0.0
    last_funding_update: Optional[datetime] = None

    @property
    def age_hours(self) -> float:
        return (datetime.now(timezone.utc) - self.opened_at).total_seconds() / 3600

    def price_pnl(self, mark_long: float, mark_short: float) -> float:
        return self.size * (
            (mark_long - self.entry_long) + (self.entry_short - mark_short)
        )

    @property
    def funding_pnl(self) -> float:
        return self.funding_long_accumulated + self.funding_short_accumulated

    def total_pnl(self, mark_long: float, mark_short: float) -> float:
        return self.price_pnl(mark_long, mark_short) + self.funding_pnl

    def accrue_funding(
        self,
        funding_rate_long: float,
        funding_rate_short: float,
        mark_long: float,
        mark_short: float,
        funding_interval_s: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        if self.last_funding_update is None:
            self.last_funding_update = now
            return

        elapsed_s = (now - self.last_funding_update).total_seconds()
        self.last_funding_update = now
        if elapsed_s <= 0:
            return

        notional_long = self.size * mark_long
        rate_per_s_long = funding_rate_long / funding_interval_s
        self.funding_long_accumulated += -rate_per_s_long * notional_long * elapsed_s

        notional_short = self.size * mark_short
        rate_per_s_short = funding_rate_short / funding_interval_s
        self.funding_short_accumulated += rate_per_s_short * notional_short * elapsed_s


# ─── GLOBAL STATE ──────────────────────────────────────────────────────────────

_trades: Dict[int, Trade] = {}
_next_trade_id: int = 1

# ConversationHandler states
(ASK_PLATFORM, ASK_LONG, ASK_SIZE, ASK_ENTRY_LONG, ASK_ENTRY_SHORT) = range(5)

# ─── ASYNC BRIDGE (APScheduler → async Telegram) ───────────────────────────────

def send_message_sync(loop: asyncio.AbstractEventLoop, bot, chat_id: str, text: str) -> None:
    future = asyncio.run_coroutine_threadsafe(
        bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown"),
        loop,
    )
    try:
        future.result(timeout=10)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# ─── SCHEDULER JOB ─────────────────────────────────────────────────────────────

def _get_marks_for_trade(trade: Trade) -> Optional[tuple[float, float, float, float, int]]:
    """
    Return (mark_long, mark_short, rate_long, rate_short, funding_interval_s)
    for a given trade, fetching from the right platform.
    Returns None on API error.
    """
    try:
        if trade.platform == "variational":
            data = variational_monitor.fetch_gold_listings()
            mark_long  = data[trade.long_ticker]["mark_price"]
            mark_short = data[trade.short_ticker]["mark_price"]
            rate_long  = data[trade.long_ticker]["funding_rate"]
            rate_short = data[trade.short_ticker]["funding_rate"]
            interval_s = data[trade.long_ticker]["funding_interval_s"]
        else:  # extended
            data = extended_monitor.fetch_oil_listings()
            mark_long  = data[trade.long_ticker]["mark_price"]
            mark_short = data[trade.short_ticker]["mark_price"]
            rate_long  = data[trade.long_ticker]["funding_rate"]
            rate_short = data[trade.short_ticker]["funding_rate"]
            interval_s = extended_monitor.FUNDING_INTERVAL_S
        return mark_long, mark_short, rate_long, rate_short, interval_s
    except Exception as e:
        log.error(f"Error fetching marks for trade #{trade.id}: {e}")
        return None


def check_and_notify(app: Application, loop: asyncio.AbstractEventLoop) -> None:
    # ── Collect alerts from all monitors ──────────────────────────────────────
    all_alerts = variational_monitor.check() + extended_monitor.check()

    for alert in all_alerts:
        send_message_sync(loop, app.bot, CHAT_ID, alert.message)

    # ── Funding accrual + PnL alerts per trade ────────────────────────────────
    for trade in list(_trades.values()):
        marks = _get_marks_for_trade(trade)
        if marks is None:
            continue

        mark_long, mark_short, rate_long, rate_short, interval_s = marks

        trade.accrue_funding(
            funding_rate_long=rate_long,
            funding_rate_short=rate_short,
            mark_long=mark_long,
            mark_short=mark_short,
            funding_interval_s=interval_s,
        )

        price_pnl = trade.price_pnl(mark_long, mark_short)
        total_pnl = trade.total_pnl(mark_long, mark_short)
        age_h     = trade.age_hours

        log.info(
            f"Trade #{trade.id} [{trade.platform}] | "
            f"Long {trade.long_ticker} / Short {trade.short_ticker} | "
            f"PricePnL={price_pnl:.2f}$ | Funding={trade.funding_pnl:.2f}$ | "
            f"Total={total_pnl:.2f}$ | Age={age_h:.1f}h"
        )

        if trade.pnl_alerted or total_pnl < PNL_THRESHOLD or age_h < PNL_MIN_HOURS:
            continue

        long_name  = trade.long_ticker.split("-")[0] if "-" in trade.long_ticker else trade.long_ticker
        short_name = trade.short_ticker.split("-")[0] if "-" in trade.short_ticker else trade.short_ticker
        f_long_sign  = "+" if trade.funding_long_accumulated >= 0 else ""
        f_short_sign = "+" if trade.funding_short_accumulated >= 0 else ""

        msg = (
            f"🟢 *PnL ALERT — Trade #{trade.id}* 🟢\n\n"
            f"Platform : *{trade.platform.capitalize()}*\n"
            f"Size : *{trade.size}* | Duration : *{age_h:.1f}h*\n\n"
            f"┌ *Long* {long_name}\n"
            f"│  Entry : {trade.entry_long:.3f} $  →  Mark : {mark_long:.3f} $\n"
            f"│  Price gain : {(mark_long - trade.entry_long) * trade.size:+.2f} $\n"
            f"│  Funding    : {f_long_sign}{trade.funding_long_accumulated:.2f} $\n"
            f"│\n"
            f"├ *Short* {short_name}\n"
            f"│  Entry : {trade.entry_short:.3f} $  →  Mark : {mark_short:.3f} $\n"
            f"│  Price gain : {(trade.entry_short - mark_short) * trade.size:+.2f} $\n"
            f"│  Funding    : {f_short_sign}{trade.funding_short_accumulated:.2f} $\n"
            f"│\n"
            f"├ Price PnL  : *{price_pnl:+.2f} $*\n"
            f"├ Funding    : *{trade.funding_pnl:+.2f} $*\n"
            f"└ *Total PnL : {total_pnl:+.2f} $* 💰\n\n"
            f"🕐 Opened {trade.opened_at.strftime('%d/%m %H:%M')} UTC"
        )
        send_message_sync(loop, app.bot, CHAT_ID, msg)
        trade.pnl_alerted = True
        log.info(f"✅ PnL alert sent for trade #{trade.id}.")

# ─── COMMAND: /newtrade ────────────────────────────────────────────────────────

async def cmd_newtrade_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text(
        "📝 *New trade — step 1 of 5*\n\n"
        "Which platform?\n"
        "Reply `gold` (Variational — XAUT/PAXG) or `oil` (Extended — WTI/XBR).",
        parse_mode="Markdown",
    )
    return ASK_PLATFORM


async def cmd_newtrade_platform(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    answer = update.message.text.strip().lower()
    if answer not in ("gold", "oil"):
        await update.message.reply_text("❌ Reply `gold` or `oil`.", parse_mode="Markdown")
        return ASK_PLATFORM

    ctx.user_data["platform"] = "variational" if answer == "gold" else "extended"

    if answer == "gold":
        ctx.user_data["tickers"] = ("XAUT", "PAXG")
        ticker_a, ticker_b = "XAUT", "PAXG"
    else:
        ticker_a = extended_monitor.MARKET_A.split("-")[0]
        ticker_b = extended_monitor.MARKET_B.split("-")[0]
        ctx.user_data["tickers"] = (
            extended_monitor.MARKET_A,
            extended_monitor.MARKET_B,
        )

    await update.message.reply_text(
        f"✅ Platform : *{answer.upper()}*\n\n"
        f"📝 *Step 2/5* — Which instrument is *LONG*?\n"
        f"Reply `{ticker_a}` or `{ticker_b}`.",
        parse_mode="Markdown",
    )
    return ASK_LONG


async def cmd_newtrade_long(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    tickers = ctx.user_data["tickers"]
    answer  = update.message.text.strip().upper()

    # Normalise: accept both "WTI" and "WTI-USD"
    if "-" not in answer and ctx.user_data["platform"] == "extended":
        answer = f"{answer}-USD"

    if answer not in tickers:
        t0 = tickers[0].split("-")[0]
        t1 = tickers[1].split("-")[0]
        await update.message.reply_text(f"❌ Reply `{t0}` or `{t1}`.", parse_mode="Markdown")
        return ASK_LONG

    ctx.user_data["long_ticker"]  = answer
    ctx.user_data["short_ticker"] = tickers[1] if answer == tickers[0] else tickers[0]
    long_name  = answer.split("-")[0]
    short_name = ctx.user_data["short_ticker"].split("-")[0]

    await update.message.reply_text(
        f"✅ Long : *{long_name}* | Short : *{short_name}*\n\n"
        f"📝 *Step 3/5* — How many units did you trade?\n"
        f"_(ex: `1`, `0.5`, `2.3`)_",
        parse_mode="Markdown",
    )
    return ASK_SIZE


async def cmd_newtrade_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        size = float(update.message.text.strip().replace(",", "."))
        assert size > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Enter a valid positive number.", parse_mode="Markdown")
        return ASK_SIZE

    ctx.user_data["size"] = size
    long_name = ctx.user_data["long_ticker"].split("-")[0]

    await update.message.reply_text(
        f"✅ Size : *{size}*\n\n"
        f"📝 *Step 4/5* — Entry price for the *LONG* leg ({long_name})?\n"
        f"_(ex: `3215.50`)_",
        parse_mode="Markdown",
    )
    return ASK_ENTRY_LONG


async def cmd_newtrade_entry_long(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text.strip().replace(",", "."))
        assert price > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Enter a valid price.", parse_mode="Markdown")
        return ASK_ENTRY_LONG

    ctx.user_data["entry_long"] = price
    short_name = ctx.user_data["short_ticker"].split("-")[0]

    await update.message.reply_text(
        f"✅ Long entry : *{price:.3f} $*\n\n"
        f"📝 *Step 5/5* — Entry price for the *SHORT* leg ({short_name})?\n"
        f"_(ex: `3210.00`)_",
        parse_mode="Markdown",
    )
    return ASK_ENTRY_SHORT


async def cmd_newtrade_entry_short(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    global _next_trade_id

    try:
        price = float(update.message.text.strip().replace(",", "."))
        assert price > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Enter a valid price.", parse_mode="Markdown")
        return ASK_ENTRY_SHORT

    trade = Trade(
        id           = _next_trade_id,
        platform     = ctx.user_data["platform"],
        long_ticker  = ctx.user_data["long_ticker"],
        short_ticker = ctx.user_data["short_ticker"],
        size         = ctx.user_data["size"],
        entry_long   = ctx.user_data["entry_long"],
        entry_short  = price,
        opened_at    = datetime.now(timezone.utc),
    )
    _trades[trade.id] = trade
    _next_trade_id += 1

    long_name  = trade.long_ticker.split("-")[0]
    short_name = trade.short_ticker.split("-")[0]
    spread_entry = trade.entry_long - trade.entry_short
    notional     = trade.size * ((trade.entry_long + price) / 2)

    await update.message.reply_text(
        f"✅ *Trade #{trade.id} saved!*\n\n"
        f"Platform : *{trade.platform.capitalize()}*\n"
        f"┌ Long  *{long_name}* @ {trade.entry_long:.3f} $\n"
        f"└ Short *{short_name}* @ {price:.3f} $\n\n"
        f"Size : *{trade.size}* (~{notional:,.0f} $ notional)\n"
        f"Entry spread : *{spread_entry:+.3f} $*\n\n"
        f"🔔 PnL alert if total ≥ +{PNL_THRESHOLD:.0f}$ after {PNL_MIN_HOURS:.0f}h\n"
        f"🕐 Opened {trade.opened_at.strftime('%d/%m/%Y at %H:%M')} UTC",
        parse_mode="Markdown",
    )
    ctx.user_data.clear()
    return ConversationHandler.END


async def cmd_newtrade_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("❌ Trade creation cancelled.")
    return ConversationHandler.END

# ─── COMMAND: /trades ──────────────────────────────────────────────────────────

async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _trades:
        await update.message.reply_text("📭 No open trades.")
        return

    lines = ["📋 *Open trades*\n"]
    for trade in _trades.values():
        marks = _get_marks_for_trade(trade)
        long_name  = trade.long_ticker.split("-")[0]
        short_name = trade.short_ticker.split("-")[0]

        if marks:
            mark_long, mark_short, *_ = marks
            price_pnl = trade.price_pnl(mark_long, mark_short)
            fund_pnl  = trade.funding_pnl
            total_pnl = price_pnl + fund_pnl
            emoji     = "🟢" if total_pnl >= 0 else "🔴"
            pnl_block = (
                f"   Price PnL : *{price_pnl:+.2f} $*\n"
                f"   Funding   : *{fund_pnl:+.2f} $*\n"
                f"   Total PnL : *{total_pnl:+.2f} $*\n"
            )
        else:
            emoji     = "⚪"
            pnl_block = "   PnL : N/A\n"

        lines.append(
            f"{emoji} *Trade #{trade.id}* [{trade.platform.capitalize()}] | "
            f"Long {long_name} / Short {short_name} | Size {trade.size}\n"
            f"   Entry : {trade.entry_long:.3f} / {trade.entry_short:.3f} $\n"
            + pnl_block +
            f"   Age : {trade.age_hours:.1f}h | Alert : {'✅' if trade.pnl_alerted else '🕐'}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─── COMMAND: /closetrade ──────────────────────────────────────────────────────

async def cmd_closetrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: `/closetrade <id>`", parse_mode="Markdown")
        return

    try:
        trade_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")
        return

    trade = _trades.pop(trade_id, None)
    if trade is None:
        await update.message.reply_text(f"❌ Trade #{trade_id} not found.")
        return

    long_name  = trade.long_ticker.split("-")[0]
    short_name = trade.short_ticker.split("-")[0]
    marks = _get_marks_for_trade(trade)

    if marks:
        mark_long, mark_short, *_ = marks
        price_pnl = trade.price_pnl(mark_long, mark_short)
        fund_pnl  = trade.funding_pnl
        total_pnl = price_pnl + fund_pnl
        emoji     = "🟢" if total_pnl >= 0 else "🔴"
        pnl_lines = (
            f"Price PnL : *{price_pnl:+.2f} $*\n"
            f"Funding   : *{fund_pnl:+.2f} $*\n"
            f"{emoji} *Total PnL : {total_pnl:+.2f} $*"
        )
    else:
        pnl_lines = "⚪ PnL : N/A"

    await update.message.reply_text(
        f"🗑 *Trade #{trade_id} closed* [{trade.platform.capitalize()}]\n\n"
        f"Long  {long_name} x{trade.size} @ {trade.entry_long:.3f} $\n"
        f"Short {short_name} x{trade.size} @ {trade.entry_short:.3f} $\n\n"
        + pnl_lines +
        f"\n⏱ Duration : {trade.age_hours:.1f}h",
        parse_mode="Markdown",
    )

# ─── COMMAND: /gold and /oil ───────────────────────────────────────────────────

async def cmd_gold(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(variational_monitor.price_message(), parse_mode="Markdown")


async def cmd_oil(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(extended_monitor.price_message(), parse_mode="Markdown")

# ─── COMMAND: /status and /start ───────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 *Multi-market bot active!*\n\n"
        f"Chat ID: `{chat_id}`\n\n"
        f"*Price commands:*\n"
        f"/gold — XAUT/PAXG prices (Variational)\n"
        f"/oil — WTI/XBR prices (Extended)\n\n"
        f"*Trade commands:*\n"
        f"/newtrade — Create a trade to monitor\n"
        f"/trades — List open trades\n"
        f"/closetrade <id> — Close a trade\n\n"
        f"*Other:*\n"
        f"/status — Bot status",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"✅ *Bot live*\n"
        f"Check every {CHECK_INTERVAL}s\n"
        f"Trades open : {len(_trades)}\n\n"
        f"*Variational thresholds:*\n"
        f"  Spread : {variational_monitor.SPREAD_THRESHOLD} $\n"
        f"  Funding : {variational_monitor.FUNDING_THRESHOLD} %\n\n"
        f"*Extended thresholds:*\n"
        f"  Spread : {extended_monitor.SPREAD_THRESHOLD} $\n"
        f"  Funding : {extended_monitor.FUNDING_THRESHOLD*100:.6f} %",
        parse_mode="Markdown",
    )

# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    newtrade_conv = ConversationHandler(
        entry_points=[CommandHandler("newtrade", cmd_newtrade_start)],
        states={
            ASK_PLATFORM:    [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_newtrade_platform)],
            ASK_LONG:        [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_newtrade_long)],
            ASK_SIZE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_newtrade_size)],
            ASK_ENTRY_LONG:  [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_newtrade_entry_long)],
            ASK_ENTRY_SHORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_newtrade_entry_short)],
        },
        fallbacks=[CommandHandler("cancel", cmd_newtrade_cancel)],
    )

    app.add_handler(newtrade_conv)
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("gold",       cmd_gold))
    app.add_handler(CommandHandler("oil",        cmd_oil))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("trades",     cmd_trades))
    app.add_handler(CommandHandler("closetrade", cmd_closetrade))

    loop = asyncio.get_event_loop()

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=lambda: check_and_notify(app, loop),
        trigger="interval",
        seconds=CHECK_INTERVAL,
        id="multi_market_check",
    )
    scheduler.start()
    log.info(f"🚀 Bot started — checking every {CHECK_INTERVAL}s")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
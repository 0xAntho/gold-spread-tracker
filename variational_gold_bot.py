import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

import requests
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

# ─── CONFIG ────────────────────────────────────────────────────────────────────

load_dotenv()

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
CHAT_ID           = os.environ["CHAT_ID"]
SPREAD_THRESHOLD  = float(os.getenv("SPREAD_THRESHOLD", "10.0"))
FUNDING_THRESHOLD = float(os.getenv("FUNDING_THRESHOLD", "0.5"))
CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL", "60"))
PNL_THRESHOLD     = float(os.getenv("PNL_THRESHOLD", "10.0"))
PNL_MIN_HOURS     = float(os.getenv("PNL_MIN_HOURS", "24.0"))

BASE_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io"

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
    long_ticker: str
    short_ticker: str
    entry_long: float
    entry_short: float
    size: float
    opened_at: datetime
    pnl_alerted: bool = False

    # Accumulated funding (in $), updated at each check_and_notify tick.
    # Positive = received, Negative = paid.
    # Convention: long position pays funding when rate > 0, receives when rate < 0.
    #             short position is the opposite.
    funding_long_accumulated: float = 0.0
    funding_short_accumulated: float = 0.0

    last_funding_update: Optional[datetime] = None

    @property
    def age_hours(self) -> float:
        return (datetime.now(timezone.utc) - self.opened_at).total_seconds() / 3600

    def price_pnl(self, mark_long: float, mark_short: float) -> float:
        """Unrealised price PnL (excluding funding), scaled by size."""
        return self.size * (
            (mark_long - self.entry_long) + (self.entry_short - mark_short)
        )

    @property
    def funding_pnl(self) -> float:
        """Total net funding received/paid so far (positive = net received)."""
        return self.funding_long_accumulated + self.funding_short_accumulated

    def total_pnl(self, mark_long: float, mark_short: float) -> float:
        return self.price_pnl(mark_long, mark_short) + self.funding_pnl

    def accrue_funding(
        self,
        funding_rate_long: float,   # % per funding_interval (e.g. 0.01 for 0.01%)
        funding_rate_short: float,
        mark_long: float,
        mark_short: float,
        funding_interval_long_s: int,
        funding_interval_short_s: int,
    ) -> None:
        """
        Accumulate funding for the elapsed time since the last call.

        Variational funding_rate is expressed as a percentage per funding_interval_s.
        The payment for one tick is:  rate/100 * notional
        where notional = size * mark_price.

        We pro-rate by elapsed_seconds / funding_interval_s to handle
        checks that don't align with funding intervals.

        Sign convention (mirrors perpetuals):
          - Long  pays +rate * notional  (negative for the holder when rate > 0)
          - Short receives +rate * notional (positive for the holder when rate > 0)
        """
        now = datetime.now(timezone.utc)

        if self.last_funding_update is None:
            self.last_funding_update = now
            return

        elapsed_s = (now - self.last_funding_update).total_seconds()
        self.last_funding_update = now

        if elapsed_s <= 0:
            return

        # Long leg: longs pay when rate > 0, receive when rate < 0
        notional_long = self.size * mark_long
        rate_per_second_long = (funding_rate_long / 100) / funding_interval_long_s
        funding_long_tick = -rate_per_second_long * notional_long * elapsed_s
        self.funding_long_accumulated += funding_long_tick

        # Short leg: shorts receive when rate > 0, pay when rate < 0
        notional_short = self.size * mark_short
        rate_per_second_short = (funding_rate_short / 100) / funding_interval_short_s
        funding_short_tick = +rate_per_second_short * notional_short * elapsed_s
        self.funding_short_accumulated += funding_short_tick


# ─── GLOBAL STATE ──────────────────────────────────────────────────────────────

_trades: Dict[int, Trade] = {}
_next_trade_id: int = 1

_alert_sent_spread  = False
_alert_sent_funding = False

(
    ASK_LONG,
    ASK_SIZE,
    ASK_ENTRY_LONG,
    ASK_ENTRY_SHORT,
) = range(4)

# ─── API ───────────────────────────────────────────────────────────────────────

def fetch_gold_listings() -> dict:
    """Fetch XAUT and PAXG data from the Variational API."""
    resp = requests.get(f"{BASE_URL}/metadata/stats", timeout=10)
    resp.raise_for_status()
    data = resp.json()

    result = {}
    for listing in data.get("listings", []):
        ticker = listing["ticker"].upper()
        if ticker in ("XAUT", "PAXG"):
            result[ticker] = {
                "mark_price":         float(listing["mark_price"]),
                "funding_rate":       float(listing["funding_rate"]),
                "funding_interval_s": listing.get("funding_interval_s", 28800),
                "volume_24h":         float(listing["volume_24h"]),
            }
    return result

# ─── ASYNC BRIDGE ──────────────────────────────────────────────────────────────

def send_message_sync(loop: asyncio.AbstractEventLoop, bot, chat_id: str, text: str) -> None:
    future = asyncio.run_coroutine_threadsafe(
        bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown"),
        loop,
    )
    try:
        future.result(timeout=10)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# ─── MONITORING LOGIC ──────────────────────────────────────────────────────────

def check_and_notify(app: Application, loop: asyncio.AbstractEventLoop) -> None:
    global _alert_sent_spread, _alert_sent_funding

    try:
        data = fetch_gold_listings()
    except Exception as e:
        log.error(f"API error: {e}")
        return

    if "XAUT" not in data or "PAXG" not in data:
        log.warning("XAUT or PAXG not found in API response.")
        return

    xaut = data["XAUT"]
    paxg = data["PAXG"]

    spread_price   = abs(xaut["mark_price"] - paxg["mark_price"])
    spread_funding = abs(xaut["funding_rate"] - paxg["funding_rate"])

    log.info(
        f"XAUT={xaut['mark_price']:.2f} | PAXG={paxg['mark_price']:.2f} | "
        f"Spread={spread_price:.2f}$ | ΔFunding={spread_funding*100:.4f}%"
    )

    # ── Price spread alert ─────────────────────────────────────────────────────
    if spread_price >= SPREAD_THRESHOLD:
        if not _alert_sent_spread:
            direction = "XAUT > PAXG" if xaut["mark_price"] > paxg["mark_price"] else "PAXG > XAUT"
            msg = (
                f"🚨 *GOLD SPREAD ALERT* 🚨\n\n"
                f"📈 Price gap : *{spread_price:.2f} $* ({direction})\n\n"
                f"┌ *XAUT* : {xaut['mark_price']:.2f} $\n"
                f"│  Funding : {xaut['funding_rate']*100:.4f}% / {xaut['funding_interval_s']//3600}h\n"
                f"│\n"
                f"└ *PAXG* : {paxg['mark_price']:.2f} $\n"
                f"   Funding : {paxg['funding_rate']*100:.4f}% / {paxg['funding_interval_s']//3600}h\n\n"
                f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
            )
            send_message_sync(loop, app.bot, CHAT_ID, msg)
            _alert_sent_spread = True
            log.info("✅ Spread alert sent.")
    else:
        _alert_sent_spread = False

    # ── Funding rate alert ─────────────────────────────────────────────────────
    if spread_funding * 100 >= FUNDING_THRESHOLD:
        if not _alert_sent_funding:
            msg = (
                f"⚡ *GOLD FUNDING ALERT* ⚡\n\n"
                f"📊 Funding gap : *{spread_funding:.4f}%*\n\n"
                f"┌ *XAUT* : {xaut['funding_rate']*100:.4f}% / {xaut['funding_interval_s']//3600}h\n"
                f"└ *PAXG* : {paxg['funding_rate']*100:.4f}% / {paxg['funding_interval_s']//3600}h\n\n"
                f"💡 Potential carry opportunity!\n"
                f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
            )
            send_message_sync(loop, app.bot, CHAT_ID, msg)
            _alert_sent_funding = True
            log.info("✅ Funding alert sent.")
    else:
        _alert_sent_funding = False

    # ── Funding accrual + PnL alerts ──────────────────────────────────────────
    rates = {
        "XAUT": (xaut["funding_rate"], xaut["funding_interval_s"], xaut["mark_price"]),
        "PAXG": (paxg["funding_rate"], paxg["funding_interval_s"], paxg["mark_price"]),
    }

    for trade in list(_trades.values()):
        long_rate,  long_interval_s,  mark_long  = rates[trade.long_ticker]
        short_rate, short_interval_s, mark_short = rates[trade.short_ticker]

        # Accrue funding for this tick
        trade.accrue_funding(
            funding_rate_long=long_rate,
            funding_rate_short=short_rate,
            mark_long=mark_long,
            mark_short=mark_short,
            funding_interval_long_s=long_interval_s,
            funding_interval_short_s=short_interval_s,
        )

        price_pnl = trade.price_pnl(mark_long, mark_short)
        total_pnl = trade.total_pnl(mark_long, mark_short)
        age_h     = trade.age_hours

        log.info(
            f"Trade #{trade.id} | Long {trade.long_ticker} x{trade.size} | "
            f"Short {trade.short_ticker} x{trade.size} | "
            f"PricePnL={price_pnl:.2f}$ | Funding={trade.funding_pnl:.2f}$ | "
            f"Total={total_pnl:.2f}$ | Age={age_h:.1f}h"
        )

        if trade.pnl_alerted:
            continue

        if total_pnl >= PNL_THRESHOLD and age_h >= PNL_MIN_HOURS:
            pnl_emoji = "🟢"
            f_long_sign  = "+" if trade.funding_long_accumulated >= 0 else ""
            f_short_sign = "+" if trade.funding_short_accumulated >= 0 else ""
            msg = (
                f"{pnl_emoji} *PnL ALERT — Trade #{trade.id}* {pnl_emoji}\n\n"
                f"Size : *{trade.size} token(s)*\n"
                f"Duration : *{age_h:.1f}h*\n\n"
                f"┌ *Long* {trade.long_ticker}\n"
                f"│  Entry : {trade.entry_long:.2f} $\n"
                f"│  Mark  : {mark_long:.2f} $\n"
                f"│  Price gain : {(mark_long - trade.entry_long) * trade.size:+.2f} $\n"
                f"│  Funding    : {f_long_sign}{trade.funding_long_accumulated:.2f} $\n"
                f"│\n"
                f"├ *Short* {trade.short_ticker}\n"
                f"│  Entry : {trade.entry_short:.2f} $\n"
                f"│  Mark  : {mark_short:.2f} $\n"
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
        "📝 *New trade — step 1 of 4*\n\n"
        "Which instrument is in a *LONG* position?\n"
        "Reply `XAUT` or `PAXG`.",
        parse_mode="Markdown",
    )
    return ASK_LONG


async def cmd_newtrade_long(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    answer = update.message.text.strip().upper()
    if answer not in ("XAUT", "PAXG"):
        await update.message.reply_text("❌ Answer `XAUT` or `PAXG`.", parse_mode="Markdown")
        return ASK_LONG

    ctx.user_data["long_ticker"]  = answer
    ctx.user_data["short_ticker"] = "PAXG" if answer == "XAUT" else "XAUT"

    await update.message.reply_text(
        f"✅ Long : *{answer}* | Short : *{ctx.user_data['short_ticker']}*\n\n"
        f"📝 *Step 2/4* — How many tokens did you trade?\n"
        f"_(ex: `1`, `0.5`, `2.3`)_",
        parse_mode="Markdown",
    )
    return ASK_SIZE


async def cmd_newtrade_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        size = float(update.message.text.strip().replace(",", "."))
        assert size > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Enter a valid positive number (ex: `0.5`).", parse_mode="Markdown")
        return ASK_SIZE

    ctx.user_data["size"] = size
    long = ctx.user_data["long_ticker"]

    await update.message.reply_text(
        f"✅ Size : *{size} token(s)*\n\n"
        f"📝 *Step 3/4* — Entry price for the *LONG* leg ({long})?\n"
        f"_(ex: `3215.50`)_",
        parse_mode="Markdown",
    )
    return ASK_ENTRY_LONG


async def cmd_newtrade_entry_long(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text.strip().replace(",", "."))
        assert price > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Enter a valid price (ex: `3215.50`).", parse_mode="Markdown")
        return ASK_ENTRY_LONG

    ctx.user_data["entry_long"] = price
    short = ctx.user_data["short_ticker"]

    await update.message.reply_text(
        f"✅ Long entry : *{price:.2f} $*\n\n"
        f"📝 *Step 4/4* — Entry price for the *SHORT* leg ({short})?\n"
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
        await update.message.reply_text("❌ Enter a valid price (ex: `3210.00`).", parse_mode="Markdown")
        return ASK_ENTRY_SHORT

    ctx.user_data["entry_short"] = price

    trade = Trade(
        id           = _next_trade_id,
        long_ticker  = ctx.user_data["long_ticker"],
        short_ticker = ctx.user_data["short_ticker"],
        size         = ctx.user_data["size"],
        entry_long   = ctx.user_data["entry_long"],
        entry_short  = ctx.user_data["entry_short"],
        opened_at    = datetime.now(timezone.utc),
    )
    _trades[trade.id] = trade
    _next_trade_id += 1

    spread_entry = trade.entry_long - trade.entry_short
    notional     = trade.size * ((trade.entry_long + trade.entry_short) / 2)

    await update.message.reply_text(
        f"✅ *Trade #{trade.id} saved!*\n\n"
        f"┌ Long  *{trade.long_ticker}* @ {trade.entry_long:.2f} $\n"
        f"└ Short *{trade.short_ticker}* @ {trade.entry_short:.2f} $\n\n"
        f"Size : *{trade.size} token(s)* (~{notional:,.0f} $ notional)\n"
        f"Entry spread : *{spread_entry:+.2f} $*\n\n"
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

    try:
        data = fetch_gold_listings()
        market = {k: v for k, v in data.items()}
    except Exception:
        market = {}

    lines = ["📋 *Open trades*\n"]
    for trade in _trades.values():
        long_data  = market.get(trade.long_ticker)
        short_data = market.get(trade.short_ticker)

        if long_data and short_data:
            mark_long  = long_data["mark_price"]
            mark_short = short_data["mark_price"]
            price_pnl  = trade.price_pnl(mark_long, mark_short)
            fund_pnl   = trade.funding_pnl
            total_pnl  = price_pnl + fund_pnl
            emoji      = "🟢" if total_pnl >= 0 else "🔴"
            pnl_block  = (
                f"   Price PnL : *{price_pnl:+.2f} $*\n"
                f"   Funding   : *{fund_pnl:+.2f} $*\n"
                f"   Total PnL : *{total_pnl:+.2f} $*\n"
            )
        else:
            emoji     = "⚪"
            pnl_block = "   PnL : N/A\n"

        age_h = trade.age_hours
        lines.append(
            f"{emoji} *Trade #{trade.id}* | "
            f"Long {trade.long_ticker} / Short {trade.short_ticker} | "
            f"Size {trade.size}\n"
            f"   Entry : {trade.entry_long:.2f} / {trade.entry_short:.2f} $\n"
            + pnl_block +
            f"   Age : {age_h:.1f}h | Alert : {'✅' if trade.pnl_alerted else '🕐'}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─── COMMAND: /closetrade <id> ─────────────────────────────────────────────────

async def cmd_closetrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "Usage: `/closetrade <id>`\nEx: `/closetrade 1`",
            parse_mode="Markdown",
        )
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

    try:
        data       = fetch_gold_listings()
        mark_long  = data[trade.long_ticker]["mark_price"]
        mark_short = data[trade.short_ticker]["mark_price"]
        price_pnl  = trade.price_pnl(mark_long, mark_short)
        fund_pnl   = trade.funding_pnl
        total_pnl  = price_pnl + fund_pnl
        emoji      = "🟢" if total_pnl >= 0 else "🔴"
        pnl_lines  = (
            f"Price PnL : *{price_pnl:+.2f} $*\n"
            f"Funding   : *{fund_pnl:+.2f} $*\n"
            f"{emoji} *Total PnL : {total_pnl:+.2f} $*"
        )
    except Exception:
        pnl_lines = "⚪ PnL : N/A"

    await update.message.reply_text(
        f"🗑 *Trade #{trade_id} closed*\n\n"
        f"Long  {trade.long_ticker} x{trade.size} @ {trade.entry_long:.2f} $\n"
        f"Short {trade.short_ticker} x{trade.size} @ {trade.entry_short:.2f} $\n\n"
        + pnl_lines +
        f"\n⏱ Duration : {trade.age_hours:.1f}h",
        parse_mode="Markdown",
    )

# ─── EXISTING COMMANDS ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 XAUT/PAXG bot active!\n\n"
        f"Chat ID: `{chat_id}`\n\n"
        f"Commands:\n"
        f"/price — Current price + spread\n"
        f"/threshold — View configured thresholds\n"
        f"/status — Bot status\n"
        f"/newtrade — Create a trade to monitor\n"
        f"/trades — List open trades\n"
        f"/closetrade <id> — Close a trade",
        parse_mode="Markdown",
    )


async def cmd_prix(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = fetch_gold_listings()
    except Exception as e:
        await update.message.reply_text(f"❌ API error: {e}")
        return

    if "XAUT" not in data or "PAXG" not in data:
        await update.message.reply_text("⚠️ XAUT or PAXG not found in the API.")
        return

    xaut = data["XAUT"]
    paxg = data["PAXG"]
    spread = abs(xaut["mark_price"] - paxg["mark_price"])
    spread_funding = abs(xaut["funding_rate"] - paxg["funding_rate"])

    emoji_spread  = "🔴" if spread >= SPREAD_THRESHOLD else "🟢"
    emoji_funding = "🔴" if spread_funding * 100 >= FUNDING_THRESHOLD else "🟢"

    msg = (
        f"💰 *Gold price — Variational*\n\n"
        f"*XAUT*\n"
        f"  Mark : {xaut['mark_price']:.2f} $\n"
        f"  Funding : {xaut['funding_rate']*100:.4f}% / {xaut['funding_interval_s']//3600}h\n"
        f"  Vol 24h : {xaut['volume_24h']:,.0f} $\n\n"
        f"*PAXG*\n"
        f"  Mark : {paxg['mark_price']:.2f} $\n"
        f"  Funding : {paxg['funding_rate']*100:.4f}% / {paxg['funding_interval_s']//3600}h\n"
        f"  Vol 24h : {paxg['volume_24h']:,.0f} $\n\n"
        f"{emoji_spread} Spread : *{spread:.2f} $*\n"
        f"{emoji_funding} Δ Funding : *{spread_funding:.4f}%*\n\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_seuil(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"⚙️ *Configured thresholds*\n\n"
        f"• Spread : *{SPREAD_THRESHOLD} $*\n"
        f"• Funding spread : *{FUNDING_THRESHOLD} %*\n"
        f"• PnL alert : *+{PNL_THRESHOLD} $* after *{PNL_MIN_HOURS:.0f}h*\n"
        f"• Check interval : *{CHECK_INTERVAL}s*",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"✅ Bot live\n"
        f"Check every {CHECK_INTERVAL}s\n"
        f"Spread alert sent : {'Yes' if _alert_sent_spread else 'No'}\n"
        f"Funding alert sent : {'Yes' if _alert_sent_funding else 'No'}\n"
        f"Trades open : {len(_trades)}"
    )

# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    newtrade_conv = ConversationHandler(
        entry_points=[CommandHandler("newtrade", cmd_newtrade_start)],
        states={
            ASK_LONG:        [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_newtrade_long)],
            ASK_SIZE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_newtrade_size)],
            ASK_ENTRY_LONG:  [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_newtrade_entry_long)],
            ASK_ENTRY_SHORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_newtrade_entry_short)],
        },
        fallbacks=[CommandHandler("cancel", cmd_newtrade_cancel)],
    )

    app.add_handler(newtrade_conv)
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("price",      cmd_prix))
    app.add_handler(CommandHandler("threshold",  cmd_seuil))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("trades",     cmd_trades))
    app.add_handler(CommandHandler("closetrade", cmd_closetrade))

    loop = asyncio.get_event_loop()

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=lambda: check_and_notify(app, loop),
        trigger="interval",
        seconds=CHECK_INTERVAL,
        id="gold_spread_check",
    )
    scheduler.start()
    log.info(f"🚀 Bot started — checking every {CHECK_INTERVAL}s")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
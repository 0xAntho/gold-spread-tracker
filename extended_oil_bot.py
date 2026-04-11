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
SPREAD_THRESHOLD  = float(os.getenv("SPREAD_THRESHOLD", "0.50"))   # $ spread entre WTI et XBR
FUNDING_THRESHOLD = float(os.getenv("FUNDING_THRESHOLD", "0.00005")) # décimal brut (ex: 0.00005 = 0.005%)
CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL", "60"))
PNL_THRESHOLD     = float(os.getenv("PNL_THRESHOLD", "10.0"))
PNL_MIN_HOURS     = float(os.getenv("PNL_MIN_HOURS", "24.0"))

# Extended mainnet API — pas d'authentification requise pour les données publiques
BASE_URL = "https://api.starknet.extended.exchange/api/v1"

# Noms de marché sur Extended (format {ASSET}-USD)
# ⚠️  XBR-USD = Brent Crude Oil (confirmé dans la doc Extended).
#     WTI-USD = WTI Crude Oil (à confirmer sur https://app.extended.exchange).
#     Possibilité que WTI soit listé sous un autre nom — ajuster MARKET_A si besoin.
MARKET_A = os.getenv("MARKET_A", "WTI-USD")
MARKET_B = os.getenv("MARKET_B", "XBR-USD")

# Interval de funding sur Extended TradFi : 1 heure (fixe, non retourné par l'API)
FUNDING_INTERVAL_S = 3600

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
    # Convention: long pays when rate > 0, receives when rate < 0.
    #             short is the opposite.
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
        funding_rate_long: float,   # décimal brut, ex: -0.000059 (= -0.0059% / 1h)
        funding_rate_short: float,
        mark_long: float,
        mark_short: float,
    ) -> None:
        """
        Accumule le funding pour le temps écoulé depuis le dernier appel.

        Extended funding_rate est exprimé en décimal par heure (funding_interval = 1h).
        Ex: 0.000059 signifie 0.0059% par heure.
        Le paiement pour un tick complet est : rate * notional
        On pro-rate par elapsed_seconds / 3600.

        Sign convention :
          - Long  paie +rate * notional  (négatif pour le holder quand rate > 0)
          - Short reçoit +rate * notional (positif pour le holder quand rate > 0)
        """
        now = datetime.now(timezone.utc)

        if self.last_funding_update is None:
            self.last_funding_update = now
            return

        elapsed_s = (now - self.last_funding_update).total_seconds()
        self.last_funding_update = now

        if elapsed_s <= 0:
            return

        # Long leg : longs paient quand rate > 0, reçoivent quand rate < 0
        notional_long = self.size * mark_long
        rate_per_second_long = funding_rate_long / FUNDING_INTERVAL_S
        funding_long_tick = -rate_per_second_long * notional_long * elapsed_s
        self.funding_long_accumulated += funding_long_tick

        # Short leg : shorts reçoivent quand rate > 0, paient quand rate < 0
        notional_short = self.size * mark_short
        rate_per_second_short = funding_rate_short / FUNDING_INTERVAL_S
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

def fetch_market_stats(market: str) -> dict:
    """Fetch stats for a single market from the Extended API.

    Returns dict with keys: mark_price, index_price, funding_rate, volume_24h.

    Notes:
        - funding_rate is a raw decimal (e.g. -0.000059 = -0.0059% per hour).
          Do NOT multiply by 100 when storing; only multiply for display.
        - funding_interval is fixed at 3600s (1h) for Extended TradFi markets.
        - No API key required for public market data endpoints.
        - User-Agent header is mandatory per the Extended API docs.
    """
    url = f"{BASE_URL}/info/markets/{market}/stats"
    resp = requests.get(url, timeout=10, headers={"User-Agent": "extended-oil-bot/1.0"})
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "OK":
        raise ValueError(f"API error for {market}: {data}")

    d = data["data"]
    return {
        "mark_price":   float(d["markPrice"]),
        "index_price":  float(d["indexPrice"]),
        "funding_rate": float(d["fundingRate"]),  # décimal brut, ex: -0.000059
        "volume_24h":   float(d["dailyVolume"]),
    }


def fetch_oil_listings() -> dict:
    """Fetch MARKET_A and MARKET_B data from the Extended API."""
    result = {}
    for ticker in (MARKET_A, MARKET_B):
        result[ticker] = fetch_market_stats(ticker)
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
        data = fetch_oil_listings()
    except Exception as e:
        log.error(f"API error: {e}")
        return

    if MARKET_A not in data or MARKET_B not in data:
        log.warning(f"{MARKET_A} or {MARKET_B} not found in API response.")
        return

    mkt_a = data[MARKET_A]
    mkt_b = data[MARKET_B]
    ticker_a = MARKET_A.split("-")[0]
    ticker_b = MARKET_B.split("-")[0]

    spread_price   = abs(mkt_a["mark_price"] - mkt_b["mark_price"])
    spread_funding = abs(mkt_a["funding_rate"] - mkt_b["funding_rate"])  # décimal brut

    log.info(
        f"{MARKET_A}={mkt_a['mark_price']:.3f} | {MARKET_B}={mkt_b['mark_price']:.3f} | "
        f"Spread={spread_price:.3f}$ | "
        f"ΔFunding={spread_funding*100:.6f}% | "
        f"FR_A={mkt_a['funding_rate']*100:.6f}% FR_B={mkt_b['funding_rate']*100:.6f}%"
    )

    # ── Price spread alert ─────────────────────────────────────────────────────
    if spread_price >= SPREAD_THRESHOLD:
        if not _alert_sent_spread:
            direction = f"{ticker_a} > {ticker_b}" if mkt_a["mark_price"] > mkt_b["mark_price"] else f"{ticker_b} > {ticker_a}"
            msg = (
                f"🚨 *OIL SPREAD ALERT* 🚨\n\n"
                f"📈 Price gap : *{spread_price:.3f} $* ({direction})\n\n"
                f"┌ *{ticker_a}* : {mkt_a['mark_price']:.3f} $\n"
                f"│  Funding : {mkt_a['funding_rate']*100:.6f}% / 1h\n"
                f"│  Index   : {mkt_a['index_price']:.3f} $\n"
                f"│\n"
                f"└ *{ticker_b}* : {mkt_b['mark_price']:.3f} $\n"
                f"   Funding : {mkt_b['funding_rate']*100:.6f}% / 1h\n"
                f"   Index   : {mkt_b['index_price']:.3f} $\n\n"
                f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
            )
            send_message_sync(loop, app.bot, CHAT_ID, msg)
            _alert_sent_spread = True
            log.info("✅ Spread alert sent.")
    else:
        _alert_sent_spread = False

    # ── Funding rate alert ─────────────────────────────────────────────────────
    # FUNDING_THRESHOLD est en décimal brut (même unité que l'API)
    if spread_funding >= FUNDING_THRESHOLD:
        if not _alert_sent_funding:
            msg = (
                f"⚡ *OIL FUNDING ALERT* ⚡\n\n"
                f"📊 Funding gap : *{spread_funding*100:.6f}%* / 1h\n\n"
                f"┌ *{ticker_a}* : {mkt_a['funding_rate']*100:.6f}% / 1h\n"
                f"└ *{ticker_b}* : {mkt_b['funding_rate']*100:.6f}% / 1h\n\n"
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
        MARKET_A: (mkt_a["funding_rate"], mkt_a["mark_price"]),
        MARKET_B: (mkt_b["funding_rate"], mkt_b["mark_price"]),
    }

    for trade in list(_trades.values()):
        long_rate,  mark_long  = rates[trade.long_ticker]
        short_rate, mark_short = rates[trade.short_ticker]

        trade.accrue_funding(
            funding_rate_long=long_rate,
            funding_rate_short=short_rate,
            mark_long=mark_long,
            mark_short=mark_short,
        )

        price_pnl = trade.price_pnl(mark_long, mark_short)
        total_pnl = trade.total_pnl(mark_long, mark_short)
        age_h     = trade.age_hours

        long_name  = trade.long_ticker.split("-")[0]
        short_name = trade.short_ticker.split("-")[0]

        log.info(
            f"Trade #{trade.id} | Long {long_name} x{trade.size} | "
            f"Short {short_name} x{trade.size} | "
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
                f"Size : *{trade.size} barrel(s)*\n"
                f"Duration : *{age_h:.1f}h*\n\n"
                f"┌ *Long* {long_name}\n"
                f"│  Entry : {trade.entry_long:.3f} $\n"
                f"│  Mark  : {mark_long:.3f} $\n"
                f"│  Price gain : {(mark_long - trade.entry_long) * trade.size:+.2f} $\n"
                f"│  Funding    : {f_long_sign}{trade.funding_long_accumulated:.2f} $\n"
                f"│\n"
                f"├ *Short* {short_name}\n"
                f"│  Entry : {trade.entry_short:.3f} $\n"
                f"│  Mark  : {mark_short:.3f} $\n"
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
    ticker_a = MARKET_A.split("-")[0]
    ticker_b = MARKET_B.split("-")[0]
    await update.message.reply_text(
        "📝 *New trade — step 1 of 4*\n\n"
        f"Which instrument is in a *LONG* position?\n"
        f"Reply `{ticker_a}` or `{ticker_b}`.",
        parse_mode="Markdown",
    )
    return ASK_LONG


async def cmd_newtrade_long(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ticker_a = MARKET_A.split("-")[0]
    ticker_b = MARKET_B.split("-")[0]
    answer = update.message.text.strip().upper()

    # Accepter les deux formats (ex: "WTI" ou "WTI-USD")
    answer_market = answer if "-" in answer else f"{answer}-USD"

    if answer_market not in (MARKET_A, MARKET_B):
        await update.message.reply_text(
            f"❌ Answer `{ticker_a}` or `{ticker_b}`.", parse_mode="Markdown"
        )
        return ASK_LONG

    ctx.user_data["long_ticker"]  = answer_market
    ctx.user_data["short_ticker"] = MARKET_B if answer_market == MARKET_A else MARKET_A

    long_name  = ctx.user_data["long_ticker"].split("-")[0]
    short_name = ctx.user_data["short_ticker"].split("-")[0]

    await update.message.reply_text(
        f"✅ Long : *{long_name}* | Short : *{short_name}*\n\n"
        f"📝 *Step 2/4* — How many barrels did you trade?\n"
        f"_(ex: `1`, `0.5`, `10`)_",
        parse_mode="Markdown",
    )
    return ASK_SIZE


async def cmd_newtrade_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        size = float(update.message.text.strip().replace(",", "."))
        assert size > 0
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ Enter a valid positive number (ex: `1`).", parse_mode="Markdown"
        )
        return ASK_SIZE

    ctx.user_data["size"] = size
    long_name = ctx.user_data["long_ticker"].split("-")[0]

    await update.message.reply_text(
        f"✅ Size : *{size} barrel(s)*\n\n"
        f"📝 *Step 3/4* — Entry price for the *LONG* leg ({long_name})?\n"
        f"_(ex: `75.320`)_",
        parse_mode="Markdown",
    )
    return ASK_ENTRY_LONG


async def cmd_newtrade_entry_long(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text.strip().replace(",", "."))
        assert price > 0
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ Enter a valid price (ex: `75.320`).", parse_mode="Markdown"
        )
        return ASK_ENTRY_LONG

    ctx.user_data["entry_long"] = price
    short_name = ctx.user_data["short_ticker"].split("-")[0]

    await update.message.reply_text(
        f"✅ Long entry : *{price:.3f} $*\n\n"
        f"📝 *Step 4/4* — Entry price for the *SHORT* leg ({short_name})?\n"
        f"_(ex: `78.500`)_",
        parse_mode="Markdown",
    )
    return ASK_ENTRY_SHORT


async def cmd_newtrade_entry_short(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    global _next_trade_id

    try:
        price = float(update.message.text.strip().replace(",", "."))
        assert price > 0
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ Enter a valid price (ex: `78.500`).", parse_mode="Markdown"
        )
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
    long_name    = trade.long_ticker.split("-")[0]
    short_name   = trade.short_ticker.split("-")[0]

    await update.message.reply_text(
        f"✅ *Trade #{trade.id} saved!*\n\n"
        f"┌ Long  *{long_name}* @ {trade.entry_long:.3f} $\n"
        f"└ Short *{short_name}* @ {trade.entry_short:.3f} $\n\n"
        f"Size : *{trade.size} barrel(s)* (~{notional:,.0f} $ notional)\n"
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

    try:
        data = fetch_oil_listings()
    except Exception:
        data = {}

    lines = ["📋 *Open trades*\n"]
    for trade in _trades.values():
        long_data  = data.get(trade.long_ticker)
        short_data = data.get(trade.short_ticker)
        long_name  = trade.long_ticker.split("-")[0]
        short_name = trade.short_ticker.split("-")[0]

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
            f"Long {long_name} / Short {short_name} | "
            f"Size {trade.size} bbl\n"
            f"   Entry : {trade.entry_long:.3f} / {trade.entry_short:.3f} $\n"
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

    long_name  = trade.long_ticker.split("-")[0]
    short_name = trade.short_ticker.split("-")[0]

    try:
        data       = fetch_oil_listings()
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
        f"Long  {long_name} x{trade.size} bbl @ {trade.entry_long:.3f} $\n"
        f"Short {short_name} x{trade.size} bbl @ {trade.entry_short:.3f} $\n\n"
        + pnl_lines +
        f"\n⏱ Duration : {trade.age_hours:.1f}h",
        parse_mode="Markdown",
    )

# ─── COMMANDS ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ticker_a = MARKET_A.split("-")[0]
    ticker_b = MARKET_B.split("-")[0]
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 {ticker_a}/{ticker_b} oil bot active! (Extended Exchange)\n\n"
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


async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = fetch_oil_listings()
    except Exception as e:
        await update.message.reply_text(f"❌ API error: {e}")
        return

    if MARKET_A not in data or MARKET_B not in data:
        await update.message.reply_text(f"⚠️ {MARKET_A} or {MARKET_B} not found in API.")
        return

    mkt_a = data[MARKET_A]
    mkt_b = data[MARKET_B]
    ticker_a = MARKET_A.split("-")[0]
    ticker_b = MARKET_B.split("-")[0]

    spread = abs(mkt_a["mark_price"] - mkt_b["mark_price"])
    spread_funding = abs(mkt_a["funding_rate"] - mkt_b["funding_rate"])

    emoji_spread  = "🔴" if spread >= SPREAD_THRESHOLD else "🟢"
    emoji_funding = "🔴" if spread_funding >= FUNDING_THRESHOLD else "🟢"

    msg = (
        f"🛢 *Oil prices — Extended Exchange*\n\n"
        f"*{ticker_a}* (WTI Crude)\n"
        f"  Mark  : {mkt_a['mark_price']:.3f} $\n"
        f"  Index : {mkt_a['index_price']:.3f} $\n"
        f"  Funding : {mkt_a['funding_rate']*100:.6f}% / 1h\n"
        f"  Vol 24h : {mkt_a['volume_24h']:,.0f} $\n\n"
        f"*{ticker_b}* (Brent Crude)\n"
        f"  Mark  : {mkt_b['mark_price']:.3f} $\n"
        f"  Index : {mkt_b['index_price']:.3f} $\n"
        f"  Funding : {mkt_b['funding_rate']*100:.6f}% / 1h\n"
        f"  Vol 24h : {mkt_b['volume_24h']:,.0f} $\n\n"
        f"{emoji_spread} Spread : *{spread:.3f} $*\n"
        f"{emoji_funding} Δ Funding : *{spread_funding*100:.6f}%*\n\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_threshold(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"⚙️ *Configured thresholds*\n\n"
        f"• Spread : *{SPREAD_THRESHOLD} $*\n"
        f"• Funding spread : *{FUNDING_THRESHOLD*100:.6f} %* / 1h\n"
        f"• PnL alert : *+{PNL_THRESHOLD} $* after *{PNL_MIN_HOURS:.0f}h*\n"
        f"• Check interval : *{CHECK_INTERVAL}s*\n\n"
        f"Markets monitored:\n"
        f"• A : `{MARKET_A}`\n"
        f"• B : `{MARKET_B}`",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"✅ Bot live (Extended Exchange)\n"
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
    app.add_handler(CommandHandler("price",      cmd_price))
    app.add_handler(CommandHandler("threshold",  cmd_threshold))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("trades",     cmd_trades))
    app.add_handler(CommandHandler("closetrade", cmd_closetrade))

    loop = asyncio.get_event_loop()

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=lambda: check_and_notify(app, loop),
        trigger="interval",
        seconds=CHECK_INTERVAL,
        id="oil_spread_check",
    )
    scheduler.start()
    log.info(f"🚀 Bot started — checking every {CHECK_INTERVAL}s | {MARKET_A} vs {MARKET_B}")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
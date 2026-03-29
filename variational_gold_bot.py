import logging
import os
from datetime import datetime

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─── CONFIG ────────────────────────────────────────────────────────────────────

load_dotenv()

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
CHAT_ID           = os.environ["CHAT_ID"]
SPREAD_THRESHOLD  = float(os.getenv("SPREAD_THRESHOLD", "10.0"))
FUNDING_THRESHOLD = float(os.getenv("FUNDING_THRESHOLD", "0.5"))
CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL", "60"))

BASE_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io"

# ─── LOGGING ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

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
                "funding_rate":       float(listing["funding_rate"]) * 100,  # as %
                "funding_interval_s": listing.get("funding_interval_s", 28800),
                "volume_24h":         float(listing["volume_24h"]),
            }
    return result

# ─── MONITORING LOGIC ──────────────────────────────────────────────────────────

# Track whether an alert has already been sent to prevent spam
_alert_sent_spread  = False
_alert_sent_funding = False


def check_and_notify(app: Application) -> None:
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
        f"Spread={spread_price:.2f}$ | ΔFunding={spread_funding:.4f}%"
    )

    # ── Price spread alert ─────────────────────────────────────────────────────
    if spread_price >= SPREAD_THRESHOLD:
        if not _alert_sent_spread:
            direction = "XAUT > PAXG" if xaut["mark_price"] > paxg["mark_price"] else "PAXG > XAUT"
            msg = (
                f"🚨 *GOLD SPREAD ALERT* 🚨\n\n"
                f"📈 Price gap : *{spread_price:.2f} $* ({direction})\n\n"
                f"┌ *XAUT* : {xaut['mark_price']:.2f} $\n"
                f"│  Funding : {xaut['funding_rate']:.4f}% / {xaut['funding_interval_s']//3600}h\n"
                f"│\n"
                f"└ *PAXG* : {paxg['mark_price']:.2f} $\n"
                f"   Funding : {paxg['funding_rate']:.4f}% / {paxg['funding_interval_s']//3600}h\n\n"
                f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
            )
            app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            _alert_sent_spread = True
            log.info("✅ Spread alert sent.")
    else:
        # Reset once the gap falls back below threshold
        _alert_sent_spread = False

    # ── Funding rate alert ─────────────────────────────────────────────────────
    if spread_funding >= FUNDING_THRESHOLD:
        if not _alert_sent_funding:
            msg = (
                f"⚡ *GOLD FUNDING ALERT* ⚡\n\n"
                f"📊 Funding gap : *{spread_funding:.4f}%*\n\n"
                f"┌ *XAUT* : {xaut['funding_rate']:.4f}% / {xaut['funding_interval_s']//3600}h\n"
                f"└ *PAXG* : {paxg['funding_rate']:.4f}% / {paxg['funding_interval_s']//3600}h\n\n"
                f"💡 Potential carry opportunity!\n"
                f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
            )
            app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            _alert_sent_funding = True
            log.info("✅ Funding alert sent.")
    else:
        _alert_sent_funding = False

# ─── TELEGRAM COMMANDS ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 XAUT/PAXG bot active !\n\n"
        f"Chat ID : `{chat_id}`\n\n"
        f"Commands :\n"
        f"/price — Acutal price + spread\n"
        f"/threshold — View configured thresholds\n"
        f"/status — Bot status",
        parse_mode="Markdown",
    )


async def cmd_prix(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = fetch_gold_listings()
    except Exception as e:
        await update.message.reply_text(f"❌ API error : {e}")
        return

    if "XAUT" not in data or "PAXG" not in data:
        await update.message.reply_text("⚠️ XAUT or PAXG not found in the API.")
        return

    xaut = data["XAUT"]
    paxg = data["PAXG"]
    spread = abs(xaut["mark_price"] - paxg["mark_price"])
    spread_funding = abs(xaut["funding_rate"] - paxg["funding_rate"])

    emoji_spread  = "🔴" if spread >= SPREAD_THRESHOLD else "🟢"
    emoji_funding = "🔴" if spread_funding >= FUNDING_THRESHOLD else "🟢"

    msg = (
        f"💰 *Gold price — Variational*\n\n"
        f"*XAUT*\n"
        f"  Mark : {xaut['mark_price']:.2f} $\n"
        f"  Funding : {xaut['funding_rate']:.4f}% / {xaut['funding_interval_s']//3600}h\n"
        f"  Vol 24h : {xaut['volume_24h']:,.0f} $\n\n"
        f"*PAXG*\n"
        f"  Mark : {paxg['mark_price']:.2f} $\n"
        f"  Funding : {paxg['funding_rate']:.4f}% / {paxg['funding_interval_s']//3600}h\n"
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
        f"• Check interval : *{CHECK_INTERVAL}s*",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"✅ Bot now live\n"
        f"Check every {CHECK_INTERVAL}s\n"
        f"Spread alert active : {'Yes' if _alert_sent_spread else 'No'}\n"
        f"Funding alert active : {'Yes' if _alert_sent_funding else 'No'}"
    )

# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Register commands
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("price",   cmd_prix))
    app.add_handler(CommandHandler("threshold",  cmd_seuil))
    app.add_handler(CommandHandler("status", cmd_status))

    # Scheduler for automatic monitoring
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=lambda: check_and_notify(app),
        trigger="interval",
        seconds=CHECK_INTERVAL,
        id="gold_spread_check",
    )
    scheduler.start()
    log.info(f"🚀 Bot started — checking every {CHECK_INTERVAL}s")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
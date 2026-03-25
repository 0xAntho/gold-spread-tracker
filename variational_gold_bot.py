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
    """Récupère les données XAUT et PAXG depuis l'API Variational."""
    resp = requests.get(f"{BASE_URL}/metadata/stats", timeout=10)
    resp.raise_for_status()
    data = resp.json()

    result = {}
    for listing in data.get("listings", []):
        ticker = listing["ticker"].upper()
        if ticker in ("XAUT", "PAXG"):
            result[ticker] = {
                "mark_price":    float(listing["mark_price"]),
                "funding_rate":  float(listing["funding_rate"]) * 100,  # en %
                "funding_interval_s": listing.get("funding_interval_s", 28800),
                "bid_1k": float(listing["quotes"]["size_1k"]["bid"]),
                "ask_1k": float(listing["quotes"]["size_1k"]["ask"]),
                "volume_24h": float(listing["volume_24h"]),
            }
    return result

# ─── LOGIQUE DE SURVEILLANCE ───────────────────────────────────────────────────

# Garde en mémoire si une alerte a déjà été envoyée pour éviter le spam
_alert_sent_spread  = False
_alert_sent_funding = False


def check_and_notify(app: Application) -> None:
    global _alert_sent_spread, _alert_sent_funding

    try:
        data = fetch_gold_listings()
    except Exception as e:
        log.error(f"Erreur API : {e}")
        return

    if "XAUT" not in data or "PAXG" not in data:
        log.warning("XAUT ou PAXG introuvable dans la réponse API.")
        return

    xaut = data["XAUT"]
    paxg = data["PAXG"]

    spread_price   = abs(xaut["mark_price"] - paxg["mark_price"])
    spread_funding = abs(xaut["funding_rate"] - paxg["funding_rate"])

    log.info(
        f"XAUT={xaut['mark_price']:.2f} | PAXG={paxg['mark_price']:.2f} | "
        f"Spread={spread_price:.2f}$ | ΔFunding={spread_funding:.4f}%"
    )

    # ── Alerte spread de prix ──────────────────────────────────────────────────
    if spread_price >= SPREAD_THRESHOLD:
        if not _alert_sent_spread:
            direction = "XAUT > PAXG" if xaut["mark_price"] > paxg["mark_price"] else "PAXG > XAUT"
            msg = (
                f"🚨 *ALERTE SPREAD OR* 🚨\n\n"
                f"📈 Écart de prix : *{spread_price:.2f} $* ({direction})\n\n"
                f"┌ *XAUT* : {xaut['mark_price']:.2f} $\n"
                f"│  Funding : {xaut['funding_rate']:.4f}% / {xaut['funding_interval_s']//3600}h\n"
                f"│  Bid/Ask 1k : {xaut['bid_1k']:.2f} / {xaut['ask_1k']:.2f}\n"
                f"│\n"
                f"└ *PAXG* : {paxg['mark_price']:.2f} $\n"
                f"   Funding : {paxg['funding_rate']:.4f}% / {paxg['funding_interval_s']//3600}h\n"
                f"   Bid/Ask 1k : {paxg['bid_1k']:.2f} / {paxg['ask_1k']:.2f}\n\n"
                f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
            )
            app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            _alert_sent_spread = True
            log.info("✅ Alerte spread envoyée.")
    else:
        _alert_sent_spread = False  # Reset quand l'écart repasse sous le seuil

    # ── Alerte funding rate ────────────────────────────────────────────────────
    if spread_funding >= FUNDING_THRESHOLD:
        if not _alert_sent_funding:
            msg = (
                f"⚡ *ALERTE FUNDING OR* ⚡\n\n"
                f"📊 Écart de funding : *{spread_funding:.4f}%*\n\n"
                f"┌ *XAUT* : {xaut['funding_rate']:.4f}% / {xaut['funding_interval_s']//3600}h\n"
                f"└ *PAXG* : {paxg['funding_rate']:.4f}% / {paxg['funding_interval_s']//3600}h\n\n"
                f"💡 Opportunité de carry potentielle !\n"
                f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
            )
            app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            _alert_sent_funding = True
            log.info("✅ Alerte funding envoyée.")
    else:
        _alert_sent_funding = False

# ─── COMMANDES TELEGRAM ────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 Bot XAUT/PAXG actif !\n\n"
        f"Ton Chat ID : `{chat_id}`\n\n"
        f"Commandes :\n"
        f"/prix — Prix actuels + spread\n"
        f"/seuil — Voir les seuils configurés\n"
        f"/status — État du bot",
        parse_mode="Markdown",
    )


async def cmd_prix(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = fetch_gold_listings()
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur API : {e}")
        return

    if "XAUT" not in data or "PAXG" not in data:
        await update.message.reply_text("⚠️ XAUT ou PAXG non trouvé dans l'API.")
        return

    xaut = data["XAUT"]
    paxg = data["PAXG"]
    spread = abs(xaut["mark_price"] - paxg["mark_price"])
    spread_funding = abs(xaut["funding_rate"] - paxg["funding_rate"])

    emoji_spread  = "🔴" if spread >= SPREAD_THRESHOLD else "🟢"
    emoji_funding = "🔴" if spread_funding >= FUNDING_THRESHOLD else "🟢"

    msg = (
        f"💰 *Prix Or — Variational*\n\n"
        f"*XAUT*\n"
        f"  Mark : {xaut['mark_price']:.2f} $\n"
        f"  Funding : {xaut['funding_rate']:.4f}% / {xaut['funding_interval_s']//3600}h\n"
        f"  Bid/Ask 1k : {xaut['bid_1k']:.2f} / {xaut['ask_1k']:.2f}\n"
        f"  Vol 24h : {xaut['volume_24h']:,.0f} $\n\n"
        f"*PAXG*\n"
        f"  Mark : {paxg['mark_price']:.2f} $\n"
        f"  Funding : {paxg['funding_rate']:.4f}% / {paxg['funding_interval_s']//3600}h\n"
        f"  Bid/Ask 1k : {paxg['bid_1k']:.2f} / {paxg['ask_1k']:.2f}\n"
        f"  Vol 24h : {paxg['volume_24h']:,.0f} $\n\n"
        f"{emoji_spread} Spread prix : *{spread:.2f} $* (seuil : {SPREAD_THRESHOLD} $)\n"
        f"{emoji_funding} Δ Funding : *{spread_funding:.4f}%* (seuil : {FUNDING_THRESHOLD} %)\n\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_seuil(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"⚙️ *Seuils configurés*\n\n"
        f"• Spread prix : *{SPREAD_THRESHOLD} $*\n"
        f"• Écart funding : *{FUNDING_THRESHOLD} %*\n"
        f"• Intervalle de vérif : *{CHECK_INTERVAL}s*",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"✅ Bot opérationnel\n"
        f"Vérification toutes les {CHECK_INTERVAL}s\n"
        f"Alerte spread active : {'Oui' if _alert_sent_spread else 'Non'}\n"
        f"Alerte funding active : {'Oui' if _alert_sent_funding else 'Non'}"
    )

# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commandes
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("prix",   cmd_prix))
    app.add_handler(CommandHandler("seuil",  cmd_seuil))
    app.add_handler(CommandHandler("status", cmd_status))

    # Scheduler pour la surveillance automatique
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=lambda: check_and_notify(app),
        trigger="interval",
        seconds=CHECK_INTERVAL,
        id="gold_spread_check",
    )
    scheduler.start()
    log.info(f"🚀 Bot démarré — vérification toutes les {CHECK_INTERVAL}s")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
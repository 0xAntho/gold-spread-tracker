"""
variational_monitor.py
~~~~~~~~~~~~~~~~~~~~~~
Monitor for XAUT/PAXG spread and funding on Variational DEX.
This module has zero Telegram dependency — it only fetches data and returns Alert objects.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# ─── CONFIG ────────────────────────────────────────────────────────────────────

SPREAD_THRESHOLD  = float(os.getenv("GOLD_SPREAD_THRESHOLD", "10.0"))
FUNDING_THRESHOLD = float(os.getenv("GOLD_FUNDING_THRESHOLD", "0.5"))   # % after ×100

BASE_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io"

# ─── ALERT MODEL ───────────────────────────────────────────────────────────────

@dataclass
class Alert:
    message: str
    source: str = "variational"   # for logging


# ─── API ───────────────────────────────────────────────────────────────────────

def fetch_gold_listings() -> dict:
    """Fetch XAUT and PAXG data from the Variational API.

    Returns dict keyed by ticker with keys:
        mark_price, funding_rate (raw decimal), funding_interval_s, volume_24h.
    """
    resp = requests.get(f"{BASE_URL}/metadata/stats", timeout=10)
    resp.raise_for_status()
    data = resp.json()

    result = {}
    for listing in data.get("listings", []):
        ticker = listing["ticker"].upper()
        if ticker in ("XAUT", "PAXG"):
            result[ticker] = {
                "mark_price":         float(listing["mark_price"]),
                "funding_rate":       float(listing["funding_rate"]),  # raw decimal
                "funding_interval_s": listing.get("funding_interval_s", 28800),
                "volume_24h":         float(listing["volume_24h"]),
            }
    return result


# ─── STATE ─────────────────────────────────────────────────────────────────────

_alert_sent_spread  = False
_alert_sent_funding = False


# ─── MAIN CHECK ────────────────────────────────────────────────────────────────

def check() -> list[Alert]:
    """Fetch latest data and return a list of Alert objects to send."""
    global _alert_sent_spread, _alert_sent_funding

    alerts = []

    try:
        data = fetch_gold_listings()
    except Exception as e:
        log.error(f"[Variational] API error: {e}")
        return alerts

    if "XAUT" not in data or "PAXG" not in data:
        log.warning("[Variational] XAUT or PAXG not found in API response.")
        return alerts

    xaut = data["XAUT"]
    paxg = data["PAXG"]

    spread_price   = abs(xaut["mark_price"] - paxg["mark_price"])
    spread_funding = abs(xaut["funding_rate"] - paxg["funding_rate"])

    log.info(
        f"[Variational] XAUT={xaut['mark_price']:.2f} | PAXG={paxg['mark_price']:.2f} | "
        f"Spread={spread_price:.2f}$ | ΔFunding={spread_funding*100:.4f}%"
    )

    # ── Price spread alert ─────────────────────────────────────────────────────
    if spread_price >= SPREAD_THRESHOLD:
        if not _alert_sent_spread:
            direction = "XAUT > PAXG" if xaut["mark_price"] > paxg["mark_price"] else "PAXG > XAUT"
            alerts.append(Alert(
                message=(
                    f"🚨 *GOLD SPREAD ALERT* 🚨\n\n"
                    f"📈 Price gap : *{spread_price:.2f} $* ({direction})\n\n"
                    f"┌ *XAUT* : {xaut['mark_price']:.2f} $\n"
                    f"│  Funding : {xaut['funding_rate']*100:.4f}% / {xaut['funding_interval_s']//3600}h\n"
                    f"│\n"
                    f"└ *PAXG* : {paxg['mark_price']:.2f} $\n"
                    f"   Funding : {paxg['funding_rate']*100:.4f}% / {paxg['funding_interval_s']//3600}h\n\n"
                    f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
                )
            ))
            _alert_sent_spread = True
            log.info("[Variational] ✅ Spread alert queued.")
    else:
        _alert_sent_spread = False

    # ── Funding rate alert ─────────────────────────────────────────────────────
    if spread_funding * 100 >= FUNDING_THRESHOLD:
        if not _alert_sent_funding:
            alerts.append(Alert(
                message=(
                    f"⚡ *GOLD FUNDING ALERT* ⚡\n\n"
                    f"📊 Funding gap : *{spread_funding*100:.4f}%*\n\n"
                    f"┌ *XAUT* : {xaut['funding_rate']*100:.4f}% / {xaut['funding_interval_s']//3600}h\n"
                    f"└ *PAXG* : {paxg['funding_rate']*100:.4f}% / {paxg['funding_interval_s']//3600}h\n\n"
                    f"💡 Potential carry opportunity!\n"
                    f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
                )
            ))
            _alert_sent_funding = True
            log.info("[Variational] ✅ Funding alert queued.")
    else:
        _alert_sent_funding = False

    return alerts


def price_message() -> str:
    """Return a formatted price message for the /gold command."""
    try:
        data = fetch_gold_listings()
    except Exception as e:
        return f"❌ Variational API error: {e}"

    if "XAUT" not in data or "PAXG" not in data:
        return "⚠️ XAUT or PAXG not found in the API."

    xaut = data["XAUT"]
    paxg = data["PAXG"]
    spread = abs(xaut["mark_price"] - paxg["mark_price"])
    spread_funding = abs(xaut["funding_rate"] - paxg["funding_rate"])

    emoji_spread  = "🔴" if spread >= SPREAD_THRESHOLD else "🟢"
    emoji_funding = "🔴" if spread_funding * 100 >= FUNDING_THRESHOLD else "🟢"

    return (
        f"💰 *Gold — Variational*\n\n"
        f"*XAUT*\n"
        f"  Mark : {xaut['mark_price']:.2f} $\n"
        f"  Funding : {xaut['funding_rate']*100:.4f}% / {xaut['funding_interval_s']//3600}h\n"
        f"  Vol 24h : {xaut['volume_24h']:,.0f} $\n\n"
        f"*PAXG*\n"
        f"  Mark : {paxg['mark_price']:.2f} $\n"
        f"  Funding : {paxg['funding_rate']*100:.4f}% / {paxg['funding_interval_s']//3600}h\n"
        f"  Vol 24h : {paxg['volume_24h']:,.0f} $\n\n"
        f"{emoji_spread} Spread : *{spread:.2f} $*\n"
        f"{emoji_funding} Δ Funding : *{spread_funding*100:.4f}%*\n\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
    )
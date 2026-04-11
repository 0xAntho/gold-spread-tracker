"""
extended_monitor.py
~~~~~~~~~~~~~~~~~~~
Monitor for WTI/XBR spread and funding on Extended Exchange.
This module has zero Telegram dependency — it only fetches data and returns Alert objects.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# ─── CONFIG ────────────────────────────────────────────────────────────────────

SPREAD_THRESHOLD  = float(os.getenv("OIL_SPREAD_THRESHOLD", "0.50"))
FUNDING_THRESHOLD = float(os.getenv("OIL_FUNDING_THRESHOLD", "0.00005"))  # raw decimal

MARKET_A = os.getenv("OIL_MARKET_A", "WTI-USD")
MARKET_B = os.getenv("OIL_MARKET_B", "XBR-USD")

# Extended TradFi funding interval is fixed at 1h
FUNDING_INTERVAL_S = 3600

BASE_URL = "https://api.starknet.extended.exchange/api/v1"

# ─── ALERT MODEL ───────────────────────────────────────────────────────────────

@dataclass
class Alert:
    message: str
    source: str = "extended"


# ─── API ───────────────────────────────────────────────────────────────────────

def fetch_market_stats(market: str) -> dict:
    """Fetch stats for a single market from the Extended API.

    Returns dict with keys: mark_price, index_price, funding_rate, volume_24h.

    Notes:
        - funding_rate is a raw decimal per hour (e.g. -0.000059 = -0.0059%/h).
        - User-Agent header is mandatory per Extended API docs.
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
        "funding_rate": float(d["fundingRate"]),  # raw decimal per hour
        "volume_24h":   float(d["dailyVolume"]),
    }


def fetch_oil_listings() -> dict:
    """Fetch MARKET_A and MARKET_B data from the Extended API."""
    return {
        MARKET_A: fetch_market_stats(MARKET_A),
        MARKET_B: fetch_market_stats(MARKET_B),
    }


# ─── STATE ─────────────────────────────────────────────────────────────────────

_alert_sent_spread  = False
_alert_sent_funding = False


# ─── MAIN CHECK ────────────────────────────────────────────────────────────────

def check() -> list[Alert]:
    """Fetch latest data and return a list of Alert objects to send."""
    global _alert_sent_spread, _alert_sent_funding

    alerts = []
    ticker_a = MARKET_A.split("-")[0]
    ticker_b = MARKET_B.split("-")[0]

    try:
        data = fetch_oil_listings()
    except Exception as e:
        log.error(f"[Extended] API error: {e}")
        return alerts

    mkt_a = data[MARKET_A]
    mkt_b = data[MARKET_B]

    spread_price   = abs(mkt_a["mark_price"] - mkt_b["mark_price"])
    spread_funding = abs(mkt_a["funding_rate"] - mkt_b["funding_rate"])

    log.info(
        f"[Extended] {ticker_a}={mkt_a['mark_price']:.3f} | {ticker_b}={mkt_b['mark_price']:.3f} | "
        f"Spread={spread_price:.3f}$ | ΔFunding={spread_funding*100:.6f}%"
    )

    # ── Price spread alert ─────────────────────────────────────────────────────
    if spread_price >= SPREAD_THRESHOLD:
        if not _alert_sent_spread:
            direction = f"{ticker_a} > {ticker_b}" if mkt_a["mark_price"] > mkt_b["mark_price"] else f"{ticker_b} > {ticker_a}"
            alerts.append(Alert(
                message=(
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
            ))
            _alert_sent_spread = True
            log.info("[Extended] ✅ Spread alert queued.")
    else:
        _alert_sent_spread = False

    # ── Funding rate alert ─────────────────────────────────────────────────────
    if spread_funding >= FUNDING_THRESHOLD:
        if not _alert_sent_funding:
            alerts.append(Alert(
                message=(
                    f"⚡ *OIL FUNDING ALERT* ⚡\n\n"
                    f"📊 Funding gap : *{spread_funding*100:.6f}%* / 1h\n\n"
                    f"┌ *{ticker_a}* : {mkt_a['funding_rate']*100:.6f}% / 1h\n"
                    f"└ *{ticker_b}* : {mkt_b['funding_rate']*100:.6f}% / 1h\n\n"
                    f"💡 Potential carry opportunity!\n"
                    f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
                )
            ))
            _alert_sent_funding = True
            log.info("[Extended] ✅ Funding alert queued.")
    else:
        _alert_sent_funding = False

    return alerts


def price_message() -> str:
    """Return a formatted price message for the /oil command."""
    ticker_a = MARKET_A.split("-")[0]
    ticker_b = MARKET_B.split("-")[0]

    try:
        data = fetch_oil_listings()
    except Exception as e:
        return f"❌ Extended API error: {e}"

    mkt_a = data[MARKET_A]
    mkt_b = data[MARKET_B]

    spread = abs(mkt_a["mark_price"] - mkt_b["mark_price"])
    spread_funding = abs(mkt_a["funding_rate"] - mkt_b["funding_rate"])

    emoji_spread  = "🔴" if spread >= SPREAD_THRESHOLD else "🟢"
    emoji_funding = "🔴" if spread_funding >= FUNDING_THRESHOLD else "🟢"

    return (
        f"🛢 *Oil — Extended Exchange*\n\n"
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
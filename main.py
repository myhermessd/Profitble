"""
Whale & Insider Detection Bot
==============================
Monitors Hyperliquid WebSocket + Polymarket API in real time.
Detects Type A (Insider), Type B (Skilled Whale), Type C (Big Money).
Sends Telegram alerts. Execution disabled by default.

Set these in Railway Variables tab:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import asyncio
import json
import os
import sys
import time
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
import websockets
import httpx

# ── Logging (unbuffered for Railway) ───────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("whale_bot")

# ── Environment Variables ───────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    log.critical("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. Add them in Railway Variables tab.")
    sys.exit(1)

# ── Hyperliquid Config ──────────────────────────────────────────────────────────
WS_URL   = "wss://api.hyperliquid.xyz/ws"
INFO_URL = "https://api.hyperliquid.xyz/info"

# ── Polymarket Config ───────────────────────────────────────────────────────────
POLYMARKET_URL = "https://gamma-api.polymarket.com/markets"

# ── Execution (keep False until you verify signals are real) ────────────────────
EXECUTION_ENABLED = False

# ── Detection Thresholds ────────────────────────────────────────────────────────
MIN_POSITION_USD       = 5_000_000   # $5M minimum
MIN_LEVERAGE_TO_FLAG   = 2            # Ignore under 2x
MAX_WALLET_AGE_HOURS   = 48           # Fresh wallet
MIN_CONCENTRATION      = 0.80         # 80% of capital in one trade
CLUSTER_WINDOW_SECONDS = 3600         # 60 min rolling window
MIN_CLUSTER_SIZE       = 3            # Wallets to fire cluster alert
MAX_SIZE_VARIANCE      = 0.25         # 25% max variance across cluster
ALERT_COOLDOWN_SECONDS = 300          # 5 min cooldown same cluster

# ── Capital Sizing ──────────────────────────────────────────────────────────────
TOTAL_CAPITAL_USD = 1000   # Change to your actual capital
TYPE_A_SIZE_PCT   = 0.20   # 20% for insider pattern
TYPE_B_SIZE_PCT   = 0.10   # 10% for skilled whale
TYPE_C_SIZE_PCT   = 0.05   # 5% for big unknown money
MAX_LEVERAGE      = 10     # Never go above this

# ── Risk Management ─────────────────────────────────────────────────────────────
MAX_SIMULTANEOUS_TRADES = 3
DAILY_LOSS_LIMIT_PCT    = 0.04
CONSECUTIVE_LOSS_LIMIT  = 3

# ── Geopolitical Watchlist ──────────────────────────────────────────────────────
GEO_WATCHLIST = {
    "WTI",
    "BTC",
    "ETH",
    "GOLD",
}


# ── Data Structures ─────────────────────────────────────────────────────────────

@dataclass
class Position:
    wallet:           str
    asset:            str
    side:             str
    size_usd:         float
    entry_price:      float
    timestamp:        float
    leverage:         float
    wallet_age_hours: float
    concentration:    float
    tx_hash:          str

    @property
    def side_label(self) -> str:
        return "LONG 📈" if self.side == "B" else "SHORT 📉"


@dataclass
class Cluster:
    asset:        str
    side:         str
    positions:    list = field(default_factory=list)
    first_seen:   float = field(default_factory=time.time)
    last_alerted: float = 0.0

    @property
    def size(self) -> int:
        return len(self.positions)

    @property
    def total_exposure_usd(self) -> float:
        return sum(p.size_usd for p in self.positions)

    @property
    def avg_wallet_age_hours(self) -> float:
        if not self.positions:
            return 0
        return sum(p.wallet_age_hours for p in self.positions) / len(self.positions)

    @property
    def size_variance(self) -> float:
        if len(self.positions) < 2:
            return 0.0
        sizes = [p.size_usd for p in self.positions]
        mean = sum(sizes) / len(sizes)
        variance = sum((s - mean) ** 2 for s in sizes) / len(sizes)
        return (variance ** 0.5) / mean


# ── Insider Probability Score ───────────────────────────────────────────────────

def calculate_ips(position: Position, cluster_size: int = 1) -> dict:
    score = 0
    breakdown = {}

    # Size Score (0-30)
    if position.size_usd >= 50_000_000:
        s = 30
    elif position.size_usd >= 20_000_000:
        s = 22
    elif position.size_usd >= 10_000_000:
        s = 15
    else:
        s = 0
    score += s
    breakdown["size"] = s

    # Leverage Score (0-20)
    if 20 <= position.leverage <= 50:
        l = 20
    elif 10 <= position.leverage < 20:
        l = 14
    elif 5 <= position.leverage < 10:
        l = 10
    elif 2 <= position.leverage < 5:
        l = 8
    else:
        l = 2
    score += l
    breakdown["leverage"] = l

    # Wallet Novelty Score (0-20)
    if position.wallet_age_hours < 24:
        w = 20
    elif position.wallet_age_hours < 48:
        w = 15
    elif position.wallet_age_hours < 168:
        w = 8
    elif position.wallet_age_hours < 720:
        w = 3
    else:
        w = 0
    score += w
    breakdown["wallet_novelty"] = w

    # Concentration Score (0-15)
    if position.concentration >= 0.90:
        c = 15
    elif position.concentration >= 0.80:
        c = 10
    elif position.concentration >= 0.60:
        c = 5
    else:
        c = 0
    score += c
    breakdown["concentration"] = c

    # Cluster Score (0-15)
    if cluster_size >= 5:
        cl = 15
    elif cluster_size >= 3:
        cl = 10
    elif cluster_size >= 2:
        cl = 5
    else:
        cl = 0
    score += cl
    breakdown["cluster"] = cl

    # Trader type
    if score >= 75 and position.wallet_age_hours < 48 and position.concentration >= 0.80:
        trader_type = "A"
    elif score >= 50 and position.wallet_age_hours >= 168:
        trader_type = "B"
    elif score >= 40:
        trader_type = "C"
    else:
        trader_type = None

    return {"score": score, "breakdown": breakdown, "trader_type": trader_type}


def get_copy_leverage(whale_leverage: float) -> float:
    return min(whale_leverage / 2, MAX_LEVERAGE)


def get_position_size_usd(trader_type: str) -> float:
    if trader_type == "A":
        return TOTAL_CAPITAL_USD * TYPE_A_SIZE_PCT
    elif trader_type == "B":
        return TOTAL_CAPITAL_USD * TYPE_B_SIZE_PCT
    elif trader_type == "C":
        return TOTAL_CAPITAL_USD * TYPE_C_SIZE_PCT
    return 0


# ── Telegram ────────────────────────────────────────────────────────────────────

async def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            }, timeout=10.0)
        except Exception as e:
            log.error(f"Telegram send failed: {e}")


def format_single_alert(position: Position, ips: dict, pm_spike: bool = False) -> str:
    trader_type = ips["trader_type"]
    score = ips["score"]

    if trader_type == "A":
        header = "🚨 TYPE A — INSIDER PATTERN DETECTED"
    elif trader_type == "B":
        header = "🐋 TYPE B — SKILLED WHALE DETECTED"
    elif trader_type == "C":
        header = "👀 TYPE C — BIG MONEY DETECTED"
    else:
        return ""

    copy_size = get_position_size_usd(trader_type)
    copy_lev  = get_copy_leverage(position.leverage)
    pm_note   = "\n⚡️ <b>POLYMARKET SPIKE CONFIRMED</b>" if pm_spike else ""

    return (
        f"\n{header}\n\n"
        f"📊 <b>Asset:</b> {position.asset}\n"
        f"📍 <b>Direction:</b> {position.side_label}\n"
        f"💰 <b>Whale Size:</b> ${position.size_usd:,.0f}\n"
        f"⚡️ <b>Leverage:</b> {position.leverage:.0f}x\n"
        f"🎯 <b>Concentration:</b> {position.concentration:.0%}\n"
        f"🕐 <b>Wallet Age:</b> {position.wallet_age_hours:.1f}h\n"
        f"🔑 <b>Wallet:</b> {position.wallet[:8]}...\n\n"
        f"📈 <b>IPS Score:</b> {score}/100\n"
        f"   Size:{ips['breakdown']['size']} Lev:{ips['breakdown']['leverage']} "
        f"Novelty:{ips['breakdown']['wallet_novelty']} Conc:{ips['breakdown']['concentration']}\n\n"
        f"💼 <b>Suggested Copy:</b> ${copy_size:,.0f} @ {copy_lev:.0f}x\n"
        f"🔴 ALERTS ONLY MODE{pm_note}"
    )


def format_cluster_alert(cluster: Cluster, pm_spike: bool = False) -> str:
    avg_age = cluster.avg_wallet_age_hours

    if avg_age < 24 and cluster.size >= 4:
        header = "🚨🚨 TYPE A CLUSTER — HIGH CONFIDENCE INSIDER"
    elif avg_age < 48 and cluster.size >= 3:
        header = "🚨 CLUSTER — PROBABLE INSIDER"
    else:
        header = "⚠️ CLUSTER DETECTED — MONITOR"

    pm_note = "\n⚡️ <b>POLYMARKET SPIKE CONFIRMED</b>" if pm_spike else ""

    wallets = ""
    for i, p in enumerate(cluster.positions, 1):
        wallets += f"\n  [{i}] {p.wallet[:8]}... ${p.size_usd:,.0f} @ {p.leverage:.0f}x | Age:{p.wallet_age_hours:.1f}h"

    return (
        f"\n{header}\n\n"
        f"📊 <b>Asset:</b> {cluster.asset}\n"
        f"📍 <b>Direction:</b> {'LONG 📈' if cluster.side == 'B' else 'SHORT 📉'}\n"
        f"👥 <b>Wallets:</b> {cluster.size}\n"
        f"💰 <b>Total Exposure:</b> ${cluster.total_exposure_usd:,.0f}\n"
        f"🕐 <b>Avg Wallet Age:</b> {avg_age:.1f}h\n"
        f"📉 <b>Size Variance:</b> {cluster.size_variance:.1%}\n"
        f"⏱ <b>Window:</b> {(time.time() - cluster.first_seen) / 60:.1f} min\n"
        f"<b>Wallets:</b>{wallets}{pm_note}\n\n"
        f"🔴 ALERTS ONLY MODE"
    )


# ── Polymarket Check ────────────────────────────────────────────────────────────

async def check_polymarket_spike(asset: str) -> bool:
    geo_keywords = {
        "WTI":      ["iran", "oil", "ceasefire", "hormuz", "opec"],
        "WTIOIL":   ["iran", "oil", "ceasefire", "hormuz"],
        "BRENTOIL": ["iran", "oil", "ceasefire", "hormuz"],
        "XAU":      ["war", "iran", "conflict", "gold"],
        "GOLD":     ["war", "iran", "conflict"],
        "BTC":      ["bitcoin", "crypto", "fed", "rates"],
        "ETH":      ["ethereum", "crypto"],
    }
    keywords = geo_keywords.get(asset.upper(), [])
    if not keywords:
        return False

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                POLYMARKET_URL,
                params={"active": "true", "limit": 100, "_sort": "volume24hr", "_order": "desc"},
                timeout=10.0
            )
            resp.raise_for_status()
            markets = resp.json()

            for market in markets:
                question = market.get("question", "").lower()
                volume   = float(market.get("volume24hr", 0))
                prices   = market.get("outcomePrices", ["0.5"])
                prob     = float(prices[0]) if prices else 0.5

                if (any(kw in question for kw in keywords)
                        and prob < 0.20
                        and volume > 50_000):
                    log.info(f"Polymarket spike: {question[:60]} | Vol:${volume:,.0f} | Prob:{prob:.0%}")
                    return True
    except Exception as e:
        log.warning(f"Polymarket check failed: {e}")
    return False


# ── Hyperliquid API Helpers ─────────────────────────────────────────────────────

async def get_wallet_age_hours(wallet: str, client: httpx.AsyncClient) -> float:
    try:
        resp = await client.post(INFO_URL, json={
            "type": "userLedger",
            "user": wallet,
            "startTime": 0,
        }, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        if not data or not isinstance(data, list):
            return 0.0
        first_ts_ms = data[0].get("time", time.time() * 1000)
        return round((time.time() * 1000 - first_ts_ms) / 3_600_000, 2)
    except Exception as e:
        log.warning(f"Wallet age lookup failed {wallet[:8]}: {e}")
        return 9999.0


async def get_wallet_concentration(wallet: str, size_usd: float,
                                   client: httpx.AsyncClient) -> float:
    try:
        resp = await client.post(INFO_URL, json={
            "type": "clearinghouseState",
            "user": wallet,
        }, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        account_value = float(data.get("marginSummary", {}).get("accountValue", 0))
        if account_value <= 0:
            return 1.0
        return min(size_usd / account_value, 1.0)
    except Exception as e:
        log.warning(f"Concentration lookup failed {wallet[:8]}: {e}")
        return 0.0


# ── Cluster Engine ──────────────────────────────────────────────────────────────

class ClusterEngine:
    def __init__(self):
        self.clusters: dict         = defaultdict(lambda: Cluster(asset="", side=""))
        self.alerted_clusters: dict = {}
        self.alerted_singles: dict  = {}

    def _evict_stale(self, cluster: Cluster):
        cutoff = time.time() - CLUSTER_WINDOW_SECONDS
        cluster.positions = [p for p in cluster.positions if p.timestamp > cutoff]

    async def ingest(self, position: Position):
        if position.size_usd < MIN_POSITION_USD:
            return
        if position.leverage < MIN_LEVERAGE_TO_FLAG:
            return
        if position.asset not in GEO_WATCHLIST:
            return

        # Old wallet — score as single only
        if position.wallet_age_hours > MAX_WALLET_AGE_HOURS:
            await self._evaluate_single(position)
            return

        # Low concentration — score as single only
        if position.concentration < MIN_CONCENTRATION:
            await self._evaluate_single(position)
            return

        # Add to cluster
        key     = (position.asset, position.side)
        cluster = self.clusters[key]
        cluster.asset = position.asset
        cluster.side  = position.side

        existing = {p.wallet for p in cluster.positions}
        if position.wallet not in existing:
            cluster.positions.append(position)
            log.info(f"Cluster [{position.asset}/{position.side}]: {cluster.size} wallets | {position.wallet[:8]}... ${position.size_usd:,.0f}")

        self._evict_stale(cluster)
        await self._evaluate_cluster(key, cluster)
        await self._evaluate_single(position, cluster_size=cluster.size)

    async def _evaluate_single(self, position: Position, cluster_size: int = 1):
        ips = calculate_ips(position, cluster_size)
        if not ips["trader_type"]:
            return

        cooldown_key = f"{position.wallet}_{position.asset}"
        if time.time() - self.alerted_singles.get(cooldown_key, 0) < ALERT_COOLDOWN_SECONDS:
            return

        self.alerted_singles[cooldown_key] = time.time()
        pm_spike = await check_polymarket_spike(position.asset)
        alert    = format_single_alert(position, ips, pm_spike)
        if alert:
            log.info(f"Alert Type-{ips['trader_type']}: {position.asset} {position.side_label} ${position.size_usd:,.0f} IPS:{ips['score']}")
            await send_telegram(alert)

    async def _evaluate_cluster(self, key: tuple, cluster: Cluster):
        if cluster.size < MIN_CLUSTER_SIZE:
            return
        if cluster.size_variance > MAX_SIZE_VARIANCE:
            return
        if time.time() - self.alerted_clusters.get(key, 0) < ALERT_COOLDOWN_SECONDS:
            return

        self.alerted_clusters[key] = time.time()
        pm_spike = await check_polymarket_spike(cluster.asset)
        alert    = format_cluster_alert(cluster, pm_spike)
        log.info(f"Cluster alert: {cluster.asset} {cluster.size} wallets ${cluster.total_exposure_usd:,.0f}")
        await send_telegram(alert)


# ── WebSocket Parser ────────────────────────────────────────────────────────────

class HyperliquidFeedParser:
    def __init__(self, engine: ClusterEngine):
        self.engine       = engine
        self.http_client: Optional[httpx.AsyncClient] = None
        self._age_cache:  dict = {}
        self._cache_ttl   = 300

    async def _get_wallet_age_cached(self, wallet: str) -> float:
        now    = time.time()
        cached = self._age_cache.get(wallet)
        if cached and (now - cached[1]) < self._cache_ttl:
            return cached[0]
        age = await get_wallet_age_hours(wallet, self.http_client)
        self._age_cache[wallet] = (age, now)
        return age

    def _parse_fill(self, fill: dict) -> Optional[dict]:
        try:
            return {
                "wallet":   fill["user"],
                "asset":    fill["coin"].upper(),
                "side":     fill["side"],
                "size":     float(fill["sz"]),
                "price":    float(fill["px"]),
                "hash":     fill.get("hash", ""),
                "time":     fill.get("time", time.time() * 1000) / 1000,
                "leverage": float(fill.get("leverage", {}).get("value", 1)),
            }
        except (KeyError, ValueError, TypeError):
            return None

    async def _handle_message(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        if msg.get("channel") != "trades":
            return

        data  = msg.get("data", {})
        fills = data if isinstance(data, list) else [data]

        for fill in fills:
            parsed = self._parse_fill(fill)
            if not parsed:
                continue
            if parsed["asset"] not in GEO_WATCHLIST:
                continue

            size_usd = parsed["size"] * parsed["price"]
            if size_usd < MIN_POSITION_USD:
                continue

            wallet_age    = await self._get_wallet_age_cached(parsed["wallet"])
            concentration = await get_wallet_concentration(
                parsed["wallet"], size_usd, self.http_client
            )

            position = Position(
                wallet=parsed["wallet"],
                asset=parsed["asset"],
                side=parsed["side"],
                size_usd=size_usd,
                entry_price=parsed["price"],
                timestamp=parsed["time"],
                leverage=parsed["leverage"],
                wallet_age_hours=wallet_age,
                concentration=concentration,
                tx_hash=parsed["hash"],
            )

            await self.engine.ingest(position)

    async def run(self):
        self.http_client = httpx.AsyncClient()

        subscriptions = [
            {"method": "subscribe", "subscription": {"type": "trades", "coin": asset}}
            for asset in GEO_WATCHLIST
        ]

        log.info(f"Connecting to Hyperliquid WebSocket...")
        log.info(f"Watching: {', '.join(sorted(GEO_WATCHLIST))}")
        log.info(f"Mode: {'EXECUTION ENABLED' if EXECUTION_ENABLED else 'ALERTS ONLY'}")

        await send_telegram(
            "🤖 <b>Whale Bot Online</b>\n\n"
            f"Watching: {', '.join(sorted(GEO_WATCHLIST))}\n"
            f"Min position: ${MIN_POSITION_USD:,.0f}\n"
            f"Capital: ${TOTAL_CAPITAL_USD:,}\n"
            f"Mode: {'🟢 EXECUTION' if EXECUTION_ENABLED else '🔴 ALERTS ONLY'}"
        )

        while True:
            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=20,
                    ping_timeout=30
                ) as ws:
                    log.info("Connected. Subscribing to feeds...")
                    for sub in subscriptions:
                        await ws.send(json.dumps(sub))
                    log.info(f"Live. Monitoring {len(subscriptions)} asset feeds.")

                    async for raw_message in ws:
                        await self._handle_message(raw_message)

            except websockets.exceptions.ConnectionClosed as e:
                log.warning(f"WebSocket disconnected: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                log.error(f"Unexpected error: {e}. Reconnecting in 10s...")
                await asyncio.sleep(10)


# ── Entry Point ─────────────────────────────────────────────────────────────────

async def main():
    log.info("=" * 55)
    log.info("  Whale & Insider Detection Bot")
    log.info(f"  Capital: ${TOTAL_CAPITAL_USD:,}")
    log.info(f"  A(Insider):{TYPE_A_SIZE_PCT:.0%} B(Skilled):{TYPE_B_SIZE_PCT:.0%} C(Big):{TYPE_C_SIZE_PCT:.0%}")
    log.info("=" * 55)

    engine = ClusterEngine()
    parser = HyperliquidFeedParser(engine=engine)
    await parser.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")

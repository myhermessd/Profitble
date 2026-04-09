# 🐋 Whale & Insider Detection Bot

Monitors Hyperliquid and Polymarket in real time. Detects large coordinated positions and sends Telegram alerts. No execution — alerts only.

---

## How It Works

Connects to Hyperliquid's WebSocket feed and watches for large trades on BTC, ETH, SOL, and BNB. Each trade is scored using an **Insider Probability Score (IPS)** based on:

- Position size
- Leverage
- Wallet age
- Capital concentration
- Cluster behavior (multiple wallets moving together)

If a Polymarket market shows unusual activity on the same asset, that's flagged too.

**Three alert types:**
- 🚨 **Type A — Insider Pattern** — fresh wallet, high concentration, high score
- 🐋 **Type B — Skilled Whale** — experienced wallet, large coordinated move
- 👀 **Type C — Big Money** — large position, lower confidence

---

## Setup

### 1. Clone & push to GitHub
```bash
git clone <your-repo>
cd <your-repo>
```

### 2. Deploy on Railway
- Create a new project on [Railway](https://railway.app)
- Connect your GitHub repo
- Railway will auto-detect Python and install dependencies

### 3. Set environment variables in Railway

Go to your service → **Variables** tab and add:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | The chat or group ID to send alerts to |

---

## Files

| File | Purpose |
|---|---|
| `detector.py` | Main bot logic |
| `requirements.txt` | Python dependencies |
| `railway.toml` | Railway deploy config |

---

## Detection Thresholds

| Setting | Value |
|---|---|
| Minimum position size | $10,000,000 |
| Minimum leverage | 5x |
| Fresh wallet threshold | < 48 hours |
| Minimum concentration | 80% of capital in one trade |
| Cluster window | 60 minutes |
| Cluster min size | 3 wallets |

---

## Dependencies

```
websockets==12.0
httpx==0.27.0
```

---

## Notes

- Execution is permanently disabled in this build
- If Telegram env vars are not set, alerts print to console only
- Bot auto-reconnects on WebSocket disconnect

# Chainlink Source Confirmation

The default PolyEdge BTC 15-minute Up/Down target resolves from Chainlink Data
Streams, not Binance, Coinbase, or the older push-based Chainlink on-chain
BTC/USD price feed. For this default target, the bot uses Polymarket RTDS
Chainlink `btc/usd` as the primary
free public source.

## Free RTDS Source

Polymarket documents a no-auth Real-Time Data Socket for crypto prices:

```text
wss://ws-live-data.polymarket.com
```

Subscribe to Chainlink BTC/USD:

```json
{
  "action": "subscribe",
  "subscriptions": [
    {
      "topic": "crypto_prices_chainlink",
      "type": "*",
      "filters": "{\"symbol\":\"btc/usd\"}"
    }
  ]
}
```

The bot treats this stream as the primary reference price for paper trading and
gated live decisions. Binance/Coinbase feeds are used only as cross-checks.

## Public Source

Current Polymarket BTC 15m market descriptions for the default target say:

```text
The resolution source for this market is information from Chainlink,
specifically the BTC/USD data stream available at
https://data.chain.link/streams/btc-usd.
```

The public Chainlink page identifies the stream as:

```text
URL: https://data.chain.link/streams/btc-usd
Redirect/details URL: https://data.chain.link/streams/btc-usd-cexprice-streams
Product: BTC / USD
Product name: BTC/USD-RefPrice-DS-Premium-Global-003
Base asset: BTC_CR
Quote asset: USD_FX
Market hours: 24/7/365
Public shortened feed ID: 0x0003...75b8
```

The full feed ID is required for authenticated Data Streams API calls. The
public page and public search snippets expose only the shortened form.

## Authenticated Confirmation

Direct Chainlink Data Streams access is still useful later. Chainlink Data
Streams REST mainnet endpoint:

```text
https://api.dataengine.chain.link
```

Latest report endpoint:

```text
GET /api/v1/reports/latest?feedID=<FULL_FEED_ID>
```

Authentication requires:

```text
Authorization: <API key UUID>
X-Authorization-Timestamp: <milliseconds since Unix epoch>
X-Authorization-Signature-SHA256: <HMAC-SHA256 signature>
```

The string to sign is:

```text
METHOD FULL_PATH BODY_HASH API_KEY TIMESTAMP
```

For a GET request, `BODY_HASH` is the SHA-256 hash of the empty body.

## Repo Command

Run:

```bash
source .venv/bin/activate
polyedge confirm-source
```

Without Chainlink credentials, the command verifies the Polymarket market text
and reports that authenticated report checking was skipped. With credentials and
full feed ID configured, it calls `/api/v1/reports/latest` and confirms:

```text
report.feedID matches CHAINLINK_DATA_STREAMS_FEED_ID
observationsTimestamp is current
fullReport exists
```

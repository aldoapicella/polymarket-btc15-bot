# Postman API Access

Import these two files into Postman:

```text
postman/polymarket-btc15-bot.postman_collection.json
postman/polymarket-btc15-bot.postman_environment.json
```

Select the `Polymarket BTC 15m Bot - Azure` environment before sending
requests.

## Token Setup

The API bearer token is stored locally in:

```text
data/api-bearer-token.txt
```

This file is ignored by git and must not be committed, pasted into chat, or
stored in the Postman collection.

Copy it to your clipboard without printing it:

```bash
./scripts/copy-api-token-to-clipboard.sh
```

Then in Postman:

```text
Environment -> Polymarket BTC 15m Bot - Azure
api_bearer_token -> Current value -> paste
```

Keep the token in the environment current value, not in the collection. Do not
export/share a Postman environment after filling in the current token value.

## Live Base URL

```text
https://polymarket-btc15-dev.calmground-23567c32.eastus.azurecontainerapps.io
```

The collection uses bearer auth at the collection level:

```text
Authorization: Bearer {{api_bearer_token}}
```

## Endpoints

```text
GET  /health
GET  /status
POST /discover
POST /confirm-source
POST /evaluate?execute=false
POST /evaluate?execute=true
POST /kill-switch
GET  /openapi.json
```

`/openapi.json` is FastAPI's generated schema route. The operational routes
require the bearer token.

## Quick Check

After setting `api_bearer_token`, run `Health` in Postman. Expected result:

```json
{
  "ok": true,
  "execution_mode": "paper",
  "kill_switch": false
}
```

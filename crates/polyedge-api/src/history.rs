use chrono::{DateTime, Utc};
use polyedge_config::RuntimeSettings;
use polyedge_storage::AzureTableClient;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::env;

const HISTORICAL_MARKET_MAX: usize = 1_000;
const CHART_POINT_MAX: usize = 5_000;

pub struct MarketHistoryStore {
    settings: RuntimeSettings,
}

impl MarketHistoryStore {
    pub fn new(settings: &RuntimeSettings) -> Self {
        Self {
            settings: settings.clone(),
        }
    }

    pub async fn markets(&self, limit: usize) -> Result<Vec<Value>, String> {
        let settings = self.settings.clone();
        let limit = limit.min(HISTORICAL_MARKET_MAX);
        tokio::task::spawn_blocking(move || {
            let Some(mut client) = table_client(&settings) else {
                return Ok(Vec::new());
            };
            let entities = client
                .query_entities(
                    &settings.azure.market_table_name,
                    Some("PartitionKey eq 'market'"),
                    HISTORICAL_MARKET_MAX,
                )
                .map_err(|error| error.to_string())?;
            let mut markets = entities
                .iter()
                .filter_map(market_from_catalog_entity)
                .collect::<Vec<_>>();
            sort_markets_newest_first(&mut markets);
            markets.truncate(limit);
            Ok(markets)
        })
        .await
        .map_err(|error| error.to_string())?
    }

    pub async fn market(&self, market_id: &str) -> Result<Option<Value>, String> {
        let settings = self.settings.clone();
        let market_id = market_id.to_owned();
        tokio::task::spawn_blocking(move || {
            let Some(entity) = load_catalog_entity_sync(&settings, &market_id)? else {
                return Ok(None);
            };
            Ok(market_from_catalog_entity(&entity))
        })
        .await
        .map_err(|error| error.to_string())?
    }

    pub async fn chart(&self, market_id: &str, range: &str) -> Result<Option<Value>, String> {
        let settings = self.settings.clone();
        let market_id = market_id.to_owned();
        let range = range.to_owned();
        tokio::task::spawn_blocking(move || {
            let Some(mut client) = table_client(&settings) else {
                return Ok(None);
            };
            let Some(entity) = load_catalog_entity_with_client(&settings, &mut client, &market_id)?
            else {
                return Ok(None);
            };
            let Some(partition_key) = value_text(&entity, "RowKey").map(str::to_owned) else {
                return Ok(None);
            };
            let filter = format!("PartitionKey eq '{}'", odata_string(&partition_key));
            let entities = client
                .query_entities(
                    &settings.azure.chart_table_name,
                    Some(&filter),
                    CHART_POINT_MAX,
                )
                .map_err(|error| error.to_string())?;
            let mut points = entities
                .iter()
                .filter_map(chart_point_from_entity)
                .collect::<Vec<_>>();
            points.sort_by_key(|point| point_bucket(point).unwrap_or(0));
            let stored_count = points.len();
            filter_chart_range(&mut points, &range);
            let market = market_from_catalog_entity(&entity);
            let domain = market
                .as_ref()
                .and_then(market_domain)
                .or_else(|| point_domain(&points));
            Ok(Some(json!({
                "source": "azure_table",
                "market_id": market_id,
                "range": range,
                "points": points,
                "domain": domain,
                "summary": {
                    "sample_count": stored_count,
                    "catalog_row_key": partition_key
                }
            })))
        })
        .await
        .map_err(|error| error.to_string())?
    }
}

pub fn max_historical_markets() -> usize {
    HISTORICAL_MARKET_MAX
}

pub fn merge_market_lists(
    live_markets: Vec<Value>,
    historical_markets: Vec<Value>,
    limit: usize,
) -> Vec<Value> {
    let mut merged = BTreeMap::new();
    for market in historical_markets {
        if let Some(market_id) = value_text(&market, "market_id") {
            merged.insert(market_id.to_owned(), market);
        }
    }
    for market in live_markets {
        let Some(market_id) = value_text(&market, "market_id").map(str::to_owned) else {
            continue;
        };
        let market = match merged.remove(&market_id) {
            Some(historical) => merge_market_payloads(historical, market),
            None => market,
        };
        merged.insert(market_id, market);
    }
    let mut markets = merged.into_values().collect::<Vec<_>>();
    sort_markets_newest_first(&mut markets);
    markets.truncate(limit);
    markets
}

pub fn overlay_detail_market(mut detail: Value, historical_market: Value) -> Value {
    let Some(object) = detail.as_object_mut() else {
        return detail;
    };
    let live_market = object.get("market").cloned().unwrap_or(Value::Null);
    let merged_market = merge_market_payloads(historical_market, live_market);
    object.insert("market".to_owned(), merged_market.clone());
    if !has_non_null(object, "fair_value") {
        if let Some(fair_value) = merged_market.get("fair_value") {
            object.insert("fair_value".to_owned(), fair_value.clone());
        }
    }
    detail
}

pub fn historical_detail(market: Value) -> Value {
    let fair_value = market.get("fair_value").cloned().unwrap_or(Value::Null);
    json!({
        "market": market,
        "fair_value": fair_value,
        "books": {
            "up": Value::Null,
            "down": Value::Null
        },
        "decisions": [],
        "execution_reports": []
    })
}

pub fn empty_chart(market_id: &str, range: &str) -> Value {
    json!({
        "market_id": market_id,
        "range": range,
        "points": [],
        "summary": {
            "sample_count": 0
        }
    })
}

fn load_catalog_entity_sync(
    settings: &RuntimeSettings,
    market_id: &str,
) -> Result<Option<Value>, String> {
    let Some(mut client) = table_client(settings) else {
        return Ok(None);
    };
    load_catalog_entity_with_client(settings, &mut client, market_id)
}

fn load_catalog_entity_with_client(
    settings: &RuntimeSettings,
    client: &mut AzureTableClient,
    market_id: &str,
) -> Result<Option<Value>, String> {
    let filter = format!(
        "PartitionKey eq 'market' and marketId eq '{}'",
        odata_string(market_id)
    );
    let mut entities = client
        .query_entities(&settings.azure.market_table_name, Some(&filter), 5)
        .map_err(|error| error.to_string())?;
    entities.sort_by_key(|entity| {
        std::cmp::Reverse(
            value_ts_ms(entity, "Timestamp")
                .or_else(|| value_ts_ms(entity, "startTs"))
                .unwrap_or(0),
        )
    });
    Ok(entities.into_iter().next())
}

fn table_client(settings: &RuntimeSettings) -> Option<AzureTableClient> {
    settings
        .azure
        .storage_account_name
        .as_ref()
        .map(|account| AzureTableClient::new(account, env::var("AZURE_CLIENT_ID").ok()))
}

fn market_from_catalog_entity(entity: &Value) -> Option<Value> {
    let mut market: Value =
        value_text(entity, "payloadJson").and_then(|text| serde_json::from_str(text).ok())?;
    let market_id = value_text(entity, "marketId").map(str::to_owned);
    if let Some(object) = market.as_object_mut() {
        if !has_non_null(object, "market_id") {
            if let Some(market_id) = &market_id {
                object.insert("market_id".to_owned(), json!(market_id));
            }
        }
        overlay_text(object, entity, "start_ts", "startTs");
        overlay_text(object, entity, "end_ts", "endTs");
        if object.get("start_price").is_none_or(Value::is_null) {
            if let Some(start_price) = value_text(entity, "chartStartPrice") {
                object.insert("start_price".to_owned(), json!(start_price));
            }
        }
        if let Some(chart_summary) = chart_summary_from_entity(entity) {
            object.insert("chart_summary".to_owned(), chart_summary);
        }
        if object.get("fair_value").is_none_or(Value::is_null) {
            if let (Some(q_up), Some(q_down)) = (
                entity_number_or_text(entity, "latestQUp"),
                entity_number_or_text(entity, "latestQDown"),
            ) {
                object.insert(
                    "fair_value".to_owned(),
                    json!({
                        "market_id": market_id,
                        "q_up": q_up,
                        "q_down": q_down,
                        "computed_ts": value_text(entity, "latestFairValueTs")
                    }),
                );
            }
        }
        normalize_historical_market_state(object);
    }
    Some(market)
}

fn chart_summary_from_entity(entity: &Value) -> Option<Value> {
    let mut summary = serde_json::Map::new();
    if let Some(market_id) = value_text(entity, "marketId") {
        summary.insert("market_id".to_owned(), json!(market_id));
    }
    if let Some(sample_count) = entity_number(entity, "chartSampleCount") {
        summary.insert("sample_count".to_owned(), json!(sample_count as i64));
    }
    if let Some(ts) = value_text(entity, "chartFirstSampleTs") {
        summary.insert("first_sample_ts".to_owned(), json!(ts));
    }
    if let Some(ts) = value_text(entity, "chartLastSampleTs") {
        summary.insert("last_sample_ts".to_owned(), json!(ts));
    }
    if let Some(start_price) = value_text(entity, "chartStartPrice") {
        summary.insert("start_price".to_owned(), json!(start_price));
    }
    if let Some(q_up) = entity_number_or_text(entity, "latestQUp") {
        summary.insert("q_up".to_owned(), json!(q_up));
    }
    if let Some(q_down) = entity_number_or_text(entity, "latestQDown") {
        summary.insert("q_down".to_owned(), json!(q_down));
    }
    if let Some(ts) = value_text(entity, "latestFairValueTs") {
        summary.insert("fair_value_ts".to_owned(), json!(ts));
    }
    (!summary.is_empty()).then_some(Value::Object(summary))
}

fn chart_point_from_entity(entity: &Value) -> Option<Value> {
    let bucket = entity_number(entity, "bucket")
        .map(|value| value as i64)
        .or_else(|| value_ts_ms(entity, "bucketTs"))?;
    let mut point = serde_json::Map::new();
    point.insert("bucket".to_owned(), json!(bucket));
    point.insert(
        "time".to_owned(),
        json!(value_text(entity, "bucketTs").unwrap_or_default()),
    );
    for (target, source) in [
        ("qUp", "qUp"),
        ("qDown", "qDown"),
        ("upBid", "upBid"),
        ("upAsk", "upAsk"),
        ("downBid", "downBid"),
        ("downAsk", "downAsk"),
        ("distanceBps", "distanceBps"),
        ("referencePrice", "referencePrice"),
        ("fillPrice", "fillPrice"),
        ("fillSize", "fillSize"),
    ] {
        if let Some(value) = entity_number(entity, source) {
            point.insert(target.to_owned(), json!(value));
        }
    }
    if let Some(outcome) = value_text(entity, "fillOutcome") {
        point.insert("fillOutcome".to_owned(), json!(outcome));
    }
    Some(Value::Object(point))
}

fn merge_market_payloads(historical: Value, live: Value) -> Value {
    let (Some(historical), Some(live)) = (historical.as_object(), live.as_object()) else {
        return live;
    };
    let mut merged = historical.clone();
    for (key, value) in live {
        merged.insert(key.clone(), value.clone());
    }
    for key in ["chart_summary", "fair_value", "start_price"] {
        if !has_non_null(&merged, key) {
            if let Some(value) = historical.get(key) {
                merged.insert(key.to_owned(), value.clone());
            }
        }
    }
    Value::Object(merged)
}

fn normalize_historical_market_state(object: &mut serde_json::Map<String, Value>) {
    let Some(end_ts) = object
        .get("end_ts")
        .and_then(Value::as_str)
        .and_then(parse_ts)
    else {
        return;
    };
    if end_ts < Utc::now() {
        object.insert("is_active".to_owned(), json!(false));
        object.insert("is_tradeable".to_owned(), json!(false));
        object.insert("status".to_owned(), json!("closed"));
    }
}

fn overlay_text(
    object: &mut serde_json::Map<String, Value>,
    entity: &Value,
    target: &str,
    source: &str,
) {
    if !has_non_null(object, target) {
        if let Some(value) = value_text(entity, source) {
            object.insert(target.to_owned(), json!(value));
        }
    }
}

fn filter_chart_range(points: &mut Vec<Value>, range: &str) {
    let Some(window_ms) = chart_window_ms(range) else {
        return;
    };
    let Some(last_bucket) = points.iter().filter_map(point_bucket).max() else {
        return;
    };
    let cutoff = last_bucket - window_ms;
    points.retain(|point| point_bucket(point).is_some_and(|bucket| bucket >= cutoff));
}

fn chart_window_ms(range: &str) -> Option<i64> {
    match range {
        "1m" => Some(60_000),
        "5m" => Some(5 * 60_000),
        _ => None,
    }
}

fn market_domain(market: &Value) -> Option<Value> {
    let start = value_ts_ms(market, "start_ts")?;
    let end = value_ts_ms(market, "end_ts")?;
    (end > start).then_some(json!([start, end]))
}

fn point_domain(points: &[Value]) -> Option<Value> {
    let mut buckets = points.iter().filter_map(point_bucket);
    let first = buckets.next()?;
    let (min, max) = buckets.fold((first, first), |(min, max), bucket| {
        (min.min(bucket), max.max(bucket))
    });
    Some(json!([
        min,
        if max > min { max } else { min + 15 * 60_000 }
    ]))
}

fn point_bucket(point: &Value) -> Option<i64> {
    point.get("bucket").and_then(|value| match value {
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|value| value as i64)),
        Value::String(text) => text.parse().ok(),
        _ => None,
    })
}

fn value_ts_ms(value: &Value, key: &str) -> Option<i64> {
    value
        .get(key)
        .and_then(Value::as_str)
        .and_then(parse_ts)
        .map(|ts| ts.timestamp_millis())
}

fn parse_ts(value: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|ts| ts.with_timezone(&Utc))
}

fn value_text<'a>(value: &'a Value, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}

fn entity_number(value: &Value, key: &str) -> Option<f64> {
    value.get(key).and_then(|value| match value {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.parse().ok(),
        _ => None,
    })
}

fn entity_number_or_text(value: &Value, key: &str) -> Option<Value> {
    value.get(key).and_then(|value| match value {
        Value::Number(_) => Some(value.clone()),
        Value::String(text) if !text.is_empty() => Some(json!(text)),
        _ => None,
    })
}

fn has_non_null(object: &serde_json::Map<String, Value>, key: &str) -> bool {
    object.get(key).is_some_and(|value| !value.is_null())
}

fn sort_markets_newest_first(markets: &mut [Value]) {
    markets.sort_by_key(|market| std::cmp::Reverse(value_ts_ms(market, "start_ts").unwrap_or(0)));
}

fn odata_string(value: &str) -> String {
    value.replace('\'', "''")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merges_live_market_without_losing_historical_chart_summary() {
        let live = json!({
            "market_id": "m1",
            "question": "live",
            "start_ts": "2026-06-11T10:00:00Z"
        });
        let historical = json!({
            "market_id": "m1",
            "question": "old",
            "start_ts": "2026-06-11T10:00:00Z",
            "chart_summary": {"sample_count": 25},
            "fair_value": {"q_up": "0.51"}
        });

        let markets = merge_market_lists(vec![live], vec![historical], 10);

        assert_eq!(markets.len(), 1);
        assert_eq!(markets[0]["question"], "live");
        assert_eq!(markets[0]["chart_summary"]["sample_count"], 25);
        assert_eq!(markets[0]["fair_value"]["q_up"], "0.51");
    }

    #[test]
    fn chart_entity_numbers_become_numeric_points() {
        let entity = json!({
            "bucket": "1781172000000",
            "bucketTs": "2026-06-11T10:00:00Z",
            "qUp": "0.52",
            "upBid": 0.51,
            "fillOutcome": "up"
        });

        let point = chart_point_from_entity(&entity).expect("chart point");

        assert_eq!(point["bucket"], 1781172000000_i64);
        assert_eq!(point["qUp"], 0.52);
        assert_eq!(point["upBid"], 0.51);
        assert_eq!(point["fillOutcome"], "up");
    }
}

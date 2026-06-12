use chrono::{SecondsFormat, Utc};
use polyedge_config::RuntimeSettings;
use polyedge_domain::MarketSpec;
use serde_json::{json, Map, Value};
use std::thread;
use tracing::warn;

const CHART_PERSIST_INTERVAL_MS: i64 = 1_000;

#[derive(Clone)]
pub(super) struct ChartPersistenceSample {
    market: MarketSpec,
    point: Value,
    sample_count: usize,
}

impl ChartPersistenceSample {
    pub(super) fn new(market: MarketSpec, point: Value, sample_count: usize) -> Self {
        Self {
            market,
            point,
            sample_count,
        }
    }
}

pub(super) fn should_persist(last_bucket_ms: Option<i64>, bucket_ms: i64) -> bool {
    last_bucket_ms.is_none_or(|last| bucket_ms.saturating_sub(last) >= CHART_PERSIST_INTERVAL_MS)
}

pub(super) fn spawn_persist(settings: RuntimeSettings, sample: ChartPersistenceSample) {
    thread::spawn(move || {
        if let Err(error) = persist_sample(&settings, &sample) {
            warn!("chart history persistence failed: {error}");
        }
    });
}

fn persist_sample(
    settings: &RuntimeSettings,
    sample: &ChartPersistenceSample,
) -> Result<(), String> {
    let Some(mut client) = crate::history::table_client(settings) else {
        return Ok(());
    };
    let catalog = catalog_entity(sample)?;
    let chart = chart_entity(sample)?;
    client
        .insert_or_merge_entity(&settings.azure.market_table_name, &catalog)
        .map_err(|error| error.to_string())?;
    client
        .insert_or_merge_entity(&settings.azure.chart_table_name, &chart)
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn catalog_entity(sample: &ChartPersistenceSample) -> Result<Value, String> {
    let market = &sample.market;
    let sample_time = point_time(&sample.point)
        .unwrap_or_else(|| Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true));
    let mut entity = Map::new();
    entity.insert("PartitionKey".to_owned(), json!("market"));
    entity.insert("RowKey".to_owned(), json!(market.market_id.to_string()));
    entity.insert("marketId".to_owned(), json!(market.market_id.to_string()));
    entity.insert("question".to_owned(), json!(market.question));
    entity.insert(
        "startTs".to_owned(),
        json!(market.start_ts.to_rfc3339_opts(SecondsFormat::Secs, true)),
    );
    entity.insert(
        "endTs".to_owned(),
        json!(market.end_ts.to_rfc3339_opts(SecondsFormat::Secs, true)),
    );
    entity.insert(
        "payloadJson".to_owned(),
        json!(serde_json::to_string(market).map_err(|error| error.to_string())?),
    );
    entity.insert("chartLastSampleTs".to_owned(), json!(sample_time));
    entity.insert(
        "chartSampleCount".to_owned(),
        json!(sample.sample_count.to_string()),
    );
    if let Some(start_price) = market.start_price {
        entity.insert("chartStartPrice".to_owned(), json!(start_price.to_string()));
    }
    if sample.sample_count <= 1 {
        entity.insert(
            "chartFirstSampleTs".to_owned(),
            json!(point_time(&sample.point)
                .unwrap_or_else(|| Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true))),
        );
    }
    copy_number_as_string(&sample.point, &mut entity, "latestQUp", "qUp");
    copy_number_as_string(&sample.point, &mut entity, "latestQDown", "qDown");
    if let Some(time) = point_time(&sample.point) {
        entity.insert("latestFairValueTs".to_owned(), json!(time));
    }
    Ok(Value::Object(entity))
}

fn chart_entity(sample: &ChartPersistenceSample) -> Result<Value, String> {
    let bucket =
        point_bucket_ms(&sample.point).ok_or_else(|| "chart sample missing bucket".to_owned())?;
    let bucket_ts = point_time(&sample.point)
        .unwrap_or_else(|| Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true));
    let mut entity = Map::new();
    entity.insert(
        "PartitionKey".to_owned(),
        json!(sample.market.market_id.to_string()),
    );
    entity.insert("RowKey".to_owned(), json!(format!("{bucket:019}")));
    entity.insert(
        "marketId".to_owned(),
        json!(sample.market.market_id.to_string()),
    );
    entity.insert("bucket".to_owned(), json!(bucket.to_string()));
    entity.insert("bucketTs".to_owned(), json!(bucket_ts));
    for field in [
        "qUp",
        "qDown",
        "upBid",
        "upAsk",
        "downBid",
        "downAsk",
        "distanceBps",
        "referencePrice",
        "fillPrice",
        "fillSize",
    ] {
        copy_number_as_string(&sample.point, &mut entity, field, field);
    }
    if let Some(outcome) = sample.point.get("fillOutcome").and_then(Value::as_str) {
        entity.insert("fillOutcome".to_owned(), json!(outcome));
    }
    Ok(Value::Object(entity))
}

pub(super) fn point_bucket_ms(point: &Value) -> Option<i64> {
    point.get("bucket").and_then(|value| match value {
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|value| value as i64)),
        Value::String(text) => text.parse().ok(),
        _ => None,
    })
}

fn point_time(point: &Value) -> Option<String> {
    point.get("time").and_then(Value::as_str).map(str::to_owned)
}

fn copy_number_as_string(
    point: &Value,
    entity: &mut Map<String, Value>,
    target: &str,
    source: &str,
) {
    let value = point.get(source).and_then(|value| match value {
        Value::Number(number) => number.as_f64().map(|value| value.to_string()),
        Value::String(text) if text.parse::<f64>().is_ok() => Some(text.clone()),
        _ => None,
    });
    if let Some(value) = value {
        entity.insert(target.to_owned(), json!(value));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;
    use polyedge_domain::{ConditionId, MarketId, TokenId};

    fn market() -> MarketSpec {
        MarketSpec {
            asset: "BTC".to_owned(),
            horizon: "15m".to_owned(),
            event_id: None,
            event_slug: None,
            market_id: MarketId::new("m1"),
            market_slug: None,
            condition_id: ConditionId::new("c1"),
            question: "BTC up?".to_owned(),
            description: None,
            up_token_id: TokenId::new("up"),
            down_token_id: TokenId::new("down"),
            start_ts: Utc.timestamp_opt(1_781_172_000, 0).unwrap(),
            end_ts: Utc.timestamp_opt(1_781_172_900, 0).unwrap(),
            start_price: None,
            resolution_source: "chainlink_reference".to_owned(),
            tick_size: rust_decimal::Decimal::new(1, 2),
            minimum_order_size: rust_decimal::Decimal::from(5),
            neg_risk: false,
            fees_enabled: true,
            accepting_orders: true,
            status: polyedge_domain::MarketStatus::Tradeable,
            raw: Default::default(),
        }
    }

    #[test]
    fn chart_row_uses_market_partition_and_padded_bucket() {
        let sample = ChartPersistenceSample::new(
            market(),
            json!({
                "bucket": 1781172000123_i64,
                "time": "2026-06-11T10:00:00.123Z",
                "qUp": 0.52
            }),
            7,
        );

        let entity = chart_entity(&sample).expect("chart entity");

        assert_eq!(entity["PartitionKey"], "m1");
        assert_eq!(entity["RowKey"], "0000001781172000123");
        assert_eq!(entity["qUp"], "0.52");
    }

    #[test]
    fn persistence_throttle_keeps_one_second_spacing() {
        assert!(should_persist(None, 1_000));
        assert!(!should_persist(Some(1_000), 1_999));
        assert!(should_persist(Some(1_000), 2_000));
    }
}

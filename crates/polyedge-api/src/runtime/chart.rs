use super::RuntimeData;
use chrono::{DateTime, SecondsFormat, Utc};
use polyedge_domain::MarketSpec;
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use serde_json::{json, Value};

pub(super) fn chart_sample_from_data(
    market: &MarketSpec,
    data: &RuntimeData,
    now: DateTime<Utc>,
) -> Value {
    let mut point = serde_json::Map::new();
    point.insert("bucket".to_owned(), json!(now.timestamp_millis()));
    point.insert(
        "time".to_owned(),
        json!(now.to_rfc3339_opts(SecondsFormat::Millis, true)),
    );
    if let Some(fair_value) = data.fair_values.get(&market.market_id) {
        insert_number_from_value(&mut point, "qUp", fair_value.get("q_up"));
        insert_number_from_value(&mut point, "qDown", fair_value.get("q_down"));
    }
    if let Some(book) = data.books.get(&market.up_token_id) {
        insert_decimal(
            &mut point,
            "upBid",
            book.best_bid().map(|level| level.price),
        );
        insert_decimal(
            &mut point,
            "upAsk",
            book.best_ask().map(|level| level.price),
        );
    }
    if let Some(book) = data.books.get(&market.down_token_id) {
        insert_decimal(
            &mut point,
            "downBid",
            book.best_bid().map(|level| level.price),
        );
        insert_decimal(
            &mut point,
            "downAsk",
            book.best_ask().map(|level| level.price),
        );
    }
    if let Some(reference) = &data.reference {
        insert_decimal(&mut point, "referencePrice", Some(reference.price));
        if let Some(start_price) = market.start_price {
            if start_price > Decimal::ZERO {
                let distance =
                    ((reference.price - start_price) / start_price) * Decimal::from(10_000);
                insert_decimal(&mut point, "distanceBps", Some(distance));
            }
        }
    }
    for report in data.execution_reports.iter().rev() {
        if report.market_id != market.market_id || report.filled_size <= Decimal::ZERO {
            continue;
        }
        let age_ms = now
            .signed_duration_since(report.local_ts)
            .num_milliseconds()
            .abs();
        if age_ms > 1_500 {
            break;
        }
        insert_decimal(&mut point, "fillPrice", report.avg_price);
        insert_decimal(&mut point, "fillSize", Some(report.filled_size));
        if let Some(outcome) = report.raw.get("outcome").and_then(Value::as_str) {
            point.insert("fillOutcome".to_owned(), json!(outcome));
        }
        break;
    }
    Value::Object(point)
}

fn insert_number_from_value(
    point: &mut serde_json::Map<String, Value>,
    key: &str,
    value: Option<&Value>,
) {
    let number = match value {
        Some(Value::Number(number)) => number.as_f64(),
        Some(Value::String(text)) => text.parse::<f64>().ok(),
        _ => None,
    };
    if let Some(number) = number.filter(|value| value.is_finite()) {
        point.insert(key.to_owned(), json!(number));
    }
}

fn insert_decimal(point: &mut serde_json::Map<String, Value>, key: &str, value: Option<Decimal>) {
    if let Some(number) = value.and_then(|value| value.to_f64()) {
        if number.is_finite() {
            point.insert(key.to_owned(), json!(number));
        }
    }
}

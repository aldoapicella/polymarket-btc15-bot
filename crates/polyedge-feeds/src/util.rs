use crate::FeedError;
use chrono::{DateTime, TimeZone, Utc};
use polyedge_domain::BookLevel;
use rust_decimal::Decimal;
use serde_json::Value;
use std::io::Read;
use tokio_tungstenite::tungstenite::Message;
use url::Url;

pub(crate) fn parse_datetime(value: Option<&Value>) -> Option<DateTime<Utc>> {
    match value? {
        Value::Number(number) => number
            .as_f64()
            .and_then(|value| Utc.timestamp_opt(value as i64, 0).single()),
        Value::String(text) => parse_datetime_text(text),
        _ => None,
    }
}

fn parse_datetime_text(text: &str) -> Option<DateTime<Utc>> {
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return None;
    }
    DateTime::parse_from_rfc3339(trimmed)
        .map(|value| value.with_timezone(&Utc))
        .ok()
        .or_else(|| {
            chrono::NaiveDateTime::parse_from_str(trimmed, "%Y-%m-%dT%H:%M:%S")
                .ok()
                .map(|value| value.and_utc())
        })
}

pub(crate) fn parse_ms_timestamp(value: Option<&Value>) -> Option<DateTime<Utc>> {
    let raw = match value? {
        Value::Number(number) => number.as_f64()?,
        Value::String(text) => text.parse::<f64>().ok()?,
        _ => return None,
    };
    let seconds = if raw > 10_000_000_000.0 {
        raw / 1000.0
    } else {
        raw
    };
    Utc.timestamp_opt(seconds as i64, 0).single()
}

pub(crate) fn parse_event_ts(value: Option<&Value>) -> Option<DateTime<Utc>> {
    parse_ms_timestamp(value).or_else(|| parse_datetime(value))
}

pub(crate) fn levels(value: Option<&Value>) -> Vec<BookLevel> {
    let Some(items) = value.and_then(Value::as_array) else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(|item| {
            Some(BookLevel {
                price: decimal(item.get("price"))?,
                size: decimal(item.get("size"))?,
            })
        })
        .collect()
}

pub(crate) fn decimal(value: Option<&Value>) -> Option<Decimal> {
    match value? {
        Value::String(text) => Decimal::from_str_exact(text).ok(),
        Value::Number(number) => Decimal::from_str_exact(&number.to_string()).ok(),
        _ => None,
    }
}

pub(crate) fn websocket_json(message: Message) -> Option<Value> {
    match message {
        Message::Text(text) if text == "PING" || text == "PONG" => None,
        Message::Text(text) => serde_json::from_str(&text).ok(),
        Message::Binary(bytes) => serde_json::from_slice(&bytes).ok(),
        _ => None,
    }
}

pub(crate) fn get_json(agent: &ureq::Agent, url: &str) -> Result<Value, FeedError> {
    let response = agent.get(url).call().map_err(ureq_error)?;
    let mut text = String::new();
    response
        .into_reader()
        .read_to_string(&mut text)
        .map_err(|error| FeedError::HttpTransport(error.to_string()))?;
    Ok(serde_json::from_str(&text)?)
}

pub(crate) fn ureq_error(error: ureq::Error) -> FeedError {
    match error {
        ureq::Error::Status(status, _) => FeedError::HttpStatus(status),
        ureq::Error::Transport(error) => FeedError::HttpTransport(error.to_string()),
    }
}

pub(crate) fn with_query(base: &str, params: &[(String, String)]) -> Result<Url, FeedError> {
    let mut url = Url::parse(base)?;
    {
        let mut query = url.query_pairs_mut();
        for (key, value) in params {
            query.append_pair(key, value);
        }
    }
    Ok(url)
}

pub(crate) fn value_text(value: &Value) -> String {
    match value {
        Value::String(text) => text.to_owned(),
        Value::Number(number) => number.to_string(),
        Value::Bool(value) => value.to_string(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

pub(crate) fn value_opt_text(value: Option<&Value>) -> Option<String> {
    value
        .map(value_text)
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

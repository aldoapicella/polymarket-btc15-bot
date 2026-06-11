use polyedge_reporting::{build_pnl_report, run_backtest};
use serde_json::Value;
use std::path::PathBuf;

#[test]
fn backtest_matches_cancelled_maker_fixture() {
    let mut actual = run_backtest(&fixture("events_cancelled_maker_sample.jsonl"))
        .unwrap()
        .as_value();
    let mut expected: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/backtest_cancelled_maker_expected.json"
    ))
    .unwrap();
    normalize_paths(&mut actual);
    normalize_paths(&mut expected);
    assert_eq!(actual, expected);
}

#[test]
fn pnl_matches_fixture_without_generated_timestamp() {
    let mut actual = build_pnl_report(&fixture("events_pnl_sample.jsonl")).unwrap();
    let mut expected: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/pnl_report_expected.json"
    ))
    .unwrap();
    normalize_paths(&mut actual);
    normalize_paths(&mut expected);
    assert_eq!(actual, expected);
}

fn normalize_paths(value: &mut Value) {
    match value {
        Value::Object(map) => {
            if map.contains_key("path") {
                map.insert(
                    "path".to_owned(),
                    Value::String("<fixture-path>".to_owned()),
                );
            }
            for child in map.values_mut() {
                normalize_paths(child);
            }
        }
        Value::Array(values) => {
            for child in values {
                normalize_paths(child);
            }
        }
        _ => {}
    }
}

fn fixture(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures")
        .join(name)
        .canonicalize()
        .unwrap()
}

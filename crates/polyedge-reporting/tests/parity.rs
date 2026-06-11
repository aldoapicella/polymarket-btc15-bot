use polyedge_reporting::{build_pnl_report, run_backtest};
use serde_json::Value;
use std::path::PathBuf;

#[test]
fn backtest_matches_python_cancelled_maker_fixture() {
    let actual = run_backtest(&fixture("events_cancelled_maker_sample.jsonl"))
        .unwrap()
        .as_value();
    let expected: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/backtest_cancelled_maker_expected.json"
    ))
    .unwrap();
    assert_eq!(actual, expected);
}

#[test]
fn pnl_matches_python_fixture_without_generated_timestamp() {
    let actual = build_pnl_report(&fixture("events_pnl_sample.jsonl")).unwrap();
    let expected: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/pnl_report_expected.json"
    ))
    .unwrap();
    assert_eq!(actual, expected);
}

fn fixture(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures")
        .join(name)
        .canonicalize()
        .unwrap()
}

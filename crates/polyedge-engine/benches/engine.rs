use chrono::{DateTime, Utc};
use criterion::{criterion_group, criterion_main, Criterion};
use polyedge_config::RuntimeSettings;
use polyedge_domain::{BookState, FairValue, MarketSpec, ReferencePrice, TokenId};
use polyedge_engine::{LogReturnFairValueModel, MakerFirstStrategy};
use serde_json::Value;
use std::collections::BTreeMap;

fn bench_fair_value(c: &mut Criterion) {
    let cases = fixture();
    let case = &cases["fair_value_cases"][0];
    let market: MarketSpec = serde_json::from_value(case["market"].clone()).unwrap();
    let reference: ReferencePrice = serde_json::from_value(case["reference"].clone()).unwrap();
    let now = DateTime::parse_from_rfc3339(case["now"].as_str().unwrap())
        .unwrap()
        .with_timezone(&Utc);
    let model = LogReturnFairValueModel::new(RuntimeSettings::default());
    c.bench_function("fair_value_reference_above_start", |b| {
        b.iter(|| model.compute(&market, &reference, now, Some(0.60), None))
    });
}

fn bench_strategy(c: &mut Criterion) {
    let cases = fixture();
    let case = &cases["strategy_cases"][0];
    let market: MarketSpec = serde_json::from_value(case["market"].clone()).unwrap();
    let fair_value: FairValue = serde_json::from_value(case["fair_value"].clone()).unwrap();
    let books = case["books"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(key, value)| {
            (
                TokenId::new(key.clone()),
                serde_json::from_value::<BookState>(value.clone()).unwrap(),
            )
        })
        .collect::<BTreeMap<_, _>>();
    let strategy = MakerFirstStrategy::new(RuntimeSettings::default());
    c.bench_function("maker_first_strategy", |b| {
        b.iter(|| strategy.evaluate(&market, &fair_value, &books))
    });
}

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../tests/fixtures/rust_parity_cases.json"
    ))
    .unwrap()
}

criterion_group!(benches, bench_fair_value, bench_strategy);
criterion_main!(benches);

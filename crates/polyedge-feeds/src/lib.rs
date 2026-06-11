use chrono::{DateTime, Utc};
use polyedge_domain::{BookState, ReferencePrice};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use tokio::sync::mpsc;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FeedName {
    PolymarketRtdsChainlink,
    PolymarketRtdsBinance,
    PolymarketClobMarket,
    BinanceBookTicker,
    CoinbaseTicker,
    Mock,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "type", content = "data", rename_all = "snake_case")]
pub enum FeedEvent {
    Reference(ReferencePrice),
    Book(BookState),
    Error {
        source: FeedName,
        message: String,
        ts: DateTime<Utc>,
    },
    Heartbeat {
        source: FeedName,
        ts: DateTime<Utc>,
    },
}

#[derive(Debug, Error)]
pub enum FeedError {
    #[error("feed channel is closed")]
    ChannelClosed,
}

#[derive(Clone, Debug)]
pub struct FeedPublisher {
    source: FeedName,
    sender: mpsc::Sender<FeedEvent>,
}

impl FeedPublisher {
    pub fn new(source: FeedName, sender: mpsc::Sender<FeedEvent>) -> Self {
        Self { source, sender }
    }

    pub async fn publish(&self, event: FeedEvent) -> Result<(), FeedError> {
        self.sender
            .send(event)
            .await
            .map_err(|_| FeedError::ChannelClosed)
    }

    pub async fn heartbeat(&self) -> Result<(), FeedError> {
        self.publish(FeedEvent::Heartbeat {
            source: self.source.clone(),
            ts: Utc::now(),
        })
        .await
    }
}

pub fn bounded_feed_channel(
    capacity: usize,
) -> (mpsc::Sender<FeedEvent>, mpsc::Receiver<FeedEvent>) {
    mpsc::channel(capacity)
}

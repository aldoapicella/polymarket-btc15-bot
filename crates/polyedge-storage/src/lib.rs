use chrono::{DateTime, Utc};
use percent_encoding::{utf8_percent_encode, AsciiSet, CONTROLS, NON_ALPHANUMERIC};
use polyedge_domain::RuntimeEvent;
use quick_xml::events::Event;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;
use thiserror::Error;

const AZURE_BLOB_API_VERSION: &str = "2023-11-03";
const PATH_SEGMENT_ENCODE_SET: &AsciiSet = &CONTROLS
    .add(b' ')
    .add(b'"')
    .add(b'#')
    .add(b'%')
    .add(b'<')
    .add(b'>')
    .add(b'?')
    .add(b'`')
    .add(b'{')
    .add(b'}');

#[derive(Debug, Error)]
pub enum StorageError {
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("{0} is not implemented in the Rust shadow backend yet")]
    Unsupported(&'static str),
}

pub trait EventRecorder {
    fn record(&mut self, event: &RuntimeEvent) -> Result<(), StorageError>;
}

#[derive(Clone, Debug)]
pub struct JsonlRecorder {
    path: PathBuf,
}

impl JsonlRecorder {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl EventRecorder for JsonlRecorder {
    fn record(&mut self, event: &RuntimeEvent) -> Result<(), StorageError> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        serde_json::to_writer(&mut file, event)?;
        file.write_all(b"\n")?;
        Ok(())
    }
}

#[derive(Clone, Debug, Default)]
pub struct AzureBlobRecorder;

impl EventRecorder for AzureBlobRecorder {
    fn record(&mut self, _event: &RuntimeEvent) -> Result<(), StorageError> {
        Err(StorageError::Unsupported("Azure Blob recorder"))
    }
}

#[derive(Debug, Error)]
pub enum AzureBlobError {
    #[error("Azure Blob HTTP status {0}")]
    HttpStatus(u16),
    #[error("Azure Blob HTTP transport error")]
    Transport,
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("response body was not UTF-8: {0}")]
    Utf8(#[from] std::string::FromUtf8Error),
    #[error("XML parse error: {0}")]
    Xml(#[from] quick_xml::Error),
    #[error("failed to parse Azure blob list XML: {0}")]
    XmlMessage(String),
}

#[derive(Clone, Debug)]
pub struct AzureBlobItem {
    pub name: String,
    pub content_length: u64,
}

#[derive(Clone)]
pub struct AzureBlobClient {
    account: String,
    container: String,
    sas: String,
    agent: ureq::Agent,
}

impl AzureBlobClient {
    pub fn new(
        account: impl Into<String>,
        container: impl Into<String>,
        sas: impl Into<String>,
    ) -> Self {
        Self {
            account: account.into(),
            container: container.into(),
            sas: sas.into(),
            agent: ureq::AgentBuilder::new()
                .timeout_connect(Duration::from_secs(10))
                .timeout_read(Duration::from_secs(120))
                .timeout_write(Duration::from_secs(30))
                .build(),
        }
    }

    pub fn list_blobs(
        &self,
        prefix: &str,
        max_blobs: Option<usize>,
        max_bytes: Option<u64>,
    ) -> Result<Vec<AzureBlobItem>, AzureBlobError> {
        let mut marker = String::new();
        let mut blobs = Vec::new();
        let mut selected_bytes = 0_u64;
        loop {
            let mut url = format!(
                "https://{}.blob.core.windows.net/{}?restype=container&comp=list&maxresults=5000&prefix={}",
                self.account,
                self.container,
                utf8_percent_encode(prefix, NON_ALPHANUMERIC)
            );
            if !marker.is_empty() {
                url.push_str("&marker=");
                url.push_str(&utf8_percent_encode(&marker, NON_ALPHANUMERIC).to_string());
            }
            let text = self.get_text(&append_sas(&url, &self.sas))?;
            let page = parse_blob_list(&text)?;
            for blob in page.blobs {
                if !blob.name.ends_with(".jsonl") {
                    continue;
                }
                if max_blobs.is_some_and(|limit| blobs.len() >= limit) {
                    return Ok(blobs);
                }
                if max_bytes.is_some_and(|limit| {
                    !blobs.is_empty() && selected_bytes + blob.content_length > limit
                }) {
                    return Ok(blobs);
                }
                selected_bytes += blob.content_length;
                blobs.push(blob);
            }
            marker = page.next_marker;
            if marker.is_empty() {
                return Ok(blobs);
            }
        }
    }

    pub fn download_blob_bytes(&self, name: &str) -> Result<Vec<u8>, AzureBlobError> {
        let url = append_sas(
            &format!(
                "https://{}.blob.core.windows.net/{}/{}",
                self.account,
                self.container,
                encode_blob_path(name)
            ),
            &self.sas,
        );
        let response = self.get_response(&url)?;
        let mut reader = response.into_reader();
        let mut bytes = Vec::new();
        reader.read_to_end(&mut bytes)?;
        Ok(bytes)
    }

    fn get_text(&self, url: &str) -> Result<String, AzureBlobError> {
        let response = self.get_response(url)?;
        let mut bytes = Vec::new();
        response.into_reader().read_to_end(&mut bytes)?;
        Ok(String::from_utf8(bytes)?)
    }

    fn get_response(&self, url: &str) -> Result<ureq::Response, AzureBlobError> {
        match self
            .agent
            .get(url)
            .set("x-ms-version", AZURE_BLOB_API_VERSION)
            .call()
        {
            Ok(response) => Ok(response),
            Err(ureq::Error::Status(status, _)) => Err(AzureBlobError::HttpStatus(status)),
            Err(ureq::Error::Transport(_)) => Err(AzureBlobError::Transport),
        }
    }
}

#[derive(Default)]
struct BlobListPage {
    blobs: Vec<AzureBlobItem>,
    next_marker: String,
}

fn parse_blob_list(xml: &str) -> Result<BlobListPage, AzureBlobError> {
    let mut reader = quick_xml::Reader::from_str(xml);
    reader.config_mut().trim_text(true);
    let mut buf = Vec::new();
    let mut page = BlobListPage::default();
    let mut current_tag = String::new();
    let mut in_blob = false;
    let mut name = String::new();
    let mut content_length = 0_u64;
    loop {
        match reader.read_event_into(&mut buf)? {
            Event::Start(event) => {
                current_tag = String::from_utf8_lossy(event.name().as_ref()).into_owned();
                if current_tag == "Blob" {
                    in_blob = true;
                    name.clear();
                    content_length = 0;
                }
            }
            Event::End(event) => {
                let tag = String::from_utf8_lossy(event.name().as_ref()).into_owned();
                if tag == "Blob" && in_blob {
                    page.blobs.push(AzureBlobItem {
                        name: name.clone(),
                        content_length,
                    });
                    in_blob = false;
                }
                current_tag.clear();
            }
            Event::Text(event) => {
                let text = event
                    .unescape()
                    .map_err(|error| AzureBlobError::XmlMessage(error.to_string()))?
                    .into_owned();
                if in_blob && current_tag == "Name" {
                    name = text;
                } else if in_blob && current_tag == "Content-Length" {
                    content_length = text.parse().unwrap_or(0);
                } else if !in_blob && current_tag == "NextMarker" {
                    page.next_marker = text;
                }
            }
            Event::Eof => break,
            _ => {}
        }
        buf.clear();
    }
    Ok(page)
}

fn append_sas(url: &str, sas: &str) -> String {
    let trimmed = sas.trim_start_matches('?');
    if url.contains('?') {
        format!("{url}&{trimmed}")
    } else {
        format!("{url}?{trimmed}")
    }
}

fn encode_blob_path(name: &str) -> String {
    name.split('/')
        .map(|segment| utf8_percent_encode(segment, PATH_SEGMENT_ENCODE_SET).to_string())
        .collect::<Vec<_>>()
        .join("/")
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AuditEntry {
    pub version: String,
    pub category: String,
    pub action: String,
    pub actor: Option<String>,
    pub source: String,
    pub reason: Option<String>,
    pub created_ts: DateTime<Utc>,
    pub before: Value,
    pub after: Value,
    #[serde(default)]
    pub metadata: Value,
}

#[derive(Clone, Debug, Default)]
pub struct InMemoryAuditLog {
    entries: Vec<AuditEntry>,
}

impl InMemoryAuditLog {
    pub fn record(
        &mut self,
        category: impl Into<String>,
        action: impl Into<String>,
        before: Value,
        after: Value,
    ) -> AuditEntry {
        let entry = AuditEntry {
            version: format!("rust-{}", self.entries.len() + 1),
            category: category.into(),
            action: action.into(),
            actor: None,
            source: "api".to_owned(),
            reason: None,
            created_ts: Utc::now(),
            before,
            after,
            metadata: Value::Null,
        };
        self.entries.push(entry.clone());
        entry
    }

    pub fn history(&self, limit: usize) -> Vec<AuditEntry> {
        self.entries.iter().rev().take(limit).cloned().collect()
    }
}

#[derive(Clone, Debug)]
pub struct LocalReportStore {
    root: PathBuf,
}

impl LocalReportStore {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub fn write_latest(&self, payload: &Value) -> Result<PathBuf, StorageError> {
        fs::create_dir_all(&self.root)?;
        let path = self.root.join("latest-report.json");
        fs::write(&path, serde_json::to_vec_pretty(payload)?)?;
        Ok(path)
    }
}

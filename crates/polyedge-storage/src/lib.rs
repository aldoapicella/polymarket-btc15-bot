use chrono::{DateTime, Utc};
use percent_encoding::{utf8_percent_encode, AsciiSet, CONTROLS, NON_ALPHANUMERIC};
use polyedge_domain::RuntimeEvent;
use quick_xml::events::Event;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;
use std::{env, thread};
use thiserror::Error;

const AZURE_BLOB_API_VERSION: &str = "2023-11-03";
const AZURE_BLOB_MAX_ATTEMPTS: usize = 5;
type AzureTableContinuation = Option<(String, String)>;
type AzureTablePage = (Vec<Value>, AzureTableContinuation);
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
    #[error("Azure Blob error: {0}")]
    AzureBlob(#[from] AzureBlobError),
    #[error("{0} is not implemented in the Rust backend yet")]
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
        serde_json::to_writer(&mut file, &jsonl_event_envelope(event))?;
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

#[derive(Clone)]
pub struct AzureAppendBlobRecorder {
    account: String,
    container: String,
    agent: ureq::Agent,
    token: ManagedIdentityToken,
}

impl AzureAppendBlobRecorder {
    pub fn new(
        account: impl Into<String>,
        container: impl Into<String>,
        client_id: Option<String>,
    ) -> Self {
        Self {
            account: account.into(),
            container: container.into(),
            agent: ureq::AgentBuilder::new()
                .timeout_connect(Duration::from_secs(10))
                .timeout_read(Duration::from_secs(30))
                .timeout_write(Duration::from_secs(30))
                .build(),
            token: ManagedIdentityToken::new(client_id),
        }
    }

    fn append_line(&mut self, blob_name: &str, line: &[u8]) -> Result<(), AzureBlobError> {
        self.ensure_append_blob(blob_name)?;
        let url = self.blob_url(blob_name, Some("comp=appendblock"));
        let token = self.token.access_token(&self.agent)?;
        match self
            .agent
            .put(&url)
            .set("authorization", &format!("Bearer {token}"))
            .set("x-ms-version", AZURE_BLOB_API_VERSION)
            .set("x-ms-date", &rfc1123_now())
            .set("content-type", "application/octet-stream")
            .send_bytes(line)
        {
            Ok(_) => Ok(()),
            Err(ureq::Error::Status(status, _)) => Err(AzureBlobError::HttpStatus(status)),
            Err(ureq::Error::Transport(error)) => Err(AzureBlobError::Transport(error.to_string())),
        }
    }

    fn ensure_append_blob(&mut self, blob_name: &str) -> Result<(), AzureBlobError> {
        let url = self.blob_url(blob_name, None);
        let token = self.token.access_token(&self.agent)?;
        match self
            .agent
            .put(&url)
            .set("authorization", &format!("Bearer {token}"))
            .set("x-ms-version", AZURE_BLOB_API_VERSION)
            .set("x-ms-date", &rfc1123_now())
            .set("x-ms-blob-type", "AppendBlob")
            .send_bytes(&[])
        {
            Ok(_) => Ok(()),
            Err(ureq::Error::Status(409, _)) => Ok(()),
            Err(ureq::Error::Status(status, _)) => Err(AzureBlobError::HttpStatus(status)),
            Err(ureq::Error::Transport(error)) => Err(AzureBlobError::Transport(error.to_string())),
        }
    }

    fn blob_url(&self, blob_name: &str, query: Option<&str>) -> String {
        let mut url = format!(
            "https://{}.blob.core.windows.net/{}/{}",
            self.account,
            self.container,
            encode_blob_path(blob_name)
        );
        if let Some(query) = query {
            url.push('?');
            url.push_str(query);
        }
        url
    }
}

impl EventRecorder for AzureAppendBlobRecorder {
    fn record(&mut self, event: &RuntimeEvent) -> Result<(), StorageError> {
        let envelope = jsonl_event_envelope(event);
        let mut line = serde_json::to_vec(&envelope)?;
        line.push(b'\n');
        let recorded_ts = event.ts;
        let blob_name = format!("events/{}.jsonl", recorded_ts.format("%Y/%m/%d/%H/%M"));
        self.append_line(&blob_name, &line)?;
        Ok(())
    }
}

#[derive(Debug, Error)]
pub enum AzureBlobError {
    #[error("Azure Blob HTTP status {0}")]
    HttpStatus(u16),
    #[error("managed identity token is unavailable: {0}")]
    ManagedIdentity(String),
    #[error("Azure Blob HTTP transport error: {0}")]
    Transport(String),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("response body was not UTF-8: {0}")]
    Utf8(#[from] std::string::FromUtf8Error),
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("XML parse error: {0}")]
    Xml(#[from] quick_xml::Error),
    #[error("failed to parse Azure blob list XML: {0}")]
    XmlMessage(String),
}

impl AzureBlobError {
    fn is_retryable(&self) -> bool {
        match self {
            AzureBlobError::HttpStatus(status) => is_retryable_azure_status(*status),
            AzureBlobError::Transport(_) | AzureBlobError::Io(_) => true,
            _ => false,
        }
    }
}

#[derive(Clone, Debug, Default)]
struct ManagedIdentityToken {
    client_id: Option<String>,
    access_token: Option<String>,
    expires_on_epoch: Option<i64>,
}

impl ManagedIdentityToken {
    fn new(client_id: Option<String>) -> Self {
        Self {
            client_id,
            access_token: None,
            expires_on_epoch: None,
        }
    }

    fn access_token(&mut self, agent: &ureq::Agent) -> Result<String, AzureBlobError> {
        let now = Utc::now().timestamp();
        if let (Some(token), Some(expires_on)) = (&self.access_token, self.expires_on_epoch) {
            if expires_on - now > 120 {
                return Ok(token.clone());
            }
        }
        let payload = fetch_managed_identity_token(agent, self.client_id.as_deref())?;
        let token = payload
            .get("access_token")
            .and_then(Value::as_str)
            .ok_or_else(|| AzureBlobError::ManagedIdentity("missing access_token".to_owned()))?
            .to_owned();
        let expires_on = payload
            .get("expires_on")
            .and_then(parse_expires_on)
            .unwrap_or(now + 300);
        self.access_token = Some(token.clone());
        self.expires_on_epoch = Some(expires_on);
        Ok(token)
    }
}

fn fetch_managed_identity_token(
    agent: &ureq::Agent,
    client_id: Option<&str>,
) -> Result<Value, AzureBlobError> {
    let resource = "https%3A%2F%2Fstorage.azure.com%2F";
    if let (Ok(endpoint), Ok(header)) = (env::var("IDENTITY_ENDPOINT"), env::var("IDENTITY_HEADER"))
    {
        let mut url = format!("{endpoint}?api-version=2019-08-01&resource={resource}");
        if let Some(client_id) = client_id {
            url.push_str("&client_id=");
            url.push_str(&utf8_percent_encode(client_id, NON_ALPHANUMERIC).to_string());
        }
        let response = agent
            .get(&url)
            .set("X-IDENTITY-HEADER", &header)
            .call()
            .map_err(identity_error)?;
        return parse_json_response(response);
    }

    let mut url = format!(
        "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource={resource}"
    );
    if let Some(client_id) = client_id {
        url.push_str("&client_id=");
        url.push_str(&utf8_percent_encode(client_id, NON_ALPHANUMERIC).to_string());
    }
    let response = agent
        .get(&url)
        .set("Metadata", "true")
        .call()
        .map_err(identity_error)?;
    parse_json_response(response)
}

fn identity_error(error: ureq::Error) -> AzureBlobError {
    match error {
        ureq::Error::Status(status, response) => {
            let body = response.into_string().unwrap_or_default();
            AzureBlobError::ManagedIdentity(format!("HTTP {status}: {body}"))
        }
        ureq::Error::Transport(error) => AzureBlobError::ManagedIdentity(error.to_string()),
    }
}

fn parse_json_response(response: ureq::Response) -> Result<Value, AzureBlobError> {
    let text = response
        .into_string()
        .map_err(|error| AzureBlobError::ManagedIdentity(error.to_string()))?;
    serde_json::from_str(&text).map_err(|error| AzureBlobError::ManagedIdentity(error.to_string()))
}

fn parse_expires_on(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number.as_i64(),
        Value::String(text) => text.parse::<i64>().ok(),
        _ => None,
    }
}

fn jsonl_event_envelope(event: &RuntimeEvent) -> Value {
    json!({
        "recorded_ts": event.ts,
        "event_type": event.event_type,
        "payload": event.data
    })
}

fn rfc1123_now() -> String {
    Utc::now().format("%a, %d %b %Y %H:%M:%S GMT").to_string()
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
        self.get_bytes_with_retry(&url)
    }

    fn get_text(&self, url: &str) -> Result<String, AzureBlobError> {
        Ok(String::from_utf8(self.get_bytes_with_retry(url)?)?)
    }

    fn get_bytes_with_retry(&self, url: &str) -> Result<Vec<u8>, AzureBlobError> {
        for attempt in 0..AZURE_BLOB_MAX_ATTEMPTS {
            let result = self.read_response_bytes(url);
            match result {
                Ok(bytes) => return Ok(bytes),
                Err(error) if error.is_retryable() && attempt + 1 < AZURE_BLOB_MAX_ATTEMPTS => {
                    thread::sleep(retry_delay(attempt));
                }
                Err(error) => return Err(error),
            }
        }
        unreachable!("Azure Blob byte retry loop always returns");
    }

    fn read_response_bytes(&self, url: &str) -> Result<Vec<u8>, AzureBlobError> {
        let response = self.get_response(url)?;
        let mut bytes = Vec::new();
        response.into_reader().read_to_end(&mut bytes)?;
        Ok(bytes)
    }

    fn get_response(&self, url: &str) -> Result<ureq::Response, AzureBlobError> {
        for attempt in 0..AZURE_BLOB_MAX_ATTEMPTS {
            match self
                .agent
                .get(url)
                .set("x-ms-version", AZURE_BLOB_API_VERSION)
                .call()
            {
                Ok(response) => return Ok(response),
                Err(ureq::Error::Status(status, _)) => {
                    if is_retryable_azure_status(status) && attempt + 1 < AZURE_BLOB_MAX_ATTEMPTS {
                        thread::sleep(retry_delay(attempt));
                        continue;
                    }
                    return Err(AzureBlobError::HttpStatus(status));
                }
                Err(ureq::Error::Transport(error)) => {
                    let message = error.to_string();
                    if attempt + 1 < AZURE_BLOB_MAX_ATTEMPTS {
                        thread::sleep(retry_delay(attempt));
                        continue;
                    }
                    return Err(AzureBlobError::Transport(message));
                }
            }
        }
        unreachable!("Azure Blob retry loop always returns");
    }
}

#[derive(Clone)]
pub struct AzureTableClient {
    account: String,
    agent: ureq::Agent,
    token: ManagedIdentityToken,
}

impl AzureTableClient {
    pub fn new(account: impl Into<String>, client_id: Option<String>) -> Self {
        Self {
            account: account.into(),
            agent: ureq::AgentBuilder::new()
                .timeout_connect(Duration::from_secs(10))
                .timeout_read(Duration::from_secs(30))
                .timeout_write(Duration::from_secs(30))
                .build(),
            token: ManagedIdentityToken::new(client_id),
        }
    }

    pub fn query_entities(
        &mut self,
        table: &str,
        filter: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Value>, AzureBlobError> {
        let mut entities = Vec::new();
        let mut continuation: Option<(String, String)> = None;
        while entities.len() < limit {
            let top = (limit - entities.len()).min(1000);
            let (page, next) = self.query_page(table, filter, top, continuation.as_ref())?;
            if page.is_empty() {
                break;
            }
            entities.extend(page);
            continuation = next;
            if continuation.is_none() {
                break;
            }
        }
        Ok(entities)
    }

    fn query_page(
        &mut self,
        table: &str,
        filter: Option<&str>,
        top: usize,
        continuation: Option<&(String, String)>,
    ) -> Result<AzureTablePage, AzureBlobError> {
        let mut params = vec![("$top".to_owned(), top.to_string())];
        if let Some(filter) = filter {
            params.push(("$filter".to_owned(), filter.to_owned()));
        }
        if let Some((partition_key, row_key)) = continuation {
            params.push(("NextPartitionKey".to_owned(), partition_key.clone()));
            params.push(("NextRowKey".to_owned(), row_key.clone()));
        }
        let query = params
            .iter()
            .map(|(key, value)| {
                format!(
                    "{}={}",
                    utf8_percent_encode(key, NON_ALPHANUMERIC),
                    utf8_percent_encode(value, NON_ALPHANUMERIC)
                )
            })
            .collect::<Vec<_>>()
            .join("&");
        let url = format!(
            "https://{}.table.core.windows.net/{}()?{}",
            self.account, table, query
        );
        for attempt in 0..AZURE_BLOB_MAX_ATTEMPTS {
            let token = self.token.access_token(&self.agent)?;
            let response = self
                .agent
                .get(&url)
                .set("authorization", &format!("Bearer {token}"))
                .set("x-ms-version", AZURE_BLOB_API_VERSION)
                .set("x-ms-date", &rfc1123_now())
                .set("Accept", "application/json;odata=nometadata")
                .set("DataServiceVersion", "3.0;NetFx")
                .set("MaxDataServiceVersion", "3.0;NetFx")
                .call();
            match response {
                Ok(response) => return parse_table_response(response),
                Err(ureq::Error::Status(status, _))
                    if is_retryable_azure_status(status)
                        && attempt + 1 < AZURE_BLOB_MAX_ATTEMPTS =>
                {
                    thread::sleep(retry_delay(attempt));
                }
                Err(ureq::Error::Status(status, _)) => {
                    return Err(AzureBlobError::HttpStatus(status));
                }
                Err(ureq::Error::Transport(error)) if attempt + 1 < AZURE_BLOB_MAX_ATTEMPTS => {
                    thread::sleep(retry_delay(attempt));
                    let _ = error;
                }
                Err(ureq::Error::Transport(error)) => {
                    return Err(AzureBlobError::Transport(error.to_string()));
                }
            }
        }
        unreachable!("Azure Table retry loop always returns");
    }
}

fn parse_table_response(response: ureq::Response) -> Result<AzureTablePage, AzureBlobError> {
    let next_partition_key = response
        .header("x-ms-continuation-NextPartitionKey")
        .map(str::to_owned);
    let next_row_key = response
        .header("x-ms-continuation-NextRowKey")
        .map(str::to_owned);
    let text = response.into_string()?;
    let payload: Value = serde_json::from_str(&text)?;
    let entities = payload
        .get("value")
        .or_else(|| payload.get("items"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let continuation = match (next_partition_key, next_row_key) {
        (Some(partition_key), Some(row_key)) => Some((partition_key, row_key)),
        _ => None,
    };
    Ok((entities, continuation))
}

fn is_retryable_azure_status(status: u16) -> bool {
    matches!(status, 408 | 429 | 500 | 502 | 503 | 504)
}

fn retry_delay(attempt: usize) -> Duration {
    Duration::from_millis(250 * 2_u64.pow(attempt.min(4) as u32))
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

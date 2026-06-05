from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings


EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class ChainlinkReport:
    feed_id: str
    valid_from_timestamp: int | None
    observations_timestamp: int | None
    full_report: str | None
    raw: dict[str, Any]

    @property
    def is_currentish(self) -> bool:
        if self.observations_timestamp is None:
            return False
        return abs(time.time() - self.observations_timestamp) <= 30


class ChainlinkDataStreamsClient:
    def __init__(self, settings: Settings):
        self.base_url = settings.chainlink_data_streams_api_url.rstrip("/")
        self.api_key = settings.chainlink_data_streams_api_key
        self.api_secret = settings.chainlink_data_streams_api_secret
        self.feed_id = settings.chainlink_data_streams_feed_id
        if not self.api_key or not self.api_secret or not self.feed_id:
            raise ValueError("Chainlink Data Streams API key, secret, and feed ID are required")

    async def latest_report(self) -> ChainlinkReport:
        path = "/api/v1/reports/latest"
        query = urlencode({"feedID": self.feed_id})
        full_path = f"{path}?{query}"
        headers = self.auth_headers("GET", full_path)
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.get(f"{self.base_url}{full_path}", headers=headers)
            response.raise_for_status()
            payload = response.json()
        report = payload.get("report") or {}
        return ChainlinkReport(
            feed_id=str(report.get("feedID") or ""),
            valid_from_timestamp=_int_or_none(report.get("validFromTimestamp")),
            observations_timestamp=_int_or_none(report.get("observationsTimestamp")),
            full_report=report.get("fullReport"),
            raw=payload,
        )

    def auth_headers(self, method: str, full_path: str, body: bytes = b"") -> dict[str, str]:
        assert self.api_key is not None
        timestamp = str(int(time.time() * 1000))
        body_hash = hashlib.sha256(body).hexdigest()
        string_to_sign = f"{method.upper()} {full_path} {body_hash} {self.api_key} {timestamp}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),  # type: ignore[union-attr]
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Authorization": self.api_key,
            "X-Authorization-Timestamp": timestamp,
            "X-Authorization-Signature-SHA256": signature,
        }


def validate_feed_id_shape(feed_id: str, expected_suffix: str = "75b8") -> list[str]:
    issues: list[str] = []
    if not feed_id.startswith("0x"):
        issues.append("feed ID must start with 0x")
    if len(feed_id) != 66:
        issues.append("feed ID should be a 32-byte hex value, 66 chars including 0x")
    hex_part = feed_id[2:]
    if any(char not in "0123456789abcdefABCDEF" for char in hex_part):
        issues.append("feed ID contains non-hex characters")
    if expected_suffix and not feed_id.lower().endswith(expected_suffix.lower()):
        issues.append(f"feed ID does not end with expected public suffix {expected_suffix}")
    return issues


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

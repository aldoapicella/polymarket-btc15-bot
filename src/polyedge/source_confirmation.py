from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .chainlink_streams import ChainlinkDataStreamsClient, validate_feed_id_shape
from .config import Settings
from .market_discovery import MarketDiscovery


@dataclass
class SourceConfirmation:
    ok: bool
    polymarket_market_count: int
    polymarket_confirmed_count: int
    chainlink_public_url: str
    chainlink_product_name: str
    chainlink_public_feed_id_suffix: str
    feed_id_configured: bool
    api_credentials_configured: bool
    authenticated_report_checked: bool
    issues: list[str] = field(default_factory=list)
    sample_markets: list[dict[str, Any]] = field(default_factory=list)
    authenticated_report: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "polymarket_market_count": self.polymarket_market_count,
            "polymarket_confirmed_count": self.polymarket_confirmed_count,
            "chainlink_public_url": self.chainlink_public_url,
            "chainlink_product_name": self.chainlink_product_name,
            "chainlink_public_feed_id_suffix": self.chainlink_public_feed_id_suffix,
            "feed_id_configured": self.feed_id_configured,
            "api_credentials_configured": self.api_credentials_configured,
            "authenticated_report_checked": self.authenticated_report_checked,
            "issues": self.issues,
            "sample_markets": self.sample_markets,
            "authenticated_report": self.authenticated_report,
        }


async def confirm_source(settings: Settings) -> SourceConfirmation:
    discovery = MarketDiscovery(settings)
    markets = await discovery.discover()
    expected_url = settings.chainlink_product_url.lower()
    confirmed = [
        market for market in markets
        if expected_url in (market.description or "").lower()
        and "chainlink" in (market.description or "").lower()
    ]

    issues: list[str] = []
    if not markets:
        issues.append(f"no {settings.target_asset} {settings.target_horizon} markets discovered")
    if markets and not confirmed:
        issues.append("discovered markets do not mention the expected Chainlink product URL")

    if settings.chainlink_data_streams_feed_id:
        issues.extend(
            validate_feed_id_shape(
                settings.chainlink_data_streams_feed_id,
                settings.chainlink_feed_id_suffix,
            )
        )

    api_credentials_configured = bool(
        settings.chainlink_data_streams_api_key and settings.chainlink_data_streams_api_secret
    )
    authenticated_report_checked = False
    authenticated_report: dict[str, Any] | None = None
    if settings.chainlink_data_streams_feed_id and api_credentials_configured:
        try:
            report = await ChainlinkDataStreamsClient(settings).latest_report()
            authenticated_report_checked = True
            authenticated_report = {
                "feed_id": report.feed_id,
                "valid_from_timestamp": report.valid_from_timestamp,
                "observations_timestamp": report.observations_timestamp,
                "is_currentish": report.is_currentish,
                "has_full_report": bool(report.full_report),
            }
            if report.feed_id.lower() != settings.chainlink_data_streams_feed_id.lower():
                issues.append("authenticated report feedID does not match configured feed ID")
            if not report.is_currentish:
                issues.append("authenticated report observationsTimestamp is not current")
        except Exception as exc:
            issues.append(f"authenticated Chainlink report check failed: {exc}")

    feed_ready = bool(settings.chainlink_data_streams_feed_id and api_credentials_configured)
    ok = bool(confirmed) and (not feed_ready or authenticated_report_checked) and not issues

    return SourceConfirmation(
        ok=ok,
        polymarket_market_count=len(markets),
        polymarket_confirmed_count=len(confirmed),
        chainlink_public_url=settings.chainlink_product_url,
        chainlink_product_name=settings.chainlink_product_name,
        chainlink_public_feed_id_suffix=settings.chainlink_feed_id_suffix,
        feed_id_configured=bool(settings.chainlink_data_streams_feed_id),
        api_credentials_configured=api_credentials_configured,
        authenticated_report_checked=authenticated_report_checked,
        issues=issues,
        sample_markets=[
            {
                "market_id": market.market_id,
                "market_slug": market.market_slug,
                "status": market.status.value,
                "mentions_expected_chainlink_url": market in confirmed,
            }
            for market in markets[:5]
        ],
        authenticated_report=authenticated_report,
    )

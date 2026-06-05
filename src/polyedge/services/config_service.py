from __future__ import annotations

from typing import Any

from .config_models import (
    PaperRuntimeConfig,
    RiskRuntimeConfig,
    RuntimeConfig,
    RuntimeConfigChangeRequest,
    RuntimeConfigPatch,
    RuntimeReadOnlyConfigStatus,
    StrategyRuntimeConfig,
)
from ..config import Settings
from ..runtime.event_bus import RuntimeEventBus
from .audit import AuditLog


class RuntimeConfigService:
    def __init__(
        self,
        settings: Settings,
        audit_log: AuditLog,
        event_bus: RuntimeEventBus,
    ) -> None:
        self.settings = settings
        self.audit_log = audit_log
        self.event_bus = event_bus

    def current(self) -> RuntimeConfig:
        return RuntimeConfig(
            strategy=StrategyRuntimeConfig(
                maker_margin=self.settings.maker_margin,
                maker_min_edge=self.settings.maker_min_edge,
                model_error_buffer=self.settings.model_error_buffer,
                slippage_buffer=self.settings.slippage_buffer,
                order_ttl_seconds=self.settings.order_ttl_seconds,
                final_no_trade_seconds=self.settings.final_no_trade_seconds,
            ),
            risk=RiskRuntimeConfig(
                base_order_size=self.settings.base_order_size,
                max_order_size=self.settings.max_order_size,
                max_position_per_market=self.settings.max_position_per_market,
                max_total_position=self.settings.max_total_position,
                max_daily_loss=self.settings.max_daily_loss,
                max_open_orders=self.settings.max_open_orders,
            ),
            paper=PaperRuntimeConfig(
                paper_maker_fill_policy=self.settings.paper_maker_fill_policy,
                paper_order_live_after_ms=self.settings.paper_order_live_after_ms,
            ),
            read_only=RuntimeReadOnlyConfigStatus(
                execution_mode=self.settings.execution_mode,
                allow_live=self.settings.allow_live,
                live_requested=self.settings.live_requested,
                require_exact_resolution_source_for_live=(
                    self.settings.require_exact_resolution_source_for_live
                ),
                enable_taker_orders=self.settings.enable_taker_orders,
                allow_emergency_account_cancel=self.settings.allow_emergency_account_cancel,
                require_api_auth=self.settings.require_api_auth,
                api_bearer_token_configured=bool(self.settings.api_bearer_token),
                polymarket_private_key_configured=bool(self.settings.polymarket_private_key),
                azure_storage_configured=bool(self.settings.azure_storage_account_name),
            ),
        )

    def validate(self, patch: RuntimeConfigPatch) -> dict[str, Any]:
        current = self.current()
        proposed = self._merged_config(patch)
        changes = self._changes(current, proposed)
        issues: list[str] = []

        if self.settings.live_requested:
            issues.append("runtime config apply is only enabled in paper execution_mode")

        if proposed.risk.base_order_size > proposed.risk.max_order_size:
            issues.append("base_order_size cannot exceed max_order_size")
        if proposed.risk.max_order_size > proposed.risk.max_position_per_market:
            issues.append("max_order_size cannot exceed max_position_per_market")
        if proposed.risk.max_position_per_market > proposed.risk.max_total_position:
            issues.append("max_position_per_market cannot exceed max_total_position")

        return {
            "valid": not issues,
            "issues": issues,
            "changes": changes,
            "current": current.model_dump(mode="json"),
            "proposed": proposed.model_dump(mode="json"),
        }

    async def apply(self, request: RuntimeConfigChangeRequest) -> dict[str, Any]:
        validation = self.validate(request.config)
        if not validation["valid"]:
            return {"applied": False, "validation": validation}

        before = self.current()
        proposed = self._merged_config(request.config)
        audit_entry = await self.audit_log.record(
            category="config",
            action="config_changed",
            actor=request.actor,
            source=request.source,
            reason=request.reason,
            before=before.model_dump(mode="json"),
            after=proposed.model_dump(mode="json"),
            metadata={
                "changes": validation["changes"],
                "patch": request.config.model_dump(mode="json", exclude_none=True),
            },
        )
        self._apply_config(proposed)
        self.event_bus.publish(
            "config_changed",
            {
                "version": audit_entry["version"],
                "changes": validation["changes"],
                "config": proposed.model_dump(mode="json"),
            },
        )
        return {
            "applied": True,
            "audit_version": audit_entry["version"],
            "validation": validation,
            "config": proposed.model_dump(mode="json"),
        }

    async def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self.audit_log.history("config", limit=limit)

    async def rollback(
        self,
        *,
        version: str,
        reason: str | None,
        actor: str | None,
    ) -> dict[str, Any] | None:
        if self.settings.live_requested:
            return {
                "applied": False,
                "validation": {
                    "valid": False,
                    "issues": ["runtime config rollback is only enabled in paper execution_mode"],
                },
            }
        entry = await self.audit_log.find("config", version)
        if entry is None:
            return None
        before_config = entry.get("before")
        if not isinstance(before_config, dict):
            return None
        target = RuntimeConfig.model_validate(before_config)
        current = self.current()
        changes = self._changes(current, target)
        audit_entry = await self.audit_log.record(
            category="config",
            action="config_rollback",
            actor=actor,
            source="api",
            reason=reason,
            before=current.model_dump(mode="json"),
            after=target.model_dump(mode="json"),
            metadata={
                "rollback_target_version": version,
                "changes": changes,
            },
        )
        self._apply_config(target)
        self.event_bus.publish(
            "config_changed",
            {
                "version": audit_entry["version"],
                "rollback_target_version": version,
                "changes": changes,
                "config": target.model_dump(mode="json"),
            },
        )
        return {
            "applied": True,
            "audit_version": audit_entry["version"],
            "rollback_target_version": version,
            "changes": changes,
            "config": target.model_dump(mode="json"),
        }

    def _merged_config(self, patch: RuntimeConfigPatch) -> RuntimeConfig:
        current = self.current()
        data = current.model_dump()
        patch_data = patch.model_dump(exclude_none=True)
        for section, values in patch_data.items():
            if section not in {"strategy", "risk", "paper"}:
                continue
            data[section].update(values)
        return RuntimeConfig.model_validate(data)

    def _apply_config(self, config: RuntimeConfig) -> None:
        for field, value in config.strategy.model_dump().items():
            setattr(self.settings, field, value)
        for field, value in config.risk.model_dump().items():
            setattr(self.settings, field, value)
        for field, value in config.paper.model_dump().items():
            setattr(self.settings, field, value)

    @staticmethod
    def _changes(before: RuntimeConfig, after: RuntimeConfig) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        before_data = before.model_dump(mode="json")
        after_data = after.model_dump(mode="json")
        for section in ("strategy", "risk", "paper"):
            before_section = before_data[section]
            after_section = after_data[section]
            for field, before_value in before_section.items():
                after_value = after_section[field]
                if before_value != after_value:
                    changes.append(
                        {
                            "field": f"{section}.{field}",
                            "old": before_value,
                            "new": after_value,
                        }
                    )
        return changes

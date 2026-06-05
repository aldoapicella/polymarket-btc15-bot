from __future__ import annotations

from datetime import date as Date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..services.config_models import (
    PaperRuntimeConfig,
    PaperRuntimeConfigPatch,
    RiskRuntimeConfig,
    RiskRuntimeConfigPatch,
    RuntimeConfig,
    RuntimeConfigChangeRequest,
    RuntimeConfigPatch,
    RuntimeReadOnlyConfigStatus,
    StrategyRuntimeConfig,
    StrategyRuntimeConfigPatch,
)


class KillSwitchRequest(BaseModel):
    enabled: bool
    reason: str | None = None
    actor: str | None = None
    source: Literal["api", "ui", "automation"] = "api"


class ControlActionRequest(BaseModel):
    reason: str | None = None
    actor: str | None = None
    source: Literal["api", "ui", "automation"] = "api"


class ReportBuildApiRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: Literal["auto", "local", "azure"] = "auto"
    prefix: str | None = None
    report_date: Date | None = Field(default=None, alias="date")
    settlement_window_seconds: int = 15
    force: bool = False

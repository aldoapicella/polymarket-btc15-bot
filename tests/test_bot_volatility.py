from decimal import Decimal

from polyedge.bot import PolyEdgeBot
from polyedge.config import Settings
from polyedge.execution import PaperExecutionClient
from polyedge.models import ReferencePrice, utc_now
from polyedge.recorder import JsonlRecorder


def test_bot_updates_volatility_only_on_fresh_rtds_chainlink_ticks(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        recorder_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )
    bot = PolyEdgeBot(
        settings,
        execution_client=PaperExecutionClient(),
        recorder=JsonlRecorder(settings.recorder_path),
    )
    calls: list[ReferencePrice] = []
    bot.fair_model.update_volatility = calls.append  # type: ignore[method-assign]
    now = utc_now()
    chainlink_tick = ReferencePrice(
        source="polymarket_rtds_chainlink_btc_usd",
        price=Decimal("100000"),
        source_ts=now,
        local_ts=now,
        exact_resolution_source=True,
    )
    proxy_tick = ReferencePrice(
        source="cex_median_proxy",
        price=Decimal("100001"),
        source_ts=now,
        local_ts=now,
    )

    bot._maybe_update_volatility(chainlink_tick)
    bot._maybe_update_volatility(chainlink_tick)
    bot._maybe_update_volatility(proxy_tick)
    bot._maybe_update_volatility(chainlink_tick.model_copy(update={"price": Decimal("100002")}))

    assert calls == [
        chainlink_tick,
        chainlink_tick.model_copy(update={"price": Decimal("100002")}),
    ]

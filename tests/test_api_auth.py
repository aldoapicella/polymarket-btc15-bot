from fastapi.testclient import TestClient

from polymarket_btc15_bot.api import create_app
from polymarket_btc15_bot.config import Settings


def test_api_allows_health_without_auth_when_disabled(tmp_path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            require_api_auth=False,
            recorder_path=tmp_path / "events.jsonl",
            kill_switch_file=tmp_path / "KILL_SWITCH",
        )
    )

    response = TestClient(app).get("/health")

    assert response.status_code == 200


def test_api_requires_bearer_token_when_enabled(tmp_path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            require_api_auth=True,
            api_bearer_token="secret-token",
            recorder_path=tmp_path / "events.jsonl",
            kill_switch_file=tmp_path / "KILL_SWITCH",
        )
    )
    client = TestClient(app)

    assert client.get("/health").status_code == 401
    response = client.get("/health", headers={"Authorization": "Bearer secret-token"})
    assert response.status_code == 200


def test_api_exposes_authenticated_pnl(tmp_path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            require_api_auth=True,
            api_bearer_token="secret-token",
            recorder_path=tmp_path / "events.jsonl",
            kill_switch_file=tmp_path / "KILL_SWITCH",
        )
    )

    response = TestClient(app).get(
        "/pnl",
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200
    assert response.json()["summary"]["actual_paper_state"] == "flat"

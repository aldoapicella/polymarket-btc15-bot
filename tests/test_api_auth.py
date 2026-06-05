from fastapi.testclient import TestClient

from polyedge.api import create_app
from polyedge.config import Settings


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


def test_api_exposes_versioned_aliases_with_auth(tmp_path) -> None:
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
    headers = {"Authorization": "Bearer secret-token"}

    assert client.get("/api/v1/health", headers=headers).status_code == 200
    assert client.get("/api/v1/status", headers=headers).status_code == 200
    assert client.get("/api/v1/snapshot", headers=headers).status_code == 200


def test_runtime_config_apply_is_runtime_scoped_and_audited(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        require_api_auth=True,
        api_bearer_token="secret-token",
        recorder_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )
    app = create_app(settings)
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token"}

    current = client.get("/api/v1/config/current", headers=headers)
    assert current.status_code == 200
    body = current.json()
    assert "api_bearer_token" not in body["read_only"]
    assert body["read_only"]["api_bearer_token_configured"] is True

    response = client.post(
        "/api/v1/config/apply",
        headers=headers,
        json={
            "config": {
                "strategy": {
                    "maker_margin": "0.02",
                }
            },
            "reason": "tighten paper quote margin",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["applied"] is True
    assert payload["config"]["strategy"]["maker_margin"] == "0.02"
    assert str(settings.maker_margin) == "0.02"
    assert list((tmp_path / "config" / "history").rglob("*.json"))


def test_runtime_config_rejects_deployment_fields(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        require_api_auth=False,
        recorder_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )
    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        "/api/v1/config/validate",
        json={
            "allow_live": True,
            "execution_mode": "live",
        },
    )

    assert response.status_code == 422
    assert settings.allow_live is False
    assert settings.execution_mode == "paper"


def test_websocket_sends_initial_snapshot_with_token_auth(tmp_path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            require_api_auth=True,
            api_bearer_token="secret-token",
            recorder_path=tmp_path / "events.jsonl",
            kill_switch_file=tmp_path / "KILL_SWITCH",
        )
    )

    with TestClient(app).websocket_connect("/api/v1/ws/live?token=secret-token") as websocket:
        message = websocket.receive_json()

    assert message["type"] == "status_snapshot"
    assert "status" in message["data"]


def test_control_pause_resume_is_audited(tmp_path) -> None:
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
    headers = {"Authorization": "Bearer secret-token"}

    paused = client.post(
        "/api/v1/control/pause",
        headers=headers,
        json={"reason": "operator hold", "source": "ui"},
    )

    assert paused.status_code == 200
    paused_payload = paused.json()
    assert paused_payload["control"]["paused"] is True
    assert paused_payload["audit_version"]
    assert client.get("/api/v1/status", headers=headers).json()["control"]["paused"] is True

    resumed = client.post(
        "/api/v1/control/resume",
        headers=headers,
        json={"reason": "resume paper loop", "source": "ui"},
    )

    assert resumed.status_code == 200
    assert resumed.json()["control"]["paused"] is False
    assert client.get("/api/v1/status", headers=headers).json()["control"]["paused"] is False
    assert list((tmp_path / "control" / "history").rglob("*.json"))

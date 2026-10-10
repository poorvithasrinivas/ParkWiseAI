
import importlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

import backend.db as database


@pytest.fixture(scope="session")
def test_app(tmp_path_factory):
    """
    Create an isolated database before importing the Flask application.
    The real data/parkwise.db is not used by these tests.
    """
    temp_dir = tmp_path_factory.mktemp("parkwise_test")
    test_db_path = temp_dir / "parkwise_test.db"

    original_db_path = database.DB_PATH
    original_data_dir = database.DATA_DIR

    database.DB_PATH = test_db_path
    database.DATA_DIR = temp_dir

    sys.modules.pop("backend.app", None)
    application_module = importlib.import_module("backend.app")
    application_module.app.config.update(TESTING=True)

    with database.get_connection() as connection:
        connection.executemany(
            """
            INSERT OR IGNORE INTO parking_slots
                (slot_id, zone, slot_type, is_active)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("TA01", "TEST_A", "REGULAR", 1),
                ("TA02", "TEST_A", "REGULAR", 1),
                ("TB01", "TEST_B", "EV", 1),
                ("TB02", "TEST_B", "ACCESSIBLE", 0),
            ],
        )

    yield application_module.app

    database.DB_PATH = original_db_path
    database.DATA_DIR = original_data_dir


@pytest.fixture
def client(test_app):
    return test_app.test_client()


def clear_scheduled_snapshots():
    """Remove scheduled snapshots from the isolated test database."""
    with database.get_connection() as connection:
        connection.execute(
            """
            DELETE FROM occupancy_history
            WHERE data_source = 'SCHEDULED_SNAPSHOT'
            """
        )


def insert_scheduled_snapshots(count=5):
    """Insert predictable snapshots spaced exactly 15 minutes apart."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    with database.get_connection() as connection:
        for index in range(count):
            timestamp = now - timedelta(minutes=15 * (count - 1 - index))

            connection.execute(
                """
                INSERT INTO occupancy_history (
                    timestamp,
                    occupied_count,
                    total_capacity,
                    data_source
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    timestamp.isoformat().replace("+00:00", "Z"),
                    index % 4,
                    3,
                    "SCHEDULED_SNAPSHOT",
                ),
            )


def test_health_endpoint_responds(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.is_json


def test_slots_endpoint_returns_json(client):
    response = client.get("/api/slots")

    assert response.status_code == 200
    assert response.is_json


def test_occupancy_analytics_returns_expected_capacity(client):
    response = client.get("/api/analytics/occupancy")

    assert response.status_code == 200

    result = response.get_json()

    assert result["data_source"] == "LIVE_PARKING_SESSIONS"
    assert result["occupancy"]["active_capacity"] == 3
    assert result["occupancy"]["occupied_slots"] == 0
    assert result["occupancy"]["available_slots"] == 3
    assert result["occupancy"]["occupancy_percentage"] == 0.0

    assert len(result["zones"]) == 2


def test_forecast_reports_insufficient_data_initially(client):
    clear_scheduled_snapshots()

    response = client.get("/api/analytics/forecast")

    assert response.status_code == 200

    result = response.get_json()

    assert result["status"] == "insufficient_data"
    assert result["observations_available"] == 0
    assert result["observations_required"] == 5
    assert result["predictions"] == []


def test_forecast_reports_insufficient_data_with_four_snapshots(client):
    clear_scheduled_snapshots()
    insert_scheduled_snapshots(count=4)

    response = client.get("/api/analytics/forecast")

    assert response.status_code == 200

    result = response.get_json()

    assert result["status"] == "insufficient_data"
    assert result["observations_available"] == 4
    assert result["observations_required"] == 5
    assert result["predictions"] == []


def test_forecast_returns_prediction_with_five_snapshots(
    client, monkeypatch
):
    clear_scheduled_snapshots()
    insert_scheduled_snapshots(count=5)

    def fake_predict_occupancy(
        recent_occupancies,
        total_capacity,
        forecast_minutes=30,
        observation_timestamp=None,
        data_source="provided_data",
    ):
        return {
            "forecast_minutes": forecast_minutes,
            "current_occupied": recent_occupancies[-1],
            "predicted_occupied": 2,
            "total_capacity": total_capacity,
            "occupancy_percentage": round(2 / total_capacity * 100, 2),
            "data_source": data_source,
        }

    monkeypatch.setattr(
        "backend.analytics_routes.predict_occupancy",
        fake_predict_occupancy,
    )

    response = client.get(
        "/api/analytics/forecast?forecast_minutes=30"
    )

    assert response.status_code == 200

    result = response.get_json()

    assert result["status"] == "success"
    assert result["observations_available"] == 5
    assert result["observations_required"] == 5
    assert len(result["predictions"]) == 1
    assert result["predictions"][0]["forecast_minutes"] == 30
    assert result["predictions"][0]["predicted_occupied"] == 2
    assert result["predictions"][0]["total_capacity"] == 3
    assert (
        result["predictions"][0]["data_source"]
        == "SCHEDULED_SNAPSHOT"
    )


@pytest.mark.parametrize("invalid_horizon", [0, 15, 45, 90, -30])
def test_forecast_rejects_invalid_horizons(client, invalid_horizon):
    response = client.get(
        f"/api/analytics/forecast?forecast_minutes={invalid_horizon}"
    )

    assert response.status_code == 400

    result = response.get_json()

    assert result["error"]["code"] == "INVALID_FORECAST_HORIZON"


def test_unknown_endpoint_returns_not_found(client):
    response = client.get("/api/this-endpoint-does-not-exist")

    assert response.status_code == 404

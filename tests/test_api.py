
import importlib
import sys

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

    # Import only after redirecting the database paths.
    sys.modules.pop("backend.app", None)
    application_module = importlib.import_module("backend.app")
    application_module.app.config.update(TESTING=True)

    # Add a small, predictable parking layout for the tests.
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

    # Restore the module's original database configuration.
    database.DB_PATH = original_db_path
    database.DATA_DIR = original_data_dir


@pytest.fixture
def client(test_app):
    return test_app.test_client()


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

    # Only active slots should contribute to capacity.
    assert len(result["zones"]) == 2


def test_forecast_reports_insufficient_data_initially(client):
    response = client.get("/api/analytics/forecast")

    assert response.status_code == 200

    result = response.get_json()

    assert result["status"] == "insufficient_data"
    assert result["observations_available"] == 0
    assert result["observations_required"] == 5
    assert result["predictions"] == []


def test_unknown_endpoint_returns_not_found(client):
    response = client.get("/api/this-endpoint-does-not-exist")

    assert response.status_code == 404

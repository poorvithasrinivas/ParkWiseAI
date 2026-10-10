
from datetime import datetime, timedelta, timezone

import pytest

import backend.db as database


@pytest.fixture
def forecast_client(tmp_path, monkeypatch):
    """Use an isolated temporary database for forecast integration tests."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "forecast_test.db")
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)

    database.initialize_database()

    from backend.app import app

    app.config.update(TESTING=True)

    with database.get_connection() as connection:
        now = datetime.now(timezone.utc).replace(
            second=0, microsecond=0
        )

        for index, occupied in enumerate([10, 12, 11, 13, 15]):
            timestamp = now - timedelta(minutes=15 * (4 - index))

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
                    occupied,
                    100,
                    "SCHEDULED_SNAPSHOT",
                ),
            )

    yield app.test_client()


def test_real_ml_models_work_through_forecast_api(forecast_client):
    """Verify both saved ML models work through the actual API."""
    response = forecast_client.get("/api/analytics/forecast")

    assert response.status_code == 200

    result = response.get_json()

    assert result["status"] == "success"
    assert result["observations_available"] == 5
    assert result["observations_required"] == 5

    predictions = result["predictions"]
    assert len(predictions) == 2

    by_horizon = {
        prediction["forecast_minutes"]: prediction
        for prediction in predictions
    }

    assert set(by_horizon) == {30, 60}

    for prediction in predictions:
        assert prediction["current_occupied"] == 15
        assert prediction["total_capacity"] == 100
        assert 0 <= prediction["predicted_occupied"] <= 100
        assert 0 <= prediction["predicted_occupancy_pct"] <= 100
        assert prediction["model_name"] == "RandomForestRegressor"
        assert prediction["data_source"] == "SCHEDULED_SNAPSHOT"
        assert prediction["forecast_for"]
        assert prediction["observation_timestamp"]


@pytest.mark.parametrize("horizon", [30, 60])
def test_real_ml_model_can_be_selected_by_horizon(
    forecast_client, horizon
):
    response = forecast_client.get(
        f"/api/analytics/forecast?forecast_minutes={horizon}"
    )

    assert response.status_code == 200

    result = response.get_json()

    assert result["status"] == "success"
    assert len(result["predictions"]) == 1
    assert result["predictions"][0]["forecast_minutes"] == horizon


@pytest.mark.parametrize("invalid_horizon", ["abc", "0", "15", "45", "90"])
def test_invalid_forecast_horizon_returns_400(
    forecast_client, invalid_horizon
):
    response = forecast_client.get(
        f"/api/analytics/forecast?forecast_minutes={invalid_horizon}"
    )

    assert response.status_code == 400

    result = response.get_json()
    assert result["error"]["code"] == "INVALID_FORECAST_HORIZON"


def test_insufficient_history_returns_empty_predictions(forecast_client):
    with database.get_connection() as connection:
        connection.execute(
            """
            DELETE FROM occupancy_history
            WHERE data_source = 'SCHEDULED_SNAPSHOT'
            """
        )

        now = datetime.now(timezone.utc).replace(
            second=0, microsecond=0
        )

        # Only four observations are available.
        for index, occupied in enumerate([10, 12, 11, 13]):
            timestamp = now - timedelta(minutes=15 * (3 - index))

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
                    occupied,
                    100,
                    "SCHEDULED_SNAPSHOT",
                ),
            )

    response = forecast_client.get("/api/analytics/forecast")

    assert response.status_code == 200

    result = response.get_json()
    assert result["status"] == "insufficient_data"
    assert result["observations_available"] == 4
    assert result["observations_required"] == 5
    assert result["predictions"] == []


def test_missing_model_returns_503(forecast_client, monkeypatch):
    def missing_model(**kwargs):
        raise FileNotFoundError("Test model file is missing")

    monkeypatch.setattr(
        "backend.analytics_routes.predict_occupancy",
        missing_model,
    )

    response = forecast_client.get(
        "/api/analytics/forecast?forecast_minutes=30"
    )

    assert response.status_code == 503

    result = response.get_json()
    assert result["error"]["code"] == "MODEL_NOT_FOUND"


def test_unexpected_forecast_error_returns_500(
    forecast_client, monkeypatch
):
    def failed_prediction(**kwargs):
        raise RuntimeError("Simulated forecasting failure")

    monkeypatch.setattr(
        "backend.analytics_routes.predict_occupancy",
        failed_prediction,
    )

    response = forecast_client.get(
        "/api/analytics/forecast?forecast_minutes=60"
    )

    assert response.status_code == 500

    result = response.get_json()
    assert result["error"]["code"] == "FORECAST_FAILED"

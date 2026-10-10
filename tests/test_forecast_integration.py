
from datetime import datetime, timedelta, timezone

import pytest

import backend.db as database


@pytest.fixture
def forecast_client(tmp_path, monkeypatch):
    """Use an isolated temporary database for real model integration."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "forecast_test.db")
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)

    database.initialize_database()

    # Import the Flask app after setting up the isolated database.
    from backend.app import app

    app.config.update(TESTING=True)

    with database.get_connection() as connection:
        now = datetime.now(timezone.utc).replace(
            second=0, microsecond=0
        )

        # Five observations, exactly 15 minutes apart.
        for index, occupied in enumerate([10, 12, 11, 13, 15]):
            timestamp = now - timedelta(
                minutes=15 * (4 - index)
            )

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
    """Verify the API invokes both actual saved forecasting models."""
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

    for horizon, prediction in by_horizon.items():
        assert prediction["current_occupied"] == 15
        assert prediction["total_capacity"] == 100
        assert 0 <= prediction["predicted_occupied"] <= 100
        assert 0 <= prediction["predicted_occupancy_pct"] <= 100
        assert prediction["model_name"] == "RandomForestRegressor"
        assert prediction["data_source"] == "SCHEDULED_SNAPSHOT"
        assert prediction["forecast_for"]
        assert prediction["observation_timestamp"]


@pytest.mark.parametrize(
    ("horizon", "expected_count"),
    [(30, 1), (60, 1)],
)
def test_real_ml_model_can_be_selected_by_horizon(
    forecast_client, horizon, expected_count
):
    response = forecast_client.get(
        f"/api/analytics/forecast?forecast_minutes={horizon}"
    )

    assert response.status_code == 200

    result = response.get_json()

    assert result["status"] == "success"
    assert len(result["predictions"]) == expected_count
    assert result["predictions"][0]["forecast_minutes"] == horizon

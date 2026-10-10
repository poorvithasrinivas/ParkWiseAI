
from pathlib import Path

import joblib
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent
MODEL_DIR = PROJECT_DIR / "models"

SUPPORTED_HORIZONS = (30, 60)


def predict_occupancy(
    recent_occupancies,
    total_capacity,
    forecast_minutes=30,
    observation_timestamp=None,
    data_source="provided_data",
):
    """
    Predict parking occupancy 30 or 60 minutes ahead.

    recent_occupancies:
        At least 5 occupancy counts, ordered oldest to newest.
        They must represent consecutive 15-minute observations.

    observation_timestamp:
        Timestamp of the newest occupancy observation.
        Must be provided explicitly.

    data_source:
        Label identifying where the input data came from.
    """

    # 1. Validate forecast horizon.
    if forecast_minutes not in SUPPORTED_HORIZONS:
        raise ValueError(
            "forecast_minutes must be either 30 or 60."
        )

    # 2. Load the correct trained model.
    model_file = (
        MODEL_DIR
        / f"occupancy_model_{forecast_minutes}min.joblib"
    )

    if not model_file.exists():
        raise FileNotFoundError(
            f"Model not found: {model_file}. "
            "Run python train.py first."
        )

    # 3. Validate parking capacity.
    if (
        isinstance(total_capacity, bool)
        or not isinstance(total_capacity, int)
        or total_capacity <= 0
    ):
        raise ValueError(
            "total_capacity must be a positive integer."
        )

    # 4. Validate occupancy history.
    if recent_occupancies is None or len(recent_occupancies) < 5:
        raise ValueError(
            "At least 5 recent occupancy observations are required."
        )

    occupancies = []

    for value in recent_occupancies:
        if isinstance(value, bool) or not isinstance(
            value, (int, float)
        ):
            raise ValueError(
                "Occupancy observations must be numbers."
            )

        if not float(value).is_integer():
            raise ValueError(
                "Occupancy observations must be whole numbers."
            )

        value = int(value)

        if value < 0 or value > total_capacity:
            raise ValueError(
                "Occupancy must be between zero and total_capacity."
            )

        occupancies.append(value)

    # 5. Require an explicit timestamp.
    # Never silently take one from the synthetic training dataset.
    if observation_timestamp is None:
        raise ValueError(
            "observation_timestamp is required. "
            "Provide the timestamp of the latest occupancy observation."
        )

    latest_timestamp = pd.Timestamp(observation_timestamp)

    if pd.isna(latest_timestamp):
        raise ValueError(
            "observation_timestamp is invalid."
        )

    # Convert timezone-aware timestamps to UTC.
    # Naive timestamps are treated as UTC.
    if latest_timestamp.tzinfo is not None:
        latest_timestamp = (
            latest_timestamp
            .tz_convert("UTC")
            .tz_localize(None)
        )

    if not isinstance(data_source, str) or not data_source.strip():
        raise ValueError(
            "data_source must be a non-empty string."
        )

    # 6. Load the trained model and its feature order.
    saved_model = joblib.load(model_file)

    model = saved_model["model"]
    features = saved_model["features"]

    # 7. Build features in the same format used during training.
    input_data = pd.DataFrame(
        [
            {
                "hour": (
                    latest_timestamp.hour
                    + latest_timestamp.minute / 60
                ),
                "day_of_week": latest_timestamp.dayofweek,
                "total_capacity": total_capacity,
                "occupancy_lag_1": occupancies[-2],
                "occupancy_lag_4": occupancies[-5],
            }
        ]
    )

    missing_features = set(features) - set(input_data.columns)

    if missing_features:
        raise ValueError(
            f"Missing model features: {sorted(missing_features)}"
        )

    input_data = input_data[features]

    # 8. Generate the prediction and enforce physical limits.
    prediction = float(model.predict(input_data)[0])

    predicted_occupied = max(
        0,
        min(total_capacity, round(prediction)),
    )

    # 9. Calculate the time for which the forecast applies.
    forecast_for = (
        latest_timestamp
        + pd.Timedelta(minutes=forecast_minutes)
    )

    # 10. Return a structured result for future API integration.
    return {
        "forecast_minutes": forecast_minutes,
        "observation_timestamp": latest_timestamp.isoformat() + "Z",
        "forecast_for": forecast_for.isoformat() + "Z",
        "current_occupied": occupancies[-1],
        "predicted_occupied": predicted_occupied,
        "total_capacity": total_capacity,
        "predicted_occupancy_pct": round(
            predicted_occupied / total_capacity * 100,
            2,
        ),
        "model_name": "RandomForestRegressor",
        "data_source": data_source,
    }


if __name__ == "__main__":
    # Clearly labelled synthetic data for local testing only.
    recent_occupancies = [10, 12, 11, 13, 15]

    for horizon in SUPPORTED_HORIZONS:
        result = predict_occupancy(
            recent_occupancies=recent_occupancies,
            total_capacity=100,
            forecast_minutes=horizon,
            observation_timestamp="2026-10-10T14:00:00",
            data_source="synthetic_demo",
        )

        print(f"\nParkWise AI - {horizon}-Minute Forecast")
        print("-" * 40)

        for key, value in result.items():
            print(f"{key}: {value}")

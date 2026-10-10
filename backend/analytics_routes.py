
from datetime import datetime, timezone

import pandas as pd
from flask import Blueprint, current_app, jsonify, request

from backend.db import get_connection
from backend.ml_forecasting.forecast import (
    SUPPORTED_HORIZONS,
    predict_occupancy,
)

analytics_bp = Blueprint("analytics", __name__)


def utc_timestamp():
    """Return a timezone-aware UTC timestamp in ISO 8601 format."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def get_current_occupancy(connection):
    """Calculate current occupancy from active parking sessions."""
    totals = connection.execute("""
        SELECT
            COUNT(*) AS total_slots,
            COALESCE(
                SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0
            ) AS active_capacity
        FROM parking_slots
    """).fetchone()

    occupied_row = connection.execute("""
        SELECT COUNT(DISTINCT r.slot_id) AS occupied_count
        FROM parking_sessions AS ps
        JOIN reservations AS r
          ON r.reservation_id = ps.reservation_id
        JOIN parking_slots AS s
          ON s.slot_id = r.slot_id
        WHERE ps.actual_exit_time IS NULL
          AND s.is_active = 1
    """).fetchone()

    total_slots = totals["total_slots"] or 0
    capacity = totals["active_capacity"] or 0
    occupied = occupied_row["occupied_count"] or 0

    return {
        "total_slots": total_slots,
        "active_capacity": capacity,
        "occupied_slots": occupied,
        "available_slots": max(capacity - occupied, 0),
        "occupancy_percentage": (
            round(occupied / capacity * 100, 2)
            if capacity else 0.0
        ),
    }


def record_live_snapshot(source):
    """Record a snapshot derived from actual parking-session data."""
    with get_connection() as connection:
        occupancy = get_current_occupancy(connection)

        connection.execute("""
            INSERT INTO occupancy_history (
                timestamp,
                occupied_count,
                total_capacity,
                data_source
            )
            VALUES (?, ?, ?, ?)
        """, (
            utc_timestamp(),
            occupancy["occupied_slots"],
            occupancy["active_capacity"],
            source,
        ))


@analytics_bp.record_once
def register_occupancy_event_hook(setup_state):
    """Record occupancy after successful entry and exit requests."""
    app = setup_state.app

    @app.after_request
    def record_after_vehicle_event(response):
        if (
            response.status_code == 200
            and request.path in ("/api/entry", "/api/exit")
        ):
            try:
                record_live_snapshot("VEHICLE_ENTRY_EXIT")
            except Exception:
                current_app.logger.exception(
                    "Unable to record occupancy snapshot"
                )

        return response


@analytics_bp.get("/api/analytics/occupancy")
def occupancy_analytics():
    try:
        with get_connection() as connection:
            current = get_current_occupancy(connection)

            zone_rows = connection.execute("""
                SELECT
                    s.zone,
                    COUNT(*) AS total_slots,
                    SUM(
                        CASE WHEN s.is_active = 1 THEN 1 ELSE 0 END
                    ) AS active_capacity,
                    SUM(
                        CASE
                            WHEN s.is_active = 1
                             AND EXISTS (
                                 SELECT 1
                                 FROM reservations AS r
                                 JOIN parking_sessions AS ps
                                   ON ps.reservation_id = r.reservation_id
                                 WHERE r.slot_id = s.slot_id
                                   AND ps.actual_exit_time IS NULL
                             )
                            THEN 1
                            ELSE 0
                        END
                    ) AS occupied_slots
                FROM parking_slots AS s
                GROUP BY s.zone
                ORDER BY s.zone
            """).fetchall()

        zones = []

        for row in zone_rows:
            capacity = row["active_capacity"] or 0
            occupied = row["occupied_slots"] or 0

            zones.append({
                "zone": row["zone"],
                "total_slots": row["total_slots"],
                "active_capacity": capacity,
                "occupied_slots": occupied,
                "available_slots": max(capacity - occupied, 0),
                "occupancy_percentage": (
                    round(occupied / capacity * 100, 2)
                    if capacity else 0.0
                ),
            })

        return jsonify({
            "timestamp": utc_timestamp(),
            "occupancy": current,
            "zones": zones,
            "data_source": "LIVE_PARKING_SESSIONS",
        }), 200

    except Exception:
        current_app.logger.exception(
            "Failed to calculate occupancy analytics"
        )
        return jsonify({
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Unable to calculate occupancy analytics.",
            }
        }), 500


@analytics_bp.get("/api/analytics/forecast")
def occupancy_forecast():
    """Forecast occupancy using the trained 30- and 60-minute models."""
    try:
        requested_horizon = request.args.get("forecast_minutes")

        if requested_horizon is not None:
            try:
                requested_horizon = int(requested_horizon)
            except ValueError:
                return jsonify({
                    "error": {
                        "code": "INVALID_FORECAST_HORIZON",
                        "message": "forecast_minutes must be 30 or 60.",
                    }
                }), 400

            if requested_horizon not in SUPPORTED_HORIZONS:
                return jsonify({
                    "error": {
                        "code": "INVALID_FORECAST_HORIZON",
                        "message": "forecast_minutes must be 30 or 60.",
                    }
                }), 400

            horizons = (requested_horizon,)
        else:
            horizons = SUPPORTED_HORIZONS

        # Only scheduled snapshots are used for forecasting.
        with get_connection() as connection:
            observations = connection.execute("""
                SELECT timestamp, occupied_count, total_capacity
                FROM occupancy_history
                WHERE data_source = 'SCHEDULED_SNAPSHOT'
                ORDER BY timestamp DESC, id DESC
                LIMIT 5
            """).fetchall()

        required = 5

        if len(observations) < required:
            return jsonify({
                "status": "insufficient_data",
                "message": (
                    "Five scheduled occupancy observations are required, "
                    "spaced approximately 15 minutes apart."
                ),
                "observations_available": len(observations),
                "observations_required": required,
                "predictions": [],
            }), 200

        # Convert newest-first database results to oldest-first.
        observations = list(reversed(observations))

        try:
            timestamps = [
                pd.Timestamp(row["timestamp"])
                for row in observations
            ]
        except (TypeError, ValueError):
            return jsonify({
                "status": "insufficient_data",
                "message": "An occupancy observation has an invalid timestamp.",
                "observations_available": len(observations),
                "observations_required": required,
                "predictions": [],
            }), 200

        if any(pd.isna(timestamp) for timestamp in timestamps):
            return jsonify({
                "status": "insufficient_data",
                "message": "An occupancy observation has an invalid timestamp.",
                "observations_available": len(observations),
                "observations_required": required,
                "predictions": [],
            }), 200

        # Ensure timestamps are consecutive 15-minute observations.
        for previous, latest in zip(timestamps, timestamps[1:]):
            elapsed_seconds = (latest - previous).total_seconds()

            if abs(elapsed_seconds - 900) > 60:
                return jsonify({
                    "status": "insufficient_data",
                    "message": (
                        "The latest observations are not consecutive "
                        "15-minute measurements. Keep the collector "
                        "running and try again after enough snapshots."
                    ),
                    "observations_available": len(observations),
                    "observations_required": required,
                    "predictions": [],
                }), 200

        capacities = {
            row["total_capacity"] for row in observations
        }

        if len(capacities) != 1:
            return jsonify({
                "status": "insufficient_data",
                "message": (
                    "Parking capacity changed during the observation "
                    "window. A consistent observation window is required."
                ),
                "observations_available": len(observations),
                "observations_required": required,
                "predictions": [],
            }), 200

        capacity = next(iter(capacities))

        if capacity is None or capacity <= 0:
            return jsonify({
                "status": "insufficient_data",
                "message": "A positive parking capacity is required.",
                "observations_available": len(observations),
                "observations_required": required,
                "predictions": [],
            }), 200

        recent_occupancies = [
            row["occupied_count"] for row in observations
        ]

        predictions = []

        for horizon in horizons:
            predictions.append(
                predict_occupancy(
                    recent_occupancies=recent_occupancies,
                    total_capacity=int(capacity),
                    forecast_minutes=horizon,
                    observation_timestamp=timestamps[-1].isoformat(),
                    data_source="SCHEDULED_SNAPSHOT",
                )
            )

        return jsonify({
            "status": "success",
            "observations_available": len(observations),
            "observations_required": required,
            "predictions": predictions,
        }), 200

    except FileNotFoundError:
        current_app.logger.exception(
            "Occupancy forecasting model file is missing"
        )
        return jsonify({
            "error": {
                "code": "MODEL_NOT_FOUND",
                "message": (
                    "The trained occupancy forecasting model is unavailable."
                ),
            }
        }), 503

    except Exception:
        current_app.logger.exception(
            "Failed to generate occupancy forecast"
        )
        return jsonify({
            "error": {
                "code": "FORECAST_FAILED",
                "message": "Unable to generate occupancy forecasts.",
            }
        }), 500

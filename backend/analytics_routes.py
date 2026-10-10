
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request

from backend.db import get_connection


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
    try:
        with get_connection() as connection:
            observations = connection.execute("""
                SELECT timestamp, occupied_count, total_capacity
                FROM occupancy_history
                WHERE data_source = 'VEHICLE_ENTRY_EXIT'
                ORDER BY timestamp DESC
                LIMIT 500
            """).fetchall()

        if len(observations) < 24:
            return jsonify({
                "status": "insufficient_data",
                "message": (
                    "At least 24 vehicle-entry/exit occupancy observations "
                    "are required before forecasting can be evaluated."
                ),
                "observations_available": len(observations),
                "observations_required": 24,
                "predictions": [],
            }), 200

        return jsonify({
            "status": "model_not_connected",
            "message": (
                "Occupancy observations are available, but the trained "
                "ML forecasting model has not been connected yet."
            ),
            "observations_available": len(observations),
            "predictions": [],
        }), 200

    except Exception:
        current_app.logger.exception(
            "Failed to retrieve occupancy forecast data"
        )
        return jsonify({
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Unable to retrieve occupancy forecast data.",
            }
        }), 500

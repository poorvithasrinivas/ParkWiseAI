
from datetime import datetime, timedelta, timezone
from statistics import median

import pandas as pd
from flask import Blueprint, current_app, jsonify, request

from backend.db import get_connection
from backend.ml_forecasting.forecast import (
    MODEL_DIR,
    SUPPORTED_HORIZONS,
    predict_occupancy,
)

analytics_bp = Blueprint("analytics", __name__)
VALID_SLOT_TYPES = {"REGULAR", "EV", "ACCESSIBLE"}
VALID_VEHICLE_TYPES = {"CAR", "BIKE", "MOTORCYCLE", "EV"}


def utc_timestamp():
    """Return a timezone-aware UTC timestamp in ISO 8601 format."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def parse_api_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A timezone-aware ISO 8601 timestamp is required.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Use a valid ISO 8601 timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamps must include a timezone, such as Z.")
    return parsed.astimezone(timezone.utc)


def db_timestamp(value):
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


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
        should_record = (
            request.path in ("/api/entry", "/api/exit")
            and response.status_code == 200
        ) or (
            request.path == "/api/reservations"
            and request.method == "POST"
            and response.status_code == 201
        )
        if should_record:
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


@analytics_bp.get("/api/analytics/intelligence")
def parking_intelligence():
    """Summarize real occupancy trends, at-risk bookings, and release estimates."""
    now = datetime.now(timezone.utc)
    now_text = db_timestamp(now)
    window_start = db_timestamp(now - timedelta(hours=24))

    try:
        with get_connection() as connection:
            occupancy = get_current_occupancy(connection)
            history = connection.execute("""
                SELECT timestamp, occupied_count, total_capacity, data_source
                FROM occupancy_history
                WHERE timestamp >= ?
                ORDER BY timestamp DESC, id DESC
                LIMIT 48
            """, (window_start,)).fetchall()
            reservations = connection.execute("""
                SELECT r.reservation_id, r.slot_id, s.zone,
                       r.start_time, r.end_time, r.status,
                       v.registration_no AS vehicle_no,
                       v.vehicle_type,
                       ps.actual_entry_time AS entry_time
                FROM reservations r
                JOIN vehicles v ON v.vehicle_id = r.vehicle_id
                JOIN parking_slots s ON s.slot_id = r.slot_id
                LEFT JOIN parking_sessions ps
                  ON ps.reservation_id = r.reservation_id
                WHERE r.status IN ('CONFIRMED', 'CHECKED_IN')
                ORDER BY r.end_time, r.start_time
            """).fetchall()
            completed = connection.execute("""
                SELECT r.end_time, ps.actual_entry_time, ps.actual_exit_time
                FROM reservations r
                JOIN parking_sessions ps
                  ON ps.reservation_id = r.reservation_id
                WHERE r.status = 'COMPLETED'
                  AND ps.actual_entry_time IS NOT NULL
                  AND ps.actual_exit_time IS NOT NULL
                ORDER BY ps.actual_exit_time DESC
                LIMIT 200
            """).fetchall()

        trends = []
        for row in reversed(history):
            capacity = int(row["total_capacity"] or 0)
            occupied = int(row["occupied_count"] or 0)
            trends.append({
                "timestamp": row["timestamp"],
                "occupied": occupied,
                "capacity": capacity,
                "occupancy_percentage": (
                    round(occupied / capacity * 100, 1) if capacity else 0
                ),
                "source": row["data_source"],
            })

        historical_overstays = []
        for row in completed:
            try:
                planned_end = parse_api_timestamp(row["end_time"])
                actual_exit = parse_api_timestamp(row["actual_exit_time"])
            except ValueError:
                continue
            late_minutes = (actual_exit - planned_end).total_seconds() / 60
            if late_minutes > 0:
                historical_overstays.append(late_minutes)

        typical_overstay_minutes = (
            round(median(historical_overstays))
            if historical_overstays else 0
        )
        releases = []
        alerts = []

        for row in reservations:
            try:
                start = parse_api_timestamp(row["start_time"])
                end = parse_api_timestamp(row["end_time"])
            except ValueError:
                current_app.logger.warning(
                    "Skipping reservation with invalid timestamps: %s",
                    row["reservation_id"],
                )
                continue

            details = {
                "reservation_id": row["reservation_id"],
                "slot_id": row["slot_id"],
                "zone": row["zone"],
                "vehicle_no": row["vehicle_no"],
                "vehicle_type": row["vehicle_type"],
                "status": row["status"],
            }

            if row["status"] == "CHECKED_IN":
                overdue_minutes = max(
                    0,
                    int((now - end).total_seconds() // 60),
                )
                if end > now:
                    estimated_release = end + timedelta(
                        minutes=typical_overstay_minutes
                    )
                    confidence = "MEDIUM" if historical_overstays else "LOW"
                    estimate_basis = (
                        f"Scheduled end plus {typical_overstay_minutes}-minute "
                        "median historical overstay."
                        if historical_overstays
                        else "Scheduled end time; no completed overstay history yet."
                    )
                else:
                    remaining_minutes = max(
                        5,
                        typical_overstay_minutes - overdue_minutes,
                    ) if historical_overstays else 15
                    estimated_release = now + timedelta(minutes=remaining_minutes)
                    confidence = "LOW"
                    estimate_basis = (
                        f"Overdue by {overdue_minutes} minutes; estimated using "
                        f"historical median overstay ({typical_overstay_minutes} minutes)."
                        if historical_overstays
                        else f"Overdue by {overdue_minutes} minutes; using a 15-minute "
                             "operational estimate until more history is available."
                    )

                releases.append({
                    **details,
                    "scheduled_end": row["end_time"],
                    "overdue_minutes": overdue_minutes,
                    "estimated_available_at": db_timestamp(estimated_release),
                    "confidence": confidence,
                    "estimate_basis": estimate_basis,
                })
                if overdue_minutes > 0:
                    alerts.append({
                        **details,
                        "kind": "OVERSTAY",
                        "severity": "HIGH" if overdue_minutes >= 15 else "MEDIUM",
                        "minutes": overdue_minutes,
                        "message": f"Vehicle is {overdue_minutes} minutes past its scheduled end.",
                        "estimated_available_at": db_timestamp(estimated_release),
                    })
                elif (end - now).total_seconds() <= 30 * 60:
                    alerts.append({
                        **details,
                        "kind": "DUE_SOON",
                        "severity": "LOW",
                        "minutes": max(0, int((end - now).total_seconds() // 60)),
                        "message": "Checked-in vehicle is due to leave soon.",
                        "estimated_available_at": db_timestamp(estimated_release),
                    })
                continue

            if now >= end:
                alerts.append({
                    **details,
                    "kind": "LIKELY_UNUSED",
                    "severity": "MEDIUM",
                    "minutes": int((now - end).total_seconds() // 60),
                    "message": "Reservation ended without a recorded vehicle entry.",
                    "estimated_available_at": now_text,
                })
            elif now >= start + timedelta(minutes=15):
                alerts.append({
                    **details,
                    "kind": "ARRIVAL_AT_RISK",
                    "severity": "MEDIUM",
                    "minutes": int((now - start).total_seconds() // 60),
                    "message": "Reservation has started, but no vehicle entry is recorded.",
                    "estimated_available_at": row["end_time"],
                })

        alerts.sort(
            key=lambda item: (
                {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(item["severity"], 3),
                -item["minutes"],
            )
        )

        return jsonify({
            "generated_at": now_text,
            "occupancy": occupancy,
            "trend": trends,
            "alerts": alerts,
            "estimated_releases": sorted(
                releases,
                key=lambda item: item["estimated_available_at"],
            ),
            "summary": {
                "trend_observations": len(trends),
                "alerts": len(alerts),
                "overstays": sum(
                    1 for item in alerts if item["kind"] == "OVERSTAY"
                ),
                "unused_at_risk": sum(
                    1 for item in alerts
                    if item["kind"] in {"LIKELY_UNUSED", "ARRIVAL_AT_RISK"}
                ),
                "estimated_releases": len(releases),
                "historical_overstay_samples": len(historical_overstays),
                "median_overstay_minutes": typical_overstay_minutes,
            },
        }), 200
    except Exception:
        current_app.logger.exception("Failed to calculate parking intelligence")
        return jsonify({
            "error": {
                "code": "INTELLIGENCE_FAILED",
                "message": "Unable to calculate parking intelligence.",
            }
        }), 500


@analytics_bp.get("/api/analytics/recommendations")
def slot_recommendations():
    """Rank available spaces using live utilization and booked demand."""
    try:
        start = parse_api_timestamp(request.args.get("start_time"))
        end = parse_api_timestamp(request.args.get("end_time"))
    except ValueError as error:
        return jsonify({
            "error": {
                "code": "INVALID_TIME_RANGE",
                "message": str(error),
            }
        }), 400

    if start >= end:
        return jsonify({
            "error": {
                "code": "INVALID_TIME_RANGE",
                "message": "start_time must be earlier than end_time.",
            }
        }), 400

    slot_type = request.args.get("slot_type", "REGULAR").strip().upper()
    vehicle_type = request.args.get("vehicle_type", "CAR").strip().upper()
    if slot_type not in VALID_SLOT_TYPES:
        return jsonify({
            "error": {
                "code": "INVALID_SLOT_TYPE",
                "message": "slot_type must be REGULAR, EV, or ACCESSIBLE.",
            }
        }), 400
    if vehicle_type not in VALID_VEHICLE_TYPES:
        return jsonify({
            "error": {
                "code": "INVALID_VEHICLE_TYPE",
                "message": "Unsupported vehicle type.",
            }
        }), 400

    start_text = db_timestamp(start)
    end_text = db_timestamp(end)

    try:
        with get_connection() as connection:
            slots = connection.execute("""
                SELECT s.slot_id, s.zone, s.slot_type,
                    EXISTS (
                        SELECT 1
                        FROM parking_sessions ps
                        JOIN reservations r
                          ON r.reservation_id = ps.reservation_id
                        WHERE r.slot_id = s.slot_id
                          AND ps.actual_exit_time IS NULL
                    ) AS occupied,
                    EXISTS (
                        SELECT 1 FROM reservations r
                        WHERE r.slot_id = s.slot_id
                          AND r.status IN ('CONFIRMED', 'CHECKED_IN')
                          AND r.start_time < ?
                          AND r.end_time > ?
                    ) AS booked
                FROM parking_slots s
                WHERE s.is_active = 1 AND s.slot_type = ?
                ORDER BY s.zone, s.slot_id
            """, (end_text, start_text, slot_type)).fetchall()

        capacities = {}
        busy_counts = {}
        eligible = []
        for row in slots:
            zone = row["zone"]
            capacities[zone] = capacities.get(zone, 0) + 1
            if row["occupied"] or row["booked"]:
                busy_counts[zone] = busy_counts.get(zone, 0) + 1
            else:
                eligible.append(row)

        zone_candidates = {}
        for row in eligible:
            zone = row["zone"]
            capacity = capacities[zone]
            projected_busy = busy_counts.get(zone, 0)
            utilization = (
                round(projected_busy / capacity * 100, 1)
                if capacity else 100.0
            )
            zone_candidates.setdefault(zone, []).append({
                "slot_id": row["slot_id"],
                "zone": zone,
                "slot_type": row["slot_type"],
                "score": round(100 - utilization, 1),
                "projected_zone_occupancy": utilization,
                "available_in_zone": len([
                    candidate for candidate in eligible
                    if candidate["zone"] == zone
                ]),
                "zone_capacity": capacity,
                "reason": (
                    f"Zone {zone} is projected to be {utilization:.0f}% occupied "
                    f"for this time window, with "
                    f"{sum(1 for candidate in eligible if candidate['zone'] == zone)} "
                    "space(s) currently free."
                ),
            })

        recommendations = []
        for zone in sorted(zone_candidates):
            zone_candidates[zone].sort(key=lambda item: item["slot_id"])
            recommendations.append(zone_candidates[zone][0])
        recommendations.sort(
            key=lambda item: (
                -item["score"],
                -item["available_in_zone"],
                item["zone"],
                item["slot_id"],
            )
        )
        recommendations = recommendations[:5]

        return jsonify({
            "generated_at": utc_timestamp(),
            "start_time": start_text,
            "end_time": end_text,
            "vehicle_type": vehicle_type,
            "slot_type": slot_type,
            "ranking_method": "LIVE_ZONE_UTILIZATION_AND_BOOKED_DEMAND",
            "available_count": len(eligible),
            "recommendations": recommendations,
        }), 200
    except Exception:
        current_app.logger.exception("Failed to recommend parking spaces")
        return jsonify({
            "error": {
                "code": "RECOMMENDATION_FAILED",
                "message": "Unable to recommend parking spaces.",
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

        # Prefer a consecutive scheduled window; otherwise use recent live events.
        required = 5
        with get_connection() as connection:
            scheduled_observations = connection.execute("""
                SELECT timestamp, occupied_count, total_capacity, data_source
                FROM occupancy_history
                WHERE data_source = 'SCHEDULED_SNAPSHOT'
                ORDER BY timestamp DESC, id DESC
                LIMIT 5
            """).fetchall()

            use_scheduled = len(scheduled_observations) == required
            if use_scheduled:
                try:
                    scheduled_timestamps = [
                        pd.Timestamp(row["timestamp"])
                        for row in scheduled_observations
                    ]
                    use_scheduled = all(
                        not pd.isna(timestamp)
                        for timestamp in scheduled_timestamps
                    ) and all(
                        abs((latest - previous).total_seconds() - 900) <= 60
                        for previous, latest in zip(
                            reversed(scheduled_timestamps[1:]),
                            reversed(scheduled_timestamps[:-1]),
                        )
                    )
                except (TypeError, ValueError):
                    use_scheduled = False

            if use_scheduled:
                observations = scheduled_observations
            else:
                observations = connection.execute("""
                    SELECT timestamp, occupied_count, total_capacity, data_source
                    FROM occupancy_history
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 5
                """).fetchall()

        if len(observations) < required:
            return jsonify({
                "status": "insufficient_data",
                "message": (
                    "Five occupancy observations are required. "
                    "Make some reservations or check in/out vehicles to generate data."
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

        # Skip strict 15-min gap check for non-scheduled data.
        all_scheduled = all(
            row["data_source"] == "SCHEDULED_SNAPSHOT"
            for row in observations
        ) if hasattr(observations[0], "keys") else False

        if all_scheduled:
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
        observation_source = (
            "SCHEDULED_SNAPSHOT"
            if all(
                row["data_source"] == "SCHEDULED_SNAPSHOT"
                for row in observations
            )
            else "LIVE_ACTIVITY_SNAPSHOTS"
        )

        predictions = []

        for horizon in horizons:
            predictions.append(
                predict_occupancy(
                    recent_occupancies=recent_occupancies,
                    total_capacity=int(capacity),
                    forecast_minutes=horizon,
                    observation_timestamp=timestamps[-1].isoformat(),
                    data_source=observation_source,
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


@analytics_bp.get("/api/analytics/model-status")
def model_status():
    models = [{
        "forecast_minutes": horizon,
        "model_name": "RandomForestRegressor",
        "file": f"occupancy_model_{horizon}min.joblib",
        "available": (
            MODEL_DIR / f"occupancy_model_{horizon}min.joblib"
        ).is_file(),
    } for horizon in SUPPORTED_HORIZONS]

    return jsonify({
        "models": models,
        "observations_required": 5,
        "scheduled_interval_minutes": 15,
    }), 200

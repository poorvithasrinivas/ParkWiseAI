
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from flask_cors import CORS

from backend.db import get_connection, initialize_database

app = Flask(__name__)
CORS(app)

initialize_database()


def parse_timestamp(value):
    """Parse an ISO 8601 timestamp and normalize it to UTC."""
    if not value:
        raise ValueError("A timestamp is required.")

    try:
        parsed = datetime.fromisoformat(
            value.strip().replace("Z", "+00:00")
        )
    except ValueError:
        raise ValueError(
            "Use ISO 8601 timestamps, for example "
            "2026-10-11T14:00:00Z."
        )

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            "Timestamps must include a timezone, such as Z for UTC."
        )

    return parsed.astimezone(timezone.utc)


def timestamp_to_db(value):
    """Return a consistently formatted UTC timestamp for SQLite."""
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


@app.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "ParkWise AI Backend"
    }), 200


@app.get("/api/slots")
def get_slots():
    """List all configured parking slots."""
    try:
        with get_connection() as connection:
            rows = connection.execute(
                """
                SELECT slot_id, zone, slot_type, is_active
                FROM parking_slots
                ORDER BY zone, slot_id
                """
            ).fetchall()

        slots = [
            {
                "slot_id": row["slot_id"],
                "zone": row["zone"],
                "slot_type": row["slot_type"],
                "is_active": bool(row["is_active"]),
                "current_status": "AVAILABLE"
            }
            for row in rows
        ]

        return jsonify({
            "total_slots": len(slots),
            "slots": slots
        }), 200

    except Exception:
        app.logger.exception("Failed to retrieve parking slots")
        return jsonify({
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Unable to retrieve parking slots."
            }
        }), 500


@app.get("/api/availability")
def get_availability():
    """
    Find slots free for the entire requested time interval.

    Required query parameters:
      start_time, end_time

    Optional query parameters:
      slot_type, zone
    """
    try:
        start = parse_timestamp(request.args.get("start_time"))
        end = parse_timestamp(request.args.get("end_time"))

        if start >= end:
            return jsonify({
                "error": {
                    "code": "INVALID_TIME_RANGE",
                    "message": "start_time must be earlier than end_time."
                }
            }), 400

        start_text = timestamp_to_db(start)
        end_text = timestamp_to_db(end)

        slot_type = request.args.get("slot_type", "").strip().upper()
        zone = request.args.get("zone", "").strip().upper()

        if slot_type and slot_type not in {
            "REGULAR", "EV", "ACCESSIBLE"
        }:
            return jsonify({
                "error": {
                    "code": "INVALID_SLOT_TYPE",
                    "message": "slot_type must be REGULAR, EV, or ACCESSIBLE."
                }
            }), 400

        query = """
            SELECT s.slot_id, s.zone, s.slot_type, s.is_active
            FROM parking_slots AS s
            WHERE s.is_active = 1

              AND NOT EXISTS (
                  SELECT 1
                  FROM reservations AS r
                  WHERE r.slot_id = s.slot_id
                    AND r.status IN ('CONFIRMED', 'CHECKED_IN')
                    AND r.start_time < ?
                    AND r.end_time > ?
              )

              AND NOT EXISTS (
                  SELECT 1
                  FROM parking_sessions AS ps
                  JOIN reservations AS r2
                    ON r2.reservation_id = ps.reservation_id
                  WHERE r2.slot_id = s.slot_id
                    AND ps.actual_exit_time IS NULL
              )
        """

        params = [end_text, start_text]

        if slot_type:
            query += " AND s.slot_type = ?"
            params.append(slot_type)

        if zone:
            query += " AND s.zone = ?"
            params.append(zone)

        query += " ORDER BY s.zone, s.slot_id"

        with get_connection() as connection:
            rows = connection.execute(query, params).fetchall()

        available_slots = [
            {
                "slot_id": row["slot_id"],
                "zone": row["zone"],
                "slot_type": row["slot_type"]
            }
            for row in rows
        ]

        return jsonify({
            "start_time": start_text,
            "end_time": end_text,
            "available_count": len(available_slots),
            "available_slots": available_slots
        }), 200

    except ValueError as error:
        return jsonify({
            "error": {
                "code": "INVALID_REQUEST",
                "message": str(error)
            }
        }), 400

    except Exception:
        app.logger.exception("Failed to check parking availability")
        return jsonify({
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Unable to check parking availability."
            }
        }), 500


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )

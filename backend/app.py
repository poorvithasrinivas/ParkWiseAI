
from datetime import datetime, timezone
import secrets
import string
import re

from flask import Flask, jsonify, request
from flask_cors import CORS

from backend.db import get_connection, initialize_database

app = Flask(__name__)
CORS(app)

initialize_database()


def parse_timestamp(value):
    """Parse a timezone-aware ISO 8601 timestamp and normalize to UTC."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A timestamp is required.")

    try:
        parsed = datetime.fromisoformat(
            value.strip().replace("Z", "+00:00")
        )
    except ValueError:
        raise ValueError(
            "Use ISO 8601 timestamps, such as 2026-10-11T14:00:00Z."
        )

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            "Timestamps must include a timezone, such as Z for UTC."
        )

    return parsed.astimezone(timezone.utc)


def timestamp_to_db(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def make_reservation_id():
    alphabet = string.ascii_uppercase + string.digits
    return "PK-" + "".join(secrets.choice(alphabet) for _ in range(10))


def error_response(code, message, status):
    return jsonify({
        "error": {
            "code": code,
            "message": message
        }
    }), status


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
        return error_response(
            "INTERNAL_ERROR",
            "Unable to retrieve parking slots.",
            500
        )


@app.get("/api/availability")
def get_availability():
    """Find slots available for the entire requested time interval."""
    try:
        start = parse_timestamp(request.args.get("start_time"))
        end = parse_timestamp(request.args.get("end_time"))

        if start >= end:
            return error_response(
                "INVALID_TIME_RANGE",
                "start_time must be earlier than end_time.",
                400
            )

        start_text = timestamp_to_db(start)
        end_text = timestamp_to_db(end)

        slot_type = request.args.get("slot_type", "").strip().upper()
        zone = request.args.get("zone", "").strip().upper()

        valid_types = {"REGULAR", "EV", "ACCESSIBLE"}

        if slot_type and slot_type not in valid_types:
            return error_response(
                "INVALID_SLOT_TYPE",
                "slot_type must be REGULAR, EV, or ACCESSIBLE.",
                400
            )

        query = """
            SELECT s.slot_id, s.zone, s.slot_type
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

        slots = [
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
            "available_count": len(slots),
            "available_slots": slots
        }), 200

    except ValueError as error:
        return error_response("INVALID_REQUEST", str(error), 400)

    except Exception:
        app.logger.exception("Failed to check parking availability")
        return error_response(
            "INTERNAL_ERROR",
            "Unable to check parking availability.",
            500
        )


@app.post("/api/reservations")
def create_reservation():
    """
    Create a reservation and atomically assign a suitable parking slot.

    Required JSON:
      vehicle_no, vehicle_type, start_time, end_time

    Optional JSON:
      slot_type, zone
    """
    body = request.get_json(silent=True)

    if not isinstance(body, dict):
        return error_response(
            "INVALID_JSON",
            "Send a valid JSON object.",
            400
        )

    vehicle_no = body.get("vehicle_no")
    vehicle_type = body.get("vehicle_type")
    requested_slot_type = body.get("slot_type", "REGULAR")
    requested_zone = body.get("zone", "")

    if not all(
        isinstance(value, str) and value.strip()
        for value in (vehicle_no, vehicle_type, requested_slot_type)
    ):
        return error_response(
            "MISSING_FIELDS",
            "vehicle_no, vehicle_type, start_time, and end_time are required.",
            400
        )

    vehicle_no = vehicle_no.strip().upper()
    vehicle_type = vehicle_type.strip().upper()
    requested_slot_type = requested_slot_type.strip().upper()

    if not re.fullmatch(r"[A-Z0-9 -]{4,15}", vehicle_no):
        return error_response(
            "INVALID_VEHICLE_NO",
            "Enter a valid vehicle registration number.",
            400
        )

    if vehicle_type not in {"CAR", "BIKE", "MOTORCYCLE", "EV"}:
        return error_response(
            "INVALID_VEHICLE_TYPE",
            "vehicle_type must be CAR, BIKE, MOTORCYCLE, or EV.",
            400
        )

    if requested_slot_type not in {"REGULAR", "EV", "ACCESSIBLE"}:
        return error_response(
            "INVALID_SLOT_TYPE",
            "slot_type must be REGULAR, EV, or ACCESSIBLE.",
            400
        )

    if not isinstance(requested_zone, str):
        return error_response(
            "INVALID_ZONE",
            "zone must be a string.",
            400
        )

    requested_zone = requested_zone.strip().upper()

    try:
        start = parse_timestamp(body.get("start_time"))
        end = parse_timestamp(body.get("end_time"))
    except ValueError as error:
        return error_response("INVALID_REQUEST", str(error), 400)

    if start >= end:
        return error_response(
            "INVALID_TIME_RANGE",
            "start_time must be earlier than end_time.",
            400
        )

    start_text = timestamp_to_db(start)
    end_text = timestamp_to_db(end)
    created_at = timestamp_to_db(datetime.now(timezone.utc))

    # Restrict a vehicle to one active/future booking that overlaps
    # its requested interval. This is checked again inside the transaction.
    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")

            existing_vehicle = connection.execute(
                """
                SELECT vehicle_id
                FROM vehicles
                WHERE registration_no = ?
                """,
                (vehicle_no,)
            ).fetchone()

            if existing_vehicle:
                vehicle_id = existing_vehicle["vehicle_id"]

                duplicate = connection.execute(
                    """
                    SELECT reservation_id
                    FROM reservations
                    WHERE vehicle_id = ?
                      AND status IN ('CONFIRMED', 'CHECKED_IN')
                      AND start_time < ?
                      AND end_time > ?
                    LIMIT 1
                    """,
                    (vehicle_id, end_text, start_text)
                ).fetchone()

                if duplicate:
                    connection.rollback()
                    return error_response(
                        "VEHICLE_ALREADY_RESERVED",
                        "This vehicle already has an overlapping reservation.",
                        409
                    )

                connection.execute(
                    """
                    UPDATE vehicles
                    SET vehicle_type = ?
                    WHERE vehicle_id = ?
                    """,
                    (vehicle_type, vehicle_id)
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO vehicles
                        (registration_no, vehicle_type, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (vehicle_no, vehicle_type, created_at)
                )
                vehicle_id = cursor.lastrowid

            # A checked-in vehicle without an exit blocks its slot,
            # even when the original reservation time has ended.
            query = """
                SELECT s.slot_id
                FROM parking_slots AS s
                WHERE s.is_active = 1
                  AND s.slot_type = ?
                  AND (? = '' OR s.zone = ?)

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

                ORDER BY s.zone, s.slot_id
                LIMIT 1
            """

            selected = connection.execute(
                query,
                (
                    requested_slot_type,
                    requested_zone,
                    requested_zone,
                    end_text,
                    start_text
                )
            ).fetchone()

            if not selected:
                connection.rollback()
                return error_response(
                    "NO_AVAILABLE_SLOT",
                    "No suitable parking slot is available for that time.",
                    409
                )

            reservation_id = make_reservation_id()

            connection.execute(
                """
                INSERT INTO reservations (
                    reservation_id,
                    vehicle_id,
                    slot_id,
                    start_time,
                    end_time,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, 'CONFIRMED', ?)
                """,
                (
                    reservation_id,
                    vehicle_id,
                    selected["slot_id"],
                    start_text,
                    end_text,
                    created_at
                )
            )

            connection.commit()

        return jsonify({
            "reservation_id": reservation_id,
            "vehicle_no": vehicle_no,
            "vehicle_type": vehicle_type,
            "slot_id": selected["slot_id"],
            "slot_type": requested_slot_type,
            "zone": requested_zone or None,
            "start_time": start_text,
            "end_time": end_text,
            "status": "CONFIRMED"
        }), 201

    except Exception:
        app.logger.exception("Failed to create reservation")
        return error_response(
            "INTERNAL_ERROR",
            "Unable to create reservation.",
            500
        )


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )

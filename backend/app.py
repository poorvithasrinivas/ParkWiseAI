
from datetime import datetime, timezone, timedelta
import secrets
import string
import re

from flask import Flask, jsonify, request
from flask_cors import CORS

from backend.db import get_connection, initialize_database

app = Flask(__name__)
CORS(app)
initialize_database()

from backend.analytics_routes import analytics_bp
app.register_blueprint(analytics_bp)
from backend.copilot_routes import copilot_bp
app.register_blueprint(copilot_bp)

VALID_SLOT_TYPES = {"REGULAR", "EV", "ACCESSIBLE"}
VALID_VEHICLE_TYPES = {"CAR", "BIKE", "MOTORCYCLE", "EV"}


def parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A timestamp is required.")
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Use an ISO 8601 timestamp with a timezone.")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamps must include a timezone, such as Z.")
    return result.astimezone(timezone.utc)


def timestamp_to_db(value):
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def error_response(code, message, status):
    return jsonify({"error": {"code": code, "message": message}}), status


def make_reservation_id():
    alphabet = string.ascii_uppercase + string.digits
    return "PK-" + "".join(secrets.choice(alphabet) for _ in range(10))


@app.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "ParkWise AI Backend"
    }), 200


@app.get("/api/slots")
def get_slots():
    try:
        with get_connection() as connection:
            rows = connection.execute("""
                SELECT s.slot_id, s.zone, s.slot_type, s.is_active,
                       active.vehicle_type,
                       active.registration_no AS vehicle_no,
                       active.slot_id IS NOT NULL AS occupied
                FROM parking_slots s
                LEFT JOIN (
                    SELECT r.slot_id, v.vehicle_type, v.registration_no
                    FROM parking_sessions ps
                    JOIN reservations r
                      ON r.reservation_id = ps.reservation_id
                    JOIN vehicles v
                      ON v.vehicle_id = r.vehicle_id
                    WHERE ps.actual_exit_time IS NULL
                ) AS active ON active.slot_id = s.slot_id
                ORDER BY s.zone, s.slot_id
            """).fetchall()

        slots = [{
            "slot_id": row["slot_id"],
            "zone": row["zone"],
            "slot_type": row["slot_type"],
            "is_active": bool(row["is_active"]),
            "current_status": "OCCUPIED" if row["occupied"] else "AVAILABLE",
            "vehicle_type": row["vehicle_type"],
            "vehicle_no": row["vehicle_no"],
        } for row in rows]

        return jsonify({"total_slots": len(slots), "slots": slots}), 200
    except Exception:
        app.logger.exception("Failed to retrieve slots")
        return error_response("INTERNAL_ERROR", "Unable to retrieve slots.", 500)


@app.get("/api/availability")
def get_availability():
    try:
        start = parse_timestamp(request.args.get("start_time"))
        end = parse_timestamp(request.args.get("end_time"))

        if start >= end:
            return error_response(
                "INVALID_TIME_RANGE",
                "start_time must be earlier than end_time.",
                400
            )

        slot_type = request.args.get("slot_type", "").strip().upper()
        zone = request.args.get("zone", "").strip().upper()

        if slot_type and slot_type not in VALID_SLOT_TYPES:
            return error_response(
                "INVALID_SLOT_TYPE",
                "slot_type must be REGULAR, EV, or ACCESSIBLE.",
                400
            )

        start_text = timestamp_to_db(start)
        end_text = timestamp_to_db(end)

        query = """
            SELECT s.slot_id, s.zone, s.slot_type
            FROM parking_slots s
            WHERE s.is_active = 1
              AND NOT EXISTS (
                  SELECT 1 FROM reservations r
                  WHERE r.slot_id = s.slot_id
                    AND r.status IN ('CONFIRMED', 'CHECKED_IN')
                    AND r.start_time < ?
                    AND r.end_time > ?
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM parking_sessions ps
                  JOIN reservations r2
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

        slots = [{
            "slot_id": row["slot_id"],
            "zone": row["zone"],
            "slot_type": row["slot_type"]
        } for row in rows]

        return jsonify({
            "start_time": start_text,
            "end_time": end_text,
            "available_count": len(slots),
            "available_slots": slots
        }), 200

    except ValueError as error:
        return error_response("INVALID_REQUEST", str(error), 400)
    except Exception:
        app.logger.exception("Availability query failed")
        return error_response(
            "INTERNAL_ERROR", "Unable to check availability.", 500
        )


@app.post("/api/reservations")
def create_reservation():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error_response("INVALID_JSON", "Send a valid JSON object.", 400)

    vehicle_no = body.get("vehicle_no")
    vehicle_type = body.get("vehicle_type")
    slot_type = body.get("slot_type", "REGULAR")
    zone = body.get("zone", "")
    preferred_slot_id = body.get("preferred_slot_id", "")

    if not all(
        isinstance(v, str) and v.strip()
        for v in (vehicle_no, vehicle_type, slot_type)
    ):
        return error_response(
            "MISSING_FIELDS",
            "vehicle_no, vehicle_type, start_time and end_time are required.",
            400
        )

    vehicle_no = vehicle_no.strip().upper()
    vehicle_type = vehicle_type.strip().upper()
    slot_type = slot_type.strip().upper()

    if not re.fullmatch(r"[A-Z0-9 -]{4,15}", vehicle_no):
        return error_response("INVALID_VEHICLE_NO", "Invalid vehicle number.", 400)
    if vehicle_type not in VALID_VEHICLE_TYPES:
        return error_response("INVALID_VEHICLE_TYPE", "Unsupported vehicle type.", 400)
    if slot_type not in VALID_SLOT_TYPES:
        return error_response("INVALID_SLOT_TYPE", "Unsupported slot type.", 400)
    if not isinstance(zone, str):
        return error_response("INVALID_ZONE", "zone must be a string.", 400)
    if not isinstance(preferred_slot_id, str):
        return error_response(
            "INVALID_SLOT", "preferred_slot_id must be a string.", 400
        )

    zone = zone.strip().upper()
    preferred_slot_id = preferred_slot_id.strip().upper()

    try:
        start = parse_timestamp(body.get("start_time"))
        end = parse_timestamp(body.get("end_time"))
    except ValueError as error:
        return error_response("INVALID_REQUEST", str(error), 400)

    if start >= end:
        return error_response(
            "INVALID_TIME_RANGE", "start_time must be earlier than end_time.", 400
        )

    start_text = timestamp_to_db(start)
    end_text = timestamp_to_db(end)
    created_at = timestamp_to_db(datetime.now(timezone.utc))

    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")

            vehicle = connection.execute(
                "SELECT vehicle_id FROM vehicles WHERE registration_no = ?",
                (vehicle_no,)
            ).fetchone()

            if vehicle:
                vehicle_id = vehicle["vehicle_id"]
                duplicate = connection.execute("""
                    SELECT reservation_id FROM reservations
                    WHERE vehicle_id = ?
                      AND status IN ('CONFIRMED', 'CHECKED_IN')
                      AND start_time < ? AND end_time > ?
                    LIMIT 1
                """, (vehicle_id, end_text, start_text)).fetchone()

                if duplicate:
                    connection.rollback()
                    return error_response(
                        "VEHICLE_ALREADY_RESERVED",
                        "This vehicle already has an overlapping reservation.",
                        409
                    )

                connection.execute(
                    "UPDATE vehicles SET vehicle_type = ? WHERE vehicle_id = ?",
                    (vehicle_type, vehicle_id)
                )
            else:
                cursor = connection.execute("""
                    INSERT INTO vehicles (registration_no, vehicle_type, created_at)
                    VALUES (?, ?, ?)
                """, (vehicle_no, vehicle_type, created_at))
                vehicle_id = cursor.lastrowid

            selected = connection.execute("""
                SELECT s.slot_id, s.zone
                FROM parking_slots s
                WHERE s.is_active = 1
                  AND s.slot_type = ?
                  AND (? = '' OR s.zone = ?)
                  AND (? = '' OR s.slot_id = ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM reservations r
                      WHERE r.slot_id = s.slot_id
                        AND r.status IN ('CONFIRMED', 'CHECKED_IN')
                        AND r.start_time < ? AND r.end_time > ?
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM parking_sessions ps
                      JOIN reservations r2
                        ON r2.reservation_id = ps.reservation_id
                      WHERE r2.slot_id = s.slot_id
                        AND ps.actual_exit_time IS NULL
                  )
                ORDER BY s.zone, s.slot_id
                LIMIT 1
            """, (
                slot_type, zone, zone,
                preferred_slot_id, preferred_slot_id,
                end_text, start_text
            )).fetchone()

            if not selected:
                connection.rollback()
                if preferred_slot_id:
                    return error_response(
                        "RECOMMENDED_SLOT_UNAVAILABLE",
                        "That suggested space is no longer available. Refresh recommendations and try another space.",
                        409
                    )
                return error_response(
                    "NO_AVAILABLE_SLOT",
                    "No suitable parking slot is available for that time.",
                    409
                )

            reservation_id = make_reservation_id()
            connection.execute("""
                INSERT INTO reservations (
                    reservation_id, vehicle_id, slot_id, start_time,
                    end_time, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, 'CONFIRMED', ?)
            """, (
                reservation_id, vehicle_id, selected["slot_id"],
                start_text, end_text, created_at
            ))
            connection.commit()

        return jsonify({
            "reservation_id": reservation_id,
            "vehicle_no": vehicle_no,
            "vehicle_type": vehicle_type,
            "slot_id": selected["slot_id"],
            "slot_type": slot_type,
            "zone": selected["zone"],
            "start_time": start_text,
            "end_time": end_text,
            "status": "CONFIRMED"
        }), 201

    except Exception:
        app.logger.exception("Reservation creation failed")
        return error_response(
            "INTERNAL_ERROR", "Unable to create reservation.", 500
        )


@app.get("/api/reservations")
def list_active_reservations():
    status_filter = request.args.get("status", "ACTIVE").strip().upper()
    status_values = {
        "ACTIVE": ("CONFIRMED", "CHECKED_IN"),
        "ALL": ("CONFIRMED", "CHECKED_IN", "COMPLETED", "CANCELLED", "EXPIRED", "NO_SHOW"),
        "CONFIRMED": ("CONFIRMED",),
        "CHECKED_IN": ("CHECKED_IN",),
    }
    statuses = status_values.get(status_filter)
    if statuses is None:
        return error_response(
            "INVALID_STATUS_FILTER",
            "status must be ACTIVE, ALL, CONFIRMED, or CHECKED_IN.",
            400
        )

    placeholders = ", ".join("?" for _ in statuses)
    try:
        with get_connection() as connection:
            rows = connection.execute(f"""
                SELECT r.reservation_id, v.registration_no AS vehicle_no,
                       v.vehicle_type, r.slot_id, s.zone, s.slot_type,
                       r.start_time, r.end_time, r.status, r.created_at,
                       ps.actual_entry_time AS entry_time,
                       ps.actual_exit_time AS exit_time
                FROM reservations AS r
                JOIN vehicles AS v ON v.vehicle_id = r.vehicle_id
                JOIN parking_slots AS s ON s.slot_id = r.slot_id
                LEFT JOIN parking_sessions AS ps
                  ON ps.reservation_id = r.reservation_id
                WHERE r.status IN ({placeholders})
                ORDER BY
                    CASE r.status WHEN 'CHECKED_IN' THEN 0 ELSE 1 END,
                    r.created_at DESC
                LIMIT 100
            """, statuses).fetchall()

        reservations = [dict(row) for row in rows]
        return jsonify({
            "total_reservations": len(reservations),
            "reservations": reservations
        }), 200
    except Exception:
        app.logger.exception("Active reservations lookup failed")
        return error_response(
            "INTERNAL_ERROR", "Unable to retrieve active reservations.", 500
        )


@app.get("/api/reservations/<reservation_id>")
def get_reservation(reservation_id):
    try:
        with get_connection() as connection:
            row = connection.execute("""
                SELECT r.reservation_id,
                       v.registration_no AS vehicle_no,
                       v.vehicle_type,
                       r.slot_id, s.zone, s.slot_type,
                       r.start_time, r.end_time, r.status, r.created_at,
                       ps.actual_entry_time AS entry_time,
                       ps.actual_exit_time AS exit_time
                FROM reservations r
                JOIN vehicles v ON v.vehicle_id = r.vehicle_id
                JOIN parking_slots s ON s.slot_id = r.slot_id
                LEFT JOIN parking_sessions ps
                  ON ps.reservation_id = r.reservation_id
                WHERE r.reservation_id = ?
            """, (reservation_id.strip().upper(),)).fetchone()

        if not row:
            return error_response(
                "RESERVATION_NOT_FOUND", "Reservation not found.", 404
            )
        return jsonify(dict(row)), 200

    except Exception:
        app.logger.exception("Reservation lookup failed")
        return error_response(
            "INTERNAL_ERROR", "Unable to retrieve reservation.", 500
        )


@app.post("/api/entry")
def vehicle_entry():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error_response("INVALID_JSON", "Send a valid JSON object.", 400)

    reservation_id = body.get("reservation_id")
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        return error_response(
            "MISSING_FIELDS", "reservation_id is required.", 400
        )

    reservation_id = reservation_id.strip().upper()
    current = datetime.now(timezone.utc)
    current_text = timestamp_to_db(current)

    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reservation = connection.execute("""
                SELECT reservation_id, start_time, end_time, status, slot_id
                FROM reservations WHERE reservation_id = ?
            """, (reservation_id,)).fetchone()

            if not reservation:
                connection.rollback()
                return error_response(
                    "RESERVATION_NOT_FOUND", "Reservation not found.", 404
                )

            if reservation["status"] != "CONFIRMED":
                connection.rollback()
                return error_response(
                    "INVALID_RESERVATION_STATUS",
                    "Only confirmed reservations can check in.",
                    409
                )

            start = parse_timestamp(reservation["start_time"])
            end = parse_timestamp(reservation["end_time"])

            if current < start - timedelta(minutes=15):
                connection.rollback()
                return error_response(
                    "TOO_EARLY",
                    "Check-in opens 15 minutes before the reservation starts.",
                    409
                )

            if current >= end:
                connection.execute("""
                    UPDATE reservations SET status = 'EXPIRED'
                    WHERE reservation_id = ?
                """, (reservation_id,))
                connection.commit()
                return error_response(
                    "RESERVATION_EXPIRED",
                    "The reservation has passed its scheduled end time.",
                    409
                )

            connection.execute("""
                INSERT INTO parking_sessions (
                    reservation_id, actual_entry_time, actual_exit_time
                ) VALUES (?, ?, NULL)
            """, (reservation_id, current_text))

            connection.execute("""
                UPDATE reservations SET status = 'CHECKED_IN'
                WHERE reservation_id = ?
            """, (reservation_id,))
            connection.commit()

        return jsonify({
            "reservation_id": reservation_id,
            "slot_id": reservation["slot_id"],
            "status": "CHECKED_IN",
            "entry_time": current_text
        }), 200

    except Exception:
        app.logger.exception("Vehicle entry failed")
        return error_response(
            "INTERNAL_ERROR", "Unable to check in vehicle.", 500
        )


@app.post("/api/exit")
def vehicle_exit():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error_response("INVALID_JSON", "Send a valid JSON object.", 400)

    reservation_id = body.get("reservation_id")
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        return error_response(
            "MISSING_FIELDS", "reservation_id is required.", 400
        )

    reservation_id = reservation_id.strip().upper()
    exit_text = timestamp_to_db(datetime.now(timezone.utc))

    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reservation = connection.execute("""
                SELECT status, slot_id FROM reservations
                WHERE reservation_id = ?
            """, (reservation_id,)).fetchone()

            if not reservation:
                connection.rollback()
                return error_response(
                    "RESERVATION_NOT_FOUND", "Reservation not found.", 404
                )

            if reservation["status"] != "CHECKED_IN":
                connection.rollback()
                return error_response(
                    "NOT_CHECKED_IN",
                    "Vehicle must be checked in before it can exit.",
                    409
                )

            session = connection.execute("""
                SELECT actual_entry_time, actual_exit_time
                FROM parking_sessions WHERE reservation_id = ?
            """, (reservation_id,)).fetchone()

            if not session or session["actual_exit_time"] is not None:
                connection.rollback()
                return error_response(
                    "NO_ACTIVE_SESSION",
                    "No active parking session exists.",
                    409
                )

            connection.execute("""
                UPDATE parking_sessions SET actual_exit_time = ?
                WHERE reservation_id = ?
            """, (exit_text, reservation_id))

            connection.execute("""
                UPDATE reservations SET status = 'COMPLETED'
                WHERE reservation_id = ?
            """, (reservation_id,))
            connection.commit()

        return jsonify({
            "reservation_id": reservation_id,
            "slot_id": reservation["slot_id"],
            "status": "COMPLETED",
            "entry_time": session["actual_entry_time"],
            "exit_time": exit_text
        }), 200

    except Exception:
        app.logger.exception("Vehicle exit failed")
        return error_response(
            "INTERNAL_ERROR", "Unable to check out vehicle.", 500
        )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)

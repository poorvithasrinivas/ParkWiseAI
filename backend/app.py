
from flask import Flask, jsonify
from flask_cors import CORS

from backend.db import get_connection, initialize_database

app = Flask(__name__)
CORS(app)

# Initialize the SQLite database and tables when the app starts.
initialize_database()


@app.get("/api/health")
def health():
    """Check whether the backend is running."""
    return jsonify({
        "status": "ok",
        "service": "ParkWise AI Backend"
    }), 200


@app.get("/api/slots")
def get_slots():
    """Return all configured parking slots."""
    try:
        with get_connection() as connection:
            rows = connection.execute(
                """
                SELECT slot_id, zone, slot_type, is_active
                FROM parking_slots
                ORDER BY zone, slot_id
                """
            ).fetchall()

        slots = []

        for row in rows:
            slots.append({
                "slot_id": row["slot_id"],
                "zone": row["zone"],
                "slot_type": row["slot_type"],
                "is_active": bool(row["is_active"]),
                # Temporary until actual parking-session tracking is implemented.
                "current_status": "AVAILABLE"
            })

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


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )

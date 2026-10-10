
from datetime import datetime, timedelta, timezone

import pytest

import backend.db as database


@pytest.fixture
def reservation_client(tmp_path, monkeypatch):
    """Run reservation tests against an isolated temporary database."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)

    from backend.db import initialize_database

    initialize_database()

    with database.get_connection() as connection:
        connection.executemany(
            """
            INSERT INTO parking_slots
                (slot_id, zone, slot_type, is_active)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("TA01", "TEST_A", "REGULAR", 1),
                ("TA02", "TEST_A", "REGULAR", 1),
                ("TB01", "TEST_B", "EV", 1),
            ],
        )

    # Import the Flask app only after configuring the temporary database.
    import backend.app as application_module

    application_module.app.config.update(TESTING=True)
    return application_module.app.test_client()


def future_times(start_minutes=60, duration_minutes=30):
    start = datetime.now(timezone.utc) + timedelta(minutes=start_minutes)
    end = start + timedelta(minutes=duration_minutes)
    return start.isoformat(), end.isoformat()


def reservation_payload(vehicle_no="KA01AB1234", slot_type="REGULAR"):
    start_time, end_time = future_times()
    return {
        "vehicle_no": vehicle_no,
        "vehicle_type": "CAR",
        "slot_type": slot_type,
        "zone": "TEST_A" if slot_type == "REGULAR" else "TEST_B",
        "start_time": start_time,
        "end_time": end_time,
    }


def test_create_reservation_returns_confirmed_reservation(reservation_client):
    response = reservation_client.post(
        "/api/reservations",
        json=reservation_payload(),
    )

    assert response.status_code == 201
    result = response.get_json()
    assert result["status"] == "CONFIRMED"
    assert result["vehicle_no"] == "KA01AB1234"
    assert result["slot_id"] in ("TA01", "TA02")
    assert result["reservation_id"]


def test_invalid_vehicle_type_is_rejected(reservation_client):
    payload = reservation_payload()
    payload["vehicle_type"] = "SPACESHIP"

    response = reservation_client.post("/api/reservations", json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "INVALID_VEHICLE_TYPE"


def test_invalid_time_range_is_rejected(reservation_client):
    payload = reservation_payload()
    payload["end_time"] = payload["start_time"]

    response = reservation_client.post("/api/reservations", json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "INVALID_TIME_RANGE"


def test_overlapping_reservation_for_same_vehicle_is_rejected(
    reservation_client,
):
    payload = reservation_payload("KA02CD5678")
    first = reservation_client.post("/api/reservations", json=payload)
    assert first.status_code == 201

    second_payload = payload.copy()
    second_payload["start_time"], second_payload["end_time"] = future_times(
        start_minutes=65,
        duration_minutes=20,
    )

    second = reservation_client.post(
        "/api/reservations",
        json=second_payload,
    )

    assert second.status_code == 409
    assert (
        second.get_json()["error"]["code"]
        == "VEHICLE_ALREADY_RESERVED"
    )


def test_reservation_can_be_retrieved(reservation_client):
    created = reservation_client.post(
        "/api/reservations",
        json=reservation_payload("KA03EF9012"),
    )
    assert created.status_code == 201

    reservation_id = created.get_json()["reservation_id"]
    response = reservation_client.get(
        f"/api/reservations/{reservation_id}"
    )

    assert response.status_code == 200
    assert response.get_json()["reservation_id"] == reservation_id
    assert response.get_json()["status"] == "CONFIRMED"


def test_entry_rejects_unknown_reservation(reservation_client):
    response = reservation_client.post(
        "/api/entry",
        json={"reservation_id": "UNKNOWN-RESERVATION"},
    )

    assert response.status_code == 404


def test_exit_rejects_unknown_reservation(reservation_client):
    response = reservation_client.post(
        "/api/exit",
        json={"reservation_id": "UNKNOWN-RESERVATION"},
    )

    assert response.status_code == 404

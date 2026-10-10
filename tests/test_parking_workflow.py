
from datetime import datetime, timedelta, timezone

import pytest

import backend.db as database


@pytest.fixture
def workflow_client(tmp_path, monkeypatch):
    """Create an isolated database for parking workflow tests."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "workflow.db")
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
                ("WA01", "WORKFLOW_A", "REGULAR", 1),
                ("WA02", "WORKFLOW_A", "REGULAR", 1),
            ],
        )

    import backend.app as application_module

    application_module.app.config.update(TESTING=True)
    return application_module.app.test_client()


def make_times():
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    end = datetime.now(timezone.utc) + timedelta(hours=1)
    return start.isoformat(), end.isoformat()


def create_reservation(client):
    start_time, end_time = make_times()

    response = client.post(
        "/api/reservations",
        json={
            "vehicle_no": "KA09XY4321",
            "vehicle_type": "CAR",
            "slot_type": "REGULAR",
            "zone": "WORKFLOW_A",
            "start_time": start_time,
            "end_time": end_time,
        },
    )

    assert response.status_code == 201
    return response.get_json()


def test_full_parking_workflow_updates_occupancy(workflow_client):
    # 1. Create a reservation.
    reservation = create_reservation(workflow_client)
    reservation_id = reservation["reservation_id"]

    assert reservation["status"] == "CONFIRMED"

    # 2. Confirm both slots are initially available.
    before_entry = workflow_client.get("/api/analytics/occupancy")
    assert before_entry.status_code == 200

    initial_occupancy = before_entry.get_json()["occupancy"]
    assert initial_occupancy["active_capacity"] == 2
    assert initial_occupancy["occupied_slots"] == 0
    assert initial_occupancy["available_slots"] == 2

    # 3. Check the vehicle in.
    entry = workflow_client.post(
        "/api/entry",
        json={"reservation_id": reservation_id},
    )

    assert entry.status_code == 200
    assert entry.get_json()["status"] == "CHECKED_IN"

    # 4. Occupancy should increase after check-in.
    during_session = workflow_client.get("/api/analytics/occupancy")
    assert during_session.status_code == 200

    current_occupancy = during_session.get_json()["occupancy"]
    assert current_occupancy["occupied_slots"] == 1
    assert current_occupancy["available_slots"] == 1

    # 5. Check the vehicle out.
    exit_response = workflow_client.post(
        "/api/exit",
        json={"reservation_id": reservation_id},
    )

    assert exit_response.status_code == 200
    assert exit_response.get_json()["status"] == "COMPLETED"

    # 6. Occupancy should return to its initial state.
    after_exit = workflow_client.get("/api/analytics/occupancy")
    assert after_exit.status_code == 200

    final_occupancy = after_exit.get_json()["occupancy"]
    assert final_occupancy["active_capacity"] == 2
    assert final_occupancy["occupied_slots"] == 0
    assert final_occupancy["available_slots"] == 2


def test_entry_without_reservation_does_not_change_occupancy(
    workflow_client,
):
    before = workflow_client.get("/api/analytics/occupancy").get_json()

    response = workflow_client.post(
        "/api/entry",
        json={"reservation_id": "UNKNOWN-RESERVATION"},
    )

    assert response.status_code == 404

    after = workflow_client.get("/api/analytics/occupancy").get_json()

    assert (
        after["occupancy"]["occupied_slots"]
        == before["occupancy"]["occupied_slots"]
    )


def test_exit_before_entry_is_rejected(workflow_client):
    reservation = create_reservation(workflow_client)

    response = workflow_client.post(
        "/api/exit",
        json={"reservation_id": reservation["reservation_id"]},
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "NOT_CHECKED_IN"

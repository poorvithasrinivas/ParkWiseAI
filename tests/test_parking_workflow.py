
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

    slot_response = workflow_client.get("/api/slots")
    occupied_slot = next(
        slot for slot in slot_response.get_json()["slots"]
        if slot["slot_id"] == reservation["slot_id"]
    )
    assert occupied_slot["current_status"] == "OCCUPIED"
    assert occupied_slot["vehicle_type"] == "CAR"
    assert occupied_slot["vehicle_no"] == "KA09XY4321"

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
    available_slot = next(
        slot for slot in workflow_client.get("/api/slots").get_json()["slots"]
        if slot["slot_id"] == reservation["slot_id"]
    )
    assert available_slot["current_status"] == "AVAILABLE"
    assert available_slot["vehicle_type"] is None


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


def test_active_reservations_show_entry_and_exit_actions(workflow_client):
    reservation = create_reservation(workflow_client)
    reservation_id = reservation["reservation_id"]

    confirmed = workflow_client.get("/api/reservations?status=ACTIVE")
    assert confirmed.status_code == 200
    assert confirmed.get_json()["reservations"][0]["status"] == "CONFIRMED"

    checked_in = workflow_client.post(
        "/api/entry",
        json={"reservation_id": reservation_id},
    )
    assert checked_in.status_code == 200

    active = workflow_client.get("/api/reservations?status=ACTIVE")
    result = active.get_json()["reservations"][0]
    assert result["status"] == "CHECKED_IN"
    assert result["entry_time"]
    assert result["slot_id"] == reservation["slot_id"]


def test_recommendations_exclude_booked_spaces_and_reserve_exact_suggestion(
    workflow_client,
):
    booked = create_reservation(workflow_client)
    now = datetime.now(timezone.utc)
    start = (now + timedelta(minutes=5)).isoformat()
    end = (now + timedelta(minutes=35)).isoformat()

    response = workflow_client.get(
        "/api/analytics/recommendations",
        query_string={
            "start_time": start,
            "end_time": end,
            "slot_type": "REGULAR",
            "vehicle_type": "CAR",
        },
    )

    assert response.status_code == 200
    result = response.get_json()
    assert result["available_count"] == 1
    assert [item["slot_id"] for item in result["recommendations"]] == ["WA02"]
    assert result["recommendations"][0]["score"] == 50
    assert result["ranking_method"] == "LIVE_ZONE_UTILIZATION_AND_BOOKED_DEMAND"

    booking = workflow_client.post(
        "/api/reservations",
        json={
            "vehicle_no": "REC12345",
            "vehicle_type": "CAR",
            "slot_type": "REGULAR",
            "zone": "WORKFLOW_A",
            "preferred_slot_id": "WA02",
            "start_time": start,
            "end_time": end,
        },
    )
    assert booking.status_code == 201
    assert booking.get_json()["slot_id"] == "WA02"
    assert booked["slot_id"] == "WA01"


def test_recommendations_validate_time_range_and_types(workflow_client):
    missing_times = workflow_client.get("/api/analytics/recommendations")
    assert missing_times.status_code == 400
    assert missing_times.get_json()["error"]["code"] == "INVALID_TIME_RANGE"

    now = datetime.now(timezone.utc)
    invalid_range = workflow_client.get(
        "/api/analytics/recommendations",
        query_string={
            "start_time": (now + timedelta(hours=1)).isoformat(),
            "end_time": (now + timedelta(minutes=30)).isoformat(),
        },
    )
    assert invalid_range.status_code == 400

    invalid_type = workflow_client.get(
        "/api/analytics/recommendations",
        query_string={
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
            "slot_type": "MYSTERY",
        },
    )
    assert invalid_type.status_code == 400
    assert invalid_type.get_json()["error"]["code"] == "INVALID_SLOT_TYPE"


def test_intelligence_flags_no_show_risk_and_estimates_checked_in_release(
    workflow_client,
):
    checked_in = create_reservation(workflow_client)
    entry = workflow_client.post(
        "/api/entry",
        json={"reservation_id": checked_in["reservation_id"]},
    )
    assert entry.status_code == 200

    unused = workflow_client.post(
        "/api/reservations",
        json={
            "vehicle_no": "UNUSED123",
            "vehicle_type": "CAR",
            "slot_type": "REGULAR",
            "zone": "WORKFLOW_A",
            "start_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "end_time": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        },
    )
    assert unused.status_code == 201

    past_end = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    past_start = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with database.get_connection() as connection:
        connection.execute(
            """
            UPDATE reservations
            SET start_time = ?, end_time = ?
            WHERE reservation_id = ?
            """,
            (past_start, past_end, checked_in["reservation_id"]),
        )
        connection.execute(
            """
            UPDATE reservations
            SET start_time = ?, end_time = ?
            WHERE reservation_id = ?
            """,
            (past_start, past_end, unused.get_json()["reservation_id"]),
        )

    response = workflow_client.get("/api/analytics/intelligence")
    assert response.status_code == 200
    result = response.get_json()
    assert result["summary"]["overstays"] == 1
    assert result["summary"]["unused_at_risk"] == 1
    assert result["summary"]["estimated_releases"] == 1

    alert_kinds = {alert["kind"] for alert in result["alerts"]}
    assert "OVERSTAY" in alert_kinds
    assert "LIKELY_UNUSED" in alert_kinds
    release = result["estimated_releases"][0]
    assert release["slot_id"] == checked_in["slot_id"]
    assert release["confidence"] == "LOW"
    assert release["estimated_available_at"]
    assert result["trend"]


def test_release_estimate_uses_completed_overstay_history(workflow_client):
    completed = create_reservation(workflow_client)
    assert workflow_client.post(
        "/api/entry",
        json={"reservation_id": completed["reservation_id"]},
    ).status_code == 200

    completed_end = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    completed_start = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with database.get_connection() as connection:
        connection.execute(
            """
            UPDATE reservations
            SET start_time = ?, end_time = ?
            WHERE reservation_id = ?
            """,
            (completed_start, completed_end, completed["reservation_id"]),
        )

    assert workflow_client.post(
        "/api/exit",
        json={"reservation_id": completed["reservation_id"]},
    ).status_code == 200

    active = workflow_client.post(
        "/api/reservations",
        json={
            "vehicle_no": "ACTIVE678",
            "vehicle_type": "CAR",
            "slot_type": "REGULAR",
            "zone": "WORKFLOW_A",
            "start_time": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "end_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    )
    assert active.status_code == 201
    assert workflow_client.post(
        "/api/entry",
        json={"reservation_id": active.get_json()["reservation_id"]},
    ).status_code == 200

    result = workflow_client.get("/api/analytics/intelligence").get_json()
    release = result["estimated_releases"][0]
    assert result["summary"]["historical_overstay_samples"] == 1
    assert result["summary"]["median_overstay_minutes"] >= 29
    assert release["confidence"] == "MEDIUM"
    assert "median historical overstay" in release["estimate_basis"]

from datetime import datetime, timedelta, timezone

import pytest
import backend.db as database
import backend.db as database


@pytest.fixture
def copilot_client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "copilot.db")
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    database.initialize_database()
    with database.get_connection() as connection:
        connection.executemany(
            """
            INSERT INTO parking_slots (slot_id, zone, slot_type, is_active)
            VALUES (?, ?, ?, 1)
            """,
            [
                ("CA01", "COPILOT_A", "REGULAR"),
                ("CA02", "COPILOT_A", "REGULAR"),
                ("CB01", "COPILOT_B", "EV"),
                ("CB02", "COPILOT_B", "ACCESSIBLE"),
            ],
        )

    from backend.app import app

    app.config.update(TESTING=True)
    return app.test_client()


def send_chat(client, message, chat_id=None):
    response = client.post(
        "/api/chat",
        json={
            "message": message,
            "chat_id": chat_id,
            "timezone_offset_minutes": 0,
        },
    )
    return response


def test_copilot_answers_live_availability_from_application_api(copilot_client):
    expected = copilot_client.get("/api/slots").get_json()
    response = send_chat(
        copilot_client,
        "How many parking spaces are available right now?",
    )

    assert response.status_code == 200
    result = response.get_json()
    live_available = sum(
        1 for slot in expected["slots"]
        if slot["is_active"] and slot["current_status"] == "AVAILABLE"
    )
    assert f"{live_available} matching space(s) are available now" in result["reply"]
    assert result["assistant_mode"] == "DETERMINISTIC"
    assert result["actions"][0]["page"] == "parking"


def test_copilot_lists_occupied_spaces_without_vehicle_registration(copilot_client):
    reservation_response = copilot_client.post(
        "/api/reservations",
        json={
            "vehicle_no": "KA11AB1234",
            "vehicle_type": "CAR",
            "slot_type": "REGULAR",
            "zone": "COPILOT_A",
            "start_time": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "end_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    )
    assert reservation_response.status_code == 201
    reservation_id = reservation_response.get_json()["reservation_id"]
    assert copilot_client.post(
        "/api/entry",
        json={"reservation_id": reservation_id},
    ).status_code == 200

    response = send_chat(copilot_client, "Which spaces are currently occupied?")
    assert response.status_code == 200
    assert reservation_response.get_json()["slot_id"] in response.get_json()["reply"]
    assert "KA11AB1234" not in response.get_json()["reply"]


def test_copilot_reports_forecast_insufficient_data_honestly(copilot_client):
    result = send_chat(
        copilot_client,
        "What will occupancy be in 30 minutes?",
    ).get_json()

    assert "forecast is not available yet" in result["reply"]
    assert "0/5 observations" in result["reply"]
    assert "predicts 0" not in result["reply"]


def test_copilot_forecast_uses_existing_trained_forecast_api(copilot_client):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    with database.get_connection() as connection:
        for index, occupied in enumerate([0, 1, 1, 2, 2]):
            timestamp = now - timedelta(minutes=15 * (4 - index))
            connection.execute(
                """
                INSERT INTO occupancy_history (
                    timestamp, occupied_count, total_capacity, data_source
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    timestamp.isoformat().replace("+00:00", "Z"),
                    occupied,
                    4,
                    "SCHEDULED_SNAPSHOT",
                ),
            )

    forecast = copilot_client.get(
        "/api/analytics/forecast?forecast_minutes=30"
    ).get_json()
    assert forecast["status"] == "success"
    prediction = forecast["predictions"][0]
    answer = send_chat(
        copilot_client,
        "What will occupancy be in 30 minutes?",
    ).get_json()

    assert f"{prediction['predicted_occupied']}/4 occupied" in answer["reply"]
    assert f"{prediction['predicted_occupancy_pct']:.1f}%" in answer["reply"]
    assert prediction["observation_timestamp"] in answer["reply"]


def test_copilot_recommendations_respect_live_space_type(copilot_client):
    ev_answer = send_chat(
        copilot_client,
        "Find me an EV charging space",
    ).get_json()
    accessible_answer = send_chat(
        copilot_client,
        "Find me an accessible parking space",
    ).get_json()

    assert "CB01" in ev_answer["reply"]
    assert "EV" in ev_answer["reply"]
    assert "CB02" in accessible_answer["reply"]
    assert "ACCESSIBLE" in accessible_answer["reply"]


def test_reservation_requires_separate_explicit_confirmation(copilot_client):
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=17, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(hours=2)
    first = send_chat(
        copilot_client,
        "Book a motorcycle slot for KA01AB1234 "
        f"{start.isoformat()} to {end.isoformat()}",
    )
    assert first.status_code == 200
    state = first.get_json()
    assert state["state"]["phase"] == "awaiting_confirmation"
    assert "Nothing has been reserved yet" in state["reply"]
    assert copilot_client.get("/api/reservations?status=ACTIVE").get_json()[
        "total_reservations"
    ] == 0

    selected_slot = state["state"]["available_slots"][1]["slot_id"]
    selection = send_chat(
        copilot_client,
        selected_slot,
        chat_id=state["chat_id"],
    )
    assert selected_slot in selection.get_json()["reply"]
    assert "reply “confirm”" in selection.get_json()["reply"].lower()
    assert copilot_client.get("/api/reservations?status=ACTIVE").get_json()[
        "total_reservations"
    ] == 0

    confirmation = send_chat(
        copilot_client,
        "confirm",
        chat_id=state["chat_id"],
    )
    assert confirmation.status_code == 200
    booking = confirmation.get_json()
    assert "Reservation confirmed by ParkWise" in booking["reply"]
    assert f"Reservation ID: PK-" in booking["reply"]
    assert selected_slot in booking["reply"]
    assert copilot_client.get("/api/reservations?status=ACTIVE").get_json()[
        "total_reservations"
    ] == 1


def test_reservation_no_discards_pending_booking(copilot_client):
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=17, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(hours=1)
    pending = send_chat(
        copilot_client,
        "Reserve an EV parking slot for KA22CD5678 "
        f"{start.isoformat()} to {end.isoformat()}",
    ).get_json()
    assert pending["state"]["phase"] == "awaiting_confirmation"

    declined = send_chat(
        copilot_client,
        "no",
        chat_id=pending["chat_id"],
    )
    assert declined.status_code == 200
    assert "Nothing was booked" in declined.get_json()["reply"]
    assert declined.get_json()["state"] is None
    assert copilot_client.get("/api/reservations?status=ACTIVE").get_json()[
        "total_reservations"
    ] == 0


def test_reservation_collects_details_over_multiple_messages(copilot_client):
    first = send_chat(
        copilot_client,
        "I want to reserve a motorcycle parking slot",
    ).get_json()
    assert first["state"]["flow"] == "reservation"
    assert "vehicle registration number" in first["reply"]

    details = send_chat(
        copilot_client,
        "Registration KA33BC9911. Tomorrow 5 pm to 7 pm.",
        chat_id=first["chat_id"],
    )
    assert details.status_code == 200
    pending = details.get_json()
    assert pending["state"]["phase"] == "awaiting_confirmation"
    assert "KA33BC9911 (MOTORCYCLE)" in pending["reply"]
    assert "Nothing has been reserved yet" in pending["reply"]
    assert copilot_client.get("/api/reservations?status=ACTIVE").get_json()[
        "total_reservations"
    ] == 0


def test_copilot_validates_inputs_and_does_not_accept_client_booking_state(
    copilot_client,
):
    empty = copilot_client.post("/api/chat", json={"message": " "})
    assert empty.status_code == 400
    assert empty.get_json()["error"]["code"] == "INVALID_MESSAGE"

    long_message = copilot_client.post(
        "/api/chat",
        json={"message": "x" * 501},
    )
    assert long_message.status_code == 400

    forged_state = copilot_client.post(
        "/api/chat",
        json={
            "message": "confirm",
            "state": {
                "flow": "reservation",
                "phase": "awaiting_confirmation",
                "draft": {
                    "vehicle_no": "KA01AB1234",
                    "vehicle_type": "CAR",
                },
                "available_slots": [{"slot_id": "WA01", "zone": "WORKFLOW_A"}],
            },
        },
    )
    assert forged_state.status_code == 200
    assert "deterministic intent-based helper" in forged_state.get_json()["reply"]
    assert copilot_client.get("/api/reservations?status=ACTIVE").get_json()[
        "total_reservations"
    ] == 0

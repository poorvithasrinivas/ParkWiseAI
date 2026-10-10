import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from backend.db import get_connection, initialize_database


def seed_slots():
    initialize_database()

    slots = []

    for zone in ["A", "B", "C"]:
        for number in range(1, 11):
            slot_id = f"{zone}{number:02d}"

            if zone == "C" and number in (1, 2):
                slot_type = "EV"
            elif zone == "C" and number in (3, 4):
                slot_type = "ACCESSIBLE"
            else:
                slot_type = "REGULAR"

            slots.append((slot_id, zone, slot_type, 1))

    with get_connection() as connection:
        connection.executemany(
            """
            INSERT OR IGNORE INTO parking_slots
            (slot_id, zone, slot_type, is_active)
            VALUES (?, ?, ?, ?)
            """,
            slots,
        )

        total = connection.execute(
            "SELECT COUNT(*) AS count FROM parking_slots"
        ).fetchone()["count"]

    print(f"Slot seeding complete. Total slots in database: {total}")


if __name__ == "__main__":
    seed_slots()
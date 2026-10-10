
"""Collect real parking occupancy snapshots every 15 minutes.

Run from the project root:
    python -m backend.occupancy_collector
"""

import time
from datetime import datetime, timezone

from backend.db import get_connection, initialize_database

INTERVAL_SECONDS = 15 * 60
DATA_SOURCE = "SCHEDULED_SNAPSHOT"


def utc_timestamp():
    """Return the current UTC timestamp in ISO 8601 format."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def collect_snapshot():
    """Calculate current occupancy and save one database snapshot."""
    with get_connection() as connection:
        capacity_row = connection.execute("""
            SELECT COUNT(*) AS active_capacity
            FROM parking_slots
            WHERE is_active = 1
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

        capacity = capacity_row["active_capacity"] or 0
        occupied = occupied_row["occupied_count"] or 0

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
            occupied,
            capacity,
            DATA_SOURCE,
        ))

    print(
        f"[{utc_timestamp()}] Snapshot saved: "
        f"{occupied}/{capacity} slots occupied."
    )


def main():
    """Initialize the database, then collect snapshots continuously."""
    initialize_database()

    print("ParkWise AI occupancy collector started.")
    print("A snapshot will be collected every 15 minutes.")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            collect_snapshot()
            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nOccupancy collector stopped.")


if __name__ == "__main__":
    main()

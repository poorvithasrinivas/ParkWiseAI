PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS parking_slots (
    slot_id TEXT PRIMARY KEY,
    zone TEXT NOT NULL,
    slot_type TEXT NOT NULL DEFAULT 'REGULAR',
    is_active INTEGER NOT NULL DEFAULT 1
        CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_no TEXT NOT NULL UNIQUE,
    vehicle_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    vehicle_id INTEGER NOT NULL,
    slot_id TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CONFIRMED'
        CHECK (status IN (
            'CONFIRMED', 'CHECKED_IN', 'COMPLETED',
            'CANCELLED', 'EXPIRED', 'NO_SHOW'
        )),
    created_at TEXT NOT NULL,
    FOREIGN KEY (vehicle_id) REFERENCES vehicles(vehicle_id),
    FOREIGN KEY (slot_id) REFERENCES parking_slots(slot_id),
    CHECK (start_time < end_time)
);

CREATE TABLE IF NOT EXISTS parking_sessions (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id TEXT NOT NULL UNIQUE,
    actual_entry_time TEXT NOT NULL,
    actual_exit_time TEXT,
    FOREIGN KEY (reservation_id)
        REFERENCES reservations(reservation_id),
    CHECK (
        actual_exit_time IS NULL
        OR actual_exit_time >= actual_entry_time
    )
);

CREATE TABLE IF NOT EXISTS occupancy_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    occupied_count INTEGER NOT NULL CHECK (occupied_count >= 0),
    total_capacity INTEGER NOT NULL CHECK (total_capacity >= 0),
    data_source TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_slot_time
ON reservations(slot_id, start_time, end_time);

CREATE INDEX IF NOT EXISTS idx_occupancy_timestamp
ON occupancy_history(timestamp);
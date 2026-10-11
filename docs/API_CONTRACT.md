
# ParkWise AI — Backend API Contract

## Overview

ParkWise AI provides parking reservations, vehicle entry and exit, live occupancy analytics, and machine-learning-based occupancy forecasting.

- Base URL (local): `http://127.0.0.1:5000`
- API prefix: `/api`
- Request/response format: JSON
- Timestamp format: ISO 8601 with timezone information
- Reservation timestamps are normalized to UTC by the backend.

## 1. Create a Reservation

**Endpoint:** `POST /api/reservations`

Creates a reservation and allocates a compatible active parking slot.

### Request body

```json
{
  "vehicle_no": "KA01AB1234",
  "vehicle_type": "CAR",
  "slot_type": "REGULAR",
  "zone": "A",
  "start_time": "2026-10-12T10:00:00+05:30",
  "end_time": "2026-10-12T11:00:00+05:30"
}
```

- `vehicle_no`: Required vehicle registration number.
- `vehicle_type`: Required supported vehicle type.
- `slot_type`: Required supported slot type; defaults to `REGULAR`.
- `zone`: Optional zone filter.
- `start_time`: Required reservation start timestamp.
- `end_time`: Required reservation end timestamp, later than the start.

### Success response

**201 Created**

```json
{
  "reservation_id": "GENERATED_ID",
  "vehicle_no": "KA01AB1234",
  "vehicle_type": "CAR",
  "slot_id": "A01",
  "slot_type": "REGULAR",
  "zone": "A",
  "start_time": "UTC_TIMESTAMP",
  "end_time": "UTC_TIMESTAMP",
  "status": "CONFIRMED"
}
```

Values are illustrative. The backend generates the reservation ID and allocates a compatible slot.

### Known error responses

- `400`: Invalid JSON, missing fields, invalid vehicle number/type, invalid slot type/zone, or invalid time range.
- `409`: Vehicle already reserved or no compatible slot available.
- `500`: Internal server error.

## 2. Retrieve a Reservation

**Endpoint:** `GET /api/reservations/{reservation_id}`

Returns reservation details, vehicle and slot information, reservation status, and entry/exit timestamps when available.

### Success response

**200 OK**

```json
{
  "reservation_id": "GENERATED_ID",
  "vehicle_no": "KA01AB1234",
  "vehicle_type": "CAR",
  "slot_id": "A01",
  "zone": "A",
  "slot_type": "REGULAR",
  "start_time": "UTC_TIMESTAMP",
  "end_time": "UTC_TIMESTAMP",
  "status": "CONFIRMED",
  "created_at": "UTC_TIMESTAMP",
  "entry_time": null,
  "exit_time": null
}
```

**404 Not Found:** Reservation does not exist.

## 3. Vehicle Entry / Check-in

**Endpoint:** `POST /api/entry`

### Request body

```json
{
  "reservation_id": "GENERATED_ID"
}
```

The reservation must be confirmed. Check-in is allowed from 15 minutes before the scheduled start until before the scheduled end.

### Success response

**200 OK**

```json
{
  "reservation_id": "GENERATED_ID",
  "slot_id": "A01",
  "status": "CHECKED_IN",
  "entry_time": "UTC_TIMESTAMP"
}
```

### Known error responses

- `400`: Invalid JSON or missing fields.
- `404`: Reservation not found.
- `409`: Invalid reservation status, check-in too early, or reservation expired.
- `500`: Internal server error.

## 4. Vehicle Exit / Check-out

**Endpoint:** `POST /api/exit`

### Request body

```json
{
  "reservation_id": "GENERATED_ID"
}
```

The vehicle must have an active parking session and the reservation must be in the `CHECKED_IN` state.

### Success response

**200 OK**

```json
{
  "reservation_id": "GENERATED_ID",
  "slot_id": "A01",
  "status": "COMPLETED",
  "entry_time": "UTC_TIMESTAMP",
  "exit_time": "UTC_TIMESTAMP"
}
```

### Known error responses

- `400`: Invalid JSON or missing fields.
- `404`: Reservation not found.
- `409`: Vehicle is not checked in or no active parking session exists.
- `500`: Internal server error.

## 5. Live Occupancy Analytics

**Endpoint:** `GET /api/analytics/occupancy`

Returns current parking occupancy calculated from active parking sessions and active parking capacity.

### Success response

**200 OK**

```json
{
  "timestamp": "UTC_TIMESTAMP",
  "occupancy": {
    "total_slots": 30,
    "active_capacity": 30,
    "occupied_slots": 4,
    "available_slots": 26,
    "occupancy_percentage": 13.33
  },
  "zones": [
    {
      "zone": "A",
      "total_slots": 10,
      "active_capacity": 10,
      "occupied_slots": 2,
      "available_slots": 8,
      "occupancy_percentage": 20.0
    }
  ],
  "data_source": "LIVE_PARKING_SESSIONS"
}
```

Counts and percentages are illustrative. The response includes zone-level occupancy based on the database.

## 6. Occupancy Forecasting

**Endpoint:** `GET /api/analytics/forecast`

Uses the trained machine-learning forecasting models to estimate future parking occupancy from scheduled historical occupancy snapshots.

### Query parameters

- `forecast_minutes`: Optional. Supported values are `30` and `60`. If omitted, the endpoint returns predictions for both supported horizons.

### Data requirements

The forecasting endpoint requires **five scheduled occupancy snapshots**:

- Each snapshot records current occupied slots and total active capacity.
- Snapshots must be spaced approximately 15 minutes apart.
- All five snapshots must have consistent capacity.
- Snapshots must be recorded with the data source `SCHEDULED_SNAPSHOT`.

The occupancy collector records snapshots every 15 minutes while it is running. Predictions may remain unavailable until sufficient valid history has accumulated.

### Insufficient data response

**200 OK**

```json
{
  "status": "insufficient_data",
  "message": "Five scheduled occupancy observations are required, spaced approximately 15 minutes apart.",
  "observations_available": 1,
  "observations_required": 5,
  "predictions": []
}
```

The number of available observations is illustrative.

### Successful forecast response

**200 OK**

```json
{
  "status": "success",
  "observations_available": 5,
  "observations_required": 5,
  "predictions": [
    {
      "forecast_minutes": 30,
      "observation_timestamp": "UTC_TIMESTAMP",
      "forecast_time": "UTC_TIMESTAMP",
      "current_occupied": 15,
      "predicted_occupied": 18,
      "total_capacity": 30,
      "occupancy_percentage": 60.0,
      "model": "MODEL_NAME",
      "data_source": "SCHEDULED_SNAPSHOT"
    }
  ]
}
```

The example prediction is illustrative. Actual prediction values, timestamps, and model metadata come from the forecasting model.

### Forecast error responses

- `400`: Invalid forecast horizon; only 30 and 60 minutes are supported.
- `503`: Forecast model file is missing.
- `500`: Forecasting failed because of an unexpected error.

An HTTP `200` response with `status: "insufficient_data"` is not a successful prediction. The frontend should display an appropriate message until enough valid snapshots are available.

## 7. Health Check

**Endpoint:** `GET /api/health`

Checks whether the backend health endpoint responds.

### Expected behavior

- `200 OK`: Health endpoint responded successfully.

Refer to the actual endpoint response for its JSON fields.

## 8. Parking Slots

**Endpoint:** `GET /api/slots`

Returns parking-slot information from the backend.

The frontend should use the returned slot information rather than assuming that all slots are active or available.

Refer to the actual endpoint response for its JSON structure.

## 9. Parking Availability

**Endpoint:** `GET /api/availability`

Checks parking availability for the requested time interval.

Query parameters include:

- `start_time`: Requested interval start timestamp.
- `end_time`: Requested interval end timestamp.

Use ISO 8601 timestamps with timezone information.

Refer to the actual endpoint implementation for the complete response schema and validation rules.

## 10. Integration Notes

- Send reservation, entry, and exit requests as JSON.
- Handle HTTP status codes and error responses.
- Never assume reservation IDs or slot IDs in advance.
- Use backend occupancy values to display current parking availability.
- Distinguish live occupancy from scheduled historical snapshots.
- Treat `insufficient_data` as a forecast state, not a prediction.
- The forecasting endpoint supports 30-minute and 60-minute horizons.
- Keep the occupancy collector running to accumulate scheduled snapshots.
- Do not expose internal errors or stack traces to frontend users.

## 11. ParkWise Copilot

**Endpoint:** `POST /api/chat`

The Copilot uses deterministic intent handling and approved ParkWise endpoints.
It does not require an external LLM or API credentials. It supports current
availability, zone occupancy, EV/accessibility spaces, space recommendations,
the existing 30/60-minute forecast, and reservation preparation with explicit
confirmation.

### Request body

```json
{
  "message": "How many spaces are available right now?",
  "chat_id": null,
  "timezone_offset_minutes": 330
}
```

- `message`: Required string, 1–500 characters.
- `chat_id`: Optional opaque conversation ID returned by the previous response.
- `timezone_offset_minutes`: Optional client-local UTC offset from -840 to 840.

### Response body

Successful responses have a stable shape:

```json
{
  "chat_id": "opaque-token",
  "reply": "Live answer from ParkWise data.",
  "actions": [
    {"type": "navigate", "page": "parking", "label": "View parking map"}
  ],
  "state": null,
  "assistant_mode": "DETERMINISTIC"
}
```

Actions are limited to navigation and highlighting a returned zone or slot.
Reservation conversations use short-lived, server-side in-memory state and
expire after 30 minutes. The Copilot rechecks availability and requires a
separate confirmation before it calls the existing `POST /api/reservations`
endpoint. A successful reservation reply is based on that endpoint's response.

### Limitations and privacy

- Forecast answers use the existing trained model and report insufficient data
  rather than fabricating values.
- EV availability reports EV-designated spaces; the database does not track
  charger equipment or charger status.
- Zone congestion answers compare current occupancy. The ML models do not
  provide zone-level forecasts.
- Reservation lookup and cancellation are not offered by the Copilot. This
  installation has no authentication/ownership model or cancellation endpoint.
- Administrator-only operations are unavailable because no authenticated
  administrator role exists.
- No external LLM credentials are used or required. The assistant labels itself
  deterministic and does not claim to be generative AI.
- Chat sessions and the per-IP rate limiter are held in process memory. Run a
  single backend worker for reservation conversations; restarting the process
  clears pending conversations.

### Errors

Invalid JSON, missing or oversized messages, invalid timezone offsets, expired
chat tokens, and rate limits return a JSON `error` object and an appropriate
HTTP status. Data-service errors are logged server-side and returned to users
as concise messages.

"""Rule-based ParkWise Copilot backed by approved application endpoints."""

import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from flask import Blueprint, current_app, jsonify, request


copilot_bp = Blueprint("copilot", __name__)
MAX_MESSAGE_LENGTH = 500
MAX_REQUESTS_PER_MINUTE = 30
CHAT_TTL_SECONDS = 30 * 60
_lock = threading.Lock()
_chats = {}
_request_times = {}

REGISTRATION_PATTERN = re.compile(
    r"\b([A-Z]{2}\s?\d{1,2}\s?[A-Z]{1,3}\s?\d{1,4})\b",
    re.IGNORECASE,
)
EXPLICIT_REGISTRATION_PATTERN = re.compile(
    r"\b(?:registration|reg(?:istration)?\s*(?:no|number)|"
    r"plate|vehicle\s*(?:no|number))\s*(?:is|:)?\s*"
    r"([A-Z0-9 -]{4,15})",
    re.IGNORECASE,
)
ISO_DATE_PATTERN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
    r"(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?\b",
    re.IGNORECASE,
)
NATURAL_TIME_PATTERN = re.compile(
    r"\b(?:(today|tomorrow)\s*(?:at\s*)?)?"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
    re.IGNORECASE,
)


def _error(code, message, status):
    return jsonify({"error": {"code": code, "message": message}}), status


def _client_key():
    return request.remote_addr or "unknown"


def _take_rate_limit():
    now = time.monotonic()
    client = _client_key()
    with _lock:
        recent = [
            moment for moment in _request_times.get(client, [])
            if now - moment < 60
        ]
        if len(recent) >= MAX_REQUESTS_PER_MINUTE:
            _request_times[client] = recent
            return False
        recent.append(now)
        _request_times[client] = recent
        if len(_request_times) > 256:
            _request_times.clear()
        return True


def _get_chat(chat_id):
    now = time.monotonic()
    client = _client_key()
    with _lock:
        expired = [
            key for key, item in _chats.items()
            if item["expires_at"] <= now
        ]
        for key in expired:
            del _chats[key]

        if not chat_id:
            if len(_chats) >= 1000:
                oldest = min(_chats, key=lambda key: _chats[key]["expires_at"])
                del _chats[oldest]
            chat_id = secrets.token_urlsafe(24)
            _chats[chat_id] = {
                "client": client,
                "state": None,
                "expires_at": now + CHAT_TTL_SECONDS,
            }
            return chat_id, None, None

        item = _chats.get(chat_id)
        if not item or not secrets.compare_digest(item["client"], client):
            return None, None, _error(
                "CHAT_SESSION_EXPIRED",
                "This conversation expired. Start a new chat and try again.",
                410,
            )
        item["expires_at"] = now + CHAT_TTL_SECONDS
        return chat_id, item["state"], None


def _set_state(chat_id, state):
    with _lock:
        item = _chats.get(chat_id)
        if item:
            item["state"] = state
            item["expires_at"] = time.monotonic() + CHAT_TTL_SECONDS


def _reply(chat_id, text, actions=None, state=None):
    _set_state(chat_id, state)
    return jsonify({
        "chat_id": chat_id,
        "reply": text,
        "actions": actions or [],
        "state": state,
        "assistant_mode": "DETERMINISTIC",
    }), 200


def _application_get(path):
    """Call a fixed, registered application API without external HTTP."""
    response = current_app.test_client().get(path)
    data = response.get_json(silent=True)
    if response.status_code >= 400 or not isinstance(data, dict):
        raise RuntimeError("An application data service is unavailable.")
    return data


def _application_post(path, payload):
    response = current_app.test_client().post(path, json=payload)
    data = response.get_json(silent=True)
    if response.status_code >= 400 or not isinstance(data, dict):
        error = data.get("error", {}) if isinstance(data, dict) else {}
        if isinstance(error, dict):
            raise ValueError(error.get("message", "The request could not be completed."))
        raise ValueError("The request could not be completed.")
    return data


def _parse_vehicle_type(text):
    normalized = text.lower()
    if re.search(r"\b(electric|electric vehicle|ev)\b", normalized):
        return "EV"
    if re.search(r"\b(motorcycle|motorbike|motor cycle)\b", normalized):
        return "MOTORCYCLE"
    if re.search(r"\b(bike|bicycle|cycle)\b", normalized):
        return "BIKE"
    if re.search(r"\b(car|automobile)\b", normalized):
        return "CAR"
    return None


def _parse_slot_type(text):
    normalized = text.lower()
    if re.search(r"\b(accessible|accessibility|wheelchair)\b", normalized):
        return "ACCESSIBLE"
    if re.search(r"\b(regular|standard)\s*(?:parking\s*)?(?:slot|space|bay)?\b", normalized):
        return "REGULAR"
    if re.search(
        r"\b(?:ev(?:\s+charging)?|electric vehicle|charging)\s*"
        r"(?:parking\s*)?(?:slot|space|bay)\b",
        normalized,
    ):
        return "EV"
    return None


def _parse_zone(text):
    match = re.search(r"\bzone\s+([a-z0-9_-]+)\b", text, re.IGNORECASE)
    return match.group(1).upper() if match else None


def _parse_registration(text):
    explicit = EXPLICIT_REGISTRATION_PATTERN.search(text)
    if explicit:
        candidate = explicit.group(1).strip().upper()
        candidate = re.split(
            r"\s+(?:and|with|for|my|vehicle|type|car|bike|motorcycle|"
            r"tomorrow|today|at|from|until|to)\b",
            candidate,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" -")
        if 4 <= len(candidate) <= 15 and re.fullmatch(
            r"[A-Z0-9 -]+", candidate
        ):
            return candidate

    match = REGISTRATION_PATTERN.search(text)
    if match:
        return re.sub(r"\s+", "", match.group(1)).upper()
    return None


def _parse_booking_times(text, timezone_offset_minutes, reference_time=None):
    """Parse explicit ISO times or common today/tomorrow AM/PM pairs."""
    iso_values = []
    for match in ISO_DATE_PATTERN.finditer(text):
        value = match.group(0).replace(" ", "T")
        if value.endswith(("Z", "z")):
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        elif re.search(r"[+-]\d{2}:\d{2}$", value):
            parsed = datetime.fromisoformat(value)
        else:
            parsed = datetime.fromisoformat(value)
            parsed -= timedelta(minutes=timezone_offset_minutes)
            parsed = parsed.replace(tzinfo=timezone.utc)
        iso_values.append(parsed.astimezone(timezone.utc))

    if len(iso_values) >= 2:
        return (
            iso_values[0].isoformat(timespec="seconds").replace("+00:00", "Z"),
            iso_values[1].isoformat(timespec="seconds").replace("+00:00", "Z"),
        )

    now_local = datetime.now(timezone.utc) + timedelta(minutes=timezone_offset_minutes)
    reference_day = now_local.date()
    if reference_time:
        try:
            reference = datetime.fromisoformat(
                reference_time.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            reference_day = (
                reference + timedelta(minutes=timezone_offset_minutes)
            ).date()
        except (ValueError, TypeError):
            reference_day = now_local.date()
    times = []
    inherited_local_day = None
    for match in NATURAL_TIME_PATTERN.finditer(text):
        day_label, hour_text, minute_text, meridiem = match.groups()
        if day_label:
            day_offset = 1 if day_label.lower() == "tomorrow" else 0
            inherited_local_day = now_local.date() + timedelta(days=day_offset)
        hour = int(hour_text)
        minute = int(minute_text or 0)
        if not (1 <= hour <= 12 and 0 <= minute <= 59):
            continue
        hour %= 12
        if meridiem.lower() == "pm":
            hour += 12
        local_day = inherited_local_day or reference_day
        parsed_local = datetime(
            local_day.year, local_day.month, local_day.day, hour, minute
        )
        parsed_utc = (
            parsed_local - timedelta(minutes=timezone_offset_minutes)
        ).replace(tzinfo=timezone.utc)
        times.append(parsed_utc)

    if len(times) >= 2:
        return tuple(
            value.isoformat(timespec="seconds").replace("+00:00", "Z")
            for value in times[:2]
        )
    if len(iso_values) == 1:
        value = iso_values[0].isoformat(timespec="seconds").replace("+00:00", "Z")
        if re.search(r"\b(end|until|leave|departure)\b", text, re.I):
            return None, value
        return value, None
    if len(times) == 1:
        lowered = text.lower()
        value = times[0].isoformat(timespec="seconds").replace("+00:00", "Z")
        if re.search(r"\b(end|until|leave|departure)\b", lowered):
            return (None, value)
        return (value, None)
    return None, None


def _merge_booking(state, message, timezone_offset_minutes):
    draft = dict(state.get("draft", {})) if state else {}
    vehicle_no = _parse_registration(message)
    vehicle_type = _parse_vehicle_type(message)
    slot_type = _parse_slot_type(message)
    zone = _parse_zone(message)
    start_time, end_time = _parse_booking_times(
        message,
        timezone_offset_minutes,
        reference_time=draft.get("start_time"),
    )

    if vehicle_no:
        draft["vehicle_no"] = vehicle_no
    if vehicle_type:
        draft["vehicle_type"] = vehicle_type
    if slot_type:
        draft["slot_type"] = slot_type
    elif "slot_type" not in draft:
        draft["slot_type"] = "REGULAR"
    if zone:
        draft["zone"] = zone
    if start_time:
        draft["start_time"] = start_time
    if end_time:
        draft["end_time"] = end_time
    return draft


def _booking_missing_fields(draft):
    missing = []
    for key, label in (
        ("vehicle_no", "vehicle registration number"),
        ("vehicle_type", "vehicle type"),
        ("start_time", "arrival time"),
        ("end_time", "departure time"),
    ):
        if not draft.get(key):
            missing.append(label)
    return missing


def _booking_state_reply(chat_id, draft, timezone_offset_minutes):
    missing = _booking_missing_fields(draft)
    if missing:
        return _reply(
            chat_id,
            "I can help prepare that reservation. Please provide your " +
            ", ".join(missing) +
            ". You can send them together; use a registration like KA01AB1234 "
            "and times such as “tomorrow 5 pm to 7 pm”. "
            "The parking type defaults to regular; say EV or accessible if needed. "
            "A zone is optional.",
            state={"flow": "reservation", "draft": draft},
        )

    try:
        start = datetime.fromisoformat(
            draft["start_time"].replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        end = datetime.fromisoformat(
            draft["end_time"].replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except (TypeError, ValueError):
        draft.pop("start_time", None)
        draft.pop("end_time", None)
        return _booking_state_reply(chat_id, draft, timezone_offset_minutes)

    now = datetime.now(timezone.utc)
    if start <= now or end <= start:
        draft.pop("start_time", None)
        draft.pop("end_time", None)
        return _reply(
            chat_id,
            "Those times are invalid or already in the past. Please send a future "
            "arrival and a later departure (for example, “tomorrow 5 pm to 7 pm”).",
            state={"flow": "reservation", "draft": draft},
        )

    params = {
        "start_time": start.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "end_time": end.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "slot_type": draft.get("slot_type", "REGULAR"),
    }
    if draft.get("zone"):
        params["zone"] = draft["zone"]

    try:
        availability = _application_get(
            "/api/availability?" + urlencode(params)
        )
    except Exception:
        current_app.logger.exception("Copilot could not check availability")
        return _reply(
            chat_id,
            "I collected the details, but the live availability service is "
            "unavailable. I have not created a reservation. Please try again shortly.",
            state={"flow": "reservation", "draft": draft},
        )

    spaces = availability.get("available_slots", [])
    if not spaces:
        alternative_params = dict(params)
        alternative_params.pop("zone", None)
        try:
            alternatives = _application_get(
                "/api/availability?" + urlencode(alternative_params)
            )
            alternative_spaces = alternatives.get("available_slots", [])
        except Exception:
            current_app.logger.exception(
                "Copilot could not check alternative availability"
            )
            alternative_spaces = []

        alternate_zones = sorted({
            item["zone"] for item in alternative_spaces
            if isinstance(item, dict) and item.get("zone")
        })
        zone_message = (
            " Spaces are available in " +
            ", ".join(f"Zone {zone}" for zone in alternate_zones) + "."
            if alternate_zones else
            " No alternative spaces are available for that window."
        )
        requested_zone = (
            f" in Zone {draft['zone']}" if draft.get("zone") else ""
        )
        return _reply(
            chat_id,
            f"No {draft.get('slot_type', 'REGULAR').lower()} spaces are available "
            f"for that time{requested_zone}."
            f"{zone_message} I have not booked anything. Send a different time or zone.",
            actions=[{"type": "navigate", "page": "parking", "label": "View parking map"}],
            state={"flow": "reservation", "draft": draft},
        )

    state = {
        "flow": "reservation",
        "phase": "awaiting_confirmation",
        "draft": draft,
        "available_slots": spaces,
    }
    options = ", ".join(
        f"{item['slot_id']} (Zone {item['zone']})"
        for item in spaces[:5]
    )
    remaining = len(spaces) - min(5, len(spaces))
    if remaining:
        options += f", and {remaining} more"
    zone_text = f", Zone {draft['zone']}" if draft.get("zone") else ""
    local_start = start.strftime("%b %d, %Y %I:%M %p UTC")
    local_end = end.strftime("%b %d, %Y %I:%M %p UTC")
    return _reply(
        chat_id,
        "Please review this reservation before I submit it:\n"
        f"• Vehicle: {draft['vehicle_no']} ({draft['vehicle_type']})\n"
        f"• Space type: {draft.get('slot_type', 'REGULAR')}{zone_text}\n"
        f"• Time: {local_start} to {local_end}\n"
        f"• Available spaces checked just now: {options}\n"
        "Reply “confirm” to book the first listed space, or send an available "
        "slot ID to select it. Nothing has been reserved yet.",
        actions=[{"type": "navigate", "page": "parking", "label": "View parking map"}],
        state=state,
    )


def _continue_booking(chat_id, state, message, timezone_offset_minutes):
    lowered = message.lower().strip()
    if re.fullmatch(
        r"(cancel|stop|never mind|nevermind|no|no thanks|not now)", lowered
    ):
        return _reply(chat_id, "Okay, I cleared the pending reservation. Nothing was booked.")

    if state.get("phase") == "awaiting_confirmation":
        spaces = state.get("available_slots", [])
        selected = None
        for item in spaces:
            if re.search(rf"(?<![A-Z0-9]){re.escape(item['slot_id'])}(?![A-Z0-9])", message, re.I):
                selected = item
                break
        confirmed = bool(re.search(
            r"\b(confirm|yes|book it|proceed|go ahead|reserve it)\b",
            lowered,
        ))
        if selected and not confirmed:
            state["selected_slot_id"] = selected["slot_id"]
            return _reply(
                chat_id,
                f"{selected['slot_id']} in Zone {selected['zone']} is selected. "
                "Please reply “confirm” to submit the reservation, or “no” to "
                "discard it. I have not booked anything yet.",
                state=state,
            )
        if not selected and not confirmed:
            updated = _merge_booking(state, message, timezone_offset_minutes)
            return _booking_state_reply(chat_id, updated, timezone_offset_minutes)
        if selected is None:
            selected = next(
                (item for item in spaces
                 if item["slot_id"] == state.get("selected_slot_id")),
                spaces[0] if spaces else None,
            )
        if not selected:
            return _reply(
                chat_id,
                "The available-space list is empty. I have not booked anything; "
                "please start a new reservation request.",
            )

        draft = state.get("draft", {})
        payload = {
            "vehicle_no": draft["vehicle_no"],
            "vehicle_type": draft["vehicle_type"],
            "slot_type": draft.get("slot_type", "REGULAR"),
            "zone": selected["zone"],
            "preferred_slot_id": selected["slot_id"],
            "start_time": draft["start_time"],
            "end_time": draft["end_time"],
        }
        try:
            booking = _application_post("/api/reservations", payload)
        except ValueError as error:
            return _reply(
                chat_id,
                f"The reservation could not be completed: {error}. "
                "No booking was confirmed. Ask me to check another time or space.",
                actions=[{"type": "navigate", "page": "parking", "label": "View parking map"}],
            )
        except Exception:
            current_app.logger.exception("Copilot reservation request failed")
            return _reply(
                chat_id,
                "The reservation service is unavailable. I have not confirmed a "
                "booking; please try again shortly.",
            )

        return _reply(
            chat_id,
            "Reservation confirmed by ParkWise:\n"
            f"• Reservation ID: {booking['reservation_id']}\n"
            f"• Space: {booking['slot_id']} · Zone {booking['zone']}\n"
            f"• Vehicle: {booking['vehicle_no']} ({booking['vehicle_type']})\n"
            f"• Status: {booking['status']}\n"
            f"• Schedule: {booking['start_time']} to {booking['end_time']}",
            actions=[{"type": "navigate", "page": "reservations", "label": "Open reservations"}],
        )

    draft = _merge_booking(state, message, timezone_offset_minutes)
    return _booking_state_reply(chat_id, draft, timezone_offset_minutes)


def _live_parking_reply(chat_id, message):
    slots_data = _application_get("/api/slots")
    occupancy_data = _application_get("/api/analytics/occupancy")
    slots = slots_data.get("slots", [])
    zones = occupancy_data.get("zones", [])
    lowered = message.lower()

    active = [slot for slot in slots if slot.get("is_active")]
    occupied = [
        slot for slot in active
        if slot.get("current_status") == "OCCUPIED"
    ]
    available = [
        slot for slot in active
        if slot.get("current_status") == "AVAILABLE"
    ]
    actions = [{"type": "navigate", "page": "parking", "label": "View parking map"}]

    if "accessible" in lowered or "wheelchair" in lowered:
        matched = [slot for slot in available if slot.get("slot_type") == "ACCESSIBLE"]
        if matched:
            zones_matched = sorted({slot["zone"] for slot in matched})
            actions.append({
                "type": "highlight_zone",
                "zone": zones_matched[0],
                "label": f"Highlight Zone {zones_matched[0]}",
            })
            identifiers = ", ".join(slot["slot_id"] for slot in matched[:12])
            remainder = len(matched) - min(12, len(matched))
            suffix = f", and {remainder} more" if remainder else ""
            return _reply(
                chat_id,
                f"{len(matched)} accessible space(s) are available now: "
                f"{identifiers}{suffix}. These are spaces marked ACCESSIBLE in "
                "the live slot data.",
                actions,
            )
        return _reply(chat_id, "There are no active accessible spaces available right now.", actions)

    if re.search(r"\bev\b|electric|charg", lowered):
        matched = [slot for slot in available if slot.get("slot_type") == "EV"]
        if matched:
            counts = {}
            for slot in matched:
                counts[slot["zone"]] = counts.get(slot["zone"], 0) + 1
            summary = ", ".join(
                f"Zone {zone}: {count}"
                for zone, count in sorted(counts.items())
            )
            return _reply(
                chat_id,
                f"{len(matched)} designated EV space(s) are available now "
                f"({summary}). The live data identifies EV spaces but does not "
                "confirm charger hardware or charger status.",
                actions,
            )
        return _reply(
            chat_id,
            "No designated EV spaces are available now. The live slot data does not "
            "report charging equipment status.",
            actions,
        )

    if "occupancy" in lowered and ("zone" in lowered or "each" in lowered):
        detail = "; ".join(
            f"Zone {zone['zone']}: {zone['occupied_slots']}/"
            f"{zone['active_capacity']} occupied "
            f"({zone['occupancy_percentage']:.1f}%)"
            for zone in zones
        )
        return _reply(chat_id, f"Live zone occupancy: {detail}.", actions)

    if "which" in lowered and ("space" in lowered or "slot" in lowered) and "occupied" in lowered:
        zone_filter = _parse_zone(message)
        matching = [
            slot for slot in occupied
            if not zone_filter or str(slot["zone"]).upper() == zone_filter
        ]
        if not matching:
            return _reply(
                chat_id,
                "No active parking spaces are occupied" +
                (f" in Zone {zone_filter}" if zone_filter else "") +
                " right now.",
                actions,
            )
        summary = {}
        for slot in matching:
            summary.setdefault(slot["zone"], []).append(slot["slot_id"])
        detail = "; ".join(
            f"Zone {zone}: {', '.join(identifiers)}"
            for zone, identifiers in sorted(summary.items())
        )
        return _reply(chat_id, f"Occupied spaces right now: {detail}.", actions)

    if any(phrase in lowered for phrase in (
        "most available", "least crowded", "less crowded", "available most",
    )):
        eligible = [zone for zone in zones if zone["active_capacity"] > 0]
        if eligible:
            zone = min(
                eligible,
                key=lambda item: (
                    item["occupancy_percentage"],
                    -item["available_slots"],
                    item["zone"],
                ),
            )
            actions.append({
                "type": "highlight_zone",
                "zone": zone["zone"],
                "label": f"Highlight Zone {zone['zone']}",
            })
            return _reply(
                chat_id,
                f"Zone {zone['zone']} is currently the least occupied at "
                f"{zone['occupancy_percentage']:.1f}% "
                f"({zone['available_slots']} of {zone['active_capacity']} active "
                "spaces available). This is based on current occupancy, not a forecast.",
                actions,
            )

    if any(word in lowered for word in ("available", "free", "open")):
        zone_filter = _parse_zone(message)
        slot_type = _parse_slot_type(message)
        matching = [
            slot for slot in available
            if (not zone_filter or str(slot["zone"]).upper() == zone_filter)
            and (not slot_type or slot["slot_type"] == slot_type)
        ]
        if any(word in lowered for word in ("which", "show", "list")):
            if not matching:
                return _reply(chat_id, "There are no matching spaces available right now.", actions)
            identifiers = ", ".join(
                f"{slot['slot_id']} (Zone {slot['zone']}, {slot['slot_type']})"
                for slot in matching[:12]
            )
            remaining = len(matching) - min(12, len(matching))
            suffix = f", and {remaining} more" if remaining else ""
            return _reply(
                chat_id,
                f"{len(matching)} matching space(s) are available now: "
                f"{identifiers}{suffix}.",
                actions,
            )
        if zone_filter:
            actions.append({
                "type": "highlight_zone",
                "zone": zone_filter,
                "label": f"Highlight Zone {zone_filter}",
            })
        return _reply(
            chat_id,
            f"{len(matching)} matching space(s) are available now "
            f"out of {len(active)} active spaces.",
            actions,
        )

    if "capacity" in lowered or "total spaces" in lowered:
        return _reply(
            chat_id,
            f"There are {occupancy_data['occupancy']['active_capacity']} active "
            f"parking spaces in total ({occupancy_data['occupancy']['total_slots']} "
            "configured spaces, including inactive spaces).",
            actions,
        )

    if "occup" in lowered or "vehicle" in lowered or "parked" in lowered:
        return _reply(
            chat_id,
            f"There are {len(occupied)} vehicle(s) parked now, using "
            f"{occupancy_data['occupancy']['occupied_slots']} of "
            f"{occupancy_data['occupancy']['active_capacity']} active spaces "
            f"({occupancy_data['occupancy']['occupancy_percentage']:.1f}%).",
            actions,
        )

    zone = _parse_zone(message)
    if zone:
        zone_data = next(
            (item for item in zones if str(item["zone"]).upper() == zone),
            None,
        )
        if not zone_data:
            return _reply(chat_id, f"I couldn't find Zone {zone} in the live parking data.", actions)
        actions.append({
            "type": "highlight_zone",
            "zone": zone,
            "label": f"Highlight Zone {zone}",
        })
        return _reply(
            chat_id,
            f"Zone {zone} has {zone_data['available_slots']} available and "
            f"{zone_data['occupied_slots']} occupied spaces out of "
            f"{zone_data['active_capacity']} active "
            f"({zone_data['occupancy_percentage']:.1f}% occupied).",
            actions,
        )

    return _reply(
        chat_id,
        "I use a deterministic intent-based helper (no external language model is "
        "configured). I can answer live availability, zone occupancy, occupied or "
        "EV/accessible spaces, check reservation-time availability, explain actual "
        "30/60-minute model forecasts, and prepare a reservation for confirmation.",
    )


def _recommendation_reply(chat_id, message, timezone_offset_minutes):
    vehicle_type = _parse_vehicle_type(message)
    slot_type = _parse_slot_type(message)
    if vehicle_type and slot_type is None:
        slot_type = "REGULAR"
    zone = _parse_zone(message)
    now = datetime.now(timezone.utc)
    start = now
    end = now + timedelta(minutes=1)
    parsed_start, parsed_end = _parse_booking_times(
        message, timezone_offset_minutes
    )
    try:
        if parsed_start:
            start = datetime.fromisoformat(parsed_start.replace("Z", "+00:00"))
        if parsed_end:
            end = datetime.fromisoformat(parsed_end.replace("Z", "+00:00"))
        elif parsed_start:
            end = start + timedelta(hours=1)
    except ValueError:
        return _reply(
            chat_id,
            "I couldn't read that time. Use ISO timestamps or a time like "
            "“tomorrow 5 pm to 7 pm”, and I'll check the reservation availability.",
        )

    params = {
        "start_time": start.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "end_time": end.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    if slot_type:
        params["slot_type"] = slot_type
    if zone:
        params["zone"] = zone

    try:
        result = _application_get("/api/availability?" + urlencode(params))
    except Exception:
        current_app.logger.exception("Copilot availability request failed")
        return _reply(chat_id, "Live availability is unavailable right now. Please try again.")

    slots = result.get("available_slots", [])
    if not slots:
        return _reply(
            chat_id,
            "No matching spaces are available for that time and filter. "
            "Try another zone, space type, or time.",
            [{"type": "navigate", "page": "parking", "label": "View parking map"}],
        )

    intent_text = message.lower()
    occupancy = _application_get("/api/analytics/occupancy")
    zones = {
        str(item["zone"]).upper(): item
        for item in occupancy.get("zones", [])
    }
    counts = {}
    for slot in slots:
        counts[slot["zone"]] = counts.get(slot["zone"], 0) + 1
    less_crowded = any(
        phrase in intent_text
        for phrase in ("less crowded", "least crowded", "quiet", "less busy")
    )
    ranked = sorted(
        slots,
        key=lambda item: (
            zones.get(str(item["zone"]).upper(), {}).get(
                "occupancy_percentage", 100
            ) if less_crowded else -counts.get(item["zone"], 0),
            item["zone"],
            item["slot_id"],
        ),
    )
    recommendations = ranked[:5]
    actions = [{"type": "navigate", "page": "parking", "label": "View parking map"}]
    if recommendations:
        zone = recommendations[0]["zone"]
        actions.append({
            "type": "highlight_zone",
            "zone": zone,
            "label": f"Highlight Zone {zone}",
        })
        actions.append({
            "type": "highlight_slot",
            "slot_id": recommendations[0]["slot_id"],
            "zone": zone,
            "label": f"Highlight {recommendations[0]['slot_id']}",
        })
    details = "; ".join(
        f"{item['slot_id']} · Zone {item['zone']} · {item['slot_type']} · "
        f"available for the checked time"
        for item in recommendations
    )
    time_text = (
        f"for {start.astimezone(timezone.utc).strftime('%b %d %H:%M UTC')} to "
        f"{end.astimezone(timezone.utc).strftime('%H:%M UTC')}"
        if parsed_start or parsed_end else "right now"
    )
    qualifier = (
        "Zones are ranked by current occupancy; " if less_crowded
        else "Options are grouped using current reservation availability; "
    )
    return _reply(
        chat_id,
        f"I found {len(slots)} matching space(s) {time_text}. "
        f"{'For the requested ' + vehicle_type.lower() + ', ' if vehicle_type else ''}"
        f"{qualifier}these are availability results, not a hold or reservation. "
        f"{'The inventory does not define vehicle-specific space restrictions. ' if vehicle_type else ''}"
        f"Options: {details}.",
        actions,
    )


def _forecast_reply(chat_id, message):
    result = _application_get("/api/analytics/forecast")
    if result.get("status") != "success":
        have = result.get("observations_available", 0)
        need = result.get("observations_required", 5)
        return _reply(
            chat_id,
            f"The occupancy forecast is not available yet: "
            f"{result.get('message', 'the forecasting service has insufficient data')} "
            f"({have}/{need} observations). These are predictions, so I won't estimate "
            "a value without model output.",
        )

    horizon = 60 if re.search(r"\b(60|hour|hourly)\b", message, re.I) else 30
    prediction = next(
        (item for item in result.get("predictions", [])
         if item.get("forecast_minutes") == horizon),
        None,
    )
    if prediction is None:
        return _reply(
            chat_id,
            f"The model did not return a {horizon}-minute forecast. "
            f"It has {result.get('observations_available', 0)} observations.",
        )

    response = _application_get("/api/analytics/occupancy")
    occupied = response["occupancy"]["occupied_slots"]
    current_capacity = response["occupancy"]["active_capacity"]
    text = (
        f"Current occupancy is {occupied}/{current_capacity} spaces. "
        f"The trained {horizon}-minute model predicts "
        f"{prediction['predicted_occupied']}/{prediction['total_capacity']} occupied "
        f"({prediction['predicted_occupancy_pct']:.1f}%) by "
        f"{prediction['forecast_for']}. This prediction uses "
        f"{result['observations_available']} recent observations "
        f"({prediction.get('data_source', 'recorded occupancy')}), last observed "
        f"at {prediction['observation_timestamp']}; it is not a guarantee."
    )
    if re.search(r"\b(zone|crowd|busy|less crowded)\b", message, re.I):
        text += (
            " The model predicts overall occupancy, not zone-level forecasts. "
            "I can compare live zone occupancy, but can't claim which zone will "
            "become crowded from this model."
        )
    return _reply(
        chat_id,
        text,
        [{"type": "navigate", "page": "forecast", "label": "View AI forecasts"}],
    )


@copilot_bp.post("/api/chat")
def chat():
    if not _take_rate_limit():
        return _error(
            "RATE_LIMITED",
            "Too many chat messages. Please wait a minute and try again.",
            429,
        )
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("INVALID_JSON", "Send a JSON object with a message.", 400)

    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return _error("INVALID_MESSAGE", "A non-empty message is required.", 400)
    message = message.strip()
    if len(message) > MAX_MESSAGE_LENGTH:
        return _error(
            "MESSAGE_TOO_LONG",
            f"Messages must be {MAX_MESSAGE_LENGTH} characters or fewer.",
            400,
        )

    timezone_offset = body.get("timezone_offset_minutes", 0)
    if (
        isinstance(timezone_offset, bool)
        or not isinstance(timezone_offset, int)
        or not -840 <= timezone_offset <= 840
    ):
        return _error(
            "INVALID_TIMEZONE",
            "timezone_offset_minutes must be between -840 and 840.",
            400,
        )

    supplied_chat_id = body.get("chat_id")
    if supplied_chat_id is not None and (
        not isinstance(supplied_chat_id, str) or len(supplied_chat_id) > 80
    ):
        return _error("INVALID_CHAT_ID", "Invalid chat session identifier.", 400)
    chat_id, state, error = _get_chat(supplied_chat_id)
    if error:
        return error

    lowered = message.lower()
    if "admin" in lowered or "administrator" in lowered:
        return _reply(
            chat_id,
            "This installation has no authenticated administrator roles, so I "
            "can't provide admin-only analytics or actions. I can answer public "
            "live parking and forecast questions.",
        )

    if state and state.get("flow") == "reservation":
        try:
            return _continue_booking(
                chat_id, state, message, timezone_offset
            )
        except Exception:
            current_app.logger.exception("Copilot reservation flow failed")
            return _reply(
                chat_id,
                "I couldn't safely continue the reservation. No booking was "
                "created. Please start a new reservation request.",
            )

    if re.search(r"\b(cancel|stop|nevermind|never mind)\b", lowered):
        return _reply(
            chat_id,
            "I can clear a pending chat booking, but this installation has no "
            "reservation-cancellation endpoint. Manage existing bookings in Reservations.",
            [{"type": "navigate", "page": "reservations", "label": "Open reservations"}],
        )

    if re.search(r"\b(book|booking|reserve|reservation)\b", lowered):
        return _booking_state_reply(
            chat_id,
            _merge_booking(None, message, timezone_offset),
            timezone_offset,
        )

    if re.search(r"\b(forecast|predict|prediction|will be|in 30|in 60)\b", lowered):
        try:
            return _forecast_reply(chat_id, message)
        except Exception:
            current_app.logger.exception("Copilot forecast request failed")
            return _reply(
                chat_id,
                "I couldn't retrieve the forecasting service just now. "
                "No prediction is available; please try again later.",
                [{"type": "navigate", "page": "forecast", "label": "View AI forecasts"}],
            )

    if re.search(r"\b(recommend|suggest|find me|find a|need a|need an|get me)\b", lowered):
        try:
            return _recommendation_reply(chat_id, message, timezone_offset)
        except Exception:
            current_app.logger.exception("Copilot recommendation request failed")
            return _reply(
                chat_id,
                "I couldn't check live availability right now. Please try again.",
            )

    if re.search(r"\b(status|look up|lookup|details)\b", lowered) and re.search(
        r"\b(reservation|booking|PK-[A-Z0-9]+)\b", lowered
    ):
        return _reply(
            chat_id,
            "Reservation lookup is not enabled in the copilot because this app "
            "does not authenticate reservation ownership. You can manage a "
            "reservation using its ID in the existing Reservations or Vehicle "
            "Entry / Exit pages.",
            [{"type": "navigate", "page": "reservations", "label": "Open reservations"}],
        )

    try:
        return _live_parking_reply(chat_id, message)
    except Exception:
        current_app.logger.exception("Copilot live parking query failed")
        return _reply(
            chat_id,
            "I couldn't read current parking data right now. Please try again or "
            "open the parking map to check the connection.",
            [{"type": "navigate", "page": "parking", "label": "View parking map"}],
        )

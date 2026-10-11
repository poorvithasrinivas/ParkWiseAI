# ParkWise AI

## Run locally

From the project root, install the backend dependencies once:

```powershell
python -m pip install -r backend\requirements.txt
```

Start the Flask API in its own terminal:

```powershell
python -m backend.app
```

Optionally start the real 15-minute occupancy snapshot collector in another
terminal:

```powershell
python -m backend.occupancy_collector
```

Serve the frontend from the project root in a third terminal:

```powershell
python -m http.server 8000 --directory frontend
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The frontend calls the API
at `http://127.0.0.1:5001`.

## ParkWise Copilot

The floating **Ask ParkWise** control is available on every app page. It
answers supported questions from live slot, occupancy, availability, and model
forecast APIs; can highlight a suggested zone/space on the map; and can prepare
a reservation that is submitted only after explicit confirmation.

The Copilot is a transparent deterministic intent-based assistant. It requires
no external provider, API key, or environment variables. It does not claim
generative AI capabilities. Pending reservation conversations are held
server-side in memory for up to 30 minutes, so run one backend worker and keep
the same backend process running while completing a chat reservation.

This project does not currently authenticate users or define administrator
roles. The Copilot therefore does not expose administrator-only information or
reservation lookups, and it cannot cancel reservations because no cancellation
endpoint exists. EV space records do not indicate charger equipment or charger
availability.

See [docs/API_CONTRACT.md](docs/API_CONTRACT.md) for the chat API contract and
limitations.

## Tests

Run the backend test suite from the project root:

```powershell
python -m pytest tests -q
```
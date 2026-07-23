# Attendance Management System

FastAPI backend for employee attendance, leave management, ZKTeco device sync, and AI voice assistant (English / Urdu / Roman Urdu).

> **Note:** The Flutter mobile app lives in `attendance_mobile/` and is deployed separately. It is excluded from this repository.

## Stack

- **API:** FastAPI + Uvicorn
- **ORM:** SQLAlchemy
- **Database:** PostgreSQL (Supabase) in production, SQLite optional for local dev
- **AI:** OpenAI (GPT-4o, Whisper, TTS)
- **Frontend:** Static web dashboard in `/static`

## Local development

```bash
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env         # then fill in your keys
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000`

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Production | PostgreSQL connection string (Supabase) |
| `OPENAI_API_KEY` | Yes (for AI) | OpenAI API key |
| `OPENAI_TTS_VOICE` | No | Default: `sage` |
| `OPENAI_TTS_MODEL` | No | Default: `tts-1-hd` |
| `PORT` | Railway | Set automatically by Railway |

## Deploy to Railway

1. Push this repo to GitHub (already configured).
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
3. Select `reehaanali4790/Attendance_management_system`.
4. In **Variables**, add:
   - `DATABASE_URL` — your Supabase PostgreSQL URL (`postgresql+psycopg2://...`)
   - `OPENAI_API_KEY`
   - `OPENAI_TTS_VOICE` (optional)
   - `OPENAI_TTS_MODEL` (optional)
5. Railway will detect `railway.toml` / `Procfile` and start:
   ```
   uvicorn main:app --host 0.0.0.0 --port $PORT
   ```
6. Open the generated Railway URL (e.g. `https://your-app.up.railway.app`).

### ZKTeco device sync on Railway

The biometric device sync (`pyzk`) expects the ZKTeco uFace800 on your **local network** (e.g. `192.168.x.x`). Railway runs in the cloud and **cannot reach a local LAN device**. Attendance sync will be skipped/fail gracefully on cloud deploy. Options:

- Run sync on a machine on the same LAN as the device, pointing `DATABASE_URL` at Supabase
- Use Railway only for the API + dashboard + AI, with manual data import

## API health check

```
GET /health
```

## Project structure

```
├── main.py           # FastAPI app entry
├── routes.py         # REST API routes
├── models.py         # SQLAlchemy models
├── database.py       # DB engine (env-driven)
├── ai_service.py     # AI voice / NLP-to-SQL
├── sync_service.py   # Attendance sync logic
├── zk_service.py     # ZKTeco device integration
├── static/           # Web dashboard
└── requirements.txt
```

## License

Private — All rights reserved.

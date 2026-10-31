# Frontend (owner A: website, owner B: CRM screens)

Not scaffolded yet. Build against the backend API:

- Contract: [`../backend/openapi.json`](../backend/openapi.json) (generated from the running app; regenerate with `python -m app.export_openapi` in `backend/`)
- Live docs while the API runs: http://localhost:8000/docs
- CORS allows `http://localhost:3000` by default (`CORS_ORIGIN` in `.env`).

Key flows: `POST /v1/leads` (website form, send an `idempotency-key` header generated once per submit),
`POST /v1/imports` (CSV, per-row outcomes), `GET /v1/leads`, `GET /v1/leads/{id}`, `POST /v1/drafts/{id}/approve|reject|send`,
`POST /v1/replies`, `GET /v1/tasks`, `POST /v1/tasks/{id}/accept`, `GET /v1/dashboard`.

# Mnemos — Knowledge Production Platform

A self-hosted platform that continuously analyzes and accumulates knowledge of complex multi-language, multi-database production systems and uses that knowledge asset to handle development, Q&A, and data-lookup requests on demand.

See `Mnemos_spec.md` for the full Phase 1 design specification.

## Quick Start

```bash
cp .env.example .env
# (optional) generate a Fernet key for secret encryption
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste it into .env as FERNET_KEY=...

docker compose up -d
curl http://localhost:8080/api/v1/health
# -> {"status":"ok"}
```

## Repository Layout

```
Mnemos/
├── docker-compose.yml         # postgres + redis + platform
├── server/                    # Python FastAPI server (single process)
├── analyzers/                 # Language/DB analyzer containers
├── infra/                     # Deployment helpers
└── docs/                      # User & operator guides
```

## Development

The platform follows the 8-week roadmap defined in `Mnemos_spec.md` §15.
Currently: **Week 1 — foundation + GUI skeleton**.

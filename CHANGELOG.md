# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/) once a first
release ships (0.x releases may include breaking changes between minors).

## [Unreleased]

### Added
- Project scaffold (M0): FastAPI app under `backend/`, `structlog` JSON logging
  with request-id middleware and gzip-rotated daily log files, SQLite +
  SQLModel wired up with Alembic migrations, `ruff`/`black`/`pytest` tooling,
  CI workflow, and a `docker-compose`/`Dockerfile` skeleton. See
  [ROADMAP.md](ROADMAP.md) for what's next.

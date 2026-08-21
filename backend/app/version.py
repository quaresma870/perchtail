"""Single source of truth for the app version, read by both app.main (the
FastAPI app's own `version=`) and app.api.monitoring (the detailed health
endpoint's `version` field) — kept in its own module so neither has to
import the other to get at it."""

APP_VERSION = "0.1.1"

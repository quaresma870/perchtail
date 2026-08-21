from apscheduler.schedulers.background import BackgroundScheduler

# A single shared instance, not one per module -- app/main.py wires up every
# job on it (scratch sweeps, search indexing) and starts/stops it in the
# lifespan; app/api/monitoring.py needs to inspect job next-run times for
# the detailed health endpoint. A dedicated module (rather than a variable
# on app.main) lets both import it without main.py <-> monitoring.py
# becoming circular.
scheduler = BackgroundScheduler()

from apscheduler.schedulers.background import BackgroundScheduler

# A single shared instance, not one per module -- app/main.py wires up every
# job on it (through app/jobs.py's register) and starts/stops it in the
# lifespan; app/api/monitoring.py needs to inspect job next-run times for
# the detailed health endpoint. A dedicated module (rather than a variable
# on app.main) lets both import it without main.py <-> monitoring.py
# becoming circular.
#
# Job defaults (#195, F-I): APScheduler skips a run that starts more than
# misfire_grace_time late (by default one second), which a long request or
# a slow sweep holding the worker easily causes, and a skipped alert pass
# delays alerts. A late run still happens, and runs that piled up while one was busy collapse
# into one (coalesce) instead of running back to back.
scheduler = BackgroundScheduler(
    job_defaults={"coalesce": True, "misfire_grace_time": 300, "max_instances": 1}
)

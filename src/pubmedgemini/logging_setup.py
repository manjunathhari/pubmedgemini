import logging
import sys

APP_LOGGER = "pubmedgemini"
HANDLER_NAME = "pubmedgemini-stderr"
FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Their messages carry libpq text (host, port, role name). The app logs its own fixed kinds
# (db_unreachable, migration_failed, db_error) instead.
QUIET_LOGGERS = ("psycopg", "psycopg.pool")


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever sys.stderr is at the time of each record."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:
        pass


def configure_logging() -> None:
    """The app's logger at INFO with one stderr handler; psycopg silenced. Safe to call again."""
    app_log = logging.getLogger(APP_LOGGER)
    app_log.setLevel(logging.INFO)
    if not any(h.get_name() == HANDLER_NAME for h in app_log.handlers):
        handler = _StderrHandler()
        handler.set_name(HANDLER_NAME)
        handler.setFormatter(logging.Formatter(FORMAT))
        app_log.addHandler(handler)
    for name in QUIET_LOGGERS:
        quiet = logging.getLogger(name)
        quiet.setLevel(logging.CRITICAL + 1)
        quiet.propagate = False
        if not quiet.handlers:
            quiet.addHandler(logging.NullHandler())

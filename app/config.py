"""Configuration for the meeting invite adjudication service."""
import os

DB_PATH = os.environ.get("MEETING_DB_PATH", "/data/meetings.db")
# How many occurrences an RRULE with COUNT may contain.
MAX_COUNT = int(os.environ.get("MEETING_MAX_COUNT", "100000"))
# Maximum COUNT for which the last occurrence is precomputed at ingest time.
LAST_COMPUTE_LIMIT = int(os.environ.get("MEETING_LAST_COMPUTE_LIMIT", "5000"))

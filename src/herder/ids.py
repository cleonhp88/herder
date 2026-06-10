"""Job ID generation using ULID."""

from ulid import ULID


def new_job_id() -> str:
    """Generate a new job ID with job_ prefix and ULID suffix.

    Returns:
        A job ID in format "job_<26-char ULID>"
    """
    return f"job_{ULID()}"

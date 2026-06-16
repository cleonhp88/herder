import re
from herder.ids import new_job_id


def test_shape():
    """Job ID should have job_ prefix followed by 26-char ULID."""
    jid = new_job_id()
    assert jid.startswith("job_")
    assert re.fullmatch(r"job_[0-9A-Za-z]{26}", jid)


def test_unique():
    """Each call to new_job_id should return a different ID."""
    jid1 = new_job_id()
    jid2 = new_job_id()
    assert jid1 != jid2

import pytest

from sentinel.audit import AuditStore
from sentinel.pipeline import Sentinel
from sentinel.policy import PolicyStore


@pytest.fixture()
def sentinel(tmp_path):
    store = AuditStore(str(tmp_path / "t.db"))
    return Sentinel(store, PolicyStore())

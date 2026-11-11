from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection

from crm.api import Conflict
from crm.models import WriteLock
from tests.test_workflow import flow as flow
from tests.test_workflow import submit
from workflow.models import Decision, Quote
from workflow.services import command


@pytest.mark.django_db(transaction=True)
def test_simultaneous_approvals_have_version_conflict_and_safe_retry(flow):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locking")
    submit(flow)
    Quote.objects.filter(pk=flow["q"].pk).update(artificial_split=True)
    flow["q"].refresh_from_db()
    version = flow["q"].version
    WriteLock.objects.get_or_create(name="crm")
    barrier = Barrier(2)

    def decide(pair):
        user, role = pair
        close_old_connections()
        try:
            barrier.wait(timeout=15)
            try:
                command(
                    user,
                    Quote,
                    flow["q"].pk,
                    version,
                    "decide",
                    {"role": role, "approved": True, "reason": "Проверено"},
                )
                return role, "saved"
            except Conflict:
                return role, "conflict"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(decide, [(flow["rop"], "РОП"), (flow["user"], "Директор")])
        )
    assert sorted(r[1] for r in results) == ["conflict", "saved"]
    assert Decision.objects.filter(quote=flow["q"]).count() == 1
    q = Quote.objects.get(pk=flow["q"].pk)
    assert q.status == "pending"
    retry_role = next(r[0] for r in results if r[1] == "conflict")
    retry_user = flow["rop"] if retry_role == "РОП" else flow["user"]
    command(
        retry_user,
        Quote,
        q.pk,
        q.version,
        "decide",
        {"role": retry_role, "approved": True, "reason": "После обновления карточки"},
    )
    q.refresh_from_db()
    assert q.status == "approved" and q.decisions.count() == 2

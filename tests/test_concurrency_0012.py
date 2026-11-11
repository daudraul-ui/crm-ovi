from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection

from crm.excel import analyze
from crm.models import ImportJob, Store, WriteLock
from tests.test_task0012 import make_job


@pytest.mark.django_db(transaction=True)
def test_overlapping_imports_postgresql(user, tmp_path, settings):
    if connection.vendor != "postgresql":
        pytest.skip("Row-level locking must be checked against PostgreSQL")
    row = {
        "chain": "Сеть",
        "company": "ЮЛ",
        "manager": user.username,
        "code": "001",
        "name": "Магазин",
        "address": "Адрес",
        "region": "Москва",
        "format": "Стандарт",
    }
    jobs = [make_job(user, tmp_path, settings, [row]) for _ in range(2)]
    for job in jobs:
        job.preview = analyze(job)
        job.save()
    WriteLock.objects.get_or_create(name="crm")
    barrier = Barrier(2)

    def commit(pk):
        close_old_connections()
        try:
            job = ImportJob.objects.get(pk=pk)
            barrier.wait(timeout=15)
            try:
                analyze(job, commit=True)
                return "saved"
            except ValueError:
                return "stale"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(commit, [job.pk for job in jobs]))
    assert sorted(results) == ["saved", "stale"]
    assert Store.objects.count() == 1

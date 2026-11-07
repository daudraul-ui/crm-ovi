import time

import pytest

from crm.excel import analyze
from crm.models import Store
from tests.test_task0012 import make_job


@pytest.mark.django_db
@pytest.mark.parametrize("count", [1000, 10000])
def test_import_scale(user, tmp_path, settings, count, record_property):
    rows = [
        {
            "chain": "Сеть",
            "company": "ЮЛ",
            "manager": user.username,
            "code": str(i).zfill(6),
            "name": f"Магазин {i}",
            "address": f"Адрес {i}",
            "region": "Москва",
            "format": "Стандарт",
        }
        for i in range(count)
    ]
    job = make_job(user, tmp_path, settings, rows)
    started = time.monotonic()
    job.preview = analyze(job)
    assert not job.preview["errors"] and not job.preview["questions"]
    assert not Store.objects.exists()
    result = analyze(job, commit=True)
    assert result["counts"]["stores"] == count
    assert Store.objects.count() == count
    assert Store.objects.get(code="000000").code == "000000"
    elapsed = time.monotonic() - started
    record_property("rows", count)
    record_property("preview_and_commit_seconds", round(elapsed, 2))
    print(f"EXCEL_SCALE rows={count} preview_and_commit_seconds={elapsed:.2f}")

import io

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.deletion import ProtectedError
from openpyxl import Workbook, load_workbook

from crm.excel import HEADERS, analyze
from crm.integrity import validate_inn
from crm.models import Chain, Company, ImportJob, Project, RecoveryCode, Store
from crm.recovery import generate_codes

pytestmark = pytest.mark.django_db


def make_job(user, tmp_path, settings, rows, mode="create"):
    settings.MEDIA_ROOT = tmp_path
    book = Workbook()
    book.active.append(list(HEADERS.values()))
    for row in rows:
        book.active.append([row.get(key, "") for key in HEADERS])
    buf = io.BytesIO()
    book.save(buf)
    job = ImportJob.objects.create(
        user=user,
        original_name="test.xlsx",
        mode=mode,
        file=ContentFile(buf.getvalue(), name="test.xlsx"),
        mapping={str(i): key for i, key in enumerate(HEADERS)},
    )
    return job


def resolve(job):
    for _ in range(10):
        job.preview = analyze(job)
        if not job.preview["questions"]:
            break
        for question in job.preview["questions"]:
            options = question["options"]
            # These fixtures deliberately select allowed blank references or existing objects.
            preferred = next(
                (str(k) for k, label in options if str(k) in ("blank", "keep")),
                str(options[0][0]),
            )
            job.answers[question["key"]] = preferred
    return job.preview


def test_protect_m2m_and_archive(verified, company, user):
    chain = Chain.objects.create(name="Сеть")
    store = Store.objects.create(
        name="Магазин", code="001", chain=chain, address="Адрес"
    )
    project = Project.objects.create(name="Проект", company=company, manager=user)
    project.stores.add(store)
    with pytest.raises(ProtectedError):
        from django.db import transaction

        with transaction.atomic():
            Store.objects.filter(pk=store.pk).delete()
    assert (
        verified.post(
            f"/stores/{store.pk}/archive/", {"version": 1, "archived": "1"}
        ).status_code
        == 302
    )
    assert project.stores.get().archived
    assert (
        verified.post(
            f"/stores/{store.pk}/archive/", {"version": 2, "archived": "0"}
        ).status_code
        == 302
    )
    assert not project.stores.get().archived


def test_project_explicit_additional_client(verified, user, company):
    other = Company.objects.create(name="Другое ЮЛ", manager=user)
    chain = Chain.objects.create(name="Сеть")
    chain.companies.add(other)
    store = Store.objects.create(
        name="Магазин", code="01", chain=chain, company=other, address="Адрес"
    )
    data = {
        "name": "Проект",
        "company": company.pk,
        "manager": user.pk,
        "stores": [store.pk],
    }
    assert (
        verified.post(
            "/api/projects/", data, content_type="application/json"
        ).status_code
        == 400
    )
    data["additional_companies"] = [other.pk]
    response = verified.post("/api/projects/", data, content_type="application/json")
    assert response.status_code == 201, response.content
    project = Project.objects.get(pk=response.json()["id"])
    with pytest.raises(ValidationError):
        project.additional_companies.clear()


def test_stale_api_and_html(verified, company):
    url = f"/api/companies/{company.pk}/"
    assert (
        verified.patch(
            url, {"version": 1, "name": "Первый"}, content_type="application/json"
        ).status_code
        == 200
    )
    assert (
        verified.patch(
            url, {"version": 1, "name": "Второй"}, content_type="application/json"
        ).status_code
        == 409
    )
    assert (
        verified.patch(
            url, {"name": "Без версии"}, content_type="application/json"
        ).status_code
        == 409
    )
    response = verified.post(
        f"/companies/{company.pk}/edit/",
        {
            "record_version": 1,
            "name": "Второй",
            "inn": company.inn,
            "manager": company.manager_id,
        },
    )
    assert "Запись изменена" in response.content.decode()
    company.refresh_from_db()
    assert company.name == "Первый"


@pytest.mark.parametrize(
    "inn", ["1234567890", "123", "abcdefghij", "７７０７０８３８９３"]
)
def test_invalid_inn(inn):
    with pytest.raises(ValidationError):
        validate_inn(inn)


def test_duplicate_inn(verified, company, user):
    response = verified.post(
        "/api/companies/",
        {"name": "Дубль", "manager": user.pk, "inn": company.inn},
        content_type="application/json",
    )
    assert response.status_code == 400
    assert Company.objects.count() == 1


def test_recovery_code_single_use_requires_new_totp(client, user):
    from django_otp.plugins.otp_totp.models import TOTPDevice

    device = TOTPDevice.objects.create(user=user, confirmed=True)
    codes = generate_codes(user)
    assert not RecoveryCode.objects.filter(digest=codes[0]).exists()
    client.force_login(user)
    assert client.post("/2fa/", {"recovery_code": codes[0]}).status_code == 302
    assert not TOTPDevice.objects.filter(pk=device.pk).exists()
    assert client.get("/").url == "/2fa/"
    assert client.post("/2fa/", {"recovery_code": codes[0]}).status_code == 400


def test_file_spoof_rejected(verified, company, tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    response = verified.post(
        "/documents/new/",
        {
            "name": "Обман",
            "company": company.pk,
            "file": SimpleUploadedFile("x.pdf", b"<script>alert(1)</script>"),
        },
    )
    assert response.status_code == 200
    assert "Документ отклонён" in response.content.decode()


def test_import_preview_commit_repeat(user, tmp_path, settings):
    job = make_job(
        user,
        tmp_path,
        settings,
        [
            {
                "chain": "Сеть",
                "company": "ЮЛ",
                "inn": "7707083893",
                "manager": user.username,
                "code": "0001",
                "name": "Магазин",
                "address": "Адрес",
            }
        ],
    )
    preview = resolve(job)
    assert not preview["errors"], preview
    assert not Store.objects.exists() and not Company.objects.exists()
    result = analyze(job, commit=True)
    assert result["counts"]["stores"] == 1
    assert Store.objects.get().code == "0001"
    job.answers = {}
    resolve(job)
    analyze(job, commit=True)
    assert Store.objects.count() == 1
    assert Company.objects.count() == 1


def test_import_atomic_error(user, tmp_path, settings):
    rows = [
        {
            "chain": "Сеть",
            "company": "ЮЛ",
            "manager": user.username,
            "code": "001",
            "name": "Магазин",
            "address": "Адрес",
        },
        {
            "chain": "Сеть",
            "company": "Ошибка",
            "inn": "1234567890",
            "manager": user.username,
        },
    ]
    job = make_job(user, tmp_path, settings, rows)
    result = resolve(job)
    assert result["errors"]
    with pytest.raises(ValueError):
        analyze(job, commit=True)
    assert not Store.objects.exists() and not Chain.objects.exists()


def test_export_formula_safe_and_filters(verified, user, company):
    chain = Chain.objects.create(name="Сеть")
    Store.objects.create(name="=1+1", chain=chain, code="00001", address="Адрес")
    response = verified.get("/excel/export/stores/", {"q": "00001"})
    assert response.status_code == 200
    book = load_workbook(io.BytesIO(response.content), data_only=False)
    assert book.active["A2"].value == "00001"
    assert book.active["B2"].value == "=1+1"
    assert book.active["B2"].data_type == "s"
    book.close()


def test_employee_last_admin_and_stale(verified, user):
    from django.core import signing

    from crm.employees import employee_state

    data = {
        "username": user.username,
        "revision": signing.dumps(employee_state(user)),
        "roles": [Group.objects.get(name="Менеджер").pk],
    }
    response = verified.post(f"/settings/employees/{user.pk}/", data)
    assert "последнего" in response.content.decode()
    user.refresh_from_db()
    assert user.is_active


@pytest.mark.parametrize("missing", ["company", "region", "format"])
def test_import_missing_values_asks_before_saving(user, tmp_path, settings, missing):
    row = {
        "chain": "Сеть",
        "company": "ЮЛ",
        "manager": user.username,
        "code": "001",
        "name": "Магазин",
        "address": "Адрес",
        "region": "Москва",
        "format": "Малый",
    }
    row.pop(missing)
    job = make_job(user, tmp_path, settings, [row])
    preview = analyze(job)
    assert preview["questions"]
    assert not Store.objects.exists()
    assert not Company.objects.exists()


def test_import_update_and_changed_preview(user, tmp_path, settings):
    row = {
        "chain": "Сеть",
        "company": "ЮЛ",
        "manager": user.username,
        "code": "001",
        "name": "Магазин",
        "address": "Первый",
        "region": "Москва",
        "format": "Малый",
    }
    job = make_job(user, tmp_path, settings, [row])
    resolve(job)
    analyze(job, commit=True)
    changed = make_job(
        user, tmp_path, settings, [{**row, "address": "Второй"}], mode="update"
    )
    preview = analyze(changed)
    assert (
        preview["questions"]
        and preview["changes"][0]["differences"]["address"]["before"] == "Первый"
    )
    for question in preview["questions"]:
        changed.answers[question["key"]] = "update"
    changed.preview = analyze(changed)
    store = Store.objects.get()
    store.address = "Третий"
    store.save()
    with pytest.raises(ValueError, match="изменились"):
        analyze(changed, commit=True)
    assert Store.objects.get().address == "Третий"


def test_import_mid_commit_failure_rolls_back(user, tmp_path, settings, monkeypatch):
    row = {
        "chain": "Сеть",
        "company": "ЮЛ",
        "manager": user.username,
        "code": "001",
        "name": "Магазин",
        "address": "Адрес",
        "region": "Москва",
        "format": "Малый",
    }
    job = make_job(user, tmp_path, settings, [row])
    resolve(job)
    from crm import excel

    def fail(*args, **kwargs):
        if Store.objects.exists():
            raise RuntimeError("Injected failure")

    monkeypatch.setattr(excel, "audit", fail)
    with pytest.raises(RuntimeError):
        analyze(job, commit=True)
    assert not Store.objects.exists() and not Company.objects.exists()


@pytest.mark.parametrize("payload", [b"not a zip", b"PK\x03\x04broken"])
def test_import_damaged_file(verified, tmp_path, settings, payload):
    settings.MEDIA_ROOT = tmp_path
    response = verified.post(
        "/stores/import/", {"file": SimpleUploadedFile("broken.xlsx", payload)}
    )
    assert response.status_code == 200
    assert not ImportJob.objects.exists()


def test_import_export_permissions(verified, user):
    user.groups.set([Group.objects.get(name="Инженер")])
    assert verified.get("/stores/import/").status_code == 403
    assert verified.get("/excel/export/companies/").status_code == 403
    assert verified.get("/excel/export/stores/").status_code == 403


def test_mapping_requires_explicit_unknown_columns(user, tmp_path, settings):
    job = make_job(user, tmp_path, settings, [{"chain": "Сеть"}])
    job.mapping.pop("0")
    with pytest.raises(ValueError, match="каждую колонку"):
        analyze(job)
    job.mapping["0"] = "ignore"
    assert not analyze(job)["errors"]


def test_import_multiple_clients_and_networks(user, tmp_path, settings):
    rows = [
        {
            "chain": chain,
            "company": company,
            "manager": user.username,
            "code": code,
            "name": "Магазин",
            "address": "Адрес",
            "region": "Москва",
            "format": "Малый",
        }
        for chain, company, code in [
            ("Сеть A", "ЮЛ A", "001"),
            ("Сеть A", "ЮЛ B", "002"),
            ("Сеть B", "ЮЛ A", "001"),
        ]
    ]
    job = make_job(user, tmp_path, settings, rows)
    for _ in range(10):
        job.preview = analyze(job)
        if not job.preview["questions"]:
            break
        for question in job.preview["questions"]:
            values = [str(k) for k, _ in question["options"]]
            job.answers[question["key"]] = "new" if "new" in values else values[0]
    assert not job.preview["errors"]
    analyze(job, commit=True)
    assert Company.objects.count() == 2
    assert Chain.objects.count() == 2
    assert Store.objects.count() == 3

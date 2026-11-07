import io

import pytest
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.utils import timezone
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice
from openpyxl import Workbook

from crm.models import (
    AuditEvent,
    Chain,
    Company,
    Document,
    Notification,
    Project,
    Region,
    Store,
    StoreFormat,
    Task,
)

pytestmark = pytest.mark.django_db


def code(device):
    return str(
        totp(
            device.bin_key,
            step=device.step,
            t0=device.t0,
            digits=device.digits,
            drift=device.drift,
        )
    ).zfill(6)


def test_password_login_requires_otp(client, user):
    assert (
        client.post(
            "/login/", {"username": user.username, "password": "wrong"}
        ).status_code
        == 200
    )
    response = client.post(
        "/login/", {"username": user.username, "password": "valid-password-123"}
    )
    assert response.url == "/2fa/"
    assert client.get("/").url == "/2fa/"
    assert client.get("/api/companies/").status_code == 403
    assert client.get("/companies/").url == "/2fa/"


def test_enrollment_verification_replay(client, user):
    client.force_login(user)
    assert client.get("/2fa/").status_code == 200
    device = TOTPDevice.objects.get(user=user)
    assert not device.confirmed
    token = code(device)
    assert client.post("/2fa/", {"token": token}).status_code == 200
    device.refresh_from_db()
    assert device.confirmed
    assert client.get("/").status_code == 200
    client.post("/logout/")
    client.force_login(user)
    assert client.post("/2fa/", {"token": token}).status_code == 200
    assert client.get("/").url == "/2fa/"


def test_invalid_otp(client, user):
    client.force_login(user)
    client.get("/2fa/")
    assert client.post("/2fa/", {"token": "invalid"}).status_code == 200
    assert not TOTPDevice.objects.get(user=user).confirmed


def test_existing_otp_secret_not_exposed(client, user):
    TOTPDevice.objects.create(user=user, confirmed=True)
    client.force_login(user)
    assert b"otpauth://" not in client.get("/2fa/").content


def test_anonymous(client):
    assert client.get("/").status_code == 302
    assert client.get("/api/companies/").status_code == 403
    assert client.get("/health/").content == b"ok"


def test_roles_multiple(verified, user):
    user.groups.clear()
    user.groups.add(
        Group.objects.get(name="Инженер"), Group.objects.get(name="Менеджер")
    )
    assert verified.get("/companies/").status_code == 200
    assert verified.delete("/api/companies/1/").status_code == 403


@pytest.mark.parametrize(
    "path", ["/companies/", "/companies/new/", "/api/companies/", "/settings/"]
)
def test_no_role_access(verified, user, path):
    user.groups.clear()
    response = verified.get(path)
    assert response.status_code == (200 if path == "/settings/" else 403)


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("companies", {"name": "Новый клиент", "inn": "7736050003"}),
        ("chains", {"name": "Сеть"}),
    ],
)
def test_api_crud(verified, user, kind, payload):
    if kind == "companies":
        payload["manager"] = user.pk
    response = verified.post(f"/api/{kind}/", payload, content_type="application/json")
    assert response.status_code == 201, response.content
    pk = response.json()["id"]
    assert verified.get(f"/api/{kind}/{pk}/").status_code == 200
    assert (
        verified.patch(
            f"/api/{kind}/{pk}/",
            {
                "version": verified.get(f"/api/{kind}/{pk}/").json().get("version", 1),
                "name": "Изменено",
            },
            content_type="application/json",
        ).status_code
        == 200
    )
    assert verified.delete(f"/api/{kind}/{pk}/").status_code == 204


def test_stores_projects_crud(verified, user, company):
    chain = Chain.objects.create(name="Сеть")
    store = verified.post(
        "/api/stores/",
        {"name": "Магазин", "chain": chain.pk, "code": "001", "address": "Москва"},
        content_type="application/json",
    )
    assert store.status_code == 201, store.content
    sid = store.json()["id"]
    project = verified.post(
        "/api/projects/",
        {"name": "Проект", "company": company.pk, "manager": user.pk, "stores": [sid]},
        content_type="application/json",
    )
    assert project.status_code == 201, project.content
    pid = project.json()["id"]
    assert Project.objects.get(pk=pid).stores.get().pk == sid
    assert (
        verified.patch(
            f"/api/projects/{pid}/",
            {
                "version": verified.get(f"/api/projects/{pid}/")
                .json()
                .get("version", 1),
                "status": "done",
            },
            content_type="application/json",
        ).status_code
        == 200
    )
    assert (
        verified.patch(
            f"/api/stores/{sid}/",
            {
                "version": verified.get(f"/api/stores/{sid}/").json().get("version", 1),
                "address": "Новый адрес",
            },
            content_type="application/json",
        ).status_code
        == 200
    )
    assert (
        verified.delete(f"/api/stores/{sid}/").status_code == 400
    )  # Historical project link must survive.
    assert Project.objects.get(pk=pid).stores.filter(pk=sid).exists()
    Project.objects.get(pk=pid).stores.clear()
    assert verified.delete(f"/api/projects/{pid}/").status_code == 204


def test_store_cross_chain_and_unique(verified):
    a = Chain.objects.create(name="A")
    b = Chain.objects.create(name="B")
    region = Region.objects.create(name="Регион")
    fmt = StoreFormat.objects.create(chain=b, name="Формат")
    data = {
        "name": "Магазин",
        "chain": a.pk,
        "region": region.pk,
        "format": fmt.pk,
        "code": "1",
        "address": "Адрес",
    }
    assert (
        verified.post("/api/stores/", data, content_type="application/json").status_code
        == 400
    )
    data.pop("format")
    assert (
        verified.post("/api/stores/", data, content_type="application/json").status_code
        == 201
    )
    assert (
        verified.post("/api/stores/", data, content_type="application/json").status_code
        == 400
    )


def test_finance_protected(verified, user, company):
    user.groups.set([Group.objects.get(name="Менеджер")])
    assert "financial_notes" not in verified.get(f"/api/companies/{company.pk}/").json()
    response = verified.patch(
        f"/api/companies/{company.pk}/",
        {
            "version": verified.get(f"/api/companies/{company.pk}/")
            .json()
            .get("version", 1),
            "financial_notes": "leak",
        },
        content_type="application/json",
    )
    assert response.status_code == 403
    assert (
        b"financial_notes" not in verified.get(f"/companies/{company.pk}/edit/").content
    )
    verified.post(
        f"/companies/{company.pk}/edit/",
        {
            "name": "Изменено",
            "inn": company.inn,
            "manager": user.pk,
            "financial_notes": "leak",
        },
    )
    company.refresh_from_db()
    assert company.financial_notes == "Секрет"


def test_director_finance(verified, company):
    assert (
        verified.get(f"/api/companies/{company.pk}/").json()["financial_notes"]
        == "Секрет"
    )


def test_task_reassignment_history(verified, user):
    other = User.objects.create_user("engineer")
    response = verified.post(
        "/api/tasks/",
        {
            "name": "Замер",
            "assignee": user.pk,
            "due_at": (timezone.now() - timezone.timedelta(days=1)).isoformat(),
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    pk = response.json()["id"]
    assert Task.objects.get(pk=pk).overdue
    assert (
        verified.patch(
            f"/api/tasks/{pk}/",
            {
                "version": verified.get(f"/api/tasks/{pk}/").json().get("version", 1),
                "assignee": other.pk,
            },
            content_type="application/json",
        ).status_code
        == 200
    )
    event = AuditEvent.objects.filter(model="crm.Task", action="update").get()
    assert event.before["assignee"] == str(user.pk)
    assert event.after["assignee"] == str(other.pk)
    assert Notification.objects.filter(user=other).exists()
    from crm.tasks import notify_overdue

    notify_overdue()
    notify_overdue()
    assert Notification.objects.filter(key__startswith="overdue:").count() == 1


@pytest.fixture
def document(verified, user, company, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return Document.objects.create(
        name="Закрытый файл",
        company=company,
        uploaded_by=user,
        file=SimpleUploadedFile("secret.txt", b"secret"),
    )


def test_document_download(verified, document):
    response = verified.get(f"/documents/{document.pk}/download/")
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"secret"
    assert response["Content-Disposition"].startswith("attachment")
    assert verified.get("/private_media/" + document.file.name).status_code in [
        403,
        404,
    ]
    assert "file" not in verified.get(f"/api/documents/{document.pk}/").json()
    assert AuditEvent.objects.filter(action="download").exists()


def test_document_denied(verified, user, document):
    user.groups.clear()
    assert verified.get(f"/documents/{document.pk}/download/").status_code == 403
    assert verified.get("/api/documents/").status_code == 403


def test_document_validation(verified, company, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    assert (
        verified.post(
            "/api/documents/",
            {
                "name": "x",
                "company": company.pk,
                "file": SimpleUploadedFile("x.html", b"<script>"),
            },
        ).status_code
        == 400
    )
    assert (
        verified.post(
            "/api/documents/",
            {"name": "x", "file": SimpleUploadedFile("x.txt", b"hello")},
        ).status_code
        == 400
    )
    assert (
        verified.post(
            "/api/documents/",
            {
                "name": "x",
                "company": company.pk,
                "file": SimpleUploadedFile("x.txt", b"hello"),
            },
        ).status_code
        == 201
    )


def workbook(rows):
    book = Workbook()
    sheet = book.active
    sheet.append(["Сеть", "Код", "Название", "Адрес", "Регион", "Формат"])
    for row in rows:
        sheet.append(row)
    data = io.BytesIO()
    book.save(data)
    return SimpleUploadedFile("stores.xlsx", data.getvalue())


def test_excel_upload_requires_preview(verified, tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    Chain.objects.create(name="Сеть")
    row = ["Сеть", "1", "Магазин", "Москва", None, None]
    response = verified.post("/stores/import/", {"file": workbook([row, row])})
    assert response.status_code == 302
    assert "/excel/import/" in response.url
    assert Store.objects.count() == 0
    assert not AuditEvent.objects.filter(action="import").exists()


def test_notifications_isolated(verified, user):
    other = User.objects.create_user("other")
    note = Notification.objects.create(user=other, text="private")
    assert b"private" not in verified.get("/notifications/").content
    verified.post("/notifications/", {"id": note.pk})
    note.refresh_from_db()
    assert not note.read


def test_audit_redaction(verified, user, company):
    verified.patch(
        f"/api/companies/{company.pk}/",
        {
            "version": verified.get(f"/api/companies/{company.pk}/")
            .json()
            .get("version", 1),
            "financial_notes": "new secret",
        },
        content_type="application/json",
    )
    event = AuditEvent.objects.get(action="update")
    assert event.before["financial_notes"] == "[скрыто]"
    assert event.after["financial_notes"] == "[скрыто]"
    assert (
        verified.post(
            "/api/audit-events/", {}, content_type="application/json"
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/companies/",
        "/contacts/",
        "/chains/",
        "/regions/",
        "/formats/",
        "/stores/",
        "/projects/",
        "/tasks/",
        "/documents/",
        "/notifications/",
        "/settings/",
        "/companies/new/",
        "/stores/import/",
    ],
)
def test_pages(verified, path):
    assert verified.get(path).status_code == 200


def test_csrf_enforced(user):
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    assert (
        client.post(
            "/login/", {"username": user.username, "password": "valid-password-123"}
        ).status_code
        == 403
    )


def test_migrations():
    call_command("makemigrations", check=True, dry_run=True, verbosity=0)


def test_company_filter(verified, user, company):
    assert "Клиент" in verified.get("/companies/", {"q": company.inn}).content.decode()
    assert (
        "Клиент</td>"
        not in verified.get("/companies/", {"q": "missing"}).content.decode()
    )


@pytest.mark.parametrize("kind", ["companies", "projects", "tasks", "stores"])
def test_detail(verified, user, company, kind):
    chain = Chain.objects.create(name="Сеть")
    objects = {
        "companies": company,
        "projects": Project.objects.create(
            name="Проект", company=company, manager=user
        ),
        "tasks": Task.objects.create(
            name="Задача", assignee=user, due_at=timezone.now()
        ),
        "stores": Store.objects.create(
            name="Магазин", chain=chain, code="1", address="Адрес"
        ),
    }
    assert verified.get(f"/{kind}/{objects[kind].pk}/").status_code == 200


def test_login_rate_limit(client, user):
    from django.core.cache import cache

    cache.clear()
    for _ in range(10):
        assert (
            client.post(
                "/login/", {"username": user.username, "password": "bad"}
            ).status_code
            == 200
        )
    assert (
        client.post(
            "/login/", {"username": user.username, "password": "valid-password-123"}
        ).status_code
        == 429
    )
    cache.clear()


def test_html_create_update_delete(verified, user):
    assert (
        verified.post(
            "/companies/new/",
            {"name": "HTML компания", "manager": user.pk, "inn": "7736050003"},
        ).status_code
        == 302
    )
    obj = Company.objects.get(name="HTML компания")
    assert (
        verified.post(
            f"/companies/{obj.pk}/edit/",
            {
                "record_version": obj.version,
                "name": "Изменённая компания",
                "manager": user.pk,
                "inn": "7736050003",
            },
        ).status_code
        == 302
    )
    assert verified.get(f"/companies/{obj.pk}/delete/").status_code == 405
    assert verified.post(f"/companies/{obj.pk}/delete/").status_code == 302
    assert not Company.objects.filter(pk=obj.pk).exists()
    assert AuditEvent.objects.filter(action="delete", model="crm.Company").exists()


def test_protected_delete(verified, user, company):
    Project.objects.create(name="Проект", company=company, manager=user)
    assert verified.delete(f"/api/companies/{company.pk}/").status_code == 400
    assert Company.objects.filter(pk=company.pk).exists()
    assert not AuditEvent.objects.filter(action="delete").exists()


def test_document_parent_access(verified, user, document):
    from django.contrib.auth.models import Permission

    user.groups.clear()
    user.user_permissions.add(Permission.objects.get(codename="view_document"))
    assert verified.get(f"/documents/{document.pk}/download/").status_code == 403


@pytest.mark.parametrize(
    "path", ["/companies/?manager=invalid", "/stores/?chain=invalid"]
)
def test_invalid_filters(verified, path):
    assert verified.get(path).status_code == 200


def test_invalid_notification_id(verified):
    assert verified.post("/notifications/", {"id": "invalid"}).status_code == 200

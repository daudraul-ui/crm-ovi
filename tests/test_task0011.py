import pytest
from django.contrib.auth.models import Group, User
from django.db import IntegrityError, transaction
from django_otp.plugins.otp_totp.models import TOTPDevice

from crm.forms import form_class
from crm.models import AuditEvent, Chain, Company, Region, Store, StoreFormat

pytestmark = pytest.mark.django_db


def employee_data(**extra):
    data = dict(
        username="employee",
        first_name="Анна",
        last_name="Иванова",
        email="anna@example.org",
        password1="Employee-Strong-Password-47!",
        password2="Employee-Strong-Password-47!",
    )
    data.update(extra)
    return data


def test_employee_creation_roles_audit_and_totp(verified, client, user):
    manager = Group.objects.get(name="Менеджер")
    engineer = Group.objects.get(name="Инженер")
    response = verified.post(
        "/settings/employees/new/",
        employee_data(roles=[manager.pk, engineer.pk], is_superuser="1", is_staff="1"),
    )
    assert response.status_code == 302
    employee = User.objects.get(username="employee")
    assert employee.get_full_name() == "Анна Иванова"
    assert employee.check_password("Employee-Strong-Password-47!")
    assert not employee.is_staff and not employee.is_superuser
    assert set(employee.groups.values_list("name", flat=True)) == {
        "Менеджер",
        "Инженер",
    }
    assert not TOTPDevice.objects.filter(user=employee).exists()
    event = AuditEvent.objects.get(action="employee_created")
    assert "password" not in str(event.after).lower()
    assert event.actor == user
    client.logout()
    assert (
        client.post(
            "/login/",
            {"username": employee.username, "password": "Employee-Strong-Password-47!"},
        ).url
        == "/2fa/"
    )
    assert client.get("/companies/").url == "/2fa/"


@pytest.mark.parametrize("role", ["РОП", "Менеджер", "Инженер"])
def test_employee_endpoint_denied(verified, user, role):
    user.groups.set([Group.objects.get(name=role)])
    assert verified.get("/settings/employees/new/").status_code == 403
    assert (
        verified.post(
            "/settings/employees/new/",
            employee_data(roles=[Group.objects.get(name="Директор").pk]),
        ).status_code
        == 403
    )
    assert not User.objects.filter(username="employee").exists()
    assert "Добавить сотрудника" not in verified.get("/settings/").content.decode()


def test_employee_requires_totp(client, user):
    client.force_login(user)
    assert client.get("/settings/employees/new/").url == "/2fa/"


def test_administrator_cannot_grant_finance_role(verified, user):
    user.groups.set([Group.objects.get(name="Администратор")])
    response = verified.post(
        "/settings/employees/new/",
        employee_data(roles=[Group.objects.get(name="Директор").pk]),
    )
    assert response.status_code == 200
    assert not User.objects.filter(username="employee").exists()
    assert (
        verified.post(
            "/settings/employees/new/",
            employee_data(roles=[Group.objects.get(name="Менеджер").pk]),
        ).status_code
        == 302
    )


def test_employee_password_and_roles_validation(verified):
    assert (
        verified.post(
            "/settings/employees/new/",
            employee_data(roles=[], password1="123", password2="123"),
        ).status_code
        == 200
    )
    assert not User.objects.filter(username="employee").exists()


def test_assign_created_employee_to_company(verified, company):
    verified.post(
        "/settings/employees/new/",
        employee_data(roles=[Group.objects.get(name="Менеджер").pk]),
    )
    employee = User.objects.get(username="employee")
    assert (
        "Анна Иванова"
        in verified.get(f"/companies/{company.pk}/edit/").content.decode()
    )
    response = verified.post(
        f"/companies/{company.pk}/edit/",
        {
            "record_version": company.version,
            "name": company.name,
            "manager": employee.pk,
            "inn": company.inn,
        },
    )
    assert response.status_code == 302
    company.refresh_from_db()
    assert company.manager_id == employee.pk
    employee.is_active = False
    employee.save()
    assert (
        verified.post(
            "/api/companies/",
            {"name": "Другая", "manager": employee.pk},
            content_type="application/json",
        ).status_code
        == 400
    )


def test_chain_multiple_companies_ui_and_api(verified, company, user):
    other = Company.objects.create(name="Второе ЮЛ", manager=user)
    response = verified.post(
        "/api/chains/",
        {"name": "ТС", "companies": [company.pk, other.pk]},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    chain = Chain.objects.get(pk=response.json()["id"])
    assert set(chain.companies.all()) == {company, other}
    page = verified.get(f"/chains/{chain.pk}/").content.decode()
    assert company.name in page and other.name in page
    assert (
        verified.post(
            f"/chains/{chain.pk}/edit/",
            {
                "record_version": chain.version,
                "name": "ТС изменённая",
                "companies": [company.pk],
            },
        ).status_code
        == 302
    )
    assert list(chain.companies.all()) == [company]
    assert Company.objects.filter(pk=other.pk).exists()


def test_store_company_options_and_validation(verified, company, user):
    chain = Chain.objects.create(name="Сеть")
    chain.companies.add(company)
    other = Company.objects.create(name="Не в сети", manager=user)
    response = verified.get("/stores/options/", {"chain": chain.pk})
    assert response.json()["companies"] == [{"id": company.pk, "name": company.name}]
    base = {"name": "Магазин", "chain": chain.pk, "code": "1", "address": "Москва"}
    assert (
        verified.post(
            "/api/stores/",
            {**base, "company": other.pk},
            content_type="application/json",
        ).status_code
        == 400
    )
    assert (
        verified.post("/stores/new/", {**base, "company": other.pk}).status_code == 200
    )
    assert (
        verified.post(
            "/api/stores/",
            {**base, "company": company.pk},
            content_type="application/json",
        ).status_code
        == 201
    )
    assert (
        verified.post(
            "/api/stores/",
            {**base, "code": "2", "company": None, "format": None},
            content_type="application/json",
        ).status_code
        == 201
    )


def test_cannot_unlink_company_used_by_store(verified, company):
    chain = Chain.objects.create(name="Сеть")
    chain.companies.add(company)
    Store.objects.create(
        name="Магазин", chain=chain, company=company, code="1", address="Адрес"
    )
    assert (
        verified.patch(
            f"/api/chains/{chain.pk}/",
            {
                "version": verified.get(f"/api/chains/{chain.pk}/")
                .json()
                .get("version", 1),
                "companies": [],
            },
            content_type="application/json",
        ).status_code
        == 400
    )
    assert (
        verified.post(
            f"/chains/{chain.pk}/edit/", {"name": chain.name, "companies": []}
        ).status_code
        == 200
    )
    assert chain.companies.filter(pk=company.pk).exists()


def test_store_form_selection_and_optional_fields(user, company):
    chain = Chain.objects.create(name="Сеть")
    chain.companies.add(company)
    other = Chain.objects.create(name="Другая")
    fmt = StoreFormat.objects.create(name="Формат", chain=chain)
    StoreFormat.objects.create(name="Другой формат", chain=other)
    region = Region.objects.create(name="Москва")
    form = form_class(Store, user)(
        data={
            "name": "Магазин",
            "chain": chain.pk,
            "code": "1",
            "address": "Адрес",
            "region": region.pk,
        }
    )
    assert form.is_valid(), form.errors
    assert list(form.fields["company"].queryset) == [company]
    assert list(form.fields["format"].queryset) == [fmt]
    obj = form.save()
    assert obj.company_id is None and obj.format_id is None


def test_quick_format_permissions_and_creation(verified, user):
    chain = Chain.objects.create(name="Сеть")
    assert (
        verified.post(
            "/stores/format/new/", {"name": "", "chain": chain.pk}
        ).status_code
        == 400
    )
    response = verified.post(
        "/stores/format/new/", {"name": "Супермаркет", "chain": chain.pk}
    )
    assert response.status_code == 201
    fmt = StoreFormat.objects.get(pk=response.json()["id"])
    assert fmt.chain_id == chain.pk
    assert AuditEvent.objects.filter(action="create", model="crm.StoreFormat").exists()
    assert verified.get("/stores/options/", {"chain": chain.pk}).json()["formats"] == [
        {"id": fmt.pk, "name": fmt.name}
    ]
    user.groups.set([Group.objects.get(name="Инженер")])
    assert (
        verified.post(
            "/stores/format/new/", {"name": "Denied", "chain": chain.pk}
        ).status_code
        == 403
    )
    assert verified.get("/stores/options/", {"chain": chain.pk}).status_code == 403


@pytest.mark.parametrize("name", [" МОСКВА ", "москва", "Мо ск ва", "\tМоСкВа\n"])
def test_regions_unicode_case_and_whitespace_uniqueness(verified, name):
    Region.objects.create(name="Москва")
    response = verified.post(
        "/api/regions/", {"name": name}, content_type="application/json"
    )
    assert response.status_code == 400, response.content
    assert verified.post("/regions/new/", {"name": name}).status_code == 200
    assert Region.objects.count() == 1


def test_region_database_unique_and_global_usage(verified):
    region = Region.objects.create(name="Татарстан")
    with transaction.atomic():
        with pytest.raises(IntegrityError):
            Region.objects.create(name=" ТАТАР СТАН ")
    for index in range(2):
        chain = Chain.objects.create(name=f"Сеть {index}")
        assert (
            verified.post(
                "/api/stores/",
                {
                    "name": "Магазин",
                    "chain": chain.pk,
                    "region": region.pk,
                    "code": "1",
                    "address": "Казань",
                },
                content_type="application/json",
            ).status_code
            == 201
        )


def test_region_aliases_hidden(verified):
    region = Region.objects.create(name="Москва")
    alias = Region.objects.create(
        name="МОСКВА", canonical=region, legacy_data={"chain_id": 1}
    )
    response = verified.get("/api/regions/").json()
    assert response["count"] == 1
    assert response["results"][0] == {
        "id": region.pk,
        "name": region.name,
        "version": 1,
        "archived": False,
    }
    assert verified.get(f"/regions/{alias.pk}/edit/").status_code == 404
    assert verified.delete(f"/api/regions/{alias.pk}/").status_code == 404
    assert verified.post(f"/regions/{alias.pk}/delete/").status_code == 404


def test_dashboard_cards_links(verified):
    page = verified.get("/").content.decode()
    for kind in ("companies", "stores", "projects", "tasks"):
        assert f'class="card metric-link" href="/{kind}/"' in page

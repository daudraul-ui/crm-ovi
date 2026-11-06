import pytest
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django_otp.plugins.otp_totp.models import TOTPDevice

from crm.models import Company


@pytest.fixture
def user():
    call_command("bootstrap", verbosity=0)
    user = User.objects.create_user("director", password="valid-password-123")
    user.groups.add(Group.objects.get(name="Директор"))
    return user


@pytest.fixture
def verified(client, user):
    client.force_login(user)
    device = TOTPDevice.objects.create(user=user, confirmed=True)
    session = client.session
    session["otp_device_id"] = device.persistent_id
    session.save()
    return client


@pytest.fixture
def company(user):
    return Company.objects.create(
        name="Клиент", inn="7701234567", manager=user, financial_notes="Секрет"
    )

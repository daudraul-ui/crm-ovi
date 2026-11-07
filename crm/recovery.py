import secrets

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from .models import AuditEvent, RecoveryCode


def revoke_sessions(user):
    for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
        if str(session.get_decoded().get("_auth_user_id")) == str(user.pk):
            session.delete()


def generate_codes(user):
    RecoveryCode.objects.filter(user=user).delete()
    codes = [secrets.token_hex(10) for _ in range(10)]
    RecoveryCode.objects.bulk_create(
        [RecoveryCode(user=user, digest=make_password(code)) for code in codes]
    )
    return codes


@transaction.atomic
def recover(request, token):
    # Lock the user, not only one code: concurrent recovery cannot consume two codes.
    from django.contrib.auth import get_user_model

    get_user_model().objects.select_for_update().get(pk=request.user.pk)
    for code in RecoveryCode.objects.filter(user=request.user, used_at__isnull=True):
        if check_password(token, code.digest):
            code.used_at = timezone.now()
            code.save(update_fields=["used_at"])
            revoke_sessions(request.user)
            TOTPDevice.objects.filter(user=request.user).delete()
            RecoveryCode.objects.filter(
                user=request.user, used_at__isnull=True
            ).delete()
            AuditEvent.objects.create(
                actor=request.user,
                action="2fa_recovery",
                model="auth.User",
                object_id=str(request.user.pk),
            )
            request.session.cycle_key()
            request.session.pop("otp_device_id", None)
            return True
    return False

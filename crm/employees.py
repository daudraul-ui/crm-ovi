from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django_otp.plugins.otp_totp.models import TOTPDevice

from .models import AuditEvent, Company, Project, RecoveryCode, Task
from .recovery import generate_codes, revoke_sessions


def employee_state(user):
    return {
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "is_active": user.is_active,
        "roles": sorted(user.groups.values_list("pk", flat=True)),
    }


class EmployeeEditForm(forms.ModelForm):
    roles = forms.ModelMultipleChoiceField(
        queryset=Group.objects.none(), label="Роли", widget=forms.CheckboxSelectMultiple
    )
    revision = forms.CharField(widget=forms.HiddenInput)
    acknowledge_tasks = forms.BooleanField(
        required=False,
        label="Открытые задачи проверены; подтверждаю блокировку без автоматического переназначения",
    )

    class Meta:
        model = get_user_model()
        fields = ["first_name", "last_name", "username", "email", "is_active"]

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        names = ["Директор", "РОП", "Менеджер", "Инженер", "Администратор"]
        if not actor.has_perm("crm.view_finance"):
            names.remove("Директор")
        self.fields["roles"].queryset = Group.objects.filter(name__in=names)
        self.fields["roles"].initial = self.instance.groups.all()
        self.initial["revision"] = signing.dumps(employee_state(self.instance))
        self.original = employee_state(self.instance)

    def clean(self):
        data = super().clean()
        try:
            if signing.loads(data.get("revision", "")) != self.original:
                raise signing.BadSignature()
        except signing.BadSignature:
            raise forms.ValidationError("Сотрудник изменён. Обновите страницу.")
        if (
            not data.get("is_active")
            and Task.objects.filter(assignee=self.instance)
            .exclude(status="done")
            .exists()
            and not data.get("acknowledge_tasks")
        ):
            raise forms.ValidationError(
                "У сотрудника есть открытые задачи. Проверьте список и подтвердите блокировку."
            )
        return data


@transaction.atomic
def employee_edit(request, pk):
    if not request.user.has_perm("crm.manage_employees"):
        raise PermissionDenied
    # Serialize changes to the administrator set to protect the last active operator.
    users = list(get_user_model().objects.select_for_update().order_by("pk"))
    employee = next((u for u in users if u.pk == pk), None)
    if employee is None:
        from django.http import Http404

        raise Http404
    if not request.user.has_perm("crm.view_finance") and (
        employee.has_perm("crm.view_finance") or employee.is_superuser
    ):
        raise PermissionDenied
    form = EmployeeEditForm(request.POST or None, instance=employee, actor=request.user)
    if request.method == "POST" and form.is_valid():
        roles = form.cleaned_data["roles"]
        remains_admin = form.cleaned_data["is_active"] and (
            employee.is_superuser
            or roles.filter(permissions__codename="manage_employees").exists()
        )
        others = [
            u
            for u in users
            if u.pk != employee.pk
            and u.is_active
            and u.has_perm("crm.manage_employees")
        ]
        if not remains_admin and not others:
            form.add_error(
                None, "Нельзя отключить последнего действующего администратора."
            )
        else:
            before = form.original
            employee = form.save()
            employee.groups.set(roles)
            revoke_sessions(employee)
            AuditEvent.objects.create(
                actor=request.user,
                action="employee_updated",
                model="auth.User",
                object_id=str(pk),
                before={k: v for k, v in before.items() if k != "email"},
                after={
                    k: v for k, v in employee_state(employee).items() if k != "email"
                },
            )
            return redirect("settings")
    return render(
        request,
        "crm/employee.html",
        {
            "form": form,
            "employee": employee,
            "companies": Company.objects.filter(manager=employee),
            "projects": Project.objects.filter(manager=employee),
            "tasks": Task.objects.filter(assignee=employee).exclude(status="done"),
        },
    )


@transaction.atomic
def reset_totp(request, pk):
    if not request.user.has_perm("crm.manage_employees"):
        raise PermissionDenied
    employee = get_object_or_404(get_user_model().objects.select_for_update(), pk=pk)
    if not request.user.has_perm("crm.view_finance") and (
        employee.has_perm("crm.view_finance") or employee.is_superuser
    ):
        raise PermissionDenied
    error = None
    if request.method == "POST":
        device = (
            TOTPDevice.objects.select_for_update()
            .filter(user=request.user, confirmed=True)
            .first()
        )
        if (
            not request.POST.get("reason", "").strip()
            or not device
            or not device.verify_token(request.POST.get("token", ""))
        ):
            error = (
                "Укажите причину и действующий код вашего приложения-аутентификатора."
            )
        else:
            revoke_sessions(employee)
            TOTPDevice.objects.filter(user=employee).delete()
            RecoveryCode.objects.filter(user=employee).delete()
            AuditEvent.objects.create(
                actor=request.user,
                action="2fa_admin_reset",
                model="auth.User",
                object_id=str(pk),
                after={"reason": request.POST["reason"][:500]},
            )
            return redirect("settings")
    return render(
        request, "crm/reset_totp.html", {"employee": employee, "error": error}
    )


@transaction.atomic
def recovery_codes(request):
    error = None
    if request.method == "POST":
        device = (
            TOTPDevice.objects.select_for_update()
            .filter(user=request.user, confirmed=True)
            .first()
        )
        if device and device.verify_token(request.POST.get("token", "")):
            codes = generate_codes(request.user)
            AuditEvent.objects.create(
                actor=request.user,
                action="recovery_codes_rotated",
                model="auth.User",
                object_id=str(request.user.pk),
            )
            response = render(
                request, "registration/recovery_codes.html", {"codes": codes}
            )
            response["Cache-Control"] = "no-store"
            return response
        error = "Неверный код или слишком много попыток."
    return render(request, "crm/reset_totp.html", {"error": error, "rotate": True})

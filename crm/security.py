from django.http import JsonResponse
from django.shortcuts import redirect
from rest_framework.permissions import BasePermission


class AccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        public = request.path in (
            "/login/",
            "/logout/",
            "/health/",
        ) or request.path.startswith("/static/")
        if not public:
            if not request.user.is_authenticated:
                if request.path.startswith("/api/"):
                    return JsonResponse({"detail": "Требуется вход"}, status=403)
                return redirect("login")
            if not request.user.is_verified() and request.path != "/2fa/":
                if request.path.startswith("/api/"):
                    return JsonResponse({"detail": "Требуется 2FA"}, status=403)
                return redirect("two_factor")
        return self.get_response(request)


class VerifiedPermission(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.is_verified()


def allowed(user, model, action="view"):
    return user.has_perm(f"crm.{action}_{model._meta.model_name}")

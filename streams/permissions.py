from django.contrib.auth.models import Group
from rest_framework.permissions import BasePermission


ROLE_ADMIN = "Admin"
ROLE_OPERATOR = "Operator"


def ensure_default_roles() -> None:
    Group.objects.get_or_create(name=ROLE_ADMIN)
    Group.objects.get_or_create(name=ROLE_OPERATOR)


def is_admin_or_operator(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return user.groups.filter(name__in=(ROLE_ADMIN, ROLE_OPERATOR)).exists()


class IsAdminOrOperator(BasePermission):
    def has_permission(self, request, view):
        return is_admin_or_operator(request.user)

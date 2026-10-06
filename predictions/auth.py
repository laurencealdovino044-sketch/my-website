from django.contrib.auth.backends import ModelBackend


class SingleUserBackend(ModelBackend):
    """Apply the single-account restriction to passwords and existing sessions."""

    def user_can_authenticate(self, user):
        return user.username == "user" and super().user_can_authenticate(user)

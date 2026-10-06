from django.conf import settings
from django.db import migrations


def configure_single_user(apps, schema_editor):
    User = apps.get_model(settings.AUTH_USER_MODEL)
    users = User.objects.using(schema_editor.connection.alias)
    users.exclude(username="user").update(is_active=False)
    users.update_or_create(username="user", defaults={
        "password": 'pbkdf2_sha256$1000000$0Ty1XKOsgtGvOGyd05gtgG$3Z4K0vZNnkO+MiNk04uoCIPJamiB02XTMXiSFxRI01w=',
        "is_active": True,
        "is_staff": True,
        "is_superuser": True,
    })


class Migration(migrations.Migration):
    dependencies = [
        ("predictions", "0007_googleidentity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.RunPython(configure_single_user, migrations.RunPython.noop)]

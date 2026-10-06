from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .models import Stream
from .permissions import ROLE_OPERATOR


class StreamModelTests(TestCase):
    def test_stream_key_is_generated_and_matches_pattern(self):
        stream = Stream.objects.create(stream_name="Test Stream")
        self.assertRegex(stream.stream_key, r"^dji_[A-Za-z0-9]{32}$")

    def test_regenerate_stream_key_changes_key(self):
        stream = Stream.objects.create(stream_name="Test Stream")
        old_key = stream.stream_key
        stream.regenerate_stream_key()
        self.assertNotEqual(old_key, stream.stream_key)


class StreamAPITests(TestCase):
    def setUp(self):
        role, _ = Group.objects.get_or_create(name=ROLE_OPERATOR)
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="operator1", password="OperatorPass123!")
        self.user.groups.add(role)

        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch("streams.api.sync_stream_states")
    def test_live_endpoint_returns_live_streams(self, sync_stream_states_mock):
        Stream.objects.create(stream_name="Live Stream", is_live=True, is_active=True)
        url = reverse("live-streams")

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        sync_stream_states_mock.assert_called_once()

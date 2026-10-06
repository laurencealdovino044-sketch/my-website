from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Stream, StreamLog, extract_stream_key
from .permissions import IsAdminOrOperator
from .serializers import StreamSerializer
from .services import sync_stream_states


class StreamViewSet(viewsets.ModelViewSet):
    queryset = Stream.objects.select_related("drone", "mission").all()
    serializer_class = StreamSerializer
    permission_classes = (IsAuthenticated, IsAdminOrOperator)

    def perform_create(self, serializer):
        stream = serializer.save()
        StreamLog.objects.create(stream=stream, event=StreamLog.Event.CREATED)

    def perform_update(self, serializer):
        old_instance = self.get_object()
        was_active = old_instance.is_active

        stream = serializer.save()
        StreamLog.objects.create(stream=stream, event=StreamLog.Event.UPDATED)

        if was_active != stream.is_active:
            StreamLog.objects.create(
                stream=stream,
                event=StreamLog.Event.ENABLED if stream.is_active else StreamLog.Event.DISABLED,
            )

    @action(detail=True, methods=["post"], url_path="regenerate")
    def regenerate(self, request, pk=None):
        stream = self.get_object()
        stream.regenerate_stream_key()
        StreamLog.objects.create(stream=stream, event=StreamLog.Event.KEY_REGENERATED)
        serializer = self.get_serializer(stream)
        return Response(serializer.data, status=status.HTTP_200_OK)


class LiveStreamListAPIView(APIView):
    permission_classes = (IsAuthenticated, IsAdminOrOperator)

    def get(self, request):
        sync_stream_states()
        queryset = Stream.objects.select_related("drone", "mission").filter(is_live=True)
        serializer = StreamSerializer(queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class MediaMTXAuthAPIView(APIView):
    authentication_classes = ()
    permission_classes = ()

    def post(self, request):
        action_name = str(request.data.get("action", "")).strip()
        path_name = str(request.data.get("path", "")).strip()
        stream_key = extract_stream_key(path_name)

        if action_name != "publish":
            return Response({"detail": "Accepted"}, status=status.HTTP_200_OK)

        if not stream_key:
            return Response({"detail": "Missing stream key."}, status=status.HTTP_403_FORBIDDEN)

        is_valid = Stream.objects.filter(stream_key=stream_key, is_active=True).exists()
        if not is_valid:
            return Response({"detail": "Invalid or disabled stream key."}, status=status.HTTP_403_FORBIDDEN)

        return Response({"detail": "Accepted"}, status=status.HTTP_200_OK)

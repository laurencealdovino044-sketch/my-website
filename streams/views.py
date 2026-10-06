from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.views import LoginView, LogoutView
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView, View

from .forms import StreamForm
from .models import Stream, StreamLog
from .permissions import ensure_default_roles, is_admin_or_operator
from .services import sync_stream_states


class OperatorAccessMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return is_admin_or_operator(self.request.user)


class StreamLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def dispatch(self, request, *args, **kwargs):
        ensure_default_roles()
        return super().dispatch(request, *args, **kwargs)


class StreamLogoutView(LogoutView):
    next_page = reverse_lazy("streams:login")


class DashboardView(OperatorAccessMixin, TemplateView):
    template_name = "streams/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            sync_stream_states()
        except RuntimeError as exc:
            messages.error(self.request, f"Live status check failed: {exc}")

        streams = Stream.objects.select_related("drone", "mission")
        total_streams = streams.count()
        active_streams = streams.filter(is_live=True).count()
        offline_streams = streams.filter(is_live=False).count()
        connected_drones = streams.filter(is_live=True, drone__isnull=False).values("drone_id").distinct().count()
        recent_activity = StreamLog.objects.select_related("stream")[:8]

        context.update(
            {
                "total_streams": total_streams,
                "active_streams": active_streams,
                "offline_streams": offline_streams,
                "connected_drones": connected_drones,
                "recent_activity": recent_activity,
                "live_streams": streams.filter(is_live=True)[:6],
            }
        )
        return context


class StreamListView(OperatorAccessMixin, ListView):
    template_name = "streams/stream_list.html"
    context_object_name = "streams"
    model = Stream

    def get_queryset(self):
        try:
            sync_stream_states()
        except RuntimeError as exc:
            messages.error(self.request, f"Live status check failed: {exc}")
        return Stream.objects.select_related("drone", "mission").all()


class StreamCreateView(OperatorAccessMixin, CreateView):
    template_name = "streams/stream_form.html"
    model = Stream
    form_class = StreamForm
    success_url = reverse_lazy("streams:stream-list")

    def form_valid(self, form):
        response = super().form_valid(form)
        StreamLog.objects.create(stream=self.object, event=StreamLog.Event.CREATED)
        messages.success(self.request, "Stream created with a secure DJI stream key.")
        return response


class StreamUpdateView(OperatorAccessMixin, UpdateView):
    template_name = "streams/stream_form.html"
    model = Stream
    form_class = StreamForm
    success_url = reverse_lazy("streams:stream-list")

    def form_valid(self, form):
        old_active_state = self.get_object().is_active
        response = super().form_valid(form)
        StreamLog.objects.create(stream=self.object, event=StreamLog.Event.UPDATED)
        if old_active_state != self.object.is_active:
            StreamLog.objects.create(
                stream=self.object,
                event=StreamLog.Event.ENABLED if self.object.is_active else StreamLog.Event.DISABLED,
            )
        messages.success(self.request, "Stream updated.")
        return response


class StreamDetailView(OperatorAccessMixin, DetailView):
    template_name = "streams/stream_detail.html"
    model = Stream
    context_object_name = "stream"


class StreamViewerView(OperatorAccessMixin, DetailView):
    template_name = "streams/stream_viewer.html"
    model = Stream
    context_object_name = "stream"


class StreamDeleteView(OperatorAccessMixin, DeleteView):
    template_name = "streams/stream_confirm_delete.html"
    model = Stream
    success_url = reverse_lazy("streams:stream-list")

    def form_valid(self, form):
        messages.success(self.request, "Stream deleted.")
        return super().form_valid(form)


class StreamRegenerateKeyView(OperatorAccessMixin, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        stream = get_object_or_404(Stream, pk=pk)
        stream.regenerate_stream_key()
        StreamLog.objects.create(stream=stream, event=StreamLog.Event.KEY_REGENERATED)
        messages.success(request, "Stream key regenerated.")
        return redirect("streams:stream-detail", pk=stream.pk)


class StreamToggleActiveView(OperatorAccessMixin, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        stream = get_object_or_404(Stream, pk=pk)
        stream.is_active = not stream.is_active
        stream.save(update_fields=["is_active", "updated_at"])
        StreamLog.objects.create(
            stream=stream,
            event=StreamLog.Event.ENABLED if stream.is_active else StreamLog.Event.DISABLED,
        )
        messages.success(request, "Stream status updated.")
        return redirect("streams:stream-detail", pk=stream.pk)

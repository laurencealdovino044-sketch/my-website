from django.urls import path
from django.conf.urls.static import static
from django.conf import settings
from . import views

urlpatterns = [
    path('', views.landing_page, name='landing_page'),
    path('home/', views.home, name='home'),
    path('signup/', views.signup, name='signup'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('predict/', views.predict, name='predict'),
    path('about/', views.about, name='about'),
    path('profile/', views.profile, name='profile'),
     path('edit_profile/', views.edit_profile, name='edit_profile'),
     path('result/', views.result, name='result'),
]+ static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

from django.contrib import admin
from django.urls import path, include
from hide_admin import admin_site  # or similar
from django.conf import settings
from django.conf.urls.static import static
from store import views  # Ensure views is imported from store
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)

urlpatterns = [
    path('', include('store.urls')),
    # Google / Apple sign-in that is not set up: back to login instead of a 500 (listed before allauth)
    path('accounts/google/login/', views.social_login_guard, {'provider': 'google'}),
    path('accounts/google/login/token/', views.social_login_guard, {'provider': 'google-token'}),
    path('accounts/apple/login/', views.social_login_guard, {'provider': 'apple'}),
    path('accounts/', include('allauth.urls')),
    path('connect-broker/', views.connect_broker, name='connect_broker'), # <-- ADD THIS LINE
    path('deriv-callback/', views.deriv_callback, name='deriv_callback'),
    path('admin/hub/', views.admin_dashboard_hub_view, name='admin_dashboard_hub'),
    # Simple JWT API Endpoints
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/token/verify/', TokenVerifyView.as_view(), name='token_verify'),
    # the hidden admin's own login page crashes (hide_admin + current Django); staff-only pages send people here
    path('admin/login/', views.hidden_admin_login, name='hidden_admin_login'),
    path('admin/', admin.site.urls),

]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
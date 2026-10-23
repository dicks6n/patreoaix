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
    path('accounts/', include('allauth.urls')),
    path('connect-broker/', views.connect_broker, name='connect_broker'), # <-- ADD THIS LINE
    path('deriv-callback/', views.deriv_callback, name='deriv_callback'),
    path('admin/hub/', views.admin_dashboard_hub_view, name='admin_dashboard_hub'),
    # Simple JWT API Endpoints
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/token/verify/', TokenVerifyView.as_view(), name='token_verify'),
    path('admin/', admin.site.urls),

]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
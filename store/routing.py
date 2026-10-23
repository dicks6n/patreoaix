# store/routing.py
from django.urls import path, re_path
from . import consumers

websocket_urlpatterns = [
    path('ws/trading/', consumers.TradingConsumer.as_asgi()),
    re_path(r'^ws/live-chart/$', consumers.LiveChartConsumer.as_asgi()),
    re_path(r'^ws/trade-events/$', consumers.TradeEventConsumer.as_asgi()),
]# store/routing.py
from django.urls import path, re_path
from . import consumers

websocket_urlpatterns = [
    path('ws/trading/', consumers.TradingConsumer.as_asgi()),
    re_path(r'^ws/live-chart/$', consumers.LiveChartConsumer.as_asgi()),
    re_path(r'^ws/trade-events/$', consumers.TradeEventConsumer.as_asgi()),
]
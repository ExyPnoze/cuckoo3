# Copyright (C) 2019-2024 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

from django.urls import path
from .views import LiveSessionView

urlpatterns = [
    path("", LiveSessionView.as_view()),
]

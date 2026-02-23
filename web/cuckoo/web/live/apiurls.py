# Copyright (C) 2019-2024 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

from django.urls import path, register_converter

from cuckoo.web import converters
from .views import LiveSessionApiView

register_converter(converters.AnalysisId, "analysis_id")
register_converter(converters.TaskId, "task_id")

urlpatterns = [
    path(
        "<analysis_id:analysis_id>/task/<task_id:task_id>/live",
        LiveSessionApiView.as_view(),
    ),
]

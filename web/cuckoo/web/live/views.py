# Copyright (C) 2019-2024 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

from django.views.generic import TemplateView


class TaskLivePageView(TemplateView):
    template_name = "analysis/task_live.html.jinja2"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["analysis_id"] = kwargs.get("analysis_id", "")
        ctx["task_id"] = kwargs.get("task_id", "")
        return ctx

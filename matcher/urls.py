from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("upload/", views.upload, name="upload"),
    path("resume/<uuid:pk>/", views.detail, name="detail"),
    path("resume/<uuid:pk>/search/", views.search, name="search"),
    path("resume/<uuid:pk>/status/", views.status, name="status"),
    path("resume/<uuid:pk>/jobs/", views.results, name="results"),
    path("resume/<uuid:pk>/tailor/<str:job_key>/", views.tailor, name="tailor"),
    path("resume/<uuid:pk>/latex/", views.latex_new, name="latex_new"),
    path("resume/<uuid:pk>/editor/<int:gid>/", views.editor, name="editor"),
    path("resume/<uuid:pk>/editor/<int:gid>/compile/", views.editor_compile, name="editor_compile"),
    path("resume/<uuid:pk>/editor/<int:gid>/tex/", views.editor_tex, name="editor_tex"),
    path("resume/<uuid:pk>/ping/", views.ping, name="ping"),
    path("resume/<uuid:pk>/leave/", views.leave, name="leave"),
    path("resume/<uuid:pk>/delete/", views.delete, name="delete"),
]

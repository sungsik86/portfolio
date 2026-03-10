from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("projects/flyio-django-deploy/", views.flyio_deploy, name="flyio_deploy"),
    path("projects/trans-converter/", views.trans_converter, name="trans_converter"),
]

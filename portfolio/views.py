from django.shortcuts import render


def home(request):
    return render(request, "portfolio/index.html")


def flyio_deploy(request):
    return render(request, "portfolio/flyio_deploy.html")


def trans_converter(request):
    return render(request, "portfolio/trans_converter.html")

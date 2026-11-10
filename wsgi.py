"""WSGI entry point for hosting (e.g. PythonAnywhere - see DEPLOY.md)."""
from app import app, bootstrap

bootstrap()

application = app

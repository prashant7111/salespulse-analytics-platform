"""WSGI entry point for Gunicorn and hosted deployments."""
from server import app

__all__ = ["app"]

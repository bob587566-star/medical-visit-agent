"""Legacy fictional-data tests explicitly use a consent-free local fixture.

Production create_app defaults to consent_required=True. Account/privacy
security tests use that real default and never import this helper.
"""
from backend.main import create_app as production_app


def create_demo_app(*args, **kwargs):
    return production_app(*args, **kwargs, consent_required=False)

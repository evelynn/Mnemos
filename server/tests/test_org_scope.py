"""Org ACL helper logic (pure — no FastAPI machinery)."""

import uuid
from types import SimpleNamespace

from app.auth.org_scope import same_org


def _user(org_id):
    return SimpleNamespace(organization_id=org_id)


def test_same_org_true_when_ids_match():
    org = uuid.uuid4()
    assert same_org(_user(org), org)


def test_same_org_false_when_ids_differ():
    assert not same_org(_user(uuid.uuid4()), uuid.uuid4())


def test_same_org_rejects_when_user_has_no_org():
    # Org-less users are not platform-wide principals.
    assert not same_org(_user(None), uuid.uuid4())


def test_same_org_rejects_when_project_has_no_org():
    # Legacy/orphan projects must be assigned before becoming readable.
    assert not same_org(_user(uuid.uuid4()), None)

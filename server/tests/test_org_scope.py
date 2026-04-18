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


def test_same_org_permissive_when_user_has_no_org():
    # Pre-migration users (``organization_id`` NULL) still see their data.
    assert same_org(_user(None), uuid.uuid4())


def test_same_org_permissive_when_project_has_no_org():
    # Pre-migration projects are visible to any user.
    assert same_org(_user(uuid.uuid4()), None)

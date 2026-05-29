"""PR-88 — Secret org-scope, closes the cross-tenant ciphertext leak.

The platform review's CRITICAL #3 was that the ``secrets`` table had
no organisation column at all and ``GET /api/v1/secrets`` was an
``: CurrentUser`` route (not admin), so every authenticated user in
any org could list every tenant's ciphertext metadata (§2.8).

The fix:
* ``Secret.organization_id`` added (alembic 0021), nullable so the
  migration is online and legacy rows aren't lost;
* ``GET /secrets`` now requires admin AND filters by the caller's
  ``organization_id`` (legacy NULL rows stay visible to any admin
  until they're assigned);
* ``create_secret`` stamps the new row with the caller's org.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"
_MIG = (
    Path(__file__).resolve().parents[1]
    / "alembic" / "versions" / "0021_secret_organization_id.py"
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_secret_model_has_organization_id():
    body = _read(_APP / "models" / "auth.py")
    idx = body.find("class Secret(Base):")
    slab = body[idx:idx + 1600]
    assert "organization_id" in slab
    assert "UUID(as_uuid=True), nullable=True" in slab


def test_migration_present():
    body = _read(_MIG)
    assert "organization_id" in body
    assert 'op.add_column(' in body
    assert '"secrets"' in body
    assert "0020_finding_risk_columns" in body  # chains correctly


def test_list_secrets_admin_only_and_org_scoped():
    body = _read(_APP / "api" / "secrets.py")
    idx = body.find("async def list_secrets(")
    slab = body[idx:idx + 900]
    assert "Depends(require_admin)" in slab
    # Filtered by the caller's organization_id.
    assert "Secret.organization_id == user.organization_id" in slab


def test_create_secret_stamps_org():
    body = _read(_APP / "api" / "secrets.py")
    idx = body.find("async def create_secret(")
    slab = body[idx:idx + 900]
    assert "organization_id=user.organization_id" in slab

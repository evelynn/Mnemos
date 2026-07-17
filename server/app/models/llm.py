"""Durable, encrypted semantic candidates for physical LLM attempts.

This is deliberately a *candidate ledger*, not a generic model-output store.
Only a bounded, contract-normalized JSON object may be written, and its
plaintext never enters a database column.  Consumer-specific grounding still
decides whether the candidate becomes an accepted source-analysis product.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


MAX_SEMANTIC_CANDIDATE_BYTES = 1_048_576
MAX_SEMANTIC_CANDIDATE_CIPHERTEXT_BYTES = 1_500_000


class LLMSemanticCandidate(Base):
    """One encrypted transport-normalized candidate for one v1 attempt.

    ``payload_ciphertext`` is KMS/Fernet ciphertext over canonical UTF-8 JSON.
    The contract, hashes, identity binding, and bounded byte count are safe
    control metadata.  Prompts, source text, raw provider envelopes, and raw
    exceptions have no plaintext columns here.
    """

    __tablename__ = "llm_semantic_products"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_calls.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_name: Mapped[str] = mapped_column(String(96), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_ciphertext: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False
    )
    payload_iv: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    candidate_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="candidate"
    )
    failure_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            "attempt_no",
            name="uq_llm_semantic_products_operation_attempt",
        ),
        Index(
            "ix_llm_semantic_products_project_created",
            "project_id",
            "created_at",
        ),
        Index(
            "ix_llm_semantic_products_purpose_status",
            "purpose",
            "candidate_status",
        ),
        CheckConstraint(
            "attempt_no > 0",
            name="ck_llm_semantic_products_attempt_no",
        ),
        CheckConstraint(
            "purpose IN ('summary', 'agent_extract', 'flow', "
            "'chat_rewrite', 'chat_answer', 'second_opinion')",
            name="ck_llm_semantic_products_purpose",
        ),
        CheckConstraint(
            "length(contract_name) BETWEEN 3 AND 96",
            name="ck_llm_semantic_products_contract_name",
        ),
        CheckConstraint(
            "length(input_fingerprint) = 64 AND "
            "length(binding_fingerprint) = 64 AND "
            "length(payload_sha256) = 64",
            name="ck_llm_semantic_products_hashes",
        ),
        CheckConstraint(
            f"payload_bytes BETWEEN 2 AND {MAX_SEMANTIC_CANDIDATE_BYTES} "
            "AND length(payload_ciphertext) BETWEEN 1 AND "
            f"{MAX_SEMANTIC_CANDIDATE_CIPHERTEXT_BYTES} "
            "AND length(payload_iv) = 16",
            name="ck_llm_semantic_products_payload_bounds",
        ),
        CheckConstraint(
            "(candidate_status IN ('candidate', 'accepted') "
            "AND failure_code IS NULL) OR "
            "(candidate_status = 'rejected' AND failure_code IS NOT NULL "
            "AND length(failure_code) BETWEEN 1 AND 64)",
            name="ck_llm_semantic_products_status_shape",
        ),
    )


__all__ = [
    "LLMSemanticCandidate",
    "MAX_SEMANTIC_CANDIDATE_BYTES",
    "MAX_SEMANTIC_CANDIDATE_CIPHERTEXT_BYTES",
]

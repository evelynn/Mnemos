import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DataSample(Base):
    __tablename__ = "data_samples"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    data_entity_id: Mapped[str] = mapped_column(String, nullable=False)
    sample_rows: Mapped[list] = mapped_column(JSONB, nullable=False)
    row_count_estimate: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    column_stats: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    masking_applied: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    valid_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DataQueryLog(Base):
    __tablename__ = "data_query_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    db_component_id: Mapped[str] = mapped_column(String, nullable=False)
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    requester: Mapped[str] = mapped_column(String, nullable=False)
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    execution_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

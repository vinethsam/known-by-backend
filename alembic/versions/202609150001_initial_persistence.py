"""initial persistence schema

Revision ID: 202609150001
Revises:
Create Date: 2026-09-15 00:01:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "202609150001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("total_people", sa.Integer(), nullable=False),
        sa.Column("columns_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index(op.f("ix_jobs_status"), "jobs", ["status"], unique=False)

    op.create_table(
        "person_tasks",
        sa.Column("person_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("seed_json", sa.JSON(), nullable=False),
        sa.Column("original_row_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("leased_by", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("person_id"),
        sa.UniqueConstraint("job_id", "row_index", name="uq_person_tasks_job_row_index"),
    )
    op.create_index(
        "ix_person_tasks_claimable",
        "person_tasks",
        ["status", "lease_expires_at", "attempts", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_person_tasks_job_id"), "person_tasks", ["job_id"], unique=False)
    op.create_index(
        op.f("ix_person_tasks_lease_expires_at"), "person_tasks", ["lease_expires_at"], unique=False
    )
    op.create_index(op.f("ix_person_tasks_lease_token"), "person_tasks", ["lease_token"], unique=False)
    op.create_index(op.f("ix_person_tasks_leased_by"), "person_tasks", ["leased_by"], unique=False)
    op.create_index(op.f("ix_person_tasks_status"), "person_tasks", ["status"], unique=False)

    op.create_table(
        "sources",
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("person_id", sa.String(length=36), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["person_id"], ["person_tasks.person_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_id"),
    )
    op.create_index(op.f("ix_sources_job_id"), "sources", ["job_id"], unique=False)
    op.create_index(op.f("ix_sources_person_id"), "sources", ["person_id"], unique=False)

    op.create_table(
        "evidence_claims",
        sa.Column("claim_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("person_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("field", sa.String(length=80), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["person_id"], ["person_tasks.person_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.source_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("claim_id"),
    )
    op.create_index(op.f("ix_evidence_claims_field"), "evidence_claims", ["field"], unique=False)
    op.create_index(op.f("ix_evidence_claims_job_id"), "evidence_claims", ["job_id"], unique=False)
    op.create_index(op.f("ix_evidence_claims_person_id"), "evidence_claims", ["person_id"], unique=False)
    op.create_index(op.f("ix_evidence_claims_source_id"), "evidence_claims", ["source_id"], unique=False)

    op.create_table(
        "field_decisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("person_id", sa.String(length=36), nullable=False),
        sa.Column("field", sa.String(length=80), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["person_id"], ["person_tasks.person_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("person_id", "field", name="uq_field_decisions_person_field"),
    )
    op.create_index(op.f("ix_field_decisions_job_id"), "field_decisions", ["job_id"], unique=False)
    op.create_index(op.f("ix_field_decisions_person_id"), "field_decisions", ["person_id"], unique=False)

    op.create_table(
        "profiles",
        sa.Column("person_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("profile_confidence", sa.Float(), nullable=False),
        sa.Column("coverage", sa.Float(), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["person_id"], ["person_tasks.person_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("person_id"),
    )
    op.create_index(op.f("ix_profiles_job_id"), "profiles", ["job_id"], unique=False)
    op.create_index(op.f("ix_profiles_status"), "profiles", ["status"], unique=False)

    op.create_table(
        "usage_records",
        sa.Column("usage_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("person_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("role", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["person_id"], ["person_tasks.person_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.source_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("usage_id"),
    )
    op.create_index(op.f("ix_usage_records_job_id"), "usage_records", ["job_id"], unique=False)
    op.create_index(op.f("ix_usage_records_person_id"), "usage_records", ["person_id"], unique=False)
    op.create_index(op.f("ix_usage_records_source_id"), "usage_records", ["source_id"], unique=False)

    op.create_table(
        "retrieval_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "url", name="uq_retrieval_cache_job_url"),
    )

    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=200), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        op.f("ix_worker_heartbeats_last_seen_at"), "worker_heartbeats", ["last_seen_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_worker_heartbeats_last_seen_at"), table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
    op.drop_table("retrieval_cache")
    op.drop_index(op.f("ix_usage_records_source_id"), table_name="usage_records")
    op.drop_index(op.f("ix_usage_records_person_id"), table_name="usage_records")
    op.drop_index(op.f("ix_usage_records_job_id"), table_name="usage_records")
    op.drop_table("usage_records")
    op.drop_index(op.f("ix_profiles_status"), table_name="profiles")
    op.drop_index(op.f("ix_profiles_job_id"), table_name="profiles")
    op.drop_table("profiles")
    op.drop_index(op.f("ix_field_decisions_person_id"), table_name="field_decisions")
    op.drop_index(op.f("ix_field_decisions_job_id"), table_name="field_decisions")
    op.drop_table("field_decisions")
    op.drop_index(op.f("ix_evidence_claims_source_id"), table_name="evidence_claims")
    op.drop_index(op.f("ix_evidence_claims_person_id"), table_name="evidence_claims")
    op.drop_index(op.f("ix_evidence_claims_job_id"), table_name="evidence_claims")
    op.drop_index(op.f("ix_evidence_claims_field"), table_name="evidence_claims")
    op.drop_table("evidence_claims")
    op.drop_index(op.f("ix_sources_person_id"), table_name="sources")
    op.drop_index(op.f("ix_sources_job_id"), table_name="sources")
    op.drop_table("sources")
    op.drop_index(op.f("ix_person_tasks_status"), table_name="person_tasks")
    op.drop_index(op.f("ix_person_tasks_leased_by"), table_name="person_tasks")
    op.drop_index(op.f("ix_person_tasks_lease_token"), table_name="person_tasks")
    op.drop_index(op.f("ix_person_tasks_lease_expires_at"), table_name="person_tasks")
    op.drop_index(op.f("ix_person_tasks_job_id"), table_name="person_tasks")
    op.drop_index("ix_person_tasks_claimable", table_name="person_tasks")
    op.drop_table("person_tasks")
    op.drop_index(op.f("ix_jobs_status"), table_name="jobs")
    op.drop_table("jobs")

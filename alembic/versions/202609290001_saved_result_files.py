"""Explicit shared export library; no canonical person or evidence schema.

Revision ID: 202609290001
Revises: 202609150001
"""

import sqlalchemy as sa

from alembic import op

revision = "202609290001"
down_revision = "202609150001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_result_files",
        sa.Column("file_id", sa.String(36), nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("format", sa.String(4), nullable=False),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("research_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("list_name", sa.Text(), nullable=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("format IN ('csv', 'xlsx')", name="ck_saved_result_files_format"),
        sa.CheckConstraint("size_bytes > 0", name="ck_saved_result_files_size"),
        sa.PrimaryKeyConstraint("file_id"),
    )
    op.create_index("ix_saved_result_files_saved_at", "saved_result_files", ["saved_at", "file_id"])


def downgrade() -> None:
    op.drop_index("ix_saved_result_files_saved_at", table_name="saved_result_files")
    op.drop_table("saved_result_files")

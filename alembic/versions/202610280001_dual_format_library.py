"""Retain both export formats in one saved entry; preserve existing bytes.

Revision ID: 202610280001
Revises: 202609290001
"""

import sqlalchemy as sa

from alembic import op

revision = "202610280001"
down_revision = "202609290001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("saved_result_files", sa.Column("csv_content", sa.LargeBinary(), nullable=True))
    op.add_column("saved_result_files", sa.Column("csv_size_bytes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.execute(
        "UPDATE saved_result_files SET size_bytes = size_bytes - csv_size_bytes "
        "WHERE csv_size_bytes IS NOT NULL"
    )
    op.drop_column("saved_result_files", "csv_size_bytes")
    op.drop_column("saved_result_files", "csv_content")

"""remove delta_seconds column (now computed)

Revision ID: 0003_remove_delta_seconds
Revises: 0002_add_block_hash
Create Date: 2026-04-09

"""
from alembic import op
import sqlalchemy as sa

revision = '0003_remove_delta_seconds'
down_revision = '0002_add_block_hash'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('calendar_attestations', 'delta_seconds')


def downgrade():
    op.add_column(
        'calendar_attestations',
        sa.Column('delta_seconds', sa.Float(), nullable=True),
    )

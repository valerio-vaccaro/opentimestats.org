"""add block_hash to calendar_attestations

Revision ID: 0002_add_block_hash
Revises: 0001_initial_schema
Create Date: 2026-04-09

"""
from alembic import op
import sqlalchemy as sa

revision = '0002_add_block_hash'
down_revision = '0001_initial_schema'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'calendar_attestations',
        sa.Column('block_hash', sa.String(length=64), nullable=True),
    )


def downgrade():
    op.drop_column('calendar_attestations', 'block_hash')

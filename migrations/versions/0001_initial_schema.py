"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-04-07

"""
from alembic import op
import sqlalchemy as sa

revision = '0001_initial_schema'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'timestamp_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('filename'),
    )
    op.create_table(
        'calendar_attestations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.Integer(), nullable=False),
        sa.Column('calendar_url', sa.String(length=500), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(), nullable=True),
        sa.Column('block_height', sa.Integer(), nullable=True),
        sa.Column('delta_seconds', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['request_id'], ['timestamp_requests.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('request_id', 'calendar_url', name='uq_request_calendar'),
    )


def downgrade():
    op.drop_table('calendar_attestations')
    op.drop_table('timestamp_requests')

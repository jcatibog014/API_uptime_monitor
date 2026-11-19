"""Hourly and daily rollups (stage 4)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19 09:03:42.700955
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():

    op.create_table('rollups',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('monitor_id', sa.Integer(), nullable=False),
    sa.Column('period', sa.String(length=4), nullable=False),
    sa.Column('start', sa.DateTime(), nullable=False),
    sa.Column('checks', sa.Integer(), server_default='0', nullable=False),
    sa.Column('ok', sa.Integer(), server_default='0', nullable=False),
    sa.Column('slow', sa.Integer(), server_default='0', nullable=False),
    sa.Column('our_side', sa.Integer(), server_default='0', nullable=False),
    sa.Column('missed', sa.Integer(), server_default='0', nullable=False),
    sa.Column('sum_ms', sa.Float(), server_default='0', nullable=False),
    sa.Column('min_ms', sa.Float(), nullable=True),
    sa.Column('max_ms', sa.Float(), nullable=True),
    sa.Column('b0', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b1', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b2', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b3', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b4', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b5', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b6', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b7', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b8', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b9', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b10', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b11', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b12', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b13', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b14', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b15', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b16', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b17', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b18', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b19', sa.Integer(), server_default='0', nullable=True),
    sa.Column('b20', sa.Integer(), server_default='0', nullable=True),
    sa.ForeignKeyConstraint(['monitor_id'], ['monitors.id'], name=op.f('fk_rollups_monitor_id_monitors'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rollups')),
    sa.UniqueConstraint('monitor_id', 'period', 'start', name=op.f('uq_rollups_monitor_id_period_start'))
    )



def downgrade():

    op.drop_table('rollups')


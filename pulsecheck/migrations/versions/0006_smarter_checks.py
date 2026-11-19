"""Smarter checks: rules, slow limit, sleepy sites, heartbeats, certificates (stage 6)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-19 09:33:15.642369
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():

    with op.batch_alter_table('monitors', schema=None) as batch_op:
        batch_op.add_column(sa.Column('assertions', sa.JSON(), server_default='{}', nullable=False))
        batch_op.add_column(sa.Column('slow_ms', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('sleepy', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('heartbeat_token', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('grace_s', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('last_ping_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('cert_expires_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('cert_issuer', sa.String(length=120), server_default='', nullable=False))
        batch_op.add_column(sa.Column('cert_warned_days', sa.Integer(), nullable=True))
        batch_op.create_unique_constraint(batch_op.f('uq_monitors_heartbeat_token'), ['heartbeat_token'])




def downgrade():

    with op.batch_alter_table('monitors', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('uq_monitors_heartbeat_token'), type_='unique')
        batch_op.drop_column('cert_warned_days')
        batch_op.drop_column('cert_issuer')
        batch_op.drop_column('cert_expires_at')
        batch_op.drop_column('last_ping_at')
        batch_op.drop_column('grace_s')
        batch_op.drop_column('heartbeat_token')
        batch_op.drop_column('sleepy')
        batch_op.drop_column('slow_ms')
        batch_op.drop_column('assertions')



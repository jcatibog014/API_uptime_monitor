"""Uptime goals and the weekly report (stage 9)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-19 10:11:17.786986
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():

    with op.batch_alter_table('monitors', schema=None) as batch_op:
        batch_op.add_column(sa.Column('slo_target', sa.Float(), server_default='99.9', nullable=False))

    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.add_column(sa.Column('weekly_report', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('last_report_at', sa.DateTime(), nullable=True))




def downgrade():

    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.drop_column('last_report_at')
        batch_op.drop_column('weekly_report')

    with op.batch_alter_table('monitors', schema=None) as batch_op:
        batch_op.drop_column('slo_target')



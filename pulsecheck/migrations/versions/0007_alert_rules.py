"""Alert rules: routing, reminders, escalation, flapping (stage 7)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-19 09:54:27.733802
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():

    op.create_table('monitor_channels',
    sa.Column('monitor_id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('level', sa.Integer(), server_default='1', nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], name=op.f('fk_monitor_channels_channel_id_channels'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['monitor_id'], ['monitors.id'], name=op.f('fk_monitor_channels_monitor_id_monitors'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('monitor_id', 'channel_id', name=op.f('pk_monitor_channels'))
    )
    with op.batch_alter_table('monitor_channels', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_monitor_channels_channel_id'), ['channel_id'], unique=False)

    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reminders_sent', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('escalated_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('monitors', schema=None) as batch_op:
        batch_op.add_column(sa.Column('remind_every_m', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('escalate_after_m', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('flapping', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('flapping_since', sa.DateTime(), nullable=True))




def downgrade():

    with op.batch_alter_table('monitors', schema=None) as batch_op:
        batch_op.drop_column('flapping_since')
        batch_op.drop_column('flapping')
        batch_op.drop_column('escalate_after_m')
        batch_op.drop_column('remind_every_m')

    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.drop_column('escalated_at')
        batch_op.drop_column('reminders_sent')

    with op.batch_alter_table('monitor_channels', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_monitor_channels_channel_id'))

    op.drop_table('monitor_channels')


"""Alert channels, alerts, practice inbox and push (stage 5)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-19 09:21:04.255736
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():

    op.create_table('channels',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('settings', sa.JSON(), nullable=False),
    sa.Column('secret', sa.Text(), server_default='', nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=False),
    sa.Column('fail_streak', sa.Integer(), server_default='0', nullable=False),
    sa.Column('paused_until', sa.DateTime(), nullable=True),
    sa.Column('last_sent_at', sa.DateTime(), nullable=True),
    sa.Column('last_error', sa.String(length=300), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_channels_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_channels'))
    )
    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_channels_workspace_id'), ['workspace_id'], unique=False)

    op.create_table('push_subscriptions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('endpoint', sa.String(length=1000), nullable=False),
    sa.Column('p256dh', sa.String(length=200), nullable=False),
    sa.Column('auth', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_push_subscriptions_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_push_subscriptions')),
    sa.UniqueConstraint('endpoint', name=op.f('uq_push_subscriptions_endpoint'))
    )
    with op.batch_alter_table('push_subscriptions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_push_subscriptions_workspace_id'), ['workspace_id'], unique=False)

    op.create_table('alerts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('incident_id', sa.Integer(), nullable=True),
    sa.Column('event', sa.String(length=12), nullable=False),
    sa.Column('seq', sa.Integer(), server_default='0', nullable=False),
    sa.Column('status', sa.String(length=10), server_default='waiting', nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), server_default='', nullable=False),
    sa.Column('link', sa.String(length=500), server_default='', nullable=False),
    sa.Column('tries', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_error', sa.String(length=300), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], name=op.f('fk_alerts_channel_id_channels'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], name=op.f('fk_alerts_incident_id_incidents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_alerts_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_alerts')),
    sa.UniqueConstraint('incident_id', 'channel_id', 'event', 'seq', name=op.f('uq_alerts_incident_id_channel_id_event_seq'))
    )
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alerts_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_alerts_incident_id'), ['incident_id'], unique=False)
        batch_op.create_index('ix_alerts_workspace_id_created_at', ['workspace_id', 'created_at'], unique=False)

    op.create_table('practice_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('alert_id', sa.Integer(), nullable=False),
    sa.Column('looks_like', sa.String(length=10), nullable=False),
    sa.Column('channel_name', sa.String(length=80), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), server_default='', nullable=False),
    sa.Column('link', sa.String(length=500), server_default='', nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['alert_id'], ['alerts.id'], name=op.f('fk_practice_messages_alert_id_alerts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_practice_messages_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_practice_messages')),
    sa.UniqueConstraint('alert_id', name=op.f('uq_practice_messages_alert_id'))
    )
    with op.batch_alter_table('practice_messages', schema=None) as batch_op:
        batch_op.create_index('ix_practice_messages_workspace_id_at', ['workspace_id', 'at'], unique=False)

    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('acked_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('acked_by', sa.String(length=80), server_default='', nullable=False))




def downgrade():

    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.drop_column('acked_by')
        batch_op.drop_column('acked_at')

    with op.batch_alter_table('practice_messages', schema=None) as batch_op:
        batch_op.drop_index('ix_practice_messages_workspace_id_at')

    op.drop_table('practice_messages')
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.drop_index('ix_alerts_workspace_id_created_at')
        batch_op.drop_index(batch_op.f('ix_alerts_incident_id'))
        batch_op.drop_index(batch_op.f('ix_alerts_channel_id'))

    op.drop_table('alerts')
    with op.batch_alter_table('push_subscriptions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_push_subscriptions_workspace_id'))

    op.drop_table('push_subscriptions')
    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_channels_workspace_id'))

    op.drop_table('channels')


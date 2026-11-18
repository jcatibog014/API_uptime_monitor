"""Workspaces, users, rate limits, monitors and checks (stage 1)

Revision ID: 0001
Revises: 
Create Date: 2026-11-18 21:02:22.690286
"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():

    op.create_table('rate_limits',
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('window_start', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_rate_limits'))
    )
    op.create_table('workspaces',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('is_demo', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('demo_expires_at', sa.DateTime(), nullable=True),
    sa.Column('time_zone', sa.String(length=64), server_default='Asia/Manila', nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_workspaces')),
    sqlite_autoincrement=True
    )
    op.create_table('monitors',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('kind', sa.String(length=12), server_default='http', nullable=False),
    sa.Column('url', sa.String(length=2000), server_default='', nullable=False),
    sa.Column('method', sa.String(length=8), server_default='GET', nullable=False),
    sa.Column('expected_statuses', sa.String(length=60), server_default='200-299', nullable=False),
    sa.Column('timeout_s', sa.Integer(), server_default='10', nullable=False),
    sa.Column('interval_s', sa.Integer(), server_default='60', nullable=False),
    sa.Column('paused', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_monitors_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_monitors'))
    )
    with op.batch_alter_table('monitors', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_monitors_workspace_id'), ['workspace_id'], unique=False)

    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('is_demo', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('session_version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('failed_logins', sa.Integer(), server_default='0', nullable=False),
    sa.Column('locked_until', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_users_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email')),
    sqlite_autoincrement=True
    )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_users_workspace_id'), ['workspace_id'], unique=False)

    op.create_table('checks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('monitor_id', sa.Integer(), nullable=False),
    sa.Column('scheduled_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('slow', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('our_side', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('manual', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('error_kind', sa.String(length=20), nullable=True),
    sa.Column('message', sa.String(length=300), server_default='', nullable=False),
    sa.Column('status_code', sa.Integer(), nullable=True),
    sa.Column('remote_ip', sa.String(length=45), nullable=True),
    sa.Column('dns_ms', sa.Float(), nullable=True),
    sa.Column('connect_ms', sa.Float(), nullable=True),
    sa.Column('tls_ms', sa.Float(), nullable=True),
    sa.Column('ttfb_ms', sa.Float(), nullable=True),
    sa.Column('total_ms', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['monitor_id'], ['monitors.id'], name=op.f('fk_checks_monitor_id_monitors'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_checks')),
    sa.UniqueConstraint('monitor_id', 'scheduled_at', name=op.f('uq_checks_monitor_id_scheduled_at'))
    )



def downgrade():

    op.drop_table('checks')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_workspace_id'))

    op.drop_table('users')
    with op.batch_alter_table('monitors', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_monitors_workspace_id'))

    op.drop_table('monitors')
    op.drop_table('workspaces')
    op.drop_table('rate_limits')


"""Monitor states, incidents, canary and timers (stage 3)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19 08:46:03.005524
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():

    op.create_table('canary_checks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('target', sa.String(length=10), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('total_ms', sa.Float(), nullable=True),
    sa.Column('message', sa.String(length=300), server_default='', nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_canary_checks'))
    )
    with op.batch_alter_table('canary_checks', schema=None) as batch_op:
        batch_op.create_index('ix_canary_checks_target_at', ['target', 'at'], unique=False)

    op.create_table('timers',
    sa.Column('name', sa.String(length=40), nullable=False),
    sa.Column('next_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('name', name=op.f('pk_timers'))
    )
    op.create_table('incidents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('monitor_id', sa.Integer(), nullable=False),
    sa.Column('opened_at', sa.DateTime(), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(), nullable=False),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('cause', sa.String(length=20), nullable=True),
    sa.Column('message', sa.String(length=300), server_default='', nullable=False),
    sa.Column('failed_checks', sa.Integer(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['monitor_id'], ['monitors.id'], name=op.f('fk_incidents_monitor_id_monitors'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_incidents_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_incidents'))
    )
    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_incidents_monitor_id'), ['monitor_id'], unique=False)
        batch_op.create_index('ix_incidents_workspace_id_opened_at', ['workspace_id', 'opened_at'], unique=False)

    with op.batch_alter_table('monitors', schema=None) as batch_op:
        batch_op.add_column(sa.Column('state', sa.String(length=12), server_default='new', nullable=False))
        batch_op.add_column(sa.Column('state_since', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('fail_streak', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('ok_streak', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('confirm_tries', sa.Integer(), server_default='3', nullable=False))
        batch_op.add_column(sa.Column('recover_tries', sa.Integer(), server_default='2', nullable=False))
        batch_op.add_column(sa.Column('judged_until', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('version', sa.Integer(), server_default='1', nullable=False))




def downgrade():

    with op.batch_alter_table('monitors', schema=None) as batch_op:
        batch_op.drop_column('version')
        batch_op.drop_column('judged_until')
        batch_op.drop_column('recover_tries')
        batch_op.drop_column('confirm_tries')
        batch_op.drop_column('ok_streak')
        batch_op.drop_column('fail_streak')
        batch_op.drop_column('state_since')
        batch_op.drop_column('state')

    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.drop_index('ix_incidents_workspace_id_opened_at')
        batch_op.drop_index(batch_op.f('ix_incidents_monitor_id'))

    op.drop_table('incidents')
    op.drop_table('timers')
    with op.batch_alter_table('canary_checks', schema=None) as batch_op:
        batch_op.drop_index('ix_canary_checks_target_at')

    op.drop_table('canary_checks')


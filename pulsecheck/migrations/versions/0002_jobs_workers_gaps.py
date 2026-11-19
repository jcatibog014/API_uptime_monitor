"""Jobs, workers, gaps and the schedule (stage 2)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19 08:34:56.118482
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():

    op.create_table('workers',
    sa.Column('id', sa.String(length=80), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen', sa.DateTime(), nullable=False),
    sa.Column('jobs_done', sa.Integer(), server_default='0', nullable=False),
    sa.Column('stopped_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_workers'))
    )
    op.create_table('jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=12), server_default='queued', nullable=False),
    sa.Column('run_at', sa.DateTime(), nullable=False),
    sa.Column('priority', sa.Integer(), server_default='5', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('max_attempts', sa.Integer(), server_default='6', nullable=False),
    sa.Column('lease_until', sa.DateTime(), nullable=True),
    sa.Column('locked_by', sa.String(length=80), nullable=True),
    sa.Column('last_error', sa.Text(), server_default='', nullable=False),
    sa.Column('dedupe_key', sa.String(length=120), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_jobs_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_jobs')),
    sa.UniqueConstraint('dedupe_key', name=op.f('uq_jobs_dedupe_key'))
    )
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.create_index('ix_jobs_status_run_at', ['status', 'run_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_jobs_workspace_id'), ['workspace_id'], unique=False)

    op.create_table('gaps',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('monitor_id', sa.Integer(), nullable=False),
    sa.Column('start', sa.DateTime(), nullable=False),
    sa.Column('end', sa.DateTime(), nullable=False),
    sa.Column('slots', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['monitor_id'], ['monitors.id'], name=op.f('fk_gaps_monitor_id_monitors'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gaps'))
    )
    with op.batch_alter_table('gaps', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_gaps_monitor_id'), ['monitor_id'], unique=False)

    with op.batch_alter_table('monitors', schema=None) as batch_op:
        batch_op.add_column(sa.Column('next_check_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_monitors_next_check_at'), ['next_check_at'], unique=False)

    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.add_column(sa.Column('replaying', sa.Boolean(), server_default=sa.false(), nullable=False))




def downgrade():

    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.drop_column('replaying')

    with op.batch_alter_table('monitors', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_monitors_next_check_at'))
        batch_op.drop_column('next_check_at')

    with op.batch_alter_table('gaps', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_gaps_monitor_id'))

    op.drop_table('gaps')
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_jobs_workspace_id'))
        batch_op.drop_index('ix_jobs_status_run_at')

    op.drop_table('jobs')
    op.drop_table('workers')


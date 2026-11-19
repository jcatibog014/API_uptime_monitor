"""Status pages, incident notes, maintenance (stage 8)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-19 10:03:03.736203
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():

    op.create_table('maintenance',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=120), nullable=False),
    sa.Column('note', sa.String(length=500), server_default='', nullable=False),
    sa.Column('starts_at', sa.DateTime(), nullable=False),
    sa.Column('ends_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_maintenance_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_maintenance'))
    )
    with op.batch_alter_table('maintenance', schema=None) as batch_op:
        batch_op.create_index('ix_maintenance_workspace_id_ends_at', ['workspace_id', 'ends_at'], unique=False)

    op.create_table('status_pages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('slug', sa.String(length=60), nullable=False),
    sa.Column('intro', sa.String(length=300), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_status_pages_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_status_pages')),
    sa.UniqueConstraint('slug', name=op.f('uq_status_pages_slug'))
    )
    with op.batch_alter_table('status_pages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_status_pages_workspace_id'), ['workspace_id'], unique=False)

    op.create_table('maintenance_monitors',
    sa.Column('maintenance_id', sa.Integer(), nullable=False),
    sa.Column('monitor_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['maintenance_id'], ['maintenance.id'], name=op.f('fk_maintenance_monitors_maintenance_id_maintenance'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['monitor_id'], ['monitors.id'], name=op.f('fk_maintenance_monitors_monitor_id_monitors'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('maintenance_id', 'monitor_id', name=op.f('pk_maintenance_monitors'))
    )
    with op.batch_alter_table('maintenance_monitors', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_maintenance_monitors_monitor_id'), ['monitor_id'], unique=False)

    op.create_table('status_page_monitors',
    sa.Column('status_page_id', sa.Integer(), nullable=False),
    sa.Column('monitor_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), server_default='0', nullable=False),
    sa.Column('label', sa.String(length=80), server_default='', nullable=False),
    sa.ForeignKeyConstraint(['monitor_id'], ['monitors.id'], name=op.f('fk_status_page_monitors_monitor_id_monitors'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['status_page_id'], ['status_pages.id'], name=op.f('fk_status_page_monitors_status_page_id_status_pages'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('status_page_id', 'monitor_id', name=op.f('pk_status_page_monitors'))
    )
    with op.batch_alter_table('status_page_monitors', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_status_page_monitors_monitor_id'), ['monitor_id'], unique=False)

    op.create_table('incident_updates',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('incident_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=14), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('author', sa.String(length=80), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], name=op.f('fk_incident_updates_incident_id_incidents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_incident_updates'))
    )
    with op.batch_alter_table('incident_updates', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_incident_updates_incident_id'), ['incident_id'], unique=False)

    with op.batch_alter_table('checks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('resting', sa.Boolean(), server_default=sa.false(), nullable=False))

    with op.batch_alter_table('rollups', schema=None) as batch_op:
        batch_op.add_column(sa.Column('resting', sa.Integer(), server_default='0', nullable=False))




def downgrade():

    with op.batch_alter_table('rollups', schema=None) as batch_op:
        batch_op.drop_column('resting')

    with op.batch_alter_table('checks', schema=None) as batch_op:
        batch_op.drop_column('resting')

    with op.batch_alter_table('incident_updates', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_incident_updates_incident_id'))

    op.drop_table('incident_updates')
    with op.batch_alter_table('status_page_monitors', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_status_page_monitors_monitor_id'))

    op.drop_table('status_page_monitors')
    with op.batch_alter_table('maintenance_monitors', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_maintenance_monitors_monitor_id'))

    op.drop_table('maintenance_monitors')
    with op.batch_alter_table('status_pages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_status_pages_workspace_id'))

    op.drop_table('status_pages')
    with op.batch_alter_table('maintenance', schema=None) as batch_op:
        batch_op.drop_index('ix_maintenance_workspace_id_ends_at')

    op.drop_table('maintenance')


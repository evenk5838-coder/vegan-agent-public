"""用户隔离、记录与持久任务初始结构。"""
from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('users', sa.Column('id', sa.String(32), primary_key=True), sa.Column('username', sa.String(32), unique=True, nullable=False), sa.Column('password_hash', sa.String(255), nullable=False), sa.Column('preferences', sa.JSON, nullable=False))
    op.create_table('sessions', sa.Column('token_hash', sa.String(64), primary_key=True), sa.Column('owner', sa.String(32), nullable=False), sa.Column('expires', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_sessions_owner', 'sessions', ['owner'])
    op.create_table('records', sa.Column('id', sa.String(32), primary_key=True), sa.Column('owner', sa.String(64), nullable=False), sa.Column('kind', sa.String(32), nullable=False), sa.Column('payload', sa.JSON, nullable=False), sa.Column('created', sa.DateTime(timezone=True), nullable=False), sa.Column('expires', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_records_owner', 'records', ['owner'])
    op.create_index('ix_records_owner_kind', 'records', ['owner', 'kind'])
    op.create_table('jobs', sa.Column('id', sa.String(32), primary_key=True), sa.Column('owner', sa.String(32), nullable=False), sa.Column('kind', sa.String(24), nullable=False), sa.Column('status', sa.String(24), nullable=False), sa.Column('payload', sa.JSON, nullable=False), sa.Column('result', sa.JSON, nullable=False), sa.Column('created', sa.DateTime(timezone=True), nullable=False), sa.Column('due', sa.DateTime(timezone=True), nullable=False), sa.Column('attempts', sa.Integer, nullable=False))
    for column in ('owner', 'status', 'due'):
        op.create_index(f'ix_jobs_{column}', 'jobs', [column])


def downgrade():
    for table in ('jobs', 'records', 'sessions', 'users'):
        op.drop_table(table)

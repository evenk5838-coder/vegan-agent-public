from alembic import context
from app import core

core.init_db()
with core.engine.connect() as connection:
    context.configure(connection=connection, target_metadata=core.Base.metadata)
    with context.begin_transaction():
        context.run_migrations()

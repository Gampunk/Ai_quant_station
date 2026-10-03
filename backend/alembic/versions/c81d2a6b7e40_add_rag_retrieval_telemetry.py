"""Add user/cycle scoped RAG retrieval metadata."""
from alembic import op
import sqlalchemy as sa

revision = "c81d2a6b7e40"
down_revision = "f4a91b7c2e60"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "rag_logs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("rag_logs")}
    additions = [
        ("user_id", sa.Column("user_id", sa.Integer(), nullable=True)),
        ("cycle_id", sa.Column("cycle_id", sa.String(length=36), nullable=True)),
        ("source", sa.Column("source", sa.String(), nullable=True)),
        ("query_hash", sa.Column("query_hash", sa.String(length=64), nullable=True)),
        ("context_hash", sa.Column("context_hash", sa.String(length=64), nullable=True)),
        ("selected_memories", sa.Column("selected_memories", sa.JSON(), nullable=True)),
    ]
    for name, column in additions:
        if name not in columns:
            with op.batch_alter_table("rag_logs") as batch:
                batch.add_column(column)
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("rag_logs")}
    for name, columns in (("ix_rag_logs_user_id", ["user_id"]),
                          ("ix_rag_logs_cycle_id", ["cycle_id"])):
        if name not in indexes:
            op.create_index(name, "rag_logs", columns)


def downgrade():
    bind = op.get_bind()
    if "rag_logs" not in sa.inspect(bind).get_table_names():
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("rag_logs")}
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("rag_logs")}
    for name in ("ix_rag_logs_cycle_id", "ix_rag_logs_user_id"):
        if name in indexes:
            op.drop_index(name, table_name="rag_logs")
    for name in ("selected_memories", "context_hash", "query_hash", "source", "cycle_id", "user_id"):
        if name in columns:
            with op.batch_alter_table("rag_logs") as batch:
                batch.drop_column(name)

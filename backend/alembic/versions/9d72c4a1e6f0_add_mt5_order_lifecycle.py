"""Track MT5 broker order tickets and lifecycle state."""
from alembic import op
import sqlalchemy as sa


revision = "9d72c4a1e6f0"
down_revision = "c81d2a6b7e40"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in ("autopilot_trades", "autopilot_cycles"):
        if table not in inspector.get_table_names():
            continue
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        additions = (
            ("mt5_order_ticket", sa.Column("mt5_order_ticket", sa.BigInteger(), nullable=True)),
            ("order_status", sa.Column("order_status", sa.String(), nullable=True)),
            ("order_completed_at", sa.Column("order_completed_at", sa.DateTime(timezone=True), nullable=True)),
        )
        for name, column in additions:
            if name not in columns:
                with op.batch_alter_table(table) as batch:
                    batch.add_column(column)
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
        for name, column in ((f"ix_{table}_mt5_order_ticket", "mt5_order_ticket"),
                             (f"ix_{table}_order_status", "order_status")):
            if name not in indexes:
                op.create_index(name, table, [column])


def downgrade():
    bind = op.get_bind()
    for table in ("autopilot_cycles", "autopilot_trades"):
        if table not in sa.inspect(bind).get_table_names():
            continue
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
        for name in (f"ix_{table}_order_status", f"ix_{table}_mt5_order_ticket"):
            if name in indexes:
                op.drop_index(name, table_name=table)
        for name in ("order_completed_at", "order_status", "mt5_order_ticket"):
            if name in columns:
                with op.batch_alter_table(table) as batch:
                    batch.drop_column(name)

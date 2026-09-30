"""Persist MT5 order and deal lifecycle events with native exit reasons."""
from alembic import op
import sqlalchemy as sa


revision = "b37c8e2a91d4"
down_revision = "9d72c4a1e6f0"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "autopilot_trades" in inspector.get_table_names():
        columns = {column["name"] for column in sa.inspect(bind).get_columns("autopilot_trades")}
        if "exit_reason" not in columns:
            with op.batch_alter_table("autopilot_trades") as batch:
                batch.add_column(sa.Column("exit_reason", sa.String(), nullable=True))
        if "exit_reason_source" not in columns:
            with op.batch_alter_table("autopilot_trades") as batch:
                batch.add_column(sa.Column("exit_reason_source", sa.String(), nullable=True))
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("autopilot_trades")}
        if "ix_autopilot_trades_exit_reason" not in indexes:
            op.create_index("ix_autopilot_trades_exit_reason", "autopilot_trades", ["exit_reason"])

    if "autopilot_cycles" in inspector.get_table_names():
        cycle_columns = {column["name"] for column in sa.inspect(bind).get_columns("autopilot_cycles")}
        for name in ("exit_reason", "exit_reason_source"):
            if name not in cycle_columns:
                with op.batch_alter_table("autopilot_cycles") as batch:
                    batch.add_column(sa.Column(name, sa.String(), nullable=True))

    tables = set(sa.inspect(bind).get_table_names())
    if "autopilot_order_events" not in tables:
        op.create_table(
            "autopilot_order_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_key", sa.String(length=180), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("autopilot_trade_id", sa.Integer(), nullable=True),
            sa.Column("cycle_id", sa.String(length=36), nullable=True),
            sa.Column("prompt_number", sa.Integer(), nullable=True),
            sa.Column("symbol", sa.String(), nullable=True),
            sa.Column("event_type", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=True),
            sa.Column("order_ticket", sa.BigInteger(), nullable=True),
            sa.Column("deal_ticket", sa.BigInteger(), nullable=True),
            sa.Column("position_id", sa.BigInteger(), nullable=True),
            sa.Column("entry_type", sa.String(), nullable=True),
            sa.Column("reason_code", sa.Integer(), nullable=True),
            sa.Column("reason", sa.String(), nullable=True),
            sa.Column("volume", sa.Float(), nullable=True),
            sa.Column("price", sa.Float(), nullable=True),
            sa.Column("profit", sa.Float(), nullable=True),
            sa.Column("swap", sa.Float(), nullable=True),
            sa.Column("commission", sa.Float(), nullable=True),
            sa.Column("broker_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("comment", sa.String(length=256), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["autopilot_trade_id"], ["autopilot_trades.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("autopilot_order_events")}
    for name, columns, unique in (
        ("ix_autopilot_order_events_event_key", ["event_key"], True),
        ("ix_autopilot_order_events_user_id", ["user_id"], False),
        ("ix_autopilot_order_events_autopilot_trade_id", ["autopilot_trade_id"], False),
        ("ix_autopilot_order_events_cycle_id", ["cycle_id"], False),
        ("ix_autopilot_order_events_prompt_number", ["prompt_number"], False),
        ("ix_autopilot_order_events_event_type", ["event_type"], False),
        ("ix_autopilot_order_events_order_ticket", ["order_ticket"], False),
        ("ix_autopilot_order_events_deal_ticket", ["deal_ticket"], False),
        ("ix_autopilot_order_events_position_id", ["position_id"], False),
        ("ix_autopilot_order_events_broker_time", ["broker_time"], False),
        ("ix_autopilot_order_events_observed_at", ["observed_at"], False),
        ("ix_autopilot_order_events_user_cycle", ["user_id", "cycle_id"], False),
        ("ix_autopilot_order_events_user_prompt", ["user_id", "prompt_number"], False),
    ):
        if name not in indexes:
            op.create_index(name, "autopilot_order_events", columns, unique=unique)


def downgrade():
    bind = op.get_bind()
    if "autopilot_order_events" in sa.inspect(bind).get_table_names():
        op.drop_table("autopilot_order_events")
    if "autopilot_trades" in sa.inspect(bind).get_table_names():
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("autopilot_trades")}
        if "ix_autopilot_trades_exit_reason" in indexes:
            op.drop_index("ix_autopilot_trades_exit_reason", table_name="autopilot_trades")
        columns = {column["name"] for column in sa.inspect(bind).get_columns("autopilot_trades")}
        for name in ("exit_reason_source", "exit_reason"):
            if name in columns:
                with op.batch_alter_table("autopilot_trades") as batch:
                    batch.drop_column(name)
    if "autopilot_cycles" in sa.inspect(bind).get_table_names():
        columns = {column["name"] for column in sa.inspect(bind).get_columns("autopilot_cycles")}
        for name in ("exit_reason_source", "exit_reason"):
            if name in columns:
                with op.batch_alter_table("autopilot_cycles") as batch:
                    batch.drop_column(name)

"""Store broker quote and accepted protection levels for autopilot trades."""
from alembic import op
import sqlalchemy as sa


revision = "a6d91c4e2b70"
down_revision = "b37c8e2a91d4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    additions = {
        "autopilot_trades": (
            "proposed_entry_price", "requested_entry_price", "submitted_quote", "slippage_price",
            "requested_stop_loss", "requested_take_profit", "broker_stop_loss", "broker_take_profit",
        ),
        "autopilot_execution_attempts": (
            "proposed_entry_price", "requested_entry_price", "proposed_stop_loss", "proposed_take_profit",
            "requested_stop_loss", "requested_take_profit", "requested_lot_size", "submitted_quote", "slippage_price",
            "broker_stop_loss", "broker_take_profit", "error_category", "source",
        ),
    }
    for table, names in additions.items():
        if table not in tables:
            continue
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        with op.batch_alter_table(table) as batch:
            for name in names:
                if name not in columns:
                    column_type = sa.String() if name in ("error_category", "source") else sa.Float()
                    batch.add_column(sa.Column(name, column_type, nullable=True))
    if "autopilot_execution_attempts" in tables:
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes("autopilot_execution_attempts")}
        if "ix_autopilot_execution_attempts_error_category" not in indexes:
            op.create_index(
                "ix_autopilot_execution_attempts_error_category",
                "autopilot_execution_attempts", ["error_category"],
            )


def downgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table, names in {
        "autopilot_execution_attempts": (
            "source", "error_category", "broker_take_profit", "broker_stop_loss", "slippage_price", "submitted_quote", "requested_lot_size",
            "requested_take_profit", "requested_stop_loss", "proposed_take_profit", "proposed_stop_loss",
            "requested_entry_price", "proposed_entry_price",
        ),
        "autopilot_trades": (
            "broker_take_profit", "broker_stop_loss", "requested_take_profit", "requested_stop_loss",
            "slippage_price", "submitted_quote", "requested_entry_price", "proposed_entry_price",
        ),
    }.items():
        if table not in tables:
            continue
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        with op.batch_alter_table(table) as batch:
            for name in names:
                if name in columns:
                    batch.drop_column(name)
    if "autopilot_execution_attempts" in tables:
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes("autopilot_execution_attempts")}
        if "ix_autopilot_execution_attempts_error_category" in indexes:
            op.drop_index("ix_autopilot_execution_attempts_error_category", table_name="autopilot_execution_attempts")

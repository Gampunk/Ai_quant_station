"""Add durable autopilot cycle telemetry and stable cycle IDs."""
from alembic import op
import sqlalchemy as sa


revision = "f4a91b7c2e60"
down_revision = "e4a7b9c2d1f3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "autopilot_cycles" not in inspector.get_table_names():
        op.create_table(
        "autopilot_cycles",
        sa.Column("cycle_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("cycle_number", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("prompt_number", sa.Integer(), nullable=True),
        sa.Column("prompt_text", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("market_regime", sa.String(), nullable=True),
        sa.Column("regime_details", sa.JSON(), nullable=True),
        sa.Column("selection_context", sa.JSON(), nullable=True),
        sa.Column("market_timeframe", sa.String(), nullable=True),
        sa.Column("candles_loaded", sa.Integer(), nullable=True),
        sa.Column("market_data_hash", sa.String(length=64), nullable=True),
        sa.Column("analysis_prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("decision_source", sa.String(), nullable=True),
        sa.Column("rag_context_included", sa.Boolean(), nullable=True),
        sa.Column("rag_context_chars", sa.Integer(), nullable=True),
        sa.Column("atr_14", sa.Float(), nullable=True),
        sa.Column("avg_atr_20", sa.Float(), nullable=True),
        sa.Column("setup", sa.JSON(), nullable=True),
        sa.Column("requested_lot_size", sa.Float(), nullable=True),
        sa.Column("final_lot_size", sa.Float(), nullable=True),
        sa.Column("execution_status", sa.String(), nullable=True),
        sa.Column("mt5_ticket", sa.BigInteger(), nullable=True),
        sa.Column("trade_result", sa.String(), nullable=True),
        sa.Column("realized_profit", sa.Float(), nullable=True),
        sa.Column("trade_closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("cycle_id"),
        )
    inspector = sa.inspect(bind)
    cycle_indexes = {index["name"] for index in inspector.get_indexes("autopilot_cycles")}
    for name, columns in (
        ("ix_autopilot_cycles_user_id", ["user_id"]),
        ("ix_autopilot_cycles_status", ["status"]),
        ("ix_autopilot_cycles_outcome", ["outcome"]),
        ("ix_autopilot_cycles_prompt_number", ["prompt_number"]),
        ("ix_autopilot_cycles_market_regime", ["market_regime"]),
        ("ix_autopilot_cycles_started_at", ["started_at"]),
        ("ix_autopilot_cycles_user_started", ["user_id", "started_at"]),
        ("ix_autopilot_cycles_user_prompt", ["user_id", "prompt_number"]),
    ):
        if name not in cycle_indexes:
            op.create_index(name, "autopilot_cycles", columns)

    for table in ("autopilot_trades", "autopilot_logs", "ai_call_logs", "autopilot_execution_attempts"):
        inspector = sa.inspect(bind)
        columns = {column["name"] for column in inspector.get_columns(table)}
        indexes = {index["name"] for index in inspector.get_indexes(table)}
        if "cycle_id" not in columns:
            with op.batch_alter_table(table) as batch_op:
                batch_op.add_column(sa.Column("cycle_id", sa.String(length=36), nullable=True))
        if f"ix_{table}_cycle_id" not in indexes:
            with op.batch_alter_table(table) as batch_op:
                batch_op.create_index(f"ix_{table}_cycle_id", ["cycle_id"])


def downgrade():
    for table in ("autopilot_execution_attempts", "ai_call_logs", "autopilot_logs", "autopilot_trades"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_index(f"ix_{table}_cycle_id")
            batch_op.drop_column("cycle_id")
    op.drop_index("ix_autopilot_cycles_user_prompt", table_name="autopilot_cycles")
    op.drop_index("ix_autopilot_cycles_user_started", table_name="autopilot_cycles")
    op.drop_index("ix_autopilot_cycles_started_at", table_name="autopilot_cycles")
    op.drop_index("ix_autopilot_cycles_market_regime", table_name="autopilot_cycles")
    op.drop_index("ix_autopilot_cycles_prompt_number", table_name="autopilot_cycles")
    op.drop_index("ix_autopilot_cycles_outcome", table_name="autopilot_cycles")
    op.drop_index("ix_autopilot_cycles_status", table_name="autopilot_cycles")
    op.drop_index("ix_autopilot_cycles_user_id", table_name="autopilot_cycles")
    op.drop_table("autopilot_cycles")

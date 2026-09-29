"""Risk settings, daily equity baseline and order decisions

Revision ID: a7d2c4e6f8b1
Revises: f1c3a5e7b9d2
Create Date: 2026-09-28

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a7d2c4e6f8b1"
down_revision = "f1c3a5e7b9d2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "risk_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("changed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("changed_by_name", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("autopilot_risk_pct", sa.Float(), nullable=False),
        sa.Column("max_trade_risk_pct", sa.Float(), nullable=False),
        sa.Column("max_open_positions", sa.Integer(), nullable=False),
        sa.Column("daily_loss_pct", sa.Float(), nullable=False),
        sa.Column("min_margin_level", sa.Float(), nullable=False),
        sa.Column("max_pending_distance_pct", sa.Float(), nullable=False),
        sa.Column("require_stop_loss", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_risk_settings_id", "risk_settings", ["id"])
    op.create_index("ix_risk_settings_created_at", "risk_settings", ["created_at"])

    op.create_table(
        "risk_days",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("day", sa.String(), nullable=False),
        sa.Column("account_login", sa.BigInteger(), nullable=False),
        sa.Column("start_equity", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("day", "account_login", name="uq_risk_days_day_account"),
    )

    op.create_table(
        "risk_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("requested_volume", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("sl", sa.Float(), nullable=True),
        sa.Column("tp", sa.Float(), nullable=True),
        sa.Column("stop_distance", sa.Float(), nullable=True),
        sa.Column("risk_amount", sa.Float(), nullable=True),
        sa.Column("risk_pct", sa.Float(), nullable=True),
        sa.Column("equity", sa.Float(), nullable=True),
        sa.Column("settings_id", sa.Integer(), sa.ForeignKey("risk_settings.id", ondelete="SET NULL"), nullable=True),
        sa.Column("mt5_ticket", sa.BigInteger(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
    )
    for name, cols in [("ix_risk_decisions_id", ["id"]), ("ix_risk_decisions_created_at", ["created_at"]),
                       ("ix_risk_decisions_user_id", ["user_id"]), ("ix_risk_decisions_outcome", ["outcome"]),
                       ("ix_risk_decisions_reason_code", ["reason_code"]), ("ix_risk_decisions_mt5_ticket", ["mt5_ticket"]),
                       ("ix_risk_decisions_source_created", ["source", "created_at"])]:
        op.create_index(name, "risk_decisions", cols)


def downgrade():
    op.drop_table("risk_decisions")
    op.drop_table("risk_days")
    op.drop_table("risk_settings")

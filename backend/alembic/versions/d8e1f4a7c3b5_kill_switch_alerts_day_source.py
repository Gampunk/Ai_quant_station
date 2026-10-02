"""Kill switch, heartbeat alerts, and where each day's starting equity came from

Revision ID: d8e1f4a7c3b5
Revises: b3d6f9a2c8e1
Create Date: 2026-10-02

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d8e1f4a7c3b5"
down_revision = "b3d6f9a2c8e1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "trading_halts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("halted", sa.Boolean(), nullable=False),
        sa.Column("changed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("changed_by_name", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_trading_halts_id", "trading_halts", ["id"])
    op.create_index("ix_trading_halts_created_at", "trading_halts", ["created_at"])
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("delivered", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_alerts_id", "alerts", ["id"])
    op.create_index("ix_alerts_created_at", "alerts", ["created_at"])
    op.create_index("ix_alerts_check", "alerts", ["check"])
    with op.batch_alter_table("risk_days", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source", sa.String(), nullable=False, server_default="first_check"))


def downgrade():
    with op.batch_alter_table("risk_days", schema=None) as batch_op:
        batch_op.drop_column("source")
    op.drop_table("alerts")
    op.drop_table("trading_halts")

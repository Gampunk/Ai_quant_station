"""Autopilot daily loss brake can be switched off

Revision ID: c5e8f2a4d6b9
Revises: a7d2c4e6f8b1
Create Date: 2026-09-30

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c5e8f2a4d6b9"
down_revision = "a7d2c4e6f8b1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("autopilot_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("daily_loss_limit_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    with op.batch_alter_table("autopilot_settings", schema=None) as batch_op:
        batch_op.drop_column("daily_loss_limit_enabled")

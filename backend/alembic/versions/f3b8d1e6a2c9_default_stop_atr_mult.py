"""Optional ATR-based default stop loss, replacing upstream's fixed 0.2%

Revision ID: f3b8d1e6a2c9
Revises: e9a2c7f4b1d6
Create Date: 2026-10-03

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f3b8d1e6a2c9"
down_revision = "e9a2c7f4b1d6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("risk_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("default_stop_atr_mult", sa.Float(), nullable=False, server_default="0"))


def downgrade():
    with op.batch_alter_table("risk_settings", schema=None) as batch_op:
        batch_op.drop_column("default_stop_atr_mult")

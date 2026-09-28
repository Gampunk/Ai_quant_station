"""Record the quoted price next to the filled price

Revision ID: f1c3a5e7b9d2
Revises: e4a7b9c2d1f3
Create Date: 2026-09-28

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f1c3a5e7b9d2"
down_revision = "e4a7b9c2d1f3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("trade_records", schema=None) as batch_op:
        batch_op.add_column(sa.Column("requested_price", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("requested_exit_price", sa.Float(), nullable=True))
    with op.batch_alter_table("autopilot_trades", schema=None) as batch_op:
        batch_op.add_column(sa.Column("requested_price", sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table("autopilot_trades", schema=None) as batch_op:
        batch_op.drop_column("requested_price")
    with op.batch_alter_table("trade_records", schema=None) as batch_op:
        batch_op.drop_column("requested_exit_price")
        batch_op.drop_column("requested_price")

"""Join the Version 1 (upstream) and Version 2 (refactor) migration chains

Both branches added migrations after e4a7b9c2d1f3 without touching the same
columns, so this revision only records that both chains are applied.

Revision ID: e9a2c7f4b1d6
Revises: d8e1f4a7c3b5, a6d91c4e2b70
Create Date: 2026-10-03

"""

# revision identifiers, used by Alembic.
revision = "e9a2c7f4b1d6"
down_revision = ("d8e1f4a7c3b5", "a6d91c4e2b70")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass

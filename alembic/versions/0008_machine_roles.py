"""Machine roles + default project (Commander/Builder division of labor,
doctrine rule G10)

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "machines",
        sa.Column("role", sa.String(length=16), nullable=False, server_default="solo"),
    )
    op.create_check_constraint(
        "ck_machines_role",
        "machines",
        "role IN ('solo', 'commander', 'builder')",
    )
    # Hint only, used to pre-fill generated onboarding prompts -- never
    # enforced (a token still bootstraps any project regardless of this
    # value; see app/models.py Machine.default_project docstring).
    op.add_column("machines", sa.Column("default_project", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("machines", "default_project")
    op.drop_constraint("ck_machines_role", "machines", type_="check")
    op.drop_column("machines", "role")

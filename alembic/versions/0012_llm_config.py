"""LLM provider config -- llm_configs (owner-managed librarian LLM
provider: base_url/model/api_key, ADR-0010 phase 1). Supersede-never-erase
applies here too -- every change (rotating the key, switching provider) is
a new immutable version, never an edit. Mirrors 0007_notifications_config.py
exactly (see that migration's docstring for the same reasoning, and
app/models.py's NotificationConfig docstring for why a plain table-wide
unique index on `version` -- not the partial-unique pattern
DoctrineVersion.version needs -- is correct here: one provider config, one
global sequence, no (kind, project) dimension to partition by).

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_configs",
        sa.Column("id", sa.String(length=26), primary_key=True),
        # Sequential from 1, single global scope -- computed in the route
        # handler (app/llm_config.py's `next_version`), not a DB sequence --
        # same pattern as NotificationConfig.version.
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("base_url", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        # NULL is a legitimate, common value: Ollama and other local
        # OpenAI-compatible endpoints need no key at all.
        sa.Column("api_key", sa.Text, nullable=True),
        # Owner's free-form comment on this version, e.g. "switched to
        # local Ollama". Never served to sessions -- owner-facing only.
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_llm_configs_version",
        "llm_configs",
        ["version"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_llm_configs_version", table_name="llm_configs")
    op.drop_table("llm_configs")

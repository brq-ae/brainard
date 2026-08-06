"""Phase 4: doctrine & bootstrap -- doctrine_versions, bootstrap_fetches,
doctrine-proposal columns on knowledge_entries, projects.description

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Registry fact served by bootstrap's "project context" section
    # (contracts-v1.md §5, §6). No write endpoint yet (phase 5) -- always
    # null until then.
    op.add_column("projects", sa.Column("description", sa.Text, nullable=True))

    # Doctrine proposal flag + decision (contracts-v1.md §4): a proposal is
    # stored as an ordinary library entry -- see app/models.py KnowledgeEntry
    # docstring additions for the exclusion rules this drives.
    op.add_column(
        "knowledge_entries",
        sa.Column("is_doctrine_proposal", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.add_column("knowledge_entries", sa.Column("proposal_decision", sa.String(length=16), nullable=True))
    op.add_column(
        "knowledge_entries", sa.Column("proposal_decided_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "doctrine_versions",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("project", sa.String(length=255), sa.ForeignKey("projects.name"), nullable=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("rules", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_doctrine_versions_kind", "doctrine_versions", ["kind"])
    op.create_index("ix_doctrine_versions_project", "doctrine_versions", ["project"])
    # Per-(kind, project) version sequence, enforced as two partial unique
    # indexes rather than one UNIQUE(kind, project, version) constraint --
    # see the DoctrineVersion.version docstring in app/models.py for why a
    # plain constraint can't catch collisions among 'global' rows (Postgres
    # treats every NULL `project` as distinct).
    op.create_index(
        "ix_doctrine_versions_global_version",
        "doctrine_versions",
        ["kind", "version"],
        unique=True,
        postgresql_where=sa.text("project IS NULL"),
    )
    op.create_index(
        "ix_doctrine_versions_overlay_version",
        "doctrine_versions",
        ["project", "version"],
        unique=True,
        postgresql_where=sa.text("kind = 'overlay'"),
    )

    op.create_table(
        "bootstrap_fetches",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("machine_id", sa.String(length=26), sa.ForeignKey("machines.id"), nullable=False),
        sa.Column("project", sa.String(length=255), sa.ForeignKey("projects.name"), nullable=False),
        sa.Column("doctrine_global_version", sa.Integer, nullable=True),
        sa.Column("doctrine_overlay_version", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bootstrap_fetches_machine_id", "bootstrap_fetches", ["machine_id"])
    op.create_index("ix_bootstrap_fetches_project", "bootstrap_fetches", ["project"])


def downgrade() -> None:
    op.drop_index("ix_bootstrap_fetches_project", table_name="bootstrap_fetches")
    op.drop_index("ix_bootstrap_fetches_machine_id", table_name="bootstrap_fetches")
    op.drop_table("bootstrap_fetches")

    op.drop_index("ix_doctrine_versions_overlay_version", table_name="doctrine_versions")
    op.drop_index("ix_doctrine_versions_global_version", table_name="doctrine_versions")
    op.drop_index("ix_doctrine_versions_project", table_name="doctrine_versions")
    op.drop_index("ix_doctrine_versions_kind", table_name="doctrine_versions")
    op.drop_table("doctrine_versions")

    op.drop_column("knowledge_entries", "proposal_decided_at")
    op.drop_column("knowledge_entries", "proposal_decision")
    op.drop_column("knowledge_entries", "is_doctrine_proposal")

    op.drop_column("projects", "description")

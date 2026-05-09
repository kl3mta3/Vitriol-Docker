"""initial schema — relies on SQLAlchemy metadata create_all.

Rather than hand-write every column, we import the registered metadata
and call create_all on the bound connection. Subsequent revisions will
do hand-written op.add_column / op.create_table changes; this revision
just establishes the baseline.
"""
from alembic import op
from web.db import Base
from web import models  # noqa: F401 — register tables

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)

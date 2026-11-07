"""Merge local compatibility history and the v0.21.0 schema."""

revision: str = "zf6a7b8c9d0e"
down_revision: tuple[str, ...] = ("d0a1f879a01d", "ze5f6a7b8c9d")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

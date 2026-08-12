"""Assegnazione manuale della giacenza per ubicazione (dove si trova
fisicamente ogni articolo, codice per codice) — indipendente dal ledger di
magazzino, non genera movimenti.

Revision ID: v0w1x2y3z4a5
Revises: u9v0w1x2y3z4
"""
from alembic import op
import sqlalchemy as sa

revision = "v0w1x2y3z4a5"
down_revision = "u9v0w1x2y3z4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "material_location_stocks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("material_id", sa.Integer(), sa.ForeignKey("materials.id"), nullable=False),
        sa.Column("storage_location_id", sa.Integer(), sa.ForeignKey("storage_locations.id"), nullable=False),
        sa.Column("qty", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("material_id", "storage_location_id", name="uq_material_location"),
    )
    op.create_index("ix_material_location_stocks_material_id", "material_location_stocks", ["material_id"])
    op.create_index("ix_material_location_stocks_storage_location_id", "material_location_stocks", ["storage_location_id"])


def downgrade():
    op.drop_table("material_location_stocks")

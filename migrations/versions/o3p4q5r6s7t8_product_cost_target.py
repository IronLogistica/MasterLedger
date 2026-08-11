"""Product target costs for analysis and variance reporting.

Revision ID: o3p4q5r6s7t8
Revises: n2o3p4q5r6s7
"""
from alembic import op
import sqlalchemy as sa

revision = "o3p4q5r6s7t8"
down_revision = "n2o3p4q5r6s7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "product_cost_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("material_id", sa.Integer(), sa.ForeignKey("materials.id"), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("target_material_cost", sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("target_labor_cost", sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("target_overhead_cost", sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("notes", sa.String(300), nullable=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("material_id", "effective_date", name="uq_product_cost_target_material_date"),
    )
    op.create_index("ix_product_cost_targets_material_id", "product_cost_targets", ["material_id"])
    op.create_index("ix_product_cost_targets_effective_date", "product_cost_targets", ["effective_date"])


def downgrade():
    op.drop_index("ix_product_cost_targets_effective_date", table_name="product_cost_targets")
    op.drop_index("ix_product_cost_targets_material_id", table_name="product_cost_targets")
    op.drop_table("product_cost_targets")

"""Ciclo di lavorazione (Routing) e Centro di lavoro (WorkCenter) — metodo
SAP per l'assorbimento di manodopera diretta e overhead nel costo standard,
in aggiunta alla Distinta Base (BillOfMaterial) già esistente per i
materiali. Vedi services/routing_cost.py.

Revision ID: q5r6s7t8u9v0
Revises: p4q5r6s7t8u9
"""
from alembic import op
import sqlalchemy as sa

revision = "q5r6s7t8u9v0"
down_revision = "p4q5r6s7t8u9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "work_centers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("description", sa.String(120), nullable=False),
        sa.Column("cost_center_id", sa.Integer(), sa.ForeignKey("cost_centers.id"), nullable=True),
        sa.Column("capacity_hours_month", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("hourly_rate_labor", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=True, server_default=sa.true()),
        sa.UniqueConstraint("code", name="uq_work_centers_code"),
    )

    op.create_table(
        "routings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parent_material_id", sa.Integer(), sa.ForeignKey("materials.id"), nullable=False),
        sa.Column("version", sa.String(10), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=True, server_default=sa.true()),
        sa.Column("notes", sa.String(255), nullable=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("parent_material_id", "version", name="uq_routing_parent_version"),
    )
    op.create_index("ix_routings_parent_material_id", "routings", ["parent_material_id"])

    op.create_table(
        "routing_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("routing_id", sa.Integer(), sa.ForeignKey("routings.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("work_center_id", sa.Integer(), sa.ForeignKey("work_centers.id"), nullable=False),
        sa.Column("description", sa.String(200), nullable=True),
        sa.Column("machine_time_min", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("labor_time_min", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.UniqueConstraint("routing_id", "seq", name="uq_routing_operation_seq"),
    )

    with op.batch_alter_table("production_overhead_items") as batch_op:
        batch_op.add_column(sa.Column("work_center_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_production_overhead_items_work_center_id", "work_centers", ["work_center_id"], ["id"],
        )
    op.create_index(
        "ix_production_overhead_items_work_center_id", "production_overhead_items", ["work_center_id"],
    )


def downgrade():
    op.drop_index("ix_production_overhead_items_work_center_id", table_name="production_overhead_items")
    with op.batch_alter_table("production_overhead_items") as batch_op:
        batch_op.drop_constraint("fk_production_overhead_items_work_center_id", type_="foreignkey")
        batch_op.drop_column("work_center_id")

    op.drop_table("routing_operations")
    op.drop_index("ix_routings_parent_material_id", table_name="routings")
    op.drop_table("routings")
    op.drop_table("work_centers")

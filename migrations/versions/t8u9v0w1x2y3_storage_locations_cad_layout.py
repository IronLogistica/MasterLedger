"""Ubicazioni granulari (corsia/scaffale/ripiano/cassetta) dentro ogni Area
di Magazzino ("blocco"), con struttura attivabile in modo indipendente per
blocco, e coordinate per il layout a schermo (sede -> blocchi, blocco ->
ubicazioni).

Revision ID: t8u9v0w1x2y3
Revises: s7t8u9v0w1x2
"""
from alembic import op
import sqlalchemy as sa

revision = "t8u9v0w1x2y3"
down_revision = "s7t8u9v0w1x2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("warehouse_areas") as batch_op:
        batch_op.add_column(sa.Column("pos_x", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("pos_y", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("dim_x", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("dim_y", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("usa_corsie", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column("usa_scaffali", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column("usa_ripiani", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column("usa_cassette", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("usa_cantilever", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("area_a_terra", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "storage_locations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("warehouse_area_id", sa.Integer(), sa.ForeignKey("warehouse_areas.id"), nullable=False),
        sa.Column("codice", sa.String(40), nullable=False),
        sa.Column("corridoio", sa.String(10)),
        sa.Column("scaffale", sa.String(10)),
        sa.Column("ripiano", sa.String(10)),
        sa.Column("cassetta", sa.String(10)),
        sa.Column("tipo_stoccaggio", sa.String(20), nullable=False, server_default="SCAFFALE"),
        sa.Column("stato", sa.String(20), nullable=False, server_default="libero"),
        sa.Column("pos_x", sa.Float(), nullable=False, server_default="0"),
        sa.Column("pos_y", sa.Float(), nullable=False, server_default="0"),
        sa.Column("dim_x", sa.Float(), nullable=False, server_default="100"),
        sa.Column("dim_y", sa.Float(), nullable=False, server_default="100"),
        sa.Column("peso_max_kg", sa.Float(), nullable=True),
        sa.Column("note", sa.String(255)),
        sa.Column("active", sa.Boolean(), server_default=sa.true()),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.UniqueConstraint("warehouse_area_id", "codice", name="uq_storage_location_codice"),
    )
    op.create_index("ix_storage_locations_warehouse_area_id", "storage_locations", ["warehouse_area_id"])


def downgrade():
    op.drop_table("storage_locations")
    with op.batch_alter_table("warehouse_areas") as batch_op:
        batch_op.drop_column("area_a_terra")
        batch_op.drop_column("usa_cantilever")
        batch_op.drop_column("usa_cassette")
        batch_op.drop_column("usa_ripiani")
        batch_op.drop_column("usa_scaffali")
        batch_op.drop_column("usa_corsie")
        batch_op.drop_column("dim_y")
        batch_op.drop_column("dim_x")
        batch_op.drop_column("pos_y")
        batch_op.drop_column("pos_x")

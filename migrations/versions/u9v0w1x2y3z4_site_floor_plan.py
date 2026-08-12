"""Planimetria della sede operativa (bytes nel database, mai su disco) — per
disegnarci sopra i blocchi di magazzino posizionati.

Revision ID: u9v0w1x2y3z4
Revises: t8u9v0w1x2y3
"""
from alembic import op
import sqlalchemy as sa

revision = "u9v0w1x2y3z4"
down_revision = "t8u9v0w1x2y3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("operating_sites") as batch_op:
        batch_op.add_column(sa.Column("floor_plan_image", sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column("floor_plan_mimetype", sa.String(50), nullable=True))
        batch_op.add_column(sa.Column("floor_plan_width", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("floor_plan_height", sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table("operating_sites") as batch_op:
        batch_op.drop_column("floor_plan_height")
        batch_op.drop_column("floor_plan_width")
        batch_op.drop_column("floor_plan_mimetype")
        batch_op.drop_column("floor_plan_image")

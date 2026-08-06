"""Doppia voce di ricavi: lavorazioni in subappalto vs affidamento diretto
da grandi committenti. Aggiunge il canale ricavo sull'anagrafica cliente,
così la fatturazione da DDT (SD) sa automaticamente su quale conto ricavi
contabilizzare.

Revision ID: g1h2i3j4k5l6
Revises: 7h8i9j0k1l2m
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'g1h2i3j4k5l6'
down_revision = '7h8i9j0k1l2m'
branch_labels = None
depends_on = None


def upgrade():
    b = op.get_bind()
    i = inspect(b)
    cols = [c['name'] for c in i.get_columns('economic_subjects')]
    if 'revenue_channel' not in cols:
        # 'subappalto' | 'affidamento_diretto' | NULL (non ancora qualificato)
        op.add_column('economic_subjects', sa.Column('revenue_channel', sa.String(20), nullable=True))

    # 4000 esisteva già come conto ricavi generico: lo si restringe al canale
    # "subappalto" (lavorazioni per conto di un appaltatore principale) e si
    # aggiunge 4001 per l'"affidamento diretto" (commessa diretta di un
    # grande committente, senza un appaltatore principale in mezzo).
    b.execute(sa.text(
        "UPDATE accounts SET name=:n WHERE code=:c AND name != :n"
    ), {"c": "4000", "n": "Ricavi per Lavorazioni in Subappalto"})

    if not b.execute(sa.text("SELECT 1 FROM accounts WHERE code=:c"), {"c": "4001"}).fetchone():
        b.execute(sa.text(
            "INSERT INTO accounts (code, name, account_type, cost_relevant, cost_relevant_type, active) "
            "VALUES (:c, :n, :t, :r, :rt, true)"
        ), {"c": "4001", "n": "Ricavi per Lavorazioni in Affidamento Diretto (Grandi Committenti)",
            "t": "ricavo", "r": True, "rt": "REVENUE"})


def downgrade():
    pass

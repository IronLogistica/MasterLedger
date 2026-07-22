"""Commesse WIP e RFQ fornitori.
Revision ID: 5f6a7b8c9d0e
Revises: 4e5f6a7b8c9d
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
revision='5f6a7b8c9d0e'
down_revision='4e5f6a7b8c9d'
branch_labels=None
depends_on=None

def upgrade():
    b=op.get_bind(); i=inspect(b)
    if 'production_orders' not in i.get_table_names():
        op.create_table('production_orders',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('order_number',sa.String(30),nullable=False,unique=True),sa.Column('order_date',sa.Date(),nullable=False),sa.Column('material_id',sa.Integer(),sa.ForeignKey('materials.id'),nullable=False),sa.Column('qty_planned',sa.Numeric(14,3),nullable=False),sa.Column('status',sa.String(20),nullable=False,server_default='rilasciata'),sa.Column('cost_center_id',sa.Integer(),sa.ForeignKey('cost_centers.id')),sa.Column('notes',sa.String(300)),sa.Column('created_by_id',sa.Integer(),sa.ForeignKey('users.id')),sa.Column('created_at',sa.DateTime()))
        op.create_table('production_material_issues',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('production_order_id',sa.Integer(),sa.ForeignKey('production_orders.id'),nullable=False),sa.Column('material_id',sa.Integer(),sa.ForeignKey('materials.id'),nullable=False),sa.Column('qty',sa.Numeric(14,3),nullable=False),sa.Column('unit_cost',sa.Numeric(14,4),nullable=False),sa.Column('journal_entry_id',sa.Integer(),sa.ForeignKey('journal_entries.id'),nullable=False),sa.Column('issue_date',sa.Date(),nullable=False))
        op.create_table('production_cost_absorptions',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('production_order_id',sa.Integer(),sa.ForeignKey('production_orders.id'),nullable=False),sa.Column('cost_type',sa.String(15),nullable=False),sa.Column('amount',sa.Numeric(14,2),nullable=False),sa.Column('journal_entry_id',sa.Integer(),sa.ForeignKey('journal_entries.id'),nullable=False),sa.Column('posting_date',sa.Date(),nullable=False),sa.Column('notes',sa.String(255)))
    i=inspect(b)
    if 'requests_for_quotation' not in i.get_table_names():
        op.create_table('requests_for_quotation',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('rfq_number',sa.String(30),nullable=False,unique=True),sa.Column('request_date',sa.Date(),nullable=False),sa.Column('material_id',sa.Integer(),sa.ForeignKey('materials.id'),nullable=False),sa.Column('qty',sa.Numeric(14,3),nullable=False),sa.Column('required_date',sa.Date()),sa.Column('status',sa.String(20),nullable=False,server_default='aperta'),sa.Column('notes',sa.String(300)),sa.Column('created_by_id',sa.Integer(),sa.ForeignKey('users.id')),sa.Column('created_at',sa.DateTime()))
        op.create_table('supplier_quotations',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('rfq_id',sa.Integer(),sa.ForeignKey('requests_for_quotation.id'),nullable=False),sa.Column('economic_subject_id',sa.Integer(),sa.ForeignKey('economic_subjects.id'),nullable=False),sa.Column('offer_ref',sa.String(60)),sa.Column('unit_price',sa.Numeric(14,4),nullable=False),sa.Column('lead_days',sa.Integer()),sa.Column('valid_until',sa.Date()),sa.Column('selected',sa.Boolean(),nullable=False,server_default=sa.false()),sa.Column('purchase_order_id',sa.Integer(),sa.ForeignKey('purchase_orders.id')),sa.Column('created_at',sa.DateTime()))
    for code,name,typ,rel,rtyp in [('157000','Produzione in corso (WIP)','patrimoniale_attivo',False,None),('472000','Manodopera diretta assorbita','ricavo',True,'REVENUE'),('473000','Overhead industriali assorbiti','ricavo',True,'REVENUE'),('464000','Varianza di produzione','costo',True,'COST')]:
        if not b.execute(sa.text('SELECT 1 FROM accounts WHERE code=:c'),{'c':code}).fetchone(): b.execute(sa.text('INSERT INTO accounts (code,name,account_type,cost_relevant,cost_relevant_type,active) VALUES (:c,:n,:t,:r,:rt,true)'),{'c':code,'n':name,'t':typ,'r':rel,'rt':rtyp})
    for typ,prefix in [('OP','41'),('RFQ','35')]:
        if not b.execute(sa.text('SELECT 1 FROM document_sequences WHERE doc_type=:t'),{'t':typ}).fetchone(): b.execute(sa.text('INSERT INTO document_sequences (doc_type,prefix,current_number) VALUES (:t,:p,0)'),{'t':typ,'p':prefix})
def downgrade(): pass

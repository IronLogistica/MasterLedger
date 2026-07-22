"""Ordini di produzione a commessa: WIP, costi standard e COGM."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user
from extensions import db
from models import Account, Material, CostCenter, DocumentSequence, ProductionOrder, ProductionMaterialIssue, ProductionCostAbsorption, StandardCost
from services.posting import post_journal_entry, UnbalancedEntryError

operations_bp = Blueprint('operations', __name__, template_folder='../../templates/operations')

def _acc(code):
    a=Account.query.filter_by(code=code).first()
    if not a: raise ValueError(f'Conto {code} non presente nel piano dei conti.')
    return a

def _dec(v): return Decimal(str(v or '0').replace(',','.'))
def _standard(material_id, date):
    rows=StandardCost.query.filter_by(material_id=material_id).all()
    eligible=[x for x in rows if (x.year,x.month) <= (date.year,date.month)]
    return max(eligible,key=lambda x:(x.year,x.month)) if eligible else None

@operations_bp.route('/commesse', methods=['GET','POST'])
@login_required
def orders():
    materials=Material.query.filter_by(active=True, material_type='FERT').order_by(Material.code).all()
    centers=CostCenter.query.order_by(CostCenter.code).all()
    if request.method=='POST':
        try:
            mat=Material.query.get(request.form.get('material_id',type=int)); qty=_dec(request.form.get('qty_planned'))
            if not mat or qty<=0: raise ValueError('Seleziona un prodotto finito e una quantità positiva.')
            po=ProductionOrder(order_number=DocumentSequence.next_number('OP','41'), material_id=mat.id, qty_planned=qty,
              order_date=datetime.strptime(request.form.get('order_date'),'%Y-%m-%d').date() if request.form.get('order_date') else datetime.utcnow().date(),
              cost_center_id=request.form.get('cost_center_id',type=int),notes=request.form.get('notes','').strip(),created_by_id=current_user.id)
            db.session.add(po); db.session.commit(); flash(f'Commessa {po.order_number} rilasciata. Nessuna scrittura FI alla sola apertura: il WIP nasce dai consuntivi.', 'success')
        except (ValueError,InvalidOperation) as e:
            db.session.rollback(); flash(str(e),'danger')
        return redirect(url_for('operations.orders'))
    return render_template('operations/orders.html', orders=ProductionOrder.query.order_by(ProductionOrder.id.desc()).all(), materials=materials, centers=centers)

@operations_bp.route('/commesse/<int:order_id>/prelievo', methods=['POST'])
@login_required
def issue(order_id):
    o=ProductionOrder.query.get_or_404(order_id)
    try:
        mat=Material.query.get(request.form.get('material_id',type=int)); qty=_dec(request.form.get('qty'))
        if not mat or qty<=0: raise ValueError('Articolo e quantità positiva sono obbligatori.')
        unit=_dec(request.form.get('unit_cost')) if request.form.get('unit_cost') else Decimal(str(mat.standard_cost))
        if unit<0: raise ValueError('Il costo unitario non può essere negativo.')
        value=(qty*unit).quantize(Decimal('0.01')); wip=_acc('157000'); inv=_acc(mat.inventory_account_code)
        je=post_journal_entry(doc_type='SA',prefix='10',doc_date=None,description=f'Prelievo {mat.code} per commessa {o.order_number}',lines=[
          {'account_id':wip.id,'dare':value,'avere':0,'description':f'WIP {o.order_number} — {mat.code}'},
          {'account_id':inv.id,'dare':0,'avere':value,'description':f'Prelievo magazzino {mat.code}'}],source_module='PRODUZIONE',reference=o.order_number,created_by_id=current_user.id)
        db.session.add(ProductionMaterialIssue(production_order_id=o.id,material_id=mat.id,qty=qty,unit_cost=unit,journal_entry_id=je.id)); o.status='in_lavorazione'; db.session.commit(); flash(f'Prelievo registrato: Dare WIP / Avere magazzino € {value:.2f}.','success')
    except (ValueError,UnbalancedEntryError) as e: db.session.rollback(); flash(str(e),'danger')
    return redirect(url_for('operations.orders'))

@operations_bp.route('/commesse/<int:order_id>/assorbimento', methods=['POST'])
@login_required
def absorb(order_id):
    o=ProductionOrder.query.get_or_404(order_id)
    try:
        typ=request.form.get('cost_type'); amount=_dec(request.form.get('amount'))
        if typ not in ('MOD','OVERHEAD') or amount<=0: raise ValueError('Tipo costo e importo positivo sono obbligatori.')
        wip=_acc('157000'); offset=_acc('472000' if typ=='MOD' else '473000')
        label='MOD assorbita' if typ=='MOD' else 'Overhead industriale assorbito'
        je=post_journal_entry(doc_type='SA',prefix='10',doc_date=None,description=f'{label} — commessa {o.order_number}',lines=[{'account_id':wip.id,'dare':amount,'avere':0,'description':f'WIP {o.order_number}'},{'account_id':offset.id,'dare':0,'avere':amount,'description':label}],source_module='PRODUZIONE',reference=o.order_number,created_by_id=current_user.id)
        db.session.add(ProductionCostAbsorption(production_order_id=o.id,cost_type=typ,amount=amount,journal_entry_id=je.id,notes=request.form.get('notes','').strip())); o.status='in_lavorazione'; db.session.commit(); flash(f'{label} registrato sul WIP: € {amount:.2f}.','success')
    except (ValueError,UnbalancedEntryError) as e: db.session.rollback(); flash(str(e),'danger')
    return redirect(url_for('operations.orders'))

@operations_bp.route('/commesse/<int:order_id>/versamento', methods=['POST'])
@login_required
def receipt(order_id):
    o=ProductionOrder.query.get_or_404(order_id)
    try:
        qty=_dec(request.form.get('qty_completed'))
        if qty<=0 or qty>Decimal(str(o.qty_planned)): raise ValueError('La quantità versata deve essere positiva e non superiore al pianificato.')
        actual=o.actual_wip
        if actual<=0: raise ValueError('Non è possibile versare PF: il WIP della commessa è zero.')
        std=_standard(o.material_id,datetime.utcnow().date())
        if not std: raise ValueError('Manca un costo standard applicabile al prodotto finito: definirlo prima del versamento.')
        # Material.standard_cost è letto da SD al PGI: lo manteniamo identico
        # allo standard della commessa, per COGM e COGS coerenti.
        o.material.standard_cost = Decimal(str(std.standard_total_unitario))
        standard_total=(qty*Decimal(str(std.standard_total_unitario))).quantize(Decimal('0.01'))
        fert=_acc(o.material.inventory_account_code); wip=_acc('157000'); variance=_acc('464000'); lines=[{'account_id':fert.id,'dare':standard_total,'avere':0,'description':f'Versamento PF standard {o.material.code} — {o.order_number}'}]
        diff=(actual-standard_total).quantize(Decimal('0.01'))
        if diff>0: lines.append({'account_id':variance.id,'dare':diff,'avere':0,'description':f'Varianza produzione sfavorevole {o.order_number}'})
        elif diff<0: lines.append({'account_id':variance.id,'dare':0,'avere':-diff,'description':f'Varianza produzione favorevole {o.order_number}'})
        lines.append({'account_id':wip.id,'dare':0,'avere':actual,'description':f'Chiusura WIP {o.order_number}'})
        je=post_journal_entry(doc_type='SA',prefix='10',doc_date=None,description=f'COGM / versamento PF {o.order_number}',lines=lines,source_module='PRODUZIONE',reference=o.order_number,created_by_id=current_user.id)
        o.status='completata'; db.session.commit(); flash(f'COGM registrato: PF € {standard_total:.2f}, WIP chiuso € {actual:.2f}, varianza € {diff:.2f}.','success')
    except (ValueError,UnbalancedEntryError) as e: db.session.rollback(); flash(str(e),'danger')
    return redirect(url_for('operations.orders'))

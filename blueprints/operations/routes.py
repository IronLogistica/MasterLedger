"""Ordini di produzione a commessa: WIP, costi standard e COGM."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user
from extensions import db
from models import (Account, Material, CostCenter, DocumentSequence, ProductionOrder,
                    ProductionMaterialIssue, ProductionCostAbsorption,
                    ProductionReceipt, StandardCost)
from services.posting import post_journal_entry, UnbalancedEntryError
from services.warehouse import post_stock_movement, WarehouseError

operations_bp = Blueprint('operations', __name__, template_folder='../../templates/operations')
CENT = Decimal('0.01')


def _acc(code):
    account = Account.query.filter_by(code=code).first()
    if not account:
        raise ValueError(f'Conto {code} non presente nel piano dei conti.')
    return account


def _dec(value):
    return Decimal(str(value or '0').replace(',', '.'))


def _qty(value):
    raw = _dec(value)
    normalized = raw.quantize(Decimal('0.001'))
    if raw != normalized:
        raise ValueError('La quantità può avere al massimo tre decimali.')
    return normalized


def _standard(material_id, posting_date):
    rows = StandardCost.query.filter_by(material_id=material_id).all()
    eligible = [x for x in rows if (x.year, x.month) <= (posting_date.year, posting_date.month)]
    return max(eligible, key=lambda x: (x.year, x.month)) if eligible else None


def _posting_date():
    raw = request.form.get('posting_date', '').strip()
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date() if raw else date.today()
    except ValueError:
        raise ValueError('Data contabile non valida.')


def _center(order):
    return order.cost_center_id or None


@operations_bp.route('/commesse', methods=['GET', 'POST'])
@login_required
def orders():
    materials = Material.query.filter_by(active=True).order_by(Material.code).all()
    centers = CostCenter.query.order_by(CostCenter.code).all()
    if request.method == 'POST':
        try:
            mat = Material.query.get(request.form.get('material_id', type=int))
            qty = _qty(request.form.get('qty_planned'))
            if not mat or mat.material_type != 'FERT' or qty <= 0:
                raise ValueError('Seleziona un prodotto finito e una quantità positiva.')
            po = ProductionOrder(
                order_number=DocumentSequence.next_number('OP', '41'), material_id=mat.id,
                qty_planned=qty,
                order_date=datetime.strptime(request.form.get('order_date'), '%Y-%m-%d').date()
                if request.form.get('order_date') else date.today(),
                cost_center_id=request.form.get('cost_center_id', type=int),
                notes=request.form.get('notes', '').strip(), created_by_id=current_user.id)
            db.session.add(po)
            db.session.commit()
            flash(f'Commessa {po.order_number} rilasciata. Nessuna scrittura FI alla sola apertura: il WIP nasce dai consuntivi.', 'success')
        except (ValueError, InvalidOperation) as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return redirect(url_for('operations.orders'))
    return render_template('operations/orders.html',
                           orders=ProductionOrder.query.order_by(ProductionOrder.id.desc()).all(),
                           materials=materials, centers=centers, today=date.today())


@operations_bp.route('/commesse/<int:order_id>/prelievo', methods=['POST'])
@login_required
def issue(order_id):
    try:
        posting_date = _posting_date()
        order = ProductionOrder.query.filter_by(id=order_id).with_for_update().first_or_404()
        if order.status == 'completata':
            raise ValueError(f'Commessa {order.order_number} già completata: non si possono registrare altri prelievi.')
        mat = Material.query.filter_by(id=request.form.get('material_id', type=int)).with_for_update().first()
        qty = _qty(request.form.get('qty'))
        if not mat or qty <= 0:
            raise ValueError('Articolo e quantità positiva sono obbligatori.')
        unit = _dec(request.form.get('unit_cost')) if request.form.get('unit_cost') else Decimal(str(mat.standard_cost))
        if unit < 0:
            raise ValueError('Il costo unitario non può essere negativo.')
        value = (qty * unit).quantize(CENT, rounding=ROUND_HALF_UP)
        if value <= 0:
            raise ValueError('Il prelievo deve avere un valore contabile positivo.')
        wip, inventory = _acc('157000'), _acc(mat.inventory_account_code)
        lines = [
            {'account_id': wip.id, 'dare': value, 'avere': 0,
             'cost_center_id': _center(order), 'description': f'WIP {order.order_number} — {mat.code}'},
            {'account_id': inventory.id, 'dare': 0, 'avere': value,
             'cost_center_id': _center(order), 'description': f'Prelievo magazzino {mat.code}'},
        ]
        entry = post_journal_entry(
            doc_type='SA', prefix='10', doc_date=posting_date, posting_date=posting_date,
            description=f'Prelievo {mat.code} per commessa {order.order_number}', lines=lines,
            source_module='PRODUZIONE', reference=order.order_number,
            created_by_id=current_user.id, commit=False)
        movement = post_stock_movement(
            material_id=mat.id, qty=-qty, movement_type='production_issue',
            source_type='production_order', source_id=order.id, unit_cost=unit,
            posting_value=value, doc_date=posting_date,
            notes=f'Prelievo commessa {order.order_number}', created_by_id=current_user.id)
        db.session.add(ProductionMaterialIssue(
            production_order_id=order.id, material_id=mat.id, qty=qty, unit_cost=unit,
            journal_entry_id=entry.id, stock_movement_id=movement.id, issue_date=posting_date))
        order.status = 'in_lavorazione'
        db.session.commit()
        flash(f'Prelievo registrato atomicamente: Dare WIP / Avere magazzino € {value:.2f}.', 'success')
    except (ValueError, InvalidOperation, UnbalancedEntryError, WarehouseError) as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    return redirect(url_for('operations.orders'))


@operations_bp.route('/commesse/<int:order_id>/assorbimento', methods=['POST'])
@login_required
def absorb(order_id):
    try:
        posting_date = _posting_date()
        order = ProductionOrder.query.filter_by(id=order_id).with_for_update().first_or_404()
        if order.status == 'completata':
            raise ValueError(f'Commessa {order.order_number} già completata: non si possono registrare altri assorbimenti.')
        typ, amount = request.form.get('cost_type'), _dec(request.form.get('amount'))
        if typ not in ('MOD', 'OVERHEAD') or amount <= 0:
            raise ValueError('Tipo costo e importo positivo sono obbligatori.')
        amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
        wip, offset = _acc('157000'), _acc('472000' if typ == 'MOD' else '473000')
        label = 'MOD assorbita' if typ == 'MOD' else 'Overhead industriale assorbito'
        entry = post_journal_entry(
            doc_type='SA', prefix='10', doc_date=posting_date, posting_date=posting_date,
            description=f'{label} — commessa {order.order_number}',
            lines=[
                {'account_id': wip.id, 'dare': amount, 'avere': 0,
                 'cost_center_id': _center(order), 'description': f'WIP {order.order_number}'},
                {'account_id': offset.id, 'dare': 0, 'avere': amount,
                 'cost_center_id': _center(order), 'description': label},
            ], source_module='PRODUZIONE', reference=order.order_number,
            created_by_id=current_user.id, commit=False)
        db.session.add(ProductionCostAbsorption(
            production_order_id=order.id, cost_type=typ, amount=amount,
            journal_entry_id=entry.id, posting_date=posting_date,
            notes=request.form.get('notes', '').strip()))
        order.status = 'in_lavorazione'
        db.session.commit()
        flash(f'{label} registrato sul WIP: € {amount:.2f}.', 'success')
    except (ValueError, InvalidOperation, UnbalancedEntryError) as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    return redirect(url_for('operations.orders'))


@operations_bp.route('/commesse/<int:order_id>/versamento', methods=['POST'])
@login_required
def receipt(order_id):
    try:
        posting_date = _posting_date()
        order = ProductionOrder.query.filter_by(id=order_id).with_for_update().first_or_404()
        if order.status == 'completata':
            raise ValueError(f'Commessa {order.order_number}: versamento finale già eseguito.')
        qty = _qty(request.form.get('qty_completed'))
        planned = Decimal(str(order.qty_planned))
        completed_before = order.qty_completed
        remaining_qty = (planned - completed_before).quantize(Decimal('0.001'))
        if qty <= 0 or qty > remaining_qty:
            raise ValueError(f'La quantità versata deve essere positiva e non superiore al residuo {remaining_qty}.')
        open_wip = order.actual_wip
        if open_wip <= 0:
            raise ValueError('Non è possibile versare PF: il WIP aperto della commessa è zero.')
        standard = _standard(order.material_id, posting_date)
        if not standard:
            raise ValueError('Manca un costo standard applicabile alla data contabile del versamento.')

        standard_unit = Decimal(str(standard.standard_total_unitario))
        standard_total = (qty * standard_unit).quantize(CENT, rounding=ROUND_HALF_UP)
        is_final = qty == remaining_qty
        # Un versamento parziale scarica soltanto la quota di WIP attribuibile
        # alla quantità completata. Il finale prende tutto il residuo, evitando
        # centesimi sospesi e lasciando la commessa riconciliata a zero.
        wip_relieved = (open_wip if is_final else
                        (open_wip * qty / remaining_qty).quantize(CENT, rounding=ROUND_HALF_UP))
        diff = (wip_relieved - standard_total).quantize(CENT)
        finished, wip, variance = (_acc(order.material.inventory_account_code),
                                   _acc('157000'), _acc('464000'))
        lines = [{'account_id': finished.id, 'dare': standard_total, 'avere': 0,
                  'cost_center_id': _center(order),
                  'description': f'Versamento PF standard {order.material.code} — {order.order_number}'}]
        if diff > 0:
            lines.append({'account_id': variance.id, 'dare': diff, 'avere': 0,
                          'cost_center_id': _center(order),
                          'description': f'Varianza produzione sfavorevole {order.order_number}'})
        elif diff < 0:
            lines.append({'account_id': variance.id, 'dare': 0, 'avere': -diff,
                          'cost_center_id': _center(order),
                          'description': f'Varianza produzione favorevole {order.order_number}'})
        lines.append({'account_id': wip.id, 'dare': 0, 'avere': wip_relieved,
                      'cost_center_id': _center(order),
                      'description': f'Scarico WIP {order.order_number}'})
        entry = post_journal_entry(
            doc_type='SA', prefix='10', doc_date=posting_date, posting_date=posting_date,
            description=f'COGM / versamento PF {order.order_number}', lines=lines,
            source_module='PRODUZIONE', reference=order.order_number,
            created_by_id=current_user.id, commit=False)
        movement = post_stock_movement(
            material_id=order.material_id, qty=qty, movement_type='production_receipt',
            source_type='production_order', source_id=order.id,
            unit_cost=(standard_total / qty).quantize(Decimal('0.0001')),
            posting_value=standard_total, doc_date=posting_date,
            notes=f'Versamento commessa {order.order_number}', created_by_id=current_user.id)
        db.session.add(ProductionReceipt(
            production_order_id=order.id, qty=qty, standard_value=standard_total,
            wip_relieved=wip_relieved, variance=diff, journal_entry_id=entry.id,
            stock_movement_id=movement.id, posting_date=posting_date,
            created_by_id=current_user.id))
        order.material.standard_cost = standard_unit
        order.status = 'completata' if is_final else 'in_lavorazione'
        db.session.commit()
        flash(f'COGM registrato: PF € {standard_total:.2f}, WIP scaricato € {wip_relieved:.2f}, '
              f'WIP residuo € {(open_wip-wip_relieved):.2f}, varianza € {diff:.2f}.', 'success')
    except (ValueError, InvalidOperation, UnbalancedEntryError, WarehouseError) as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    return redirect(url_for('operations.orders'))

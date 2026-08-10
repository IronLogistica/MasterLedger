import json
from decimal import Decimal, InvalidOperation
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from extensions import db
from models import (Account, CostCenter, PayrollAccountConfig, PayrollEmployeeMapping, PayrollImport,
                     F24ImuMapping, PayrollEmployeeAllocation, JournalEntry, JournalLine)
from services.payroll import (parse_payslips, parse_f24, fingerprint, PayrollParseError,
                              post_import, posted_payslip_allocations, parse_ratei, validate_percent_splits, mapping_splits, approved_payslip_splits,
                              default_worker_inps, default_employer_contribution, validate_payslip_breakdown)
from services.posting import post_journal_entry, UnbalancedEntryError
from blueprints.decorators import commercialista_required
payroll_bp=Blueprint('payroll',__name__,template_folder='../../templates/payroll')


def f24_allocation_context(data):
    """The review UI and POST path use the same, current posted-payslip source."""
    allocations, import_ids = posted_payslip_allocations(data.get('payroll_period'))
    return allocations, import_ids


@payroll_bp.route('/', methods=['GET'])
@login_required
def index():
    return render_template('payroll/index.html', imports=PayrollImport.query.order_by(PayrollImport.created_at.desc()).limit(50).all())

@payroll_bp.route('/upload',methods=['POST'])
@login_required
def upload():
    f=request.files.get('pdf_file'); kind=request.form.get('document_kind')
    if not f or not f.filename or kind not in ('PAYSLIP','F24','RATEI'):
        flash('Selezionare PDF e tipo documento.', 'danger'); return redirect(url_for('payroll.index'))
    payload=f.read()
    if not payload.startswith(b'%PDF'):
        flash('Il file caricato non è un PDF.', 'danger'); return redirect(url_for('payroll.index'))
    fp=fingerprint(payload)
    existing=PayrollImport.query.filter_by(fingerprint=fp).first()
    if existing:
        flash(f'Documento già importato (stato: {existing.status}).', 'warning'); return redirect(url_for('payroll.review', import_id=existing.id))
    try: parsed=parse_payslips(payload) if kind=='PAYSLIP' else parse_ratei(payload) if kind=='RATEI' else parse_f24(payload)
    except PayrollParseError as e:
        flash(str(e),'danger'); return redirect(url_for('payroll.index'))
    row=PayrollImport(document_kind=kind,filename=f.filename,fingerprint=fp,document_reference=parsed.get('period') or parsed.get('due_date'),parsed_data=json.dumps(parsed),created_by_id=current_user.id)
    db.session.add(row); db.session.commit()
    return redirect(url_for('payroll.review',import_id=row.id))

@payroll_bp.route('/<int:import_id>/review',methods=['GET','POST'])
@login_required
def review(import_id):
    row=PayrollImport.query.get_or_404(import_id)
    if row.status=='posted':
        flash('Documento già contabilizzato; la prima nota è immutabile.', 'warning'); return redirect(url_for('payroll.index'))
    data=json.loads(row.parsed_data)
    centers=CostCenter.query.filter_by(active=True).order_by(CostCenter.code).all()
    automatic_allocations, matched_import_ids = (f24_allocation_context(data)
                                                  if row.document_kind == 'F24' else ([], []))
    if request.method=='POST':
        if row.document_kind in ('PAYSLIP','RATEI'):
            employees=data['employees']
            for i,x in enumerate(employees):
                splits=[]
                for j in range(8):
                    cc=request.form.get(f'cc_{i}_{j}',type=int); pct=request.form.get(f'pct_{i}_{j}','').strip()
                    if cc or pct:
                        splits.append({'cost_center_id':cc,'percentage':pct})
                try: x['splits']=[{'cost_center_id':z['cost_center_id'],'percentage':str(z['percentage'])} for z in validate_percent_splits(splits)]
                except ValueError as e:
                    flash(f"{x['name']}: {e}",'danger'); return render_template('payroll/review.html',row=row,data=data,centers=centers)
                x['cost_center_id']=x['splits'][0]['cost_center_id'] # compatibility snapshot
                if row.document_kind=='PAYSLIP':
                    try:
                        worker_inps,employer_contribution=validate_payslip_breakdown(
                            request.form.get(f'workerinps_{i}','').strip(), x['deductions'],
                            request.form.get(f'employercontrib_{i}','').strip())
                    except ValueError as e:
                        flash(f"{x['name']}: {e}",'danger'); return render_template('payroll/review.html',row=row,data=data,centers=centers)
                    x['worker_inps_contribution']=str(worker_inps); x['employer_contribution']=str(employer_contribution)
                if row.document_kind=='PAYSLIP' and request.form.get('remember_'+str(i)):
                    mapping=PayrollEmployeeMapping.query.filter_by(employee_key=x['key']).first() or PayrollEmployeeMapping(employee_key=x['key'],employee_name=x['name'],cost_center_id=x['cost_center_id'])
                    mapping.employee_name=x['name'];mapping.cost_center_id=x['cost_center_id']; mapping.allocations[:]=[]
                    for z in x['splits']: mapping.allocations.append(PayrollEmployeeAllocation(cost_center_id=z['cost_center_id'],percentage=Decimal(z['percentage'])))
                    db.session.add(mapping)
            if row.document_kind=='RATEI':
                data['provisional_confirmed']=request.form.get('provisional_confirmed')=='1'; data['include_tfr']=request.form.get('include_tfr')=='1'
        else:
            # Only fields corresponding to parsed rows are accepted; all rows stay visible,
            # while additional F24 delegations require an explicit selection.
            for i, x in enumerate(data.get('lines', [])):
                x['selected'] = request.form.get('include_'+str(i)) == '1'
                if not x['selected']:
                    continue
                if x.get('classification') in ('imu', 'review'):
                    x['account_id'] = request.form.get('account_'+str(i), type=int)
                    x['cost_center_id'] = request.form.get('cc_'+str(i), type=int) or None
                    if x.get('classification') == 'imu' and request.form.get('remember_imu_'+str(i)):
                        valid_account=Account.query.filter_by(id=x['account_id'],active=True).first() if x['account_id'] else None
                        valid_center=CostCenter.query.filter_by(id=x['cost_center_id'],active=True).first() if x['cost_center_id'] else None
                        if not x.get('municipality_code') or not valid_account or not valid_center:
                            flash('Per memorizzare IMU servono codice comune leggibile, conto attivo e centro di costo attivo.', 'danger')
                            return render_template('payroll/review.html',row=row,data=data,centers=centers,accounts=Account.query.filter_by(active=True).order_by(Account.code).all(),automatic_allocations=automatic_allocations,matched_import_ids=matched_import_ids)
                        mapping=F24ImuMapping.query.filter_by(municipality_code=x['municipality_code'],tribute_code=x['code']).first() or F24ImuMapping(municipality_code=x['municipality_code'],tribute_code=x['code'],expense_account_id=x['account_id'],cost_center_id=x['cost_center_id'])
                        mapping.expense_account_id=x['account_id']; mapping.cost_center_id=x['cost_center_id']; db.session.add(mapping)
            if automatic_allocations:
                data['allocations']=automatic_allocations
                data['allocation_source']='posted_payslips'
                data['matched_payslip_import_ids']=matched_import_ids
            else:
                alloc=[]
                for cc in centers:
                    gross=request.form.get('allocation_'+str(cc.id),'').strip()
                    if gross:
                        try: amount=Decimal(gross)
                        except InvalidOperation:
                            flash('Allocazioni F24 non valide.','danger')
                            return render_template('payroll/review.html',row=row,data=data,centers=centers,accounts=Account.query.filter_by(active=True).order_by(Account.code).all(),automatic_allocations=automatic_allocations,matched_import_ids=matched_import_ids)
                        if not amount.is_finite() or amount < 0:
                            flash('Le allocazioni F24 non possono essere negative.','danger')
                            return render_template('payroll/review.html',row=row,data=data,centers=centers,accounts=Account.query.filter_by(active=True).order_by(Account.code).all(),automatic_allocations=automatic_allocations,matched_import_ids=matched_import_ids)
                        if amount > 0: alloc.append({'cost_center_id':cc.id,'gross':str(amount.quantize(Decimal('.01')))})
                # A manual allocation is needed only when selected payroll rows exist.
                if any(x.get('selected') and x.get('classification')=='payroll' for x in data.get('lines',[])):
                    if not alloc:
                        flash('Non risultano buste contabilizzate per il periodo: indicare un riparto manuale di fallback.','danger')
                        return render_template('payroll/review.html',row=row,data=data,centers=centers,accounts=Account.query.filter_by(active=True).order_by(Account.code).all(),automatic_allocations=automatic_allocations,matched_import_ids=matched_import_ids)
                    data['allocations']=alloc; data['allocation_source']='manual_fallback'; data.pop('matched_payslip_import_ids',None)
        row.parsed_data=json.dumps(data); db.session.commit()
        try: entry=post_import(row,data,current_user.id)
        except Exception as e:
            db.session.rollback(); flash(str(e),'danger')
            return render_template('payroll/review.html',row=row,data=data,centers=centers,accounts=Account.query.filter_by(active=True).order_by(Account.code).all(),cfg=PayrollAccountConfig.query.first(),
                                   automatic_allocations=automatic_allocations,matched_import_ids=matched_import_ids)
        flash(f'Contabilizzazione completata: {entry.doc_number}.','success'); return redirect(url_for('gl.entry_detail',entry_id=entry.id))
    cfg=PayrollAccountConfig.query.first()
    if row.document_kind in ('PAYSLIP','RATEI'):
        for x in data['employees']:
            # A posted monthly payslip is authoritative for ratei; mapping is the fallback.
            x['splits']=x.get('splits') or (approved_payslip_splits(data.get('payroll_period'),x['key']) if row.document_kind=='RATEI' else []) or mapping_splits(x['key'])
            if not x['splits'] and x.get('cost_center_id'): x['splits']=[{'cost_center_id':x['cost_center_id'],'percentage':'100.00'}]
            if row.document_kind=='PAYSLIP':
                # Solo precompilazione da aliquota configurata: l'importo definitivo resta sempre
                # quello che il commercialista conferma o corregge in revisione, riga per riga.
                x.setdefault('worker_inps_contribution', default_worker_inps(x['gross'], x['deductions'], cfg))
                x.setdefault('employer_contribution', default_employer_contribution(x['gross'], cfg))
    else:
        for x in data.get('lines',[]):
            if x.get('classification')=='imu':
                mapping=F24ImuMapping.query.filter_by(municipality_code=x.get('municipality_code',''),tribute_code=x.get('code','')).first()
                x['account_id']=x.get('account_id') or (mapping.expense_account_id if mapping else (cfg.imu_expense_account_id if cfg else None))
                x['cost_center_id']=x.get('cost_center_id') or (mapping.cost_center_id if mapping else None)
    return render_template('payroll/review.html',row=row,data=data,centers=centers,accounts=Account.query.filter_by(active=True).order_by(Account.code).all(),cfg=cfg,
                           automatic_allocations=automatic_allocations,matched_import_ids=matched_import_ids)

@payroll_bp.route('/pagamento-stipendi', methods=['GET', 'POST'])
@login_required
def salary_payment():
    """
    Pagamento netto stipendi — chiude via banca il debito v/dipendenti aperto
    dall'accantonamento buste paga (vedi services/payroll.py post_import,
    ramo PAYSLIP). Senza questo passaggio il Debito v/Dipendenti c/Retribuzioni
    resterebbe aperto per sempre: qui si genera la scrittura
        Dare  Debiti v/Dipendenti c/Retribuzioni
        Avere Banca c/c
    e si marcano le buste selezionate come pagate (stesso meccanismo
    is_paid/paid_by_entry_id già usato per le fatture fornitore in AP).
    """
    cfg = PayrollAccountConfig.query.first()
    if not cfg or not cfg.net_salary_payable_account_id or not cfg.bank_account_id:
        flash('Configura prima i conti "Debiti v/Dipendenti" e "Banca c/c" in Configurazione conti paghe.', 'danger')
        return redirect(url_for('payroll.config'))

    def _open_entries():
        entries = (JournalEntry.query
                   .join(JournalLine)
                   .filter(JournalEntry.is_paid.is_(False),
                           JournalEntry.is_reversed.is_(False),
                           JournalLine.account_id == cfg.net_salary_payable_account_id,
                           JournalLine.avere > 0)
                   .distinct()
                   .order_by(JournalEntry.doc_date)
                   .all())
        rows = []
        for e in entries:
            net_total = sum((l.avere for l in e.lines if l.account_id == cfg.net_salary_payable_account_id), Decimal('0'))
            if net_total > 0:
                rows.append((e, net_total))
        return rows

    if request.method == 'POST':
        selected_ids = request.form.getlist('entry_ids[]')
        if not selected_ids:
            flash('Seleziona almeno una busta paga da pagare.', 'warning')
            return redirect(url_for('payroll.salary_payment'))
        try:
            open_rows = {str(e.id): (e, net_total) for e, net_total in _open_entries()}
            total = Decimal('0')
            refs = []
            to_mark = []
            for eid in selected_ids:
                pair = open_rows.get(eid)
                if not pair:
                    continue
                entry, net_total = pair
                total += net_total
                refs.append(entry.doc_number)
                to_mark.append(entry)
            if total <= 0:
                flash('Nessun importo residuo da pagare sulle buste selezionate.', 'warning')
                return redirect(url_for('payroll.salary_payment'))

            lines = [
                {'account_id': cfg.net_salary_payable_account_id, 'dare': total, 'avere': 0},
                {'account_id': cfg.bank_account_id, 'dare': 0, 'avere': total},
            ]
            payment_entry = post_journal_entry(
                doc_type='KZ', prefix='15', doc_date=None,
                description=f"Pagamento netto stipendi — {', '.join(refs)}",
                lines=lines, source_module='PAGHE', reference=', '.join(refs),
                created_by_id=current_user.id, commit=False,
            )
            for entry in to_mark:
                entry.is_paid = True
                entry.paid_by_entry_id = payment_entry.id
            db.session.commit()
            flash(f'Pagamento stipendi registrato — Doc. {payment_entry.doc_number}, '
                  f'totale {float(total):.2f} € su {len(to_mark)} accantonamento/i.', 'success')
            return redirect(url_for('gl.entry_detail', entry_id=payment_entry.id))
        except (UnbalancedEntryError, ValueError) as e:
            db.session.rollback()
            flash(str(e), 'danger')
        return redirect(url_for('payroll.salary_payment'))

    return render_template('payroll/salary_payment.html', open_rows=_open_entries())


@payroll_bp.route('/config',methods=['GET','POST'])
@login_required
@commercialista_required
def config():
    cfg=PayrollAccountConfig.query.first() or PayrollAccountConfig()
    fields=['wage_expense_account_id','employer_burden_account_id','net_salary_payable_account_id','inps_payable_account_id','withholding_payable_account_id','bank_account_id','imu_expense_account_id','accrued_holiday_expense_account_id','accrued_permission_expense_account_id','accrued_thirteenth_expense_account_id','accrued_payable_account_id','tfr_expense_account_id','tfr_fund_account_id']
    rate_fields=['employee_inps_rate','employer_contribution_rate']
    if request.method=='POST':
        for field in fields:
            value=request.form.get(field,type=int)
            if value:
                acc=Account.query.get(value)
                if not acc or not acc.active: flash('Conto selezionato non valido/attivo.','danger'); return render_template('payroll/config.html',cfg=cfg,accounts=Account.query.filter_by(active=True).order_by(Account.code).all())
                expected = ('costo' if field in ('wage_expense_account_id','employer_burden_account_id','imu_expense_account_id','accrued_holiday_expense_account_id','accrued_permission_expense_account_id','accrued_thirteenth_expense_account_id','tfr_expense_account_id') else 'patrimoniale_attivo' if field == 'bank_account_id' else 'patrimoniale_passivo')
                if acc.account_type != expected:
                    flash(f'Il conto {acc.code} non ha natura coerente per questo campo (attesa: {expected}).', 'danger'); return render_template('payroll/config.html',cfg=cfg,accounts=Account.query.filter_by(active=True).order_by(Account.code).all())
            setattr(cfg,field,value)
        for field in rate_fields:
            raw=request.form.get(field,'').strip()
            if not raw: setattr(cfg,field,None); continue
            try: rate=Decimal(raw.replace(',','.'))
            except InvalidOperation:
                flash('Aliquota non valida: usare un numero (es. 9,19).','danger'); return render_template('payroll/config.html',cfg=cfg,accounts=Account.query.filter_by(active=True).order_by(Account.code).all())
            if rate<0 or rate>100:
                flash('Le aliquote devono essere comprese tra 0 e 100.','danger'); return render_template('payroll/config.html',cfg=cfg,accounts=Account.query.filter_by(active=True).order_by(Account.code).all())
            setattr(cfg,field,rate.quantize(Decimal('.01')))
        if not cfg.id: db.session.add(cfg)
        db.session.commit(); flash('Configurazione conti paghe salvata.','success'); return redirect(url_for('payroll.index'))
    return render_template('payroll/config.html',cfg=cfg,accounts=Account.query.filter_by(active=True).order_by(Account.code).all())

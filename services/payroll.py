"""Conservative PDF extraction and postings for Zucchetti payslips and all F24 sections."""
import hashlib, json, os, re, subprocess, tempfile
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from extensions import db
from models import Account, CostCenter, PayrollAccountConfig, PayrollImport, PayrollEmployeeMapping, PayrollEmployeeAllocation
from services.posting import post_journal_entry

class PayrollParseError(ValueError): pass
ITALIAN_MONTHS={'gennaio':1,'febbraio':2,'marzo':3,'aprile':4,'maggio':5,'giugno':6,'luglio':7,'agosto':8,'settembre':9,'ottobre':10,'novembre':11,'dicembre':12}

def payroll_period(value):
    if not value:return None
    value=str(value).strip(); m=re.search(r'\b(20\d{2})[-/](0[1-9]|1[0-2])\b',value)
    if m:return f'{m.group(1)}-{m.group(2)}'
    m=re.search(r'\b\d{2}[/.-](0[1-9]|1[0-2])[/.-](20\d{2})\b',value)
    if m:return f'{m.group(2)}-{m.group(1)}'
    m=re.search(r'\b(0[1-9]|1[0-2])\s*[/.-]\s*(20\d{2})\b',value)
    if m:return f'{m.group(2)}-{m.group(1)}'
    m=re.search(r'\b('+ '|'.join(ITALIAN_MONTHS)+r')\s+(20\d{2})\b',value,re.I)
    return f'{m.group(2)}-{ITALIAN_MONTHS[m.group(1).lower()]:02d}' if m else None

def f24_payroll_period(text,due_date=''):
    candidates=[]
    for raw in text.splitlines():
        if re.search(r'\b(?:1001|1012|1701|1704|INPS)\b',raw,re.I):
            period=payroll_period(raw)
            if period:candidates.append(period);continue
            m=re.search(r'\b(0[1-9]|1[0-2])\s+(20\d{2})\b',raw)
            if m:candidates.append(f'{m.group(2)}-{m.group(1)}')
    if candidates:return max(set(candidates),key=candidates.count)
    due=payroll_period(due_date)
    if due:
        y,m=map(int,due.split('-')); return f'{y-(m==1):04d}-{12 if m==1 else m-1:02d}'

def posted_payslip_allocations(period):
    if not period:return [],[]
    totals={}; ids=[]
    for row in PayrollImport.query.filter_by(document_kind='PAYSLIP',status='posted').all():
        try:data=json.loads(row.parsed_data)
        except (TypeError,ValueError):continue
        if payroll_period(data.get('payroll_period') or data.get('period') or row.document_reference)!=period:continue
        local={}
        for employee in data.get('employees',[]):
            try: cc=int(employee.get('cost_center_id')); gross=Decimal(str(employee.get('gross',0)))
            except (TypeError,ValueError,InvalidOperation):continue
            if cc and gross.is_finite() and gross>0:local[cc]=local.get(cc,Decimal('0'))+gross
        if local:
            ids.append(row.id)
            for cc,gross in local.items():totals[cc]=totals.get(cc,Decimal('0'))+gross
    return ([{'cost_center_id':cc,'gross':str(g.quantize(Decimal('.01')))} for cc,g in sorted(totals.items())],ids)

def money(value):
    value=re.sub(r'[^0-9,.-]','',value or '').replace('.','').replace(',','.')
    return Decimal(value).quantize(Decimal('.01'),rounding=ROUND_HALF_UP) if value else Decimal('0')
def pdf_text(payload):
    with tempfile.NamedTemporaryFile(suffix='.pdf',delete=False) as f:f.write(payload);name=f.name
    try:
        p=subprocess.run(['pdftotext','-layout',name,'-'],text=True,capture_output=True,timeout=45)
        if p.returncode:raise PayrollParseError('PDF non leggibile: '+(p.stderr.strip() or 'pdftotext ha fallito'))
        return p.stdout
    finally:os.unlink(name)

def parse_payslips(payload):
    text=pdf_text(payload); employees=[]
    for c in re.split(r'(?=Codicesdipendente)',text):
        m=re.search(r"Codicesdipendente[\s\S]{0,400}?\n\s*(\d+)\s+([A-Z][A-Z ']+?)\s+([A-Z0-9]{16})",c)
        gross=re.search(r'TOTALEsCOMPETENZE\s+([\d.,]+)',c); net=re.search(r'NETTOsDELsMESE\s*\n\s*([\d.,]+)',c); deductions=re.search(r'TOTALEsTRATTENUTE\s+([\d.,]+)',c)
        if not(m and gross and net):continue
        employees.append({'key':m.group(3) or m.group(1),'code':m.group(1),'name':' '.join(m.group(2).split()),'gross':str(money(gross.group(1))),'net':str(money(net.group(1))),'deductions':str(money(deductions.group(1))) if deductions else str(money(gross.group(1))-money(net.group(1)))})
    if not employees:raise PayrollParseError('Nessuna busta Zucchetti con totale competenze/netto riconosciuta.')
    p=re.search(r'(?:Maggio|Giugno|Luglio|Agosto|Settembre|Ottobre|Novembre|Dicembre|Gennaio|Febbraio|Marzo|Aprile)\s+20\d{2}',text,re.I)
    return {'period':p.group(0) if p else '','payroll_period':payroll_period(p.group(0)) if p else None,'employees':employees}

SECTION_MARKERS=(
 ('SEZIONE ERARIO','ERARIO'),('SEZIONE INPS','INPS'),('SEZIONE REGIONI','REGIONI'),
 ('SEZIONE IMU E ALTRI TRIBUTI LOCALI','IMU'),('SEZIONE ALTRI ENTI PREVIDENZIALI E ASSICURATIVI','ALTRI_ENTI'))
AMOUNT_RE=re.compile(r'(?<!\d)\d{1,3}(?:\.\d{3})*\s*,\s*\d{2}(?!\d)')

def _f24_line(raw,section,group):
    if not raw.strip() or 'TOTALE' in raw or 'importi a ' in raw.lower():return None
    amounts=list(AMOUNT_RE.finditer(raw))
    if not amounts:return None
    # A line always contains a tribute/contribution code before the money columns.
    codes=list(re.finditer(r'\b\d{4}\b',raw))
    if not codes:return None
    code=codes[0].group(0)
    # For ERARIO 0005/2026 follow a tribute code, while in all real layouts first 4 digits are code.
    # Ignore blank money placeholders: one printed amount is debit unless it is visibly in credit column.
    values=[money(x.group(0)) for x in amounts]
    debit=values[0] if values else Decimal('0'); credit=values[1] if len(values)>1 else Decimal('0')
    before=raw[:amounts[0].start()].rstrip()
    if len(values)==1 and before.endswith(','): debit,credit=Decimal('0'),values[0]
    year_match=re.search(r'\b(20\d{2})\b',raw)
    year=year_match.group(1) if year_match else ''
    ref=''
    if section=='INPS':
        period=re.search(r'\b(0[1-9]|1[0-2])\s+(20\d{2})\b',raw)
        ref=f'{period.group(1)}/{period.group(2)}' if period else ''
    else:
        after=raw[code and raw.find(code)+len(code):year_match.start() if year_match else amounts[0].start()]
        nums=re.findall(r'\b\d{1,4}\b',after); ref=nums[-1].zfill(4) if nums else ''
    municipality=''
    if section=='IMU':
        m=re.search(r'\b([A-Z])\s*(\d)\s*(\d)\s*(\d)\b',raw)
        municipality=''.join(m.groups()) if m else ''
    kind='payroll' if (section=='INPS' or section=='REGIONI' or (section=='ERARIO' and code in ('1001','1012','1701','1704'))) else ('imu' if section=='IMU' else 'review')
    return {'code':code,'section':section,'classification':kind,'debit':str(debit),'credit':str(credit),'year':year,'reference':ref,'municipality_code':municipality,'payment_group':group}

def parse_f24(payload):
    text=pdf_text(payload); lines=[]; payments=[]
    # A PDF can contain several F24 delegations. Keep every row and its delegation; never cut at IMU.
    for group,page in enumerate(text.split('\f')):
        if 'MODELLO DI PAGAMENTO' not in page:continue
        section=None
        for raw in page.splitlines():
            for marker,name in SECTION_MARKERS:
                if marker in raw:section=name;break
            else:
                if section:
                    item=_f24_line(raw,section,group)
                    if item:lines.append(item)
        m=re.search(r'SALDO FINALE[\s\S]{0,500}?EURO\s*[+\-]?\s*([\d.]+\s*,\s*\d{2})',page,re.I)
        payments.append({'group':group,'net_total':str(money(m.group(1))) if m else None})
    if not lines:raise PayrollParseError('Nessuna riga F24 riconosciuta: verificare che il PDF contenga sezioni e importi leggibili.')
    due=re.search(r'Scadenza\s+(\d{2}/\d{2}/\d{4})',text); due_date=due.group(1) if due else ''
    primary=next((p for p in payments if p['net_total'] is not None),None)
    # Default to the first delegation: extra delegations remain visible and must be explicitly selected in review.
    return {'due_date':due_date,'payroll_period':f24_payroll_period(text,due_date),'lines':lines,'payments':payments,'net_total':primary['net_total'] if primary else None,'default_payment_group':primary['group'] if primary else 0}

def validate_percent_splits(splits):
    """Validate positive unique centres whose rounded percentage total is exactly 100.00."""
    cleaned=[]; seen=set()
    for x in splits:
        try: cc=int(x['cost_center_id']); pct=Decimal(str(x['percentage'])).quantize(Decimal('.01'))
        except (KeyError, TypeError, ValueError, InvalidOperation): raise ValueError('Riparto per centro non valido.')
        if cc in seen or pct <= 0 or not CostCenter.query.filter_by(id=cc,active=True).first(): raise ValueError('Centri del riparto duplicati, inattivi o percentuali non positive.')
        seen.add(cc); cleaned.append({'cost_center_id':cc,'percentage':pct})
    if not cleaned or sum(x['percentage'] for x in cleaned) != Decimal('100.00'): raise ValueError('Il totale delle percentuali deve essere esattamente 100,00%.')
    return cleaned

def allocate_percent(amount, splits):
    splits=validate_percent_splits(splits); amount=Decimal(str(amount)); out=[]; remain=amount
    for i,x in enumerate(splits):
        value=remain if i==len(splits)-1 else (amount*x['percentage']/100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        remain-=value; out.append((x['cost_center_id'],value))
    return out

def validate_document_splits(splits):
    """Shared validator for AllocationSplit rows (AP/AR integration can call this safely)."""
    return validate_percent_splits(splits)

def allocate_document_amount(amount, splits):
    return allocate_percent(amount, splits)

def mapping_splits(employee_key):
    m=PayrollEmployeeMapping.query.filter_by(employee_key=employee_key).first()
    if not m:return []
    rows=[{'cost_center_id':x.cost_center_id,'percentage':str(x.percentage)} for x in m.allocations]
    return rows or [{'cost_center_id':m.cost_center_id,'percentage':'100.00'}]

def approved_payslip_splits(period, employee_key):
    """Return the immutable reviewed split of this employee's posted payslip, if available."""
    for row in PayrollImport.query.filter_by(document_kind='PAYSLIP', status='posted').order_by(PayrollImport.posted_at.desc()).all():
        try: data=json.loads(row.parsed_data)
        except (TypeError, ValueError): continue
        if payroll_period(data.get('payroll_period') or data.get('period') or row.document_reference) != period: continue
        for employee in data.get('employees',[]):
            if employee.get('key') == employee_key:
                return employee.get('splits') or ([{'cost_center_id':employee.get('cost_center_id'),'percentage':'100.00'}] if employee.get('cost_center_id') else [])
    return []

def parse_ratei(payload):
    """Fixed-column Zucchetti 'Prospetto mensile incremento oneri differiti'.
    Employee starts only match an 7-digit code, never I.N.A.I.L. continuation rows.
    """
    text=pdf_text(payload); period=payroll_period(text); employees=[]; current=None
    columns=[('amount',78,99),('contributions',99,117),('inail',117,137),('funds',137,156),('treasury',156,169),('revaluation',169,188),('substitute_tax',188,207),('tfr_on_accruals',207,229)]
    def amounts(raw):
        return {name:(str(money(m.group(0))) if (m:=re.search(r'-?\d{1,3}(?:\.\d{3})*,\d{2}',raw[a:b])) else '0.00') for name,a,b in columns}
    for raw in text.splitlines():
        m=re.match(r'^\s*(\d{7})\s+(.+?)\s+(F0[123]|M01)\s+(.+?)\s{2,}',raw)
        if m:
            code,name,kind,label=m.groups(); current={'key':code,'code':code,'name':' '.join(name.split()),'rows':[]}; employees.append(current)
            item=amounts(raw); item.update(kind=kind,label=label.strip()); current['rows'].append(item); continue
        if current and re.match(r'^\s+(?:F0[123]|M01)\s+',raw):
            m=re.match(r'^\s+(F0[123]|M01)\s+(.+?)\s{2,}',raw)
            if m:
                item=amounts(raw);item.update(kind=m.group(1),label=m.group(2).strip());current['rows'].append(item)
        elif current and re.match(r'^\s+I\.N\.A\.I\.L',raw):
            item=amounts(raw);item.update(kind='INAIL',label='I.N.A.I.L.');current['rows'].append(item)
        elif current and re.match(r'^\s+T\.F\.R\.',raw):
            item=amounts(raw);item.update(kind='TFR',label='T.F.R.');current['rows'].append(item)
    if not employees: raise PayrollParseError('Nessun dettaglio dipendente ratei Zucchetti riconosciuto.')
    return {'period':period or '', 'payroll_period':period, 'employees':employees, 'provisional':True,
            'note':'INAIL puro è conservato per controllo e non viene rilevato come costo; ratei contabilizzabili: ferie, permessi, 13a e contribuzioni.'}

def fingerprint(payload):return hashlib.sha256(payload).hexdigest()
def ensure_config(config,fields):
    missing=[label for field,label in fields if not getattr(config,field,None)]
    if missing:raise ValueError('Configurare i conti paghe mancanti: '+', '.join(missing))
    for field,_ in fields:
        if not getattr(config,field).active:raise ValueError(f'Il conto configurato {getattr(config,field).code} non è attivo.')
def allocate(amount,allocations):
    if amount<0:raise ValueError('Importo F24 negativo non ammesso.')
    cleaned=[]
    for x in allocations:
        try:cc=int(x['cost_center_id']);gross=Decimal(str(x['gross']))
        except (KeyError,TypeError,ValueError,InvalidOperation):raise ValueError('Allocazioni F24 non valide.')
        if cc and gross.is_finite() and gross>0:cleaned.append((cc,gross))
    total=sum((x[1] for x in cleaned),Decimal('0'))
    if not cleaned or total<=0:raise ValueError('La base di riparto F24 deve contenere importi positivi.')
    result=[];remain=amount
    for i,(cc,gross) in enumerate(cleaned):
        v=remain if i==len(cleaned)-1 else (amount*gross/total).quantize(Decimal('.01'),rounding=ROUND_HALF_UP);remain-=v;result.append((cc,v))
    return result

def _validated_account(account_id):
    try:account_id=int(account_id)
    except (TypeError,ValueError):raise ValueError('Selezionare un conto attivo per ogni riga F24 da contabilizzare.')
    account=Account.query.get(account_id)
    if not account or not account.active:raise ValueError('Il conto selezionato non è valido o non è attivo.')
    return account

def post_import(import_row,reviewed,user_id):
    if import_row.status=='posted':raise ValueError('Documento già contabilizzato.')
    cfg=PayrollAccountConfig.query.first()
    if not cfg:raise ValueError('Configurazione conti paghe assente.')
    if import_row.document_kind=='PAYSLIP':
        ensure_config(cfg,[('wage_expense_account','costo retribuzioni'),('net_salary_payable_account','debiti retribuzioni'),('withholding_payable_account','debiti ritenute')]); lines=[]
        for x in reviewed['employees']:
            gross,net,ded=(Decimal(str(x[k])) for k in ('gross','net','deductions'))
            if min(gross,net,ded)<0 or abs(gross-net-ded)>Decimal('.02'):raise ValueError('Busta non quadrata o con importo negativo.')
            splits=x.get('splits') or ([{'cost_center_id':x.get('cost_center_id'),'percentage':'100'}] if x.get('cost_center_id') else [])
            for cc,amount in allocate_percent(gross,splits): lines.append({'account_id':cfg.wage_expense_account_id,'dare':amount,'avere':0,'cost_center_id':cc,'description':x['name']})
            for cc,amount in allocate_percent(net,splits): lines.append({'account_id':cfg.net_salary_payable_account_id,'dare':0,'avere':amount,'cost_center_id':cc,'description':'Netto '+x['name']})
            for cc,amount in allocate_percent(ded,splits): lines.append({'account_id':cfg.withholding_payable_account_id,'dare':0,'avere':amount,'cost_center_id':cc,'description':'Trattenute '+x['name']})
        dt=date.today();desc='Accantonamento paghe '+reviewed.get('period','')
    elif import_row.document_kind=='RATEI':
        if reviewed.get('provisional') and not reviewed.get('provisional_confirmed'): raise ValueError('Rateo provvisorio: spuntare la conferma esplicita prima della contabilizzazione.')
        ensure_config(cfg,[('accrued_holiday_expense_account','costo ratei ferie'),('accrued_permission_expense_account','costo ratei permessi'),('accrued_thirteenth_expense_account','costo rateo tredicesima'),('accrued_payable_account','debiti ratei')])
        accounts={'F01':cfg.accrued_holiday_expense_account_id,'F02':cfg.accrued_permission_expense_account_id,'F03':cfg.accrued_permission_expense_account_id,'M01':cfg.accrued_thirteenth_expense_account_id}; lines=[]
        for employee in reviewed.get('employees',[]):
            splits=employee.get('splits') or mapping_splits(employee.get('key'))
            for item in employee.get('rows',[]):
                if item['kind']=='TFR' and reviewed.get('include_tfr'):
                    if not cfg.tfr_expense_account_id or not cfg.tfr_fund_account_id: raise ValueError('TFR selezionato: configurare costo TFR e fondo TFR.')
                    account, payable=cfg.tfr_expense_account_id,cfg.tfr_fund_account_id
                elif item['kind'] in accounts:
                    account, payable=accounts[item['kind']],cfg.accrued_payable_account_id
                else: continue  # INAIL puro: controllo, mai duplicato come costo
                amount=sum((Decimal(str(item.get(k,'0'))) for k in ('amount','contributions')),Decimal('0'))
                for cc,value in allocate_percent(amount,splits):
                    lines += [{'account_id':account,'dare':value,'avere':0,'cost_center_id':cc,'description':item['kind']+' '+employee['name']},{'account_id':payable,'dare':0,'avere':value,'cost_center_id':cc,'description':'Debito rateo '+item['kind']+' '+employee['name']}]
        dt=date.today();desc='Ratei differiti '+reviewed.get('period','')
    else:
        ensure_config(cfg,[('inps_payable_account','debiti INPS'),('withholding_payable_account','debiti ritenute'),('bank_account','banca')])
        selected=[x for x in reviewed.get('lines',[]) if x.get('selected')]
        if not selected:raise ValueError('Selezionare almeno una riga F24: nessuna riga può essere esclusa silenziosamente.')
        payroll=[x for x in selected if x.get('classification')=='payroll']
        if payroll:
            auto,ids=posted_payslip_allocations(reviewed.get('payroll_period'))
            if auto:allocations=auto;reviewed.update(allocations=auto,allocation_source='posted_payslips',matched_payslip_import_ids=ids)
            else:
                allocations=reviewed.get('allocations',[])
                if reviewed.get('allocation_source')!='manual_fallback' or not allocations:raise ValueError('Per le righe payroll manca il riparto da buste contabilizzate o il fallback manuale.')
        lines=[];bank=Decimal('0')
        for x in selected:
            debit,credit=Decimal(str(x['debit'])),Decimal(str(x['credit'])); cls=x.get('classification')
            if cls=='payroll':
                account_id=cfg.inps_payable_account_id if x['section']=='INPS' else cfg.withholding_payable_account_id
                for cc,amount in allocate(debit,allocations):lines.append({'account_id':account_id,'dare':amount,'avere':0,'cost_center_id':cc,'description':'F24 '+x['code']}) if amount else None
                for cc,amount in allocate(credit,allocations):lines.append({'account_id':account_id,'dare':0,'avere':amount,'cost_center_id':cc,'description':'Credito F24 '+x['code']}) if amount else None
            else:
                account=_validated_account(x.get('account_id')); cc=x.get('cost_center_id')
                if cls=='imu' and (not cc or not CostCenter.query.filter_by(id=cc,active=True).first()):raise ValueError(f"IMU {x['code']}: conto spesa e centro di costo attivo sono obbligatori.")
                if cc and not CostCenter.query.filter_by(id=cc,active=True).first():raise ValueError('Centro di costo selezionato non valido o non attivo.')
                lines.append({'account_id':account.id,'dare':debit,'avere':credit,'cost_center_id':cc or None,'description':f"F24 {x['section']} {x['code']}"})
            bank+=debit-credit
        if not bank:raise ValueError('Totale netto F24 nullo: non è possibile generare una riga banca significativa.')
        lines.append({'account_id':cfg.bank_account_id,'dare':max(-bank,Decimal('0')),'avere':max(bank,Decimal('0')),'description':'Pagamento F24 totale selezionato'})
        dt=date.today();desc='Pagamento F24 '+reviewed.get('due_date','')
    entry=post_journal_entry('PG','PG',dt,desc,lines,source_module='PAGHE',reference=import_row.fingerprint[:16],created_by_id=user_id,commit=False)
    import_row.status='posted';import_row.journal_entry_id=entry.id;import_row.parsed_data=json.dumps(reviewed);import_row.posted_at=datetime.utcnow();db.session.commit();return entry

"""Verifica che l'accantonamento buste paga e il pagamento F24 si riconcilino
esattamente sui conti INPS ed Erario, senza residui — il difetto che questo
modulo corregge: prima, l'intera voce "trattenute" andava su un unico conto
mentre l'F24 pagava sezione INPS e sezione Erario su due conti diversi,
lasciando "Debiti INPS" permanentemente scoperto e senza mai contabilizzare
gli oneri sociali a carico azienda."""
from decimal import Decimal
import json
import pytest

from extensions import db
from models import Account, CostCenter, PayrollAccountConfig, PayrollImport
from services.payroll import post_import, validate_payslip_breakdown, default_worker_inps, default_employer_contribution
from services.posting import post_journal_entry


def _acc(code, name, typ):
    acc = Account.query.filter_by(code=code).first()
    if not acc:
        acc = Account(code=code, name=name, account_type=typ)
        db.session.add(acc)
        db.session.flush()
    return acc


@pytest.fixture()
def payroll_cfg(app):
    with app.app_context():
        wage = _acc("601000", "Costo retribuzioni", "costo")
        burden = _acc("602000", "Oneri sociali azienda", "costo")
        net_payable = _acc("221000", "Debiti v/dipendenti", "patrimoniale_passivo")
        inps_payable = _acc("222000", "Debiti INPS", "patrimoniale_passivo")
        withholding = _acc("223000", "Debiti ritenute erariali", "patrimoniale_passivo")
        bank = _acc("180000", "Banca", "patrimoniale_attivo") if not Account.query.filter_by(code="180000").first() else Account.query.filter_by(code="180000").one()
        cfg = PayrollAccountConfig(
            wage_expense_account_id=wage.id, employer_burden_account_id=burden.id,
            net_salary_payable_account_id=net_payable.id, inps_payable_account_id=inps_payable.id,
            withholding_payable_account_id=withholding.id, bank_account_id=bank.id,
        )
        db.session.add(cfg); db.session.commit()
        return dict(wage=wage.id, burden=burden.id, net_payable=net_payable.id,
                    inps_payable=inps_payable.id, withholding=withholding.id, bank=bank.id, cfg_id=cfg.id)


def _import_row(app, employees):
    with app.app_context():
        row = PayrollImport(document_kind="PAYSLIP", filename="test.pdf", fingerprint="fp-" + employees[0]["key"],
                            document_reference="2026-01", parsed_data=json.dumps({"period": "Gennaio 2026", "payroll_period": "2026-01", "employees": employees}))
        db.session.add(row); db.session.commit()
        return row.id


def test_payslip_splits_inps_from_erario_and_posts_employer_burden(app, payroll_cfg, cost_center):
    with app.app_context():
        cc = cost_center()
        employees = [{
            "key": "RSSMRA80A01H501Z", "code": "1", "name": "Mario Rossi",
            "gross": "2000.00", "net": "1550.00", "deductions": "450.00",
            "worker_inps_contribution": "183.80",   # 9.19% di 2000
            "employer_contribution": "600.00",       # 30% di 2000
            "cost_center_id": cc.id, "splits": [{"cost_center_id": cc.id, "percentage": "100.00"}],
        }]
        row_id = _import_row(app, employees)
        row = PayrollImport.query.get(row_id)
        data = json.loads(row.parsed_data)
        entry = post_import(row, data, user_id=None)
        db.session.commit()

        lines = {l.account_id: l for l in entry.lines}
        # Quadratura formale, sempre garantita da post_journal_entry
        assert entry.total_dare == entry.total_avere

        wage_line = next(l for l in entry.lines if l.account_id == payroll_cfg["wage"])
        assert wage_line.dare == Decimal("2000.00")

        net_line = next(l for l in entry.lines if l.account_id == payroll_cfg["net_payable"])
        assert net_line.avere == Decimal("1550.00")

        # La quota INPS dipendente (dentro le trattenute) va sul conto INPS...
        inps_lines = [l for l in entry.lines if l.account_id == payroll_cfg["inps_payable"]]
        inps_worker = next(l for l in inps_lines if "dipendente" in l.description)
        assert inps_worker.avere == Decimal("183.80")

        # ...il resto delle trattenute (450 - 183.80 = 266.20) va su Erario, non su INPS
        withholding_line = next(l for l in entry.lines if l.account_id == payroll_cfg["withholding"])
        assert withholding_line.avere == Decimal("266.20")

        # Gli oneri sociali azienda sono un COSTO aggiuntivo (mai nel lordo busta)
        # con contropartita sullo stesso conto Debiti INPS
        burden_line = next(l for l in entry.lines if l.account_id == payroll_cfg["burden"])
        assert burden_line.dare == Decimal("600.00")
        inps_employer = next(l for l in inps_lines if "azienda" in l.description)
        assert inps_employer.avere == Decimal("600.00")

        # Il totale che finirà a debito di "Debiti INPS" (dipendente + azienda) è quanto
        # l'F24 dovrà poi pareggiare esattamente in sede di pagamento.
        total_inps_credited = sum(l.avere for l in inps_lines)
        assert total_inps_credited == Decimal("783.80")


def test_worker_inps_cannot_exceed_total_deductions():
    with pytest.raises(ValueError, match="non può superare"):
        validate_payslip_breakdown(worker_inps="500.00", deductions="450.00", employer_contribution="0")


def test_negative_amounts_rejected():
    with pytest.raises(ValueError, match="negativ"):
        validate_payslip_breakdown(worker_inps="-1", deductions="450.00", employer_contribution="0")


def test_default_prefill_uses_configured_rates(app, payroll_cfg):
    with app.app_context():
        cfg = PayrollAccountConfig.query.get(payroll_cfg["cfg_id"])
        cfg.employee_inps_rate = Decimal("9.19")
        cfg.employer_contribution_rate = Decimal("30.00")
        db.session.commit()
        assert default_worker_inps("2000.00", "450.00", cfg) == "183.80"
        assert default_employer_contribution("2000.00", cfg) == "600.00"
        # Il prefill non supera mai le trattenute reali del cedolino, anche se l'aliquota lo farebbe
        assert default_worker_inps("2000.00", "100.00", cfg) == "100.00"


def test_employer_burden_account_required_only_if_amount_positive(app, payroll_cfg, cost_center):
    """Se l'importo oneri azienda è zero, il conto oneri datoriali non deve essere
    obbligatorio: niente blocchi inutili quando quel costo non è stato valorizzato."""
    with app.app_context():
        cfg = PayrollAccountConfig.query.get(payroll_cfg["cfg_id"])
        cfg.employer_burden_account_id = None
        db.session.commit()
        cc = cost_center()
        employees = [{
            "key": "K2", "code": "2", "name": "Anna Bianchi",
            "gross": "1000.00", "net": "800.00", "deductions": "200.00",
            "worker_inps_contribution": "91.90", "employer_contribution": "0.00",
            "cost_center_id": cc.id, "splits": [{"cost_center_id": cc.id, "percentage": "100.00"}],
        }]
        row_id = _import_row(app, employees)
        row = PayrollImport.query.get(row_id)
        data = json.loads(row.parsed_data)
        entry = post_import(row, data, user_id=None)
        assert entry.total_dare == entry.total_avere

"""Invio e-mail delle richieste di preventivo."""
import smtplib
from email.message import EmailMessage
from flask import current_app

class RfqDeliveryError(Exception):
    pass

def send_rfq_email(rfq, supplier):
    cfg = current_app.config
    host, sender = cfg.get("RFQ_SMTP_HOST"), cfg.get("RFQ_FROM_EMAIL")
    if not host or not sender:
        raise RfqDeliveryError("Canale e-mail RFQ non configurato: impostare RFQ_SMTP_HOST e RFQ_FROM_EMAIL.")
    recipient = (supplier.email or supplier.pec or "").strip()
    if not recipient:
        raise RfqDeliveryError(f"Il fornitore {supplier.name} non ha e-mail/PEC in anagrafica.")
    msg = EmailMessage()
    msg["Subject"] = f"Richiesta di preventivo {rfq.rfq_number} — {rfq.material.code}"
    msg["From"] = f'{cfg.get("RFQ_FROM_NAME")} <{sender}>'
    msg["To"] = recipient
    msg.set_content(f"""Spett.le {supplier.name},

richiediamo il Vostro miglior preventivo per il seguente materiale:

RFQ: {rfq.rfq_number}
Codice: {rfq.material.code}
Descrizione: {rfq.material.description}
Quantità richiesta: {rfq.qty} {rfq.material.uom}
Consegna richiesta: {rfq.required_date or 'da definire'}
Note: {rfq.notes or '—'}

Indicare prezzo unitario, disponibilità, tempi di consegna e validità dell'offerta.

Cordiali saluti,
{cfg.get("RFQ_FROM_NAME")}
""")
    try:
        with smtplib.SMTP(host, cfg.get("RFQ_SMTP_PORT", 587), timeout=20) as smtp:
            if cfg.get("RFQ_SMTP_USE_TLS", True): smtp.starttls()
            if cfg.get("RFQ_SMTP_USERNAME"): smtp.login(cfg["RFQ_SMTP_USERNAME"], cfg.get("RFQ_SMTP_PASSWORD", ""))
            smtp.send_message(msg)
    except Exception as exc:
        raise RfqDeliveryError(str(exc)) from exc
    return recipient

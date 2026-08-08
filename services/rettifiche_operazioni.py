"""services/rettifiche_operazioni.py — Catalogo "Sezione 13: Rettifiche e
assestamenti" per Iron Appalti Srl.

Fonte: documento del commercialista (sezione_13_rettifiche_iron_appalti.md),
redatto dopo lettura integrale del piano dei conti reale (795 conti) — ogni
codice conto citato qui è stato riverificato meccanicamente contro
piano_conti_iron_appalti.csv, nessuno inventato.

RAPPORTO CON services/classificazione_operazioni.py: quel file gestisce le
12 classi "ordinarie" (AdER, TFR esterno, Cassa Edile, effetti, commissioni,
leasing, mutuo, assicurazioni, oneri doganali, ammortamenti, svalutazione
crediti, dividendi). Questo file gestisce SOLO le eccezioni/rettifiche —
per esplicita regola del commercialista (vedi "Controlli implementativi
obbligatori" nel documento originale), un documento che rientra in una di
quelle 12 classi ordinarie NON va qui, anche se "assomiglia" a una
rettifica: va classificato prima con classifica() di
classificazione_operazioni.py.

LIVELLI (dal documento originale):
  A = automatizzabile con guardrail — solo casi molto ristretti (vedi sotto)
  P = parametrizzata con controllo umano — il sistema propone, un umano sceglie
      la variante giusta e approva prima di registrare
  M = sempre manuale — richiede giudizio OIC, atto societario, perizia, o un
      conto che non esiste ancora nel piano (vedi CONTI_MANCANTI_SUGGERITI):
      per queste NON si genera mai una proposta di scrittura completa.

In ogni caso, indipendentemente dal livello, nell'app nessuna scrittura
diventa definitiva senza il click di conferma dell'operatore in Prima Nota
— questo vale anche per le [A], che restano comunque proposte, mai
registrazioni dirette.
"""

# ══════════════════════════════════════════════════════════════
# Conti che il piano attuale NON ha e che servirebbero per completare
# alcuni schemi (dal documento: "Nuovi conti suggeriti, senza codice").
# Finché non vengono creati e approvati, le rettifiche che li richiedono
# restano bloccate anche se il resto dello schema è pronto.
# ══════════════════════════════════════════════════════════════
CONTI_MANCANTI_SUGGERITI = [
    "Clienti c/crediti commerciali (patrimoniale attivo) — manca un mastro clienti generale",
    "Fornitori c/debiti commerciali (patrimoniale passivo) — manca un mastro fornitori generale",
    "Debiti v/dipendenti per retribuzioni e note spese (patrimoniale passivo)",
    "Banca c/transitori — incassi e pagamenti da identificare",
    "Accantonamento per rischi e oneri (costo)",
    "Imposte anticipate (patrimoniale attivo)",
    "Ripristini di valore immobilizzazioni (voce economica di ripresa valore)",
    "Fondo svalutazione rimanenze (patrimoniale passivo rettificativo)",
    "Differenze inventariali / ammanchi e surplus (costo/ricavo separati)",
    "Debiti v/banche per finanziamenti/mutui (patrimoniale passivo, breve e oltre 12 mesi)",
    "Clienti/fornitori e banche in valuta (patrimoniali distinti)",
]

# ══════════════════════════════════════════════════════════════
# CATALOGO — un dict per sottoclasse. "schema" è una lista di varianti;
# ogni variante è una lista di righe {conto, nome_conto, lato, ruolo}.
# "conto_mancante": True se la variante richiede un conto della lista sopra
# (schema riportato solo a titolo informativo, MAI proponibile finché quel
# conto non esiste davvero).
# ══════════════════════════════════════════════════════════════
CATALOGO_RETTIFICHE = {

    "A1": {
        "gruppo": "A. Storni, correzioni e riclassifiche documentate",
        "nome": "Storno di registrazione duplicata, errata o annullata",
        "livello": "P",
        "parole_chiave": ["doppia importazione", "movimento duplicato", "errore materiale",
                          "storno registrazione", "annullamento movimento"],
        "note": "Storno dell'importo esatto, stesso conto dell'originale invertito (un Dare "
                "diventa Avere e viceversa). Non usare per fatture/note credito/corrispettivi "
                "fiscalmente validi. Automatizzabile solo il rilevamento del duplicato; "
                "l'annullamento richiede approvazione.",
        "conto_mancante": False,
    },
    "A2": {
        "gruppo": "A. Storni, correzioni e riclassifiche documentate",
        "nome": "Riclassifica costo/ricavo già contabilizzato, stessa controparte",
        "livello": "P",
        "parole_chiave": ["riclassifica costo", "conto sbagliato", "commessa sbagliata"],
        "schema": [[
            {"conto": "055305", "nome": "utenze energia elettrica (o altro conto economico corretto)", "lato": "DARE"},
            {"conto": "055307", "nome": "altre utenze (o il conto errato effettivamente usato)", "lato": "AVERE"},
        ]],
        "note": "Pari somma tra due costi (o due ricavi): neutra su banca, IVA, controparte. Non "
                "cambia imponibile/IVA/competenza. Conti d'esempio — vanno sostituiti con quelli "
                "realmente coinvolti nell'errore.",
        "conto_mancante": False,
    },
    "A3": {
        "gruppo": "A. Storni, correzioni e riclassifiche documentate",
        "nome": "Riclassifica patrimoniale entro/oltre 12 mesi",
        "livello": "P",
        "parole_chiave": ["scadenza entro oltre 12 mesi", "riclassifica patrimoniale"],
        "schema": [
            [{"conto": "015750", "nome": "crediti v/controllate oltre es.", "lato": "DARE"},
             {"conto": "015700", "nome": "crediti v/controllate entro es.", "lato": "AVERE"}],
            [{"conto": "044401", "nome": "debiti a breve v/controllate", "lato": "DARE"},
             {"conto": "044430", "nome": "debiti a lungo v/controllate", "lato": "AVERE"}],
        ],
        "note": "Varianti alternative (non sommabili in una scrittura sola); si applica anche a "
                "044451/044480 (collegate) e 044501/044530 (controllanti). Per invertire il "
                "verso, invertire Dare/Avere della stessa coppia. Per crediti/debiti commerciali "
                "ordinari mancano i conti adeguati nel piano.",
        "conto_mancante": False,
    },
    "A4": {
        "gruppo": "A. Storni, correzioni e riclassifiche documentate",
        "nome": "Arrotondamenti tecnici di conversione/chiusura",
        "livello": "A",
        "parole_chiave": ["arrotondamento", "differenza centesimi", "conversione euro"],
        "schema": [[
            {"conto": "070009", "nome": "arrotondamenti passivi", "lato": "DARE"},
            {"conto": "065401", "nome": "arrotondamenti attivi", "lato": "AVERE"},
        ]],
        "note": "Solo un lato secondo il segno: se differenza passiva, Dare 070009 / Avere conto "
                "tecnico da chiudere; se attiva, il contrario. Soglia di pochi centesimi. Il "
                "conto 046203 è riservato ad apertura/chiusura, non a differenze ordinarie.",
        "conto_mancante": False,
    },

    "B1": {
        "gruppo": "B. Assestamenti per competenza",
        "nome": "Rateo attivo per provento maturato non incassato",
        "livello": "P",
        "parole_chiave": ["rateo attivo", "interesse maturato non incassato"],
        "schema": [[
            {"conto": "032701", "nome": "ratei attivi", "lato": "DARE"},
            {"conto": "065404", "nome": "interessi attivi c/c bancari", "lato": "AVERE"},
        ]],
        "note": "Solo la quota maturata. Storno automatico nell'esercizio successivo (Dare/Avere "
                "invertiti), poi si registra l'incasso col documento definitivo. Per altri "
                "proventi scegliere il ricavo realmente pertinente (es. 065203), non 050704 "
                "per comodità.",
        "conto_mancante": False,
    },
    "B2": {
        "gruppo": "B. Assestamenti per competenza",
        "nome": "Rateo passivo per costo maturato non pagato/fatturato",
        "livello": "P",
        "parole_chiave": ["rateo passivo", "costo maturato non fatturato"],
        "schema": [[
            {"conto": "070005", "nome": "interessi passivi c/c bancari", "lato": "DARE"},
            {"conto": "045501", "nome": "ratei passivi", "lato": "AVERE"},
        ]],
        "note": "Solo la quota maturata; storno automatico nell'esercizio successivo. Per canoni "
                "usare il costo coerente (es. 056002). Scelta 070006/070021 per mutuo resta "
                "bloccata finché non validata (vedi classe MUTUO in classificazione_operazioni).",
        "conto_mancante": False,
    },
    "B3": {
        "gruppo": "B. Assestamenti per competenza",
        "nome": "Risconto attivo — rinvio di costo di competenza futura",
        "livello": "P",
        "parole_chiave": ["risconto attivo", "affitto anticipato", "polizza pluriennale pagata"],
        "schema": [[
            {"conto": "032801", "nome": "risconti attivi", "lato": "DARE"},
            {"conto": "056002", "nome": "affitti passivi (o costo pluriperiodale coerente)", "lato": "AVERE"},
        ]],
        "note": "Solo la quota NON maturata alla chiusura. Riassorbimento nel periodo successivo "
                "(Dare/Avere invertiti). Per assicurazioni usare 070025, 055108 o 032803 secondo "
                "la polizza — 032803 è un conto di COSTO nel piano reale, non patrimoniale.",
        "conto_mancante": False,
    },
    "B4": {
        "gruppo": "B. Assestamenti per competenza",
        "nome": "Risconto passivo — rinvio di ricavo di competenza futura",
        "livello": "P",
        "parole_chiave": ["risconto passivo", "canone fatturato anticipo", "servizio incassato anticipo"],
        "schema": [[
            {"conto": "050702", "nome": "affitti attivi", "lato": "DARE"},
            {"conto": "045551", "nome": "risconti passivi", "lato": "AVERE"},
        ]],
        "note": "Solo il ricavo NON maturato. Rilascio nel periodo successivo. Per prestazioni "
                "usare 051802 se è il conto originario.",
        "conto_mancante": False,
    },
    "B5": {
        "gruppo": "B. Assestamenti per competenza",
        "nome": "Fattura da ricevere di competenza",
        "livello": "P",
        "parole_chiave": ["fattura da ricevere", "sal non fatturato", "prestazione ricevuta senza fattura"],
        "schema": [[
            {"conto": "055305", "nome": "costo di competenza documentato", "lato": "DARE"},
            {"conto": "044001", "nome": "fatture da ricevere a breve", "lato": "AVERE"},
        ]],
        "note": "Solo imponibile, quando l'IVA non è ancora detraibile. Storno nel nuovo esercizio "
                "poi si registra la fattura reale nel processo fornitori. Non usare insieme a "
                "045203 senza policy che ne distingua l'uso. Per debiti oltre 12 mesi c'è 044101.",
        "conto_mancante": False,
    },
    "B6": {
        "gruppo": "B. Assestamenti per competenza",
        "nome": "Fattura da emettere / ricavo maturato",
        "livello": "P",
        "parole_chiave": ["fattura da emettere", "sal approvato", "verbale accettazione lavori"],
        "schema": [[
            {"conto": "032804", "nome": "clienti c/fatture da emettere", "lato": "DARE"},
            {"conto": "051802", "nome": "prestazioni di servizi", "lato": "AVERE"},
        ]],
        "note": "Solo imponibile maturato con misurazione attendibile. Al momento della fattura "
                "032804 va chiuso contro il conto clienti ordinario — che nel piano manca. Per "
                "lavori su ordinazione (OIC 23) usare F2, non sommare a questa voce.",
        "conto_mancante": False,
    },
    "B7": {
        "gruppo": "B. Assestamenti per competenza",
        "nome": "Nota di credito da ricevere dal fornitore",
        "livello": "P",
        "parole_chiave": ["nota credito da ricevere", "reso a fornitore", "contestazione accettata fornitore"],
        "schema": [[
            {"conto": "030552", "nome": "forn. nota accr. da ricevere", "lato": "DARE"},
            {"conto": "054002", "nome": "merci c/acquisti (imponibile)", "lato": "AVERE"},
            {"conto": "045005", "nome": "iva acquisti (solo se rettificabile)", "lato": "AVERE"},
        ]],
        "note": "Dare totale = imponibile + eventuale IVA in Avere. Alla ricezione, 030552 va "
                "chiuso contro il mastro fornitore — che nel piano manca.",
        "conto_mancante": False,
    },
    "B8": {
        "gruppo": "B. Assestamenti per competenza",
        "nome": "Nota di credito da emettere al cliente",
        "livello": "P",
        "parole_chiave": ["nota credito da emettere", "reso cliente", "abbuono riconosciuto cliente"],
        "schema": [[
            {"conto": "051609", "nome": "resi su vendite (o 057039 sconti su vendite, per sconti)", "lato": "DARE"},
            {"conto": "045006", "nome": "iva vendite (se rettificabile)", "lato": "DARE"},
            {"conto": "045202", "nome": "clienti nota accr. da emettere (totale)", "lato": "AVERE"},
        ]],
        "note": "057039 solo per sconti, non per resi. All'emissione, 045202 va chiuso contro il "
                "mastro clienti — che nel piano manca.",
        "conto_mancante": False,
    },
    "B9": {
        "gruppo": "B. Assestamenti per competenza",
        "nome": "Canone leasing anticipato / maxicanone — quota pluriperiodale",
        "livello": "M",
        "parole_chiave": ["maxicanone", "canone leasing anticipato"],
        "schema": [[
            {"conto": "032802", "nome": "canoni anticipati leasing", "lato": "DARE"},
            {"conto": "056003", "nome": "canoni leasing iva deducibile", "lato": "AVERE"},
        ]],
        "note": "Preferibile al risconto generico per questa casistica. Non assorbire IVA, oneri "
                "finanziari o quota indeducibile (056007). Non duplicare il canone ordinario "
                "(classe LEASING).",
        "conto_mancante": False,
    },

    "C1": {
        "gruppo": "C. Crediti, debiti, banca/cassa, IVA e fisco",
        "nome": "Incasso/pagamento non identificato",
        "livello": "M",
        "parole_chiave": ["movimento bancario non riconosciuto", "bonifico non identificato"],
        "note": "Il piano non ha un conto transitorio bancario affidabile. Fino all'identificazione "
                "NON generare scrittura economica né usare 028001 come contenitore permanente. "
                "Trasferimenti banca-banca (es. Dare 032004 / Avere 032003) sono automatizzabili "
                "[A] solo se i due estratti coincidono per importo e data.",
        "conto_mancante": True,
    },
    "C2": {
        "gruppo": "C. Crediti, debiti, banca/cassa, IVA e fisco",
        "nome": "Correzione IVA indetraibile / prorata",
        "livello": "P",
        "parole_chiave": ["iva indetraibile", "prorata iva", "rettifica detraibilita iva"],
        "schema": [[
            {"conto": "057036", "nome": "iva indetr. pro-rata (o il costo originario pertinente)", "lato": "DARE"},
            {"conto": "045005", "nome": "iva acquisti", "lato": "AVERE"},
        ]],
        "note": "Solo la quota di IVA non detraibile. Non usare per sanzioni, interessi o imposte "
                "dirette. Vietato senza documento/calcolo e corretto periodo di liquidazione.",
        "conto_mancante": False,
    },
    "C3": {
        "gruppo": "C. Crediti, debiti, banca/cassa, IVA e fisco",
        "nome": "Liquidazione periodica IVA",
        "livello": "P",
        "parole_chiave": ["liquidazione iva periodica", "iva a debito periodo", "iva a credito periodo"],
        "schema": [
            [{"conto": "045006", "nome": "iva vendite", "lato": "DARE"},
             {"conto": "045005", "nome": "iva acquisti", "lato": "AVERE"},
             {"conto": "044604", "nome": "debito iva da versare (saldo, se IVA a debito)", "lato": "AVERE"}],
            [{"conto": "045006", "nome": "iva vendite", "lato": "DARE"},
             {"conto": "030354", "nome": "cred. erario c/iva da compens. (saldo, se IVA a credito)", "lato": "DARE"},
             {"conto": "045005", "nome": "iva acquisti", "lato": "AVERE"}],
        ],
        "note": "Gestire separatamente 045002/045010/045011 (sospesa), 045008 (split payment), "
                "045003 (autotrasportatori), 045004 (acconto) secondo normativa — non liquidarli "
                "dentro lo schema ordinario.",
        "conto_mancante": False,
    },
    "C4": {
        "gruppo": "C. Crediti, debiti, banca/cassa, IVA e fisco",
        "nome": "Compensazione F24 di credito già maturato",
        "livello": "P",
        "parole_chiave": ["compensazione f24", "credito compensato f24"],
        "schema": [[
            {"conto": "044692", "nome": "INPS c/contributi (debiti) (o il debito compensato)", "lato": "DARE"},
            {"conto": "030393", "nome": "Ires da compensare (o il credito usato: 030364 INPS, 030354 IVA...)", "lato": "AVERE"},
        ]],
        "note": "Varianti alternative secondo il credito usato, non cumulabili. Validazione "
                "obbligatoria di codice tributo, capienza, maturazione, blocchi, ricevuta. Non "
                "confondere il credito compensato con un pagamento bancario reale.",
        "conto_mancante": False,
    },
    "C5": {
        "gruppo": "C. Crediti, debiti, banca/cassa, IVA e fisco",
        "nome": "Accantonamento imposte correnti e riversamento stimato",
        "livello": "M",
        "parole_chiave": ["accantonamento ires", "accantonamento irap", "imposte correnti chiusura"],
        "schema": [[
            {"conto": "090005", "nome": "IRES dell'esercizio", "lato": "DARE"},
            {"conto": "044614", "nome": "debito IRES a saldo", "lato": "AVERE"},
        ], [
            {"conto": "090006", "nome": "IRAP dell'esercizio", "lato": "DARE"},
            {"conto": "044610", "nome": "debito IRAP a saldo", "lato": "AVERE"},
        ]],
        "note": "Scritture indipendenti IRES/IRAP. Prima di usarla, definire policy tra 044614/"
                "044619 (IRES) e 044610/030410 (IRAP), senza duplicare il debito. Giudizio "
                "fiscale, mai automatizzabile dalla sola contabilità.",
        "conto_mancante": False,
    },
    "C6": {
        "gruppo": "C. Crediti, debiti, banca/cassa, IVA e fisco",
        "nome": "Imposte differite — stanziamento e rilascio",
        "livello": "M",
        "parole_chiave": ["imposte differite", "fondo imposte differite"],
        "schema": [[
            {"conto": "090005", "nome": "IRES dell'esercizio (o costo imposte pertinente)", "lato": "DARE"},
            {"conto": "034102", "nome": "f.do imposte differite", "lato": "AVERE"},
        ]],
        "note": "Rilascio: scrittura invertita. Il piano non ha un conto per imposte ANTICIPATE "
                "(vedi conti mancanti) — non inventarne uno. Richiede prospetto differenze "
                "temporanee validato dal professionista.",
        "conto_mancante": False,
    },

    "D1": {
        "gruppo": "D. Personale, TFR e rapporti previdenziali",
        "nome": "Accantonamento TFR di competenza",
        "livello": "P",
        "parole_chiave": ["accantonamento tfr", "tfr di competenza"],
        "schema": [[
            {"conto": "056242", "nome": "accant. tfr dell'anno", "lato": "DARE"},
            {"conto": "034301", "nome": "f.do tratt.fine rapp. TFR", "lato": "AVERE"},
        ]],
        "note": "Scegliere UNA sola voce di costo tra 056241/056242/056243/056244 secondo policy "
                "esplicita (qui proposta 056242). Non confondere con versamenti a fondo esterno "
                "(classe TFR_ESTERNO) o pagamento TFR.",
        "conto_mancante": False,
    },
    "D2": {
        "gruppo": "D. Personale, TFR e rapporti previdenziali",
        "nome": "Rettifica contributi/ritenute su paghe già rilevate",
        "livello": "P",
        "parole_chiave": ["rettifica contributi paghe", "cedolino rettificativo", "uniemens rettifica"],
        "schema": [
            [{"conto": "056201", "nome": "Contributi INPS (costi - oneri sociali)", "lato": "DARE"},
             {"conto": "044692", "nome": "INPS c/contributi (debiti)", "lato": "AVERE"}],
            [{"conto": "056202", "nome": "oneri per contributi INAIL", "lato": "DARE"},
             {"conto": "044902", "nome": "debiti v/INAIL", "lato": "AVERE"}],
        ],
        "note": "Solo per la differenza non già registrata; righe alternative. Per sovrastima "
                "invertire la coppia. Distinguere sempre ritenute dipendenti (044601) da ritenute "
                "terzi (044602); non usare 056111 come costo automatico.",
        "conto_mancante": False,
    },
    "D3": {
        "gruppo": "D. Personale, TFR e rapporti previdenziali",
        "nome": "Debito retributivo o rimborso dipendente",
        "livello": "M",
        "parole_chiave": ["rimborso dipendente", "nota spese dipendente", "debito retributivo"],
        "note": "Non esiste un conto generale \"debiti v/dipendenti\": i conti 044801-044805 sono "
                "NOMINATIVI (una persona specifica), non riutilizzabili per altri. Per il rimborso "
                "specifico già identificato: Dare 045210 (Mierla - debiti c/rimborsi da liquidar) "
                "/ Avere 032003 — solo per QUEL caso specifico, non generalizzabile.",
        "conto_mancante": True,
    },

    "E1": {
        "gruppo": "E. Immobilizzazioni, svalutazioni, alienazioni",
        "nome": "Svalutazione durevole di immobilizzazione",
        "livello": "M",
        "parole_chiave": ["svalutazione cespite", "test recuperabilita oic 9", "perdita durevole valore"],
        "schema": [[
            {"conto": "056550", "nome": "svalutaz. beni ammortizzabili", "lato": "DARE"},
            {"conto": "014401", "nome": "f.do sval. impianti generici (o coppia coerente col bene)", "lato": "AVERE"},
        ]],
        "note": "Coppie disponibili: 014402 (impianti specifici), 014403/014404 (macchinari), "
                "014901 (attrezzature). Per immateriali: 056552 + fondo specifico (es. 012003). "
                "Richiede test di recuperabilità OIC 9 e verbale — non è un automatismo di fine anno.",
        "conto_mancante": False,
    },
    "E2": {
        "gruppo": "E. Immobilizzazioni, svalutazioni, alienazioni",
        "nome": "Dismissione/vendita cespite con plus/minusvalenza",
        "livello": "M",
        "parole_chiave": ["vendita cespite", "dismissione cespite", "plusvalenza cespite", "minusvalenza cespite"],
        "schema": [[
            {"conto": "014303", "nome": "f.do amm. macchinari specifici (fondo accumulato, per categoria)", "lato": "DARE"},
            {"conto": "032003", "nome": "Intesa San Paolo c/c (corrispettivo incassato)", "lato": "DARE"},
            {"conto": "080000", "nome": "minusval.alienazione immobil. (se minusvalenza)", "lato": "DARE"},
            {"conto": "014003", "nome": "macchinari specifici (costo storico totale, per categoria)", "lato": "AVERE"},
        ], [
            {"conto": "014303", "nome": "f.do amm. macchinari specifici", "lato": "DARE"},
            {"conto": "032003", "nome": "Intesa San Paolo c/c", "lato": "DARE"},
            {"conto": "014003", "nome": "macchinari specifici", "lato": "AVERE"},
            {"conto": "075000", "nome": "plusv. da alien. immobilizz. (se plusvalenza)", "lato": "AVERE"},
        ]],
        "note": "Se il corrispettivo non è incassato subito serve un credito cliente — conto non "
                "disponibile nel piano. Scorporare IVA della cessione quando dovuta. Aggiornare "
                "prima il fondo se il bene non è ancora completamente ammortizzato.",
        "conto_mancante": False,
    },
    "E3": {
        "gruppo": "E. Immobilizzazioni, svalutazioni, alienazioni",
        "nome": "Riclassifica spesa corrente → investimento pluriennale",
        "livello": "M",
        "parole_chiave": ["capitalizzazione spesa", "miglioria capitalizzata", "riclassifica investimento"],
        "schema": [[
            {"conto": "013203", "nome": "costi per migliorie beni di terzi", "lato": "DARE"},
            {"conto": "055007", "nome": "manut.e rip. su beni di prop.", "lato": "AVERE"},
        ]],
        "note": "Ammesso solo se soddisfa i criteri di capitalizzazione OIC (durata/utilità "
                "dimostrabili) — le manutenzioni ordinarie restano a costo. Per un cespite nuovo "
                "usare il codice reale di categoria, mai 013203 per comodità.",
        "conto_mancante": False,
    },
    "E4": {
        "gruppo": "E. Immobilizzazioni, svalutazioni, alienazioni",
        "nome": "Ripristino di valore di immobilizzazione",
        "livello": "M",
        "parole_chiave": ["ripristino valore cespite", "cessazione svalutazione"],
        "note": "Il piano NON ha un conto economico dedicato al ripristino di valore — non usare "
                "impropriamente 075000 (plusvalenze) o 072551 (rival. crediti). Serve il conto "
                "nuovo suggerito e decisione del commercialista.",
        "conto_mancante": True,
    },

    "F1": {
        "gruppo": "F. Rimanenze, lavori in corso e commesse",
        "nome": "Chiusura/apertura rimanenze materie e merci",
        "livello": "P",
        "parole_chiave": ["rimanenze finali", "rimanenze iniziali", "inventario materie", "inventario merci"],
        "schema": [[
            {"conto": "017001", "nome": "rim. materie prime (finali, per categoria)", "lato": "DARE"},
            {"conto": "056801", "nome": "rim. fin. materie prime", "lato": "AVERE"},
        ]],
        "note": "Usare solo coppie omogenee per categoria: 017002/056802/056702 (sussidiarie), "
                "017004/056803/056703 (imballaggi), 017401/056804/056704 (merci). Non sommare "
                "inventari fisici e scritture di acquisto.",
        "conto_mancante": False,
    },
    "F2": {
        "gruppo": "F. Rimanenze, lavori in corso e commesse",
        "nome": "Prodotti in corso / lavori in corso su ordinazione",
        "livello": "M",
        "parole_chiave": ["lavori in corso su ordinazione", "sal interno", "percentuale completamento oic 23"],
        "schema": [[
            {"conto": "017301", "nome": "rim. lavori in corso su ordin.", "lato": "DARE"},
            {"conto": "050501", "nome": "rim.fin.in corso su ordinaz.", "lato": "AVERE"},
        ]],
        "note": "Richiede metodologia OIC 23 costante (commessa completata o percentuale "
                "avanzamento) e documentazione perdite previste. Non sommare con B6 per lo stesso "
                "margine/SAL.",
        "conto_mancante": False,
    },
    "F3": {
        "gruppo": "F. Rimanenze, lavori in corso e commesse",
        "nome": "Svalutazione o differenza inventariale",
        "livello": "M",
        "parole_chiave": ["differenza inventariale", "ammanco magazzino", "obsolescenza magazzino"],
        "schema": [[
            {"conto": "056602", "nome": "altre svalutazioni", "lato": "DARE"},
            {"conto": "017401", "nome": "rim. merci per la vendita (o categoria verificata)", "lato": "AVERE"},
        ]],
        "note": "Il piano non ha un fondo svalutazione rimanenze dedicato — non inventarlo. Per "
                "ammanchi non attribuibili, valutare effetti assicurativi. Mai automatizzare da "
                "scostamento teorico.",
        "conto_mancante": False,
    },

    "G1": {
        "gruppo": "G. Crediti, fondi rischi, cambi, posizioni finanziarie",
        "nome": "Utilizzo fondo svalutazione crediti / perdita su credito",
        "livello": "M",
        "parole_chiave": ["stralcio credito", "perdita su credito", "credito inesigibile definitivo"],
        "note": "Il conto cliente ordinario da accreditare non è nel piano — 030553 non è un "
                "sostituto valido se il credito è un cliente ordinario. Corretta nel segno, non "
                "codificabile integralmente senza il nuovo conto clienti.",
        "conto_mancante": True,
    },
    "G2": {
        "gruppo": "G. Crediti, fondi rischi, cambi, posizioni finanziarie",
        "nome": "Ripresa di precedente svalutazione crediti",
        "livello": "M",
        "parole_chiave": ["ripresa svalutazione crediti", "recupero solvibilita cliente"],
        "schema": [[
            {"conto": "030001", "nome": "f.do sval.cred.v/clienti/breve", "lato": "DARE"},
            {"conto": "072551", "nome": "rival.crediti attivo circol.", "lato": "AVERE"},
        ]],
        "note": "Il ripristino non supera la svalutazione precedente né il valore nominale. Non "
                "usare per crediti immobilizzati (per quelli c'è 072531). Mai automatica da incasso.",
        "conto_mancante": False,
    },
    "G3": {
        "gruppo": "G. Crediti, fondi rischi, cambi, posizioni finanziarie",
        "nome": "Fondo rischi/oneri — costituzione, utilizzo, rilascio",
        "livello": "M",
        "parole_chiave": ["fondo rischi", "accantonamento controversia", "causa legale accantonamento"],
        "schema": [[
            {"conto": "034205", "nome": "f.do controversie legali (utilizzo)", "lato": "DARE"},
            {"conto": "032003", "nome": "Intesa San Paolo c/c", "lato": "AVERE"},
        ]],
        "note": "Costituzione non codificabile senza il nuovo conto di costo generico "
                "\"accantonamento rischi\". Sostituire 034205 col fondo realmente stanziato: "
                "034203, 034204, 034206, 034103. Ogni fase è manuale, giudizio OIC 31.",
        "conto_mancante": True,
    },
    "G4": {
        "gruppo": "G. Crediti, fondi rischi, cambi, posizioni finanziarie",
        "nome": "Adeguamento cambi di poste monetarie",
        "livello": "M",
        "parole_chiave": ["adeguamento cambi", "credito in valuta", "debito in valuta", "oic 26"],
        "schema": [[
            {"conto": "070010", "nome": "perdite su cambi (se perdita)", "lato": "DARE"},
            {"conto": "030553", "nome": "crediti commerciali diversi (solo se è il credito in valuta specifico)", "lato": "AVERE"},
        ]],
        "note": "030553 non è un surrogato per clienti ordinari. Mancano conti fornitori/clienti/"
                "banca in valuta dedicati — i casi comuni restano bloccati finché non creati. Non "
                "usare 034201 senza policy deliberata.",
        "conto_mancante": True,
    },
    "G5": {
        "gruppo": "G. Crediti, fondi rischi, cambi, posizioni finanziarie",
        "nome": "Depositi cauzionali e interessi su deposito",
        "livello": "P",
        "parole_chiave": ["deposito cauzionale", "cauzione fornitore", "interesse su cauzione"],
        "schema": [[
            {"conto": "016301", "nome": "depositi cauz. a fornitori", "lato": "DARE"},
            {"conto": "032003", "nome": "Intesa San Paolo c/c", "lato": "AVERE"},
        ]],
        "note": "Rimborso: scrittura invertita. Per cauzioni diverse: 016302 (varie), 016303 "
                "(telefoni), 016304 (energia elettrica). Non riclassificare un anticipo fornitore "
                "(030554) come deposito senza contratto.",
        "conto_mancante": False,
    },

    "H1": {
        "gruppo": "H. Patrimonio netto, errori pregressi, operazioni societarie",
        "nome": "Destinazione utile / copertura perdita, dopo delibera",
        "livello": "M",
        "parole_chiave": ["destinazione utile", "copertura perdita", "delibera assembleare utile"],
        "schema": [[
            {"conto": "033651", "nome": "utile d'esercizio", "lato": "DARE"},
            {"conto": "033301", "nome": "riserva legale (o 033501 riserva straordinaria)", "lato": "AVERE"},
        ]],
        "note": "Richiede bilancio approvato e verbale assembleare, non il semplice saldo di fine "
                "anno. Non duplicare la distribuzione dividendi (classe PRELIEVI_SOCI) e non usare "
                "030557 come scorciatoia.",
        "conto_mancante": False,
    },
    "H2": {
        "gruppo": "H. Patrimonio netto, errori pregressi, operazioni societarie",
        "nome": "Versamento soci in conto capitale o copertura perdite",
        "livello": "M",
        "parole_chiave": ["versamento soci conto capitale", "versamento copertura perdite"],
        "schema": [[
            {"conto": "032003", "nome": "Intesa San Paolo c/c", "lato": "DARE"},
            {"conto": "033502", "nome": "versam.in conto aumen.capitale (o 033503 copertura perdite)", "lato": "AVERE"},
        ]],
        "note": "Non usare 034601/034801 (finanziamenti soci) se è un versamento di patrimonio, "
                "né viceversa. Nessuna automazione: richiede atto, verifica restituibilità/interessi.",
        "conto_mancante": False,
    },
    "H3": {
        "gruppo": "H. Patrimonio netto, errori pregressi, operazioni societarie",
        "nome": "Correzione errore rilevante di esercizi precedenti",
        "livello": "M",
        "parole_chiave": ["errore esercizio precedente", "oic 29", "correzione errore pregresso"],
        "schema": [[
            {"conto": "033621", "nome": "utili esercizi precedenti", "lato": "DARE"},
            {"conto": "044001", "nome": "fatture da ricevere a breve (solo se è la passività reale)", "lato": "AVERE"},
        ]],
        "note": "Richiede memorandum OIC 29 con materialità, anno errore, effetti fiscali, "
                "approvazione professionale. Mai usare 080012/075011 (sopravvenienze) per aggirare "
                "questa valutazione.",
        "conto_mancante": False,
    },
}

# Regola esplicita del commercialista: un documento che appartiene a una delle
# 12 classi ordinarie NON va classificato qui come rettifica, anche se nel
# testo compaiono parole come "storno" o "rettifica" — va prima verificato
# contro classificazione_operazioni.classifica().
CLASSI_ORDINARIE_DA_NON_DUPLICARE = [
    "ADER", "TFR_ESTERNO", "CASSA_EDILE", "EFFETTI", "COMMISSIONI_BANCARIE",
    "LEASING", "INTERESSI_MUTUO", "ASSICURAZIONI", "ONERI_DOGANALI_PROVVIGIONI",
    "AMMORTAMENTI_FINE_ANNO", "SVALUTAZIONE_CREDITI", "PRELIEVI_SOCI",
]


def classifica_rettifica(testo_documento):
    """
    Come classificazione_operazioni.classifica(), ma sul catalogo rettifiche.
    Ritorna (id_sottoclasse, confidenza) o (None, 0.0).
    """
    testo = (testo_documento or "").lower()
    if not testo.strip():
        return None, 0.0
    migliore, punteggio_migliore = None, 0
    for id_sc, sc in CATALOGO_RETTIFICHE.items():
        punteggio = sum(1 for kw in sc["parole_chiave"] if kw in testo)
        if punteggio > punteggio_migliore:
            migliore, punteggio_migliore = id_sc, punteggio
    if migliore is None:
        return None, 0.0
    confidenza = 0.6 if punteggio_migliore == 1 else 0.85
    return migliore, confidenza


def guida_per_rettifica(id_sottoclasse, code_to_name=None):
    """Testo da iniettare come guida_extra per l'AI, stesso stile di
    classificazione_operazioni.guida_per_classe()."""
    sc = CATALOGO_RETTIFICHE.get(id_sottoclasse)
    if sc is None:
        return None
    righe = [f"Sottoclasse rettifica {id_sottoclasse} — {sc['nome']} (gruppo: {sc['gruppo']}, "
             f"livello {sc['livello']})."]
    if sc.get("conto_mancante"):
        righe.append("ATTENZIONE: questa rettifica richiede un conto che NON esiste ancora nel "
                      "piano dei conti — non generare una proposta completa, segnalarlo in note.")
        return "\n".join(righe)
    for i, variante in enumerate(sc.get("schema", []), start=1):
        etichetta = f"Variante {i}:" if len(sc.get("schema", [])) > 1 else "Schema:"
        righe.append(etichetta)
        for riga in variante:
            nome = code_to_name.get(riga["conto"], riga["nome"]) if code_to_name else riga["nome"]
            righe.append(f"  {riga['lato']} {riga['conto']} ({nome})")
    if sc.get("note"):
        righe.append(f"Nota: {sc['note']}")
    if sc["livello"] == "M":
        righe.append("Livello M: proponi solo a titolo indicativo, questa rettifica richiede "
                      "sempre valutazione e approvazione specifica, non solo il click di conferma "
                      "ordinario.")
    return "\n".join(righe)

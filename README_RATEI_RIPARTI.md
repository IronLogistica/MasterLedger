# Riparti multipli e oneri differiti

## Riparto del personale
Nella revisione delle buste, per ciascun dipendente si possono indicare fino a otto centri di costo e le rispettive percentuali. Il totale deve essere esattamente 100,00%. Competenze, netto e trattenute vengono ripartiti con lo stesso criterio; eventuali centesimi residui restano sull'ultima quota per mantenere la quadratura.

Il riparto può essere memorizzato nell'anagrafica del dipendente. Il prospetto ratei usa prioritariamente il riparto già approvato nella busta contabilizzata dello stesso mese; in mancanza, usa quello memorizzato.

## Import ratei/oneri differiti
Caricare il PDF scegliendo **Ratei/oneri differiti Zucchetti**. Il sistema conserva tutto il dettaglio estratto per dipendente. Registra soltanto:
- ferie (F01),
- permessi (F02/F03),
- tredicesima (M01),
- contributi collegati a tali ratei.

L'INAIL riportato come riga separata è tenuto per riconciliazione e non viene registrato nuovamente come costo. Il TFR è escluso per impostazione iniziale e richiede sia l'apposita conferma nella revisione sia la configurazione dei conti TFR: attivarlo soltanto dopo aver verificato che la busta non l'abbia già rilevato.

Il prospetto provvisorio richiede una conferma espressa prima della contabilizzazione. Quando arriverà il prospetto definitivo, importarlo come nuovo documento e confrontarlo con il provvisorio prima di procedere.

## Conti da configurare
Nella configurazione Paghe/F24 impostare: costo ratei ferie, costo ratei permessi, costo rateo tredicesima, debiti ratei. I due conti TFR sono richiesti soltanto se si attiva il TFR.

## Fatture
È presente la struttura dati comune `AllocationSplit` per ripartire le righe di fatture acquisto/vendita su più centri con percentuali che devono sommare a 100,00%. L'integrazione della maschera operativa sulle fatture va completata insieme al formato definitivo e alle regole aziendali di attribuzione (per riga, per documento o per progetto/commessa), per evitare di assegnare ricavi o costi con una logica impropria.

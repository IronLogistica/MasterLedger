"""Test del sistema di ubicazioni granulari (corsia/scaffale/ripiano/cassetta)
e delle mappe a schermo per Setup Magazzini."""
import re
from extensions import db
from models import OperatingSite, WarehouseArea, StorageLocation


def _site(app):
    with app.app_context():
        site = OperatingSite.query.first()
        if site is None:
            site = OperatingSite(code="2000", name="Sede Test", city="Test", region="Test")
            db.session.add(site); db.session.commit()
        return site.id


def test_area_with_default_structure_accepts_scaffale_location(login, app):
    with app.app_context():
        site_id = _site(app)

    r = login.post("/warehouse/areas/new", data={
        "site_id": site_id, "code": "SC01", "name": "Scaffalato Test", "area_type": "ROH",
        "usa_corsie": "1", "usa_scaffali": "1", "usa_ripiani": "1",
    })
    assert r.status_code == 302
    with app.app_context():
        area = WarehouseArea.query.filter_by(code="SC01").one()
        assert area.usa_corsie and area.usa_scaffali and area.usa_ripiani
        area_id = area.id

    r = login.post(f"/warehouse/areas/{area_id}/ubicazioni/new", data={
        "corridoio": "01", "scaffale": "05", "ripiano": "02", "tipo_stoccaggio": "SCAFFALE",
    })
    assert r.status_code == 302
    with app.app_context():
        loc = StorageLocation.query.filter_by(warehouse_area_id=area_id).one()
        assert loc.codice == "C01-S05-L02"


def test_area_a_terra_disables_other_levels_and_rejects_cantilever(login, app):
    site_id = _site(app)

    r = login.post("/warehouse/areas/new", data={
        "site_id": site_id, "code": "TR01", "name": "Terra Test", "area_type": "SCRAP",
        "area_a_terra": "1", "usa_corsie": "1", "usa_scaffali": "1",  # anche se spuntati, l'area a terra li disattiva
    })
    assert r.status_code == 302
    with app.app_context():
        area = WarehouseArea.query.filter_by(code="TR01").one()
        assert area.area_a_terra is True
        assert area.usa_corsie is False and area.usa_scaffali is False
        area_id = area.id

    # Un'ubicazione a terra usa solo la posizione libera
    r = login.post(f"/warehouse/areas/{area_id}/ubicazioni/new", data={
        "posizione_libera": "P01", "tipo_stoccaggio": "AREA_TERRA",
    })
    assert r.status_code == 302
    with app.app_context():
        assert StorageLocation.query.filter_by(warehouse_area_id=area_id, codice="P01").first() is not None

    # Un cantilever non è ammesso su un'area a terra
    r = login.post(f"/warehouse/areas/{area_id}/ubicazioni/new",
                   data={"posizione_libera": "P02", "tipo_stoccaggio": "CANTILEVER"}, follow_redirects=True)
    assert b"area a terra" in r.data.lower() or b"cantilever" in r.data.lower()
    with app.app_context():
        assert StorageLocation.query.filter_by(warehouse_area_id=area_id, codice="P02").first() is None


def test_cantilever_rejected_unless_area_activates_it(login, app):
    site_id = _site(app)
    login.post("/warehouse/areas/new", data={
        "site_id": site_id, "code": "CN01", "name": "No Cantilever", "area_type": "ROH",
        "usa_corsie": "1", "usa_scaffali": "1",
    })
    with app.app_context():
        area_id = WarehouseArea.query.filter_by(code="CN01").one().id

    r = login.post(f"/warehouse/areas/{area_id}/ubicazioni/new",
                   data={"corridoio": "01", "scaffale": "01", "tipo_stoccaggio": "CANTILEVER"},
                   follow_redirects=True)
    assert b"cantilever" in r.data.lower()
    with app.app_context():
        assert StorageLocation.query.filter_by(warehouse_area_id=area_id).count() == 0

    # Attivandolo nella struttura, la stessa richiesta deve passare
    login.post(f"/warehouse/areas/{area_id}/struttura", data={
        "usa_corsie": "1", "usa_scaffali": "1", "usa_cantilever": "1",
    })
    r = login.post(f"/warehouse/areas/{area_id}/ubicazioni/new",
                   data={"corridoio": "01", "scaffale": "01", "tipo_stoccaggio": "CANTILEVER"})
    assert r.status_code == 302
    with app.app_context():
        assert StorageLocation.query.filter_by(warehouse_area_id=area_id).count() == 1


def test_duplicate_codice_in_same_block_rejected(login, app):
    site_id = _site(app)
    login.post("/warehouse/areas/new", data={
        "site_id": site_id, "code": "DUP1", "name": "Dup Test", "area_type": "ROH",
        "usa_corsie": "1", "usa_scaffali": "1",
    })
    with app.app_context():
        area_id = WarehouseArea.query.filter_by(code="DUP1").one().id

    login.post(f"/warehouse/areas/{area_id}/ubicazioni/new", data={"corridoio": "01", "scaffale": "01"})
    r = login.post(f"/warehouse/areas/{area_id}/ubicazioni/new",
                   data={"corridoio": "01", "scaffale": "01"}, follow_redirects=True)
    assert "esiste già".encode() in r.data
    with app.app_context():
        assert StorageLocation.query.filter_by(warehouse_area_id=area_id).count() == 1


def test_grid_generation_creates_expected_count_and_skips_duplicates(login, app):
    site_id = _site(app)
    login.post("/warehouse/areas/new", data={
        "site_id": site_id, "code": "GRD1", "name": "Griglia Test", "area_type": "ROH",
        "usa_corsie": "1", "usa_scaffali": "1", "usa_ripiani": "1",
    })
    with app.app_context():
        area_id = WarehouseArea.query.filter_by(code="GRD1").one().id

    r = login.post(f"/warehouse/areas/{area_id}/ubicazioni/genera-griglia", data={
        "n_corsie": "2", "n_scaffali": "3", "n_ripiani": "2",
        "larghezza": "100", "profondita": "80", "corridoio_larghezza": "150",
    })
    assert r.status_code == 302
    with app.app_context():
        assert StorageLocation.query.filter_by(warehouse_area_id=area_id).count() == 12

    # Rilanciando, le stesse posizioni vengono saltate (nessun duplicato)
    r2 = login.post(f"/warehouse/areas/{area_id}/ubicazioni/genera-griglia", data={
        "n_corsie": "2", "n_scaffali": "3", "n_ripiani": "2",
        "larghezza": "100", "profondita": "80", "corridoio_larghezza": "150",
    }, follow_redirects=True)
    assert "saltate".encode() in r2.data
    with app.app_context():
        assert StorageLocation.query.filter_by(warehouse_area_id=area_id).count() == 12


def test_site_map_and_area_map_render(login, app):
    site_id = _site(app)
    login.post("/warehouse/areas/new", data={
        "site_id": site_id, "code": "MAP1", "name": "Mappa Test", "area_type": "FERT",
        "usa_corsie": "1", "usa_scaffali": "1",
        "pos_x": "0", "pos_y": "0", "dim_x": "200", "dim_y": "150",
    })
    with app.app_context():
        area_id = WarehouseArea.query.filter_by(code="MAP1").one().id
    login.post(f"/warehouse/areas/{area_id}/ubicazioni/new",
              data={"corridoio": "01", "scaffale": "01", "pos_x": "0", "pos_y": "0", "dim_x": "80", "dim_y": "60"})

    r = login.get(f"/warehouse/sites/{site_id}/mappa")
    assert r.status_code == 200 and b"<svg" in r.data and b"MAP1" in r.data

    r2 = login.get(f"/warehouse/areas/{area_id}")
    assert r2.status_code == 200 and b"<svg" in r2.data


def test_area_without_position_excluded_from_site_map(login, app):
    site_id = _site(app)
    login.post("/warehouse/areas/new", data={
        "site_id": site_id, "code": "NOPOS", "name": "Senza posizione", "area_type": "ROH",
        "usa_corsie": "1", "usa_scaffali": "1",
    })
    r = login.get(f"/warehouse/sites/{site_id}/mappa")
    assert r.status_code == 200
    # NOPOS non deve mai comparire dentro un <title> (che decora solo i
    # blocchi POSIZIONATI, dentro un <rect>) — può legittimamente comparire
    # altrove in pagina (es. nell'elenco "senza posizione" o nel menu a
    # tendina per assegnargli un rettangolo appena disegnato).
    titoli = re.findall(r"<title>([^<]+)</title>", r.data.decode())
    assert not any("NOPOS" in t for t in titoli)
    assert b"Blocchi senza posizione" in r.data
    assert b"NOPOS" in r.data


def _png_bytes():
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )


def test_floor_plan_upload_serve_and_remove(login, app):
    import io
    site_id = _site(app)
    r = login.post(f"/warehouse/sites/{site_id}/planimetria/carica", data={
        "planimetria": (io.BytesIO(_png_bytes()), "pianta.png"), "width": "1000", "height": "600",
    }, content_type="multipart/form-data")
    assert r.status_code == 302
    with app.app_context():
        site = OperatingSite.query.get(site_id)
        assert site.ha_planimetria is True
        assert site.floor_plan_mimetype == "image/png"
        assert site.floor_plan_width == 1000.0

    r2 = login.get(f"/warehouse/sites/{site_id}/planimetria/immagine")
    assert r2.status_code == 200
    assert r2.content_type == "image/png"
    assert len(r2.data) > 0

    r3 = login.get(f"/warehouse/sites/{site_id}/mappa")
    assert b"<image" in r3.data

    login.post(f"/warehouse/sites/{site_id}/planimetria/rimuovi", data={})
    with app.app_context():
        assert OperatingSite.query.get(site_id).ha_planimetria is False


def test_floor_plan_upload_rejects_non_image(login, app):
    import io
    site_id = _site(app)
    r = login.post(f"/warehouse/sites/{site_id}/planimetria/carica", data={
        "planimetria": (io.BytesIO(b"questo non e' un'immagine"), "finta.png"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert "non riconosciuto".encode() in r.data
    with app.app_context():
        assert OperatingSite.query.get(site_id).ha_planimetria is False


def test_area_posiziona_updates_only_position_not_structure(login, app):
    site_id = _site(app)
    login.post("/warehouse/areas/new", data={
        "site_id": site_id, "code": "POS1", "name": "Posiziona Test", "area_type": "ROH",
        "usa_corsie": "1", "usa_scaffali": "1", "usa_cassette": "1",
    })
    with app.app_context():
        area_id = WarehouseArea.query.filter_by(code="POS1").one().id

    r = login.post(f"/warehouse/areas/{area_id}/posiziona",
                   data={"pos_x": "10", "pos_y": "20", "dim_x": "150", "dim_y": "90"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    with app.app_context():
        area = WarehouseArea.query.get(area_id)
        assert (area.pos_x, area.pos_y, area.dim_x, area.dim_y) == (10.0, 20.0, 150.0, 90.0)
        # La struttura non viene toccata dal posizionamento via disegno
        assert area.usa_corsie is True and area.usa_scaffali is True and area.usa_cassette is True


def test_area_posiziona_rejects_zero_size_rectangle(login, app):
    site_id = _site(app)
    login.post("/warehouse/areas/new", data={
        "site_id": site_id, "code": "POS2", "name": "Posiziona Zero", "area_type": "ROH",
        "usa_corsie": "1",
    })
    with app.app_context():
        area_id = WarehouseArea.query.filter_by(code="POS2").one().id

    r = login.post(f"/warehouse/areas/{area_id}/posiziona",
                   data={"pos_x": "0", "pos_y": "0", "dim_x": "0", "dim_y": "0"})
    assert r.status_code == 400
    with app.app_context():
        assert WarehouseArea.query.get(area_id).pos_x is None

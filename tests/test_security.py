from extensions import db
from models import User


def test_anonymous_is_redirected_from_accounting(client):
    r = client.get("/gl/journal_entry")
    assert r.status_code == 302 and "/auth/login" in r.headers["Location"]


def test_open_redirect_after_login_is_blocked(client):
    r = client.post("/auth/login?next=https://evil.example/", data={"username": "tester", "password": "secret"})
    assert r.status_code == 302
    assert "evil.example" not in r.headers["Location"]


def test_payroll_account_config_is_commercialista_only(client, app):
    with app.app_context():
        uid = User.query.filter_by(username="operator").one().id
    with client.session_transaction() as session:
        session["_user_id"] = str(uid); session["_fresh"] = True
    assert client.get("/paghe/config").status_code == 403


def test_csrf_rejects_post_without_token(client, app):
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post("/auth/login", data={"username": "tester", "password": "secret"})
        assert response.status_code == 400
        page = client.get("/auth/login")
        assert b'name="csrf_token"' in page.data
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_logout_is_not_get_side_effect(login):
    assert login.get("/auth/logout").status_code == 405

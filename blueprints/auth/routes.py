from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user

from extensions import db
from models import User

auth_bp = Blueprint("auth", __name__, template_folder="../../templates/auth")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()

        if user is None or not user.check_password(password):
            flash("Utente o password non corretti.", "danger")
            return render_template("auth/login.html")

        if not user.is_active_flag:
            flash("Utenza disattivata. Contatta l'amministratore.", "danger")
            return render_template("auth/login.html")

        login_user(user, remember=True)
        flash(f"Benvenuto/a, {user.full_name}.", "success")
        next_page = request.args.get("next")
        return redirect(next_page or url_for("dashboard.home"))

    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Sessione terminata.", "info")
    return redirect(url_for("auth.login"))

"""
auth.py - регистрация, вход, выход
"""
from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db
from models import User

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('неверный email или пароль')

    return render_template('login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        if not email or '@' not in email:
            flash('error: wrong input')
        password = request.form.get('password')

        if User.query.filter_by(email=email).first():
            flash('такой email уже есть')
        else:
            user = User(
                email=email,
                password_hash=generate_password_hash(password),
                role='user'
            )
            db.session.add(user)
            db.session.commit()
            login_user(user)
            return redirect(url_for('index'))

    return render_template('register.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))

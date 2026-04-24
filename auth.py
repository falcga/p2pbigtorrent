"""
auth.py - регистрация, вход, выход
"""
from flask import Blueprint, render_template, redirect, url_for, request, flash
import html
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db
from models import User
from models import NewsPost
from markupsafe import Markup

try:
    import markdown as _markdown_lib
except Exception:
    _markdown_lib = None

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    public_news = NewsPost.query.filter(NewsPost.group_id.is_(None)).order_by(NewsPost.created_at.desc()).limit(20).all()
    for post in public_news:
        escaped = html.escape(post.body or '')
        if _markdown_lib is None:
            post.body_html = Markup(escaped.replace('\n', '<br>'))
        else:
            post.body_html = Markup(_markdown_lib.markdown(escaped, extensions=['extra', 'sane_lists']))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            if user.is_blocked:
                flash('аккаунт заблокирован. Обратитесь к администратору.')
                return render_template('login.html', public_news=public_news)
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('неверный email или пароль')

    return render_template('login.html', public_news=public_news)


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        if not email or '@' not in email or not password or len(password) < 6:
            flash('проверьте email и пароль (минимум 6 символов)')
            return render_template('register.html')

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

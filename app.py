"""
app.py - точка входа приложения
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

load_dotenv()

def create_app():
    app = Flask(__name__)

    # конфиг
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fallback-dev-key')

    # выбор базы данных: в памяти или файловая
    use_memory_db = os.getenv('USE_MEMORY_DB', 'false').lower() == 'true'

    if use_memory_db:
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        print('*** ВНИМАНИЕ: Используется БД в памяти (данные исчезнут при перезапуске) ***')
    else:
        # абсолютный путь к файловой БД
        basedir = Path(__file__).parent.absolute()
        instance_dir = basedir / 'instance'
        db_file = instance_dir / 'app.db'
        instance_dir.mkdir(exist_ok=True)
        app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_file}?timeout=30'

    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # импортируем расширения
    from extensions import db, login_manager

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'сначала войди в систему'

    # импорт моделей
    from models import User, File, Peer, Piece

    # создаём таблицы
    with app.app_context():
        db.create_all()
        # дефолтный админ
        if not User.query.filter_by(role='admin').first():
            from werkzeug.security import generate_password_hash
            admin = User(
                email='admin@local.local',
                password_hash=generate_password_hash('admin123'),
                role='admin'
            )
            db.session.add(admin)
            db.session.commit()
            print('создан дефолтный админ: admin@local.local / admin123')

    # загрузка пользователя
    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # подключаем blueprint'ы
    from auth import auth_bp
    from torrent_tracker import tracker_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(tracker_bp)

    # главная страница
    @app.route('/')
    def index():
        files = File.query.all()
        return render_template('index.html', files=files)

    # админка - отображение
    @app.route('/admin')
    @login_required
    def admin_panel():
        if current_user.role != 'admin':
            return 'доступ запрещён', 403
        files = File.query.all()
        return render_template('admin.html', files=files)

    # загрузка файла (только админ)
    @app.route('/admin/upload', methods=['POST'])
    @login_required
    def upload_file():
        if current_user.role != 'admin':
            return 'доступ запрещён', 403

        if 'file' not in request.files:
            flash('файл не выбран')
            return redirect(url_for('admin_panel'))

        file = request.files['file']
        if file.filename == '':
            flash('файл не выбран')
            return redirect(url_for('admin_panel'))

        try:
            from file_manager import save_uploaded_file
            saved_file = save_uploaded_file(file, current_user.id)
            flash(f'файл "{saved_file.filename}" загружен')
        except Exception as e:
            flash(f'ошибка загрузки: {str(e)}')

        return redirect(url_for('admin_panel'))

    # обработчики ошибок
    @app.errorhandler(404)
    def not_found(e):
        return render_template('404.html'), 404

    @app.errorhandler(403)
    def forbidden(e):
        return render_template('403.html'), 403

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(
        debug=os.getenv('FLASK_DEBUG', 'False').lower() == 'true',
        port=int(os.getenv('FLASK_RUN_PORT', 5000))
    )
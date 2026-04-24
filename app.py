"""
app.py - точка входа приложения
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from sqlalchemy import text

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
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

    # импортируем расширения
    from extensions import db, login_manager

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'сначала войди в систему'

    # импорт моделей
    from models import User, File, Peer, Piece, Group, FileVisibility

    # создаём таблицы
    with app.app_context():
        _run_sqlite_migrations(db)
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

    @app.after_request
    def add_security_headers(resp):
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        resp.headers['X-Frame-Options'] = 'DENY'
        resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        resp.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        resp.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; connect-src 'self';"
        )
        return resp

    # главная страница
    @app.route('/')
    @login_required
    def index():
        files = _get_files_for_user(File, FileVisibility, current_user)
        return render_template('index.html', files=files)

    # проверка работоспособности БД
    @app.route('/health')
    def health_check():
        try:
            db.session.execute(text('SELECT 1'))
            return {'status': 'healthy', 'database': 'connected'}, 200
        except Exception as e:
            return {'status': 'unhealthy', 'error': str(e)}, 500
            
    # админка - отображение
    @app.route('/admin')
    @login_required
    def admin_panel():
        if current_user.role != 'admin':
            return 'доступ запрещён', 403
        files = File.query.all()
        groups = Group.query.order_by(Group.name).all()
        users = User.query.order_by(User.email).all()
        return render_template('admin.html', files=files, groups=groups, users=users)

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
            display_name = request.form.get('display_name') or file.filename
            group_id_raw = request.form.get('group_id')
            group_id = int(group_id_raw) if group_id_raw else None

            saved_file, is_duplicate = save_uploaded_file(
                file,
                current_user.id,
                display_name=display_name,
                group_id=group_id
            )
            if is_duplicate:
                flash(f'файл уже был в системе, добавлена новая видимость: "{display_name}"')
            else:
                flash(f'файл "{saved_file.filename}" загружен')
        except Exception as e:
            flash(f'ошибка загрузки: {str(e)}')

        return redirect(url_for('admin_panel'))

    @app.route('/admin/delete/<int:file_id>', methods=['POST'])
    @login_required
    def delete_file_admin(file_id):
        if current_user.role != 'admin':
            return 'доступ запрещён', 403

        from file_manager import delete_file

        if delete_file(file_id):
            flash('файл удален')
        else:
            flash('не удалось удалить файл')
        return redirect(url_for('admin_panel'))

    @app.route('/admin/groups', methods=['POST'])
    @login_required
    def create_group():
        if current_user.role != 'admin':
            return 'доступ запрещён', 403
        name = (request.form.get('name') or '').strip()
        if not name:
            flash('название группы не может быть пустым')
            return redirect(url_for('admin_panel'))
        if Group.query.filter_by(name=name).first():
            flash('такая группа уже существует')
            return redirect(url_for('admin_panel'))

        db.session.add(Group(name=name))
        db.session.commit()
        flash('группа создана')
        return redirect(url_for('admin_panel'))

    @app.route('/admin/users/<int:user_id>/group', methods=['POST'])
    @login_required
    def set_user_group(user_id):
        if current_user.role != 'admin':
            return 'доступ запрещён', 403
        user = db.session.get(User, user_id)
        if not user:
            flash('пользователь не найден')
            return redirect(url_for('admin_panel'))

        group_ids_raw = request.form.getlist('group_ids')
        selected_groups = []
        for raw_id in group_ids_raw:
            try:
                group_id = int(raw_id)
            except ValueError:
                continue
            group = db.session.get(Group, group_id)
            if group:
                selected_groups.append(group)

        user.groups = selected_groups
        db.session.commit()
        flash('группа пользователя обновлена')
        return redirect(url_for('admin_panel'))

    @app.route('/api/files/<int:file_id>', methods=['GET'])
    @login_required
    def file_info(file_id):
        from file_manager import get_file_info

        info = get_file_info(file_id)
        if not info:
            return jsonify({'error': 'файл не найден'}), 404
        if not _user_can_access_file(FileVisibility, file_id, current_user):
            return jsonify({'error': 'доступ запрещен'}), 403
        return jsonify(info)

    @app.route('/api/files', methods=['GET'])
    @login_required
    def files_list():
        files = _get_files_for_user(File, FileVisibility, current_user)
        return jsonify([
            {
                'id': item.id,
                'filename': item.filename,
                'file_size': item.file_size,
                'piece_length': item.piece_length,
                'piece_count': item.get_piece_count(),
                'content_hash': item.content_hash,
            }
            for item in files
        ])

    # обработчики ошибок
    @app.errorhandler(404)
    def not_found(e):
        return render_template('404.html'), 404

    @app.errorhandler(403)
    def forbidden(e):
        return render_template('403.html'), 403

    return app


def _run_sqlite_migrations(db):
    """
    Легкая автоподготовка SQLite без Alembic.
    Только добавление недостающих колонок/индексов.
    """
    with db.engine.begin() as conn:
        user_cols = {r[1] for r in conn.execute(text("PRAGMA table_info('users')"))}
        if 'group_id' not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN group_id INTEGER"))

        file_cols = {r[1] for r in conn.execute(text("PRAGMA table_info('files')"))}
        if 'content_hash' not in file_cols:
            conn.execute(text("ALTER TABLE files ADD COLUMN content_hash VARCHAR(64)"))

        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS groups ("
            "id INTEGER PRIMARY KEY, "
            "name VARCHAR(120) UNIQUE NOT NULL, "
            "created_at DATETIME)"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS file_visibilities ("
            "id INTEGER PRIMARY KEY, "
            "file_id INTEGER NOT NULL, "
            "group_id INTEGER NULL, "
            "display_name VARCHAR(255) NOT NULL, "
            "created_at DATETIME)"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_file_vis_file_group "
            "ON file_visibilities (file_id, group_id)"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS user_groups ("
            "user_id INTEGER NOT NULL, "
            "group_id INTEGER NOT NULL, "
            "PRIMARY KEY (user_id, group_id))"
        ))
        conn.execute(text(
            "INSERT OR IGNORE INTO user_groups(user_id, group_id) "
            "SELECT id, group_id FROM users WHERE group_id IS NOT NULL"
        ))


def _get_files_for_user(File, FileVisibility, user):
    user_group_ids = _get_user_group_ids(user)
    rows = (
        File.query.session.query(File, FileVisibility.display_name)
        .join(FileVisibility, FileVisibility.file_id == File.id)
        .filter(
            (FileVisibility.group_id.is_(None)) |
            (FileVisibility.group_id.in_(user_group_ids))
        )
        .order_by(File.uploaded_at.desc())
        .all()
    )
    files = []
    for file_obj, display_name in rows:
        file_obj.display_name = display_name
        files.append(file_obj)
    return files


def _user_can_access_file(FileVisibility, file_id, user):
    user_group_ids = _get_user_group_ids(user)
    return FileVisibility.query.filter(
        FileVisibility.file_id == file_id,
        ((FileVisibility.group_id.is_(None)) | (FileVisibility.group_id.in_(user_group_ids)))
    ).first() is not None


def _get_user_group_ids(user):
    return [group.id for group in getattr(user, 'groups', [])]

if __name__ == '__main__':
    app = create_app()
    app.run(
        debug=os.getenv('FLASK_DEBUG', 'False').lower() == 'true',
        port=int(os.getenv('FLASK_RUN_PORT', 5000))
    )

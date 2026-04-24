"""
app.py - точка входа приложения
"""
import os
import html
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user, logout_user
from sqlalchemy import text
from markupsafe import Markup

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False

try:
    import markdown as _markdown_lib
except Exception:
    _markdown_lib = None

load_dotenv()


def _debug_log(run_id, hypothesis_id, location, message, data):
    # region agent log
    try:
        payload = {
            'sessionId': 'bbf891',
            'runId': run_id,
            'hypothesisId': hypothesis_id,
            'location': location,
            'message': message,
            'data': data,
            'timestamp': int(datetime.utcnow().timestamp() * 1000),
        }
        with open(Path(__file__).parent / 'debug-bbf891.log', 'a', encoding='utf-8') as f:
            f.write(json.dumps(payload, ensure_ascii=False) + '\n')
    except Exception:
        pass
    # endregion

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

    _configure_logging(app)

    # импортируем расширения
    from extensions import db, login_manager

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'сначала войди в систему'

    # импорт моделей
    from models import User, File, Peer, Piece, Group, FileVisibility, NewsPost, FileComment

    # создаём таблицы
    with app.app_context():
        _run_sqlite_migrations(db)
        db.create_all()
        # дефолтный админ
        if not User.query.filter_by(email='admin@local.local').first():
            from werkzeug.security import generate_password_hash
            admin = User(
                email='admin@local.local',
                password_hash=generate_password_hash('admin123'),
                role='superadmin'
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

    @app.before_request
    def enforce_block():
        app.logger.info(
            "request.start method=%s path=%s user=%s remote=%s",
            request.method,
            request.path,
            getattr(current_user, 'email', 'anonymous'),
            request.remote_addr
        )
        if not current_user.is_authenticated:
            return None
        if not getattr(current_user, 'is_blocked', False):
            return None
        blocked_until = getattr(current_user, 'blocked_until', None)
        if blocked_until and blocked_until <= datetime.utcnow():
            current_user.is_blocked = False
            current_user.block_reason = None
            current_user.blocked_until = None
            db.session.commit()
            return None
        logout_user()
        flash('доступ временно заблокирован администратором')
        return redirect(url_for('auth.login'))

    @app.context_processor
    def inject_admin_flag():
        if not current_user.is_authenticated:
            return {'can_open_admin': False, 'legal_html': _get_legal_html(), 'legal_raw': _get_legal_raw()}
        return {'can_open_admin': _can_open_admin(current_user), 'legal_html': _get_legal_html(), 'legal_raw': _get_legal_raw()}

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
        app.logger.info(
            "request.end method=%s path=%s status=%s",
            request.method,
            request.path,
            resp.status_code
        )
        return resp

    # главная страница
    @app.route('/')
    @login_required
    def index():
        files = _get_files_for_user(File, FileVisibility, current_user)
        news = _get_news_for_user(NewsPost, current_user)
        for post in news:
            post.body_html = _render_markdown(post.body)
        comments = _get_comments_for_files(FileComment, [f.id for f in files])
        publish_groups = Group.query.order_by(Group.name).all() if _is_superadmin(current_user) else current_user.admin_groups
        return render_template('index.html', files=files, news=news, comments=comments, publish_groups=publish_groups)

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
        _debug_log('pre-fix', 'H1', 'app.py:admin_panel', 'admin route entered', {
            'user_id': getattr(current_user, 'id', None),
            'role': getattr(current_user, 'role', None),
        })
        if not _can_open_admin(current_user):
            _debug_log('pre-fix', 'H1', 'app.py:admin_panel', 'admin access denied', {'user_id': getattr(current_user, 'id', None)})
            return 'доступ запрещён', 403
        try:
            files = _get_manageable_files(FileVisibility, File, current_user)
            visibilities = _get_manageable_visibilities(FileVisibility, current_user)
            groups = Group.query.order_by(Group.name).all() if _is_superadmin(current_user) else current_user.admin_groups
            users = _get_manageable_users(User, current_user)
            news = _get_news_for_user(NewsPost, current_user)
            comments = _get_comments_for_moderation(FileComment, current_user)
            _debug_log('pre-fix', 'H2', 'app.py:admin_panel', 'admin data prepared', {
                'files': len(files),
                'visibilities': len(visibilities),
                'groups': len(groups),
                'users': len(users),
                'news': len(news),
                'comments': len(comments),
            })
            return render_template('admin.html', files=files, visibilities=visibilities, groups=groups, users=users, news=news, comments=comments)
        except Exception as e:
            _debug_log('pre-fix', 'H5', 'app.py:admin_panel', 'admin route exception', {
                'exception_type': type(e).__name__,
                'exception': str(e),
            })
            app.logger.exception("admin_panel failed")
            raise

    @app.route('/admin/logs')
    @login_required
    def admin_logs():
        if not _can_open_admin(current_user):
            return 'доступ запрещён', 403
        app_log_path = Path(app.instance_path) / 'app.log'
        debug_log_path = Path(app.root_path) / 'debug-bbf891.log'
        app_lines = _tail_file(app_log_path, 300)
        debug_lines = _tail_file(debug_log_path, 300)
        return render_template('logs.html', app_log=app_lines, debug_log=debug_lines)

    # загрузка файла (только админ)
    @app.route('/admin/upload', methods=['POST'])
    @login_required
    def upload_file():
        if not _can_open_admin(current_user):
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
            if not _can_manage_group(current_user, group_id):
                flash('нет прав на выбранную группу')
                return redirect(url_for('admin_panel'))

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
        if not _can_open_admin(current_user):
            return 'доступ запрещён', 403
        if not _can_manage_file(FileVisibility, file_id, current_user):
            flash('нет прав на удаление этого файла')
            return redirect(url_for('admin_panel'))

        from file_manager import delete_file

        if delete_file(file_id):
            flash('файл удален')
        else:
            flash('не удалось удалить файл')
        return redirect(url_for('admin_panel'))

    @app.route('/admin/groups', methods=['POST'])
    @login_required
    def create_group():
        if not _is_superadmin(current_user):
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
        if not _can_open_admin(current_user):
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
        if not _can_assign_groups(current_user, selected_groups):
            flash('нет прав на назначение части групп')
            return redirect(url_for('admin_panel'))

        user.groups = selected_groups
        db.session.commit()
        flash('группа пользователя обновлена')
        return redirect(url_for('admin_panel'))

    @app.route('/admin/users/<int:user_id>/ban', methods=['POST'])
    @login_required
    def ban_user(user_id):
        if not _can_open_admin(current_user):
            return 'доступ запрещён', 403
        user = db.session.get(User, user_id)
        if not user:
            flash('пользователь не найден')
            return redirect(url_for('admin_panel'))
        if _is_superadmin(user):
            flash('нельзя блокировать администраторов')
            return redirect(url_for('admin_panel'))

        reason = (request.form.get('reason') or '').strip()
        hours_raw = request.form.get('hours') or '24'
        try:
            hours = max(1, min(24 * 365, int(hours_raw)))
        except ValueError:
            hours = 24
        user.is_blocked = True
        user.block_reason = reason or None
        user.blocked_until = datetime.utcnow() + timedelta(hours=hours)
        db.session.commit()
        flash('пользователь заблокирован')
        return redirect(url_for('admin_panel'))

    @app.route('/admin/users/<int:user_id>/unban', methods=['POST'])
    @login_required
    def unban_user(user_id):
        if not _can_open_admin(current_user):
            return 'доступ запрещён', 403
        user = db.session.get(User, user_id)
        if not user:
            flash('пользователь не найден')
            return redirect(url_for('admin_panel'))
        user.is_blocked = False
        user.block_reason = None
        user.blocked_until = None
        db.session.commit()
        flash('пользователь разблокирован')
        return redirect(url_for('admin_panel'))

    @app.route('/admin/users/<int:user_id>/group-admins', methods=['POST'])
    @login_required
    def set_user_group_admin_rights(user_id):
        if not _is_superadmin(current_user):
            return 'доступ запрещён', 403
        user = db.session.get(User, user_id)
        if not user:
            flash('пользователь не найден')
            return redirect(url_for('admin_panel'))
        group_ids = request.form.getlist('admin_group_ids')
        user.admin_groups = [g for g in Group.query.filter(Group.id.in_(group_ids)).all()] if group_ids else []
        db.session.commit()
        flash('права админа групп обновлены')
        return redirect(url_for('admin_panel'))

    @app.route('/admin/rename-visibility/<int:visibility_id>', methods=['POST'])
    @login_required
    def rename_visibility(visibility_id):
        if not _can_open_admin(current_user):
            return 'доступ запрещён', 403
        visibility = db.session.get(FileVisibility, visibility_id)
        if not visibility:
            flash('запись не найдена')
            return redirect(url_for('admin_panel'))
        if not _can_manage_group(current_user, visibility.group_id):
            flash('нет прав на переименование')
            return redirect(url_for('admin_panel'))
        new_name = (request.form.get('display_name') or '').strip()
        if not new_name:
            flash('имя не должно быть пустым')
            return redirect(url_for('admin_panel'))
        visibility.display_name = new_name
        db.session.commit()
        flash('имя файла обновлено')
        return redirect(url_for('admin_panel'))

    @app.route('/news', methods=['POST'])
    @login_required
    def create_news():
        title = (request.form.get('title') or '').strip()
        body = (request.form.get('body') or '').strip()
        group_id_raw = request.form.get('group_id')
        group_id = int(group_id_raw) if group_id_raw else None
        if not title or not body:
            flash('заполните заголовок и текст новости')
            return redirect(url_for('index'))
        if not _can_manage_group(current_user, group_id):
            flash('нет прав публиковать новости для этой группы')
            return redirect(url_for('index'))
        db.session.add(NewsPost(title=title, body=body, group_id=group_id, author_id=current_user.id))
        db.session.commit()
        flash('новость опубликована')
        return redirect(url_for('index'))

    @app.route('/admin/news/<int:news_id>/edit', methods=['POST'])
    @login_required
    def edit_news(news_id):
        if not _can_open_admin(current_user):
            return 'доступ запрещён', 403
        post = db.session.get(NewsPost, news_id)
        if not post:
            flash('новость не найдена')
            return redirect(url_for('admin_panel'))
        if not _can_manage_group(current_user, post.group_id):
            flash('нет прав на редактирование новости')
            return redirect(url_for('admin_panel'))
        title = (request.form.get('title') or '').strip()
        body = (request.form.get('body') or '').strip()
        if not title or not body:
            flash('заполните заголовок и текст')
            return redirect(url_for('admin_panel'))
        post.title = title
        post.body = body
        db.session.commit()
        flash('новость обновлена')
        return redirect(url_for('admin_panel'))

    @app.route('/admin/news/<int:news_id>/delete', methods=['POST'])
    @login_required
    def delete_news(news_id):
        if not _can_open_admin(current_user):
            return 'доступ запрещён', 403
        post = db.session.get(NewsPost, news_id)
        if not post:
            flash('новость не найдена')
            return redirect(url_for('admin_panel'))
        if not _can_manage_group(current_user, post.group_id):
            flash('нет прав на удаление новости')
            return redirect(url_for('admin_panel'))
        db.session.delete(post)
        db.session.commit()
        flash('новость удалена')
        return redirect(url_for('admin_panel'))

    @app.route('/files/<int:file_id>/comments', methods=['POST'])
    @login_required
    def add_file_comment(file_id):
        if not _user_can_access_file(FileVisibility, file_id, current_user):
            return 'доступ запрещён', 403
        content = (request.form.get('content') or '').strip()
        if not content:
            flash('комментарий пустой')
            return redirect(url_for('index'))
        db.session.add(FileComment(file_id=file_id, user_id=current_user.id, content=content))
        db.session.commit()
        return redirect(url_for('index'))

    @app.route('/admin/comments/<int:comment_id>/edit', methods=['POST'])
    @login_required
    def edit_comment(comment_id):
        if not _can_open_admin(current_user):
            return 'доступ запрещён', 403
        comment = db.session.get(FileComment, comment_id)
        if not comment:
            flash('комментарий не найден')
            return redirect(url_for('admin_panel'))
        if not _can_manage_comment(FileVisibility, comment, current_user):
            flash('нет прав на редактирование комментария')
            return redirect(url_for('admin_panel'))
        content = (request.form.get('content') or '').strip()
        if not content:
            flash('текст комментария пустой')
            return redirect(url_for('admin_panel'))
        comment.content = content
        db.session.commit()
        flash('комментарий обновлен')
        return redirect(url_for('admin_panel'))

    @app.route('/admin/comments/<int:comment_id>/delete', methods=['POST'])
    @login_required
    def delete_comment(comment_id):
        if not _can_open_admin(current_user):
            return 'доступ запрещён', 403
        comment = db.session.get(FileComment, comment_id)
        if not comment:
            flash('комментарий не найден')
            return redirect(url_for('admin_panel'))
        if not _can_manage_comment(FileVisibility, comment, current_user):
            flash('нет прав на удаление комментария')
            return redirect(url_for('admin_panel'))
        db.session.delete(comment)
        db.session.commit()
        flash('комментарий удален')
        return redirect(url_for('admin_panel'))

    @app.route('/admin/legal', methods=['POST'])
    @login_required
    def update_legal():
        if not _is_superadmin(current_user):
            return 'доступ запрещён', 403
        legal_text = request.form.get('legal_markdown') or ''
        legal_path = Path(app.root_path) / 'templates' / 'legal.md'
        legal_path.write_text(legal_text, encoding='utf-8')
        flash('legal обновлен')
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

    @app.errorhandler(500)
    def internal_error(e):
        _debug_log('pre-fix', 'H5', 'app.py:errorhandler500', 'internal server error', {
            'error_type': type(e).__name__,
            'error': str(e),
        })
        app.logger.exception("internal server error")
        return render_template('500.html'), 500

    return app


def _run_sqlite_migrations(db):
    """
    Легкая автоподготовка SQLite без Alembic.
    Только добавление недостающих колонок/индексов.
    """
    with db.engine.begin() as conn:
        user_cols = {r[1] for r in conn.execute(text("PRAGMA table_info('users')"))}
        _debug_log('pre-fix', 'H3', 'app.py:_run_sqlite_migrations', 'users columns before migration', {
            'columns': sorted(list(user_cols))
        })
        if 'group_id' not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN group_id INTEGER"))
        if 'is_blocked' not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_blocked BOOLEAN DEFAULT 0"))
        if 'block_reason' not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN block_reason TEXT"))
        if 'blocked_until' not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN blocked_until DATETIME"))

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
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS group_admins ("
            "user_id INTEGER NOT NULL, "
            "group_id INTEGER NOT NULL, "
            "PRIMARY KEY (user_id, group_id))"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS news_posts ("
            "id INTEGER PRIMARY KEY, "
            "title VARCHAR(255) NOT NULL, "
            "body TEXT NOT NULL, "
            "group_id INTEGER NULL, "
            "author_id INTEGER NOT NULL, "
            "created_at DATETIME)"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS file_comments ("
            "id INTEGER PRIMARY KEY, "
            "file_id INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, "
            "content TEXT NOT NULL, "
            "created_at DATETIME)"
        ))
        conn.execute(text(
            "UPDATE users SET role='superadmin' WHERE role='admin'"
        ))


def _configure_logging(app):
    log_dir = Path(app.instance_path)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / 'app.log'

    formatter = logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s %(message)s'
    )

    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    app.logger.setLevel(logging.INFO)
    app.logger.handlers = [file_handler]
    app.logger.propagate = False

    werkzeug_logger = logging.getLogger('werkzeug')
    werkzeug_logger.setLevel(logging.INFO)
    werkzeug_logger.handlers = [file_handler]
    werkzeug_logger.propagate = False


def _tail_file(path, max_lines):
    if not path.exists():
        return [f'лог-файл не найден: {path}']
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    return lines[-max_lines:]


def _get_files_for_user(File, FileVisibility, user):
    user_group_ids = _get_user_group_ids(user)
    rows = (
        File.query.session.query(File, FileVisibility)
        .join(FileVisibility, FileVisibility.file_id == File.id)
        .filter(
            (FileVisibility.group_id.is_(None)) |
            (FileVisibility.group_id.in_(user_group_ids))
        )
        .order_by(File.uploaded_at.desc())
        .all()
    )
    files = []
    for file_obj, visibility in rows:
        file_obj.display_name = visibility.display_name
        file_obj.visibility_id = visibility.id
        file_obj.group = visibility.group
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


def _is_superadmin(user):
    return getattr(user, 'role', '') in ('superadmin', 'admin')


def _can_open_admin(user):
    return _is_superadmin(user) or len(getattr(user, 'admin_groups', [])) > 0


def _can_manage_group(user, group_id):
    if _is_superadmin(user):
        return True
    if group_id is None:
        return False
    return group_id in [group.id for group in getattr(user, 'admin_groups', [])]


def _can_assign_groups(actor, groups):
    if _is_superadmin(actor):
        return True
    allowed = {g.id for g in getattr(actor, 'admin_groups', [])}
    return all(g.id in allowed for g in groups)


def _can_manage_file(FileVisibility, file_id, user):
    vis = FileVisibility.query.filter_by(file_id=file_id).all()
    if _is_superadmin(user):
        return True
    managed = {g.id for g in getattr(user, 'admin_groups', [])}
    return all(v.group_id in managed for v in vis if v.group_id is not None)


def _get_manageable_users(User, current_user):
    _debug_log('pre-fix', 'H4', 'app.py:_get_manageable_users', 'loading manageable users', {
        'actor_id': getattr(current_user, 'id', None),
        'actor_role': getattr(current_user, 'role', None),
    })
    if _is_superadmin(current_user):
        users = User.query.order_by(User.email).all()
        _debug_log('pre-fix', 'H4', 'app.py:_get_manageable_users', 'loaded manageable users', {'count': len(users)})
        return users
    managed = {g.id for g in current_user.admin_groups}
    result = []
    for user in User.query.order_by(User.email).all():
        user_group_ids = {g.id for g in user.groups}
        if user_group_ids & managed:
            result.append(user)
    _debug_log('pre-fix', 'H4', 'app.py:_get_manageable_users', 'loaded manageable users', {'count': len(result)})
    return result


def _can_manage_comment(FileVisibility, comment, current_user):
    if _is_superadmin(current_user):
        return True
    visibilities = FileVisibility.query.filter_by(file_id=comment.file_id).all()
    managed = {g.id for g in current_user.admin_groups}
    return any(v.group_id in managed for v in visibilities if v.group_id is not None)


def _get_manageable_files(FileVisibility, File, current_user):
    if _is_superadmin(current_user):
        return File.query.order_by(File.uploaded_at.desc()).all()
    managed = [g.id for g in current_user.admin_groups]
    file_ids = [v.file_id for v in FileVisibility.query.filter(FileVisibility.group_id.in_(managed)).all()]
    return File.query.filter(File.id.in_(file_ids)).order_by(File.uploaded_at.desc()).all()


def _get_manageable_visibilities(FileVisibility, current_user):
    if _is_superadmin(current_user):
        return FileVisibility.query.order_by(FileVisibility.id.desc()).all()
    managed = [g.id for g in current_user.admin_groups]
    return FileVisibility.query.filter(FileVisibility.group_id.in_(managed)).order_by(FileVisibility.id.desc()).all()


def _get_comments_for_moderation(FileComment, current_user):
    comments = FileComment.query.order_by(FileComment.created_at.desc()).limit(200).all()
    if _is_superadmin(current_user):
        return comments
    managed = {g.id for g in current_user.admin_groups}
    result = []
    for item in comments:
        visibilities = item.file.visibilities
        if any(v.group_id in managed for v in visibilities if v.group_id is not None):
            result.append(item)
    return result


def _get_news_for_user(NewsPost, user):
    group_ids = _get_user_group_ids(user)
    return NewsPost.query.filter(
        (NewsPost.group_id.is_(None)) | (NewsPost.group_id.in_(group_ids))
    ).order_by(NewsPost.created_at.desc()).limit(20).all()


def _render_markdown(raw_text):
    escaped = html.escape(raw_text or '')
    if _markdown_lib is None:
        rendered = escaped.replace('\n', '<br>')
    else:
        rendered = _markdown_lib.markdown(escaped, extensions=['extra', 'sane_lists'])
    return Markup(rendered)


def _get_legal_html():
    legal_path = Path(__file__).parent / 'templates' / 'legal.md'
    if not legal_path.exists():
        return Markup('<p class="text-muted mb-0">Legal information is not configured.</p>')
    content = legal_path.read_text(encoding='utf-8')
    return _render_markdown(content)


def _get_legal_raw():
    legal_path = Path(__file__).parent / 'templates' / 'legal.md'
    if not legal_path.exists():
        return ''
    return legal_path.read_text(encoding='utf-8')


def _get_comments_for_files(FileComment, file_ids):
    if not file_ids:
        return {}
    comments = FileComment.query.filter(FileComment.file_id.in_(file_ids)).order_by(FileComment.created_at.desc()).all()
    mapping = {}
    for item in comments:
        mapping.setdefault(item.file_id, []).append(item)
    return mapping

if __name__ == '__main__':
    app = create_app()
    app.run(
        debug=os.getenv('FLASK_DEBUG', 'False').lower() == 'true',
        port=int(os.getenv('FLASK_RUN_PORT', 5000))
    )

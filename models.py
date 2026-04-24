"""
models.py - модели базы данных
описываем таблицы через sqlalchemy orm
"""

from extensions import db
from flask_login import UserMixin
from datetime import datetime
import json

# db инициализируется в app.py
# здесь только определение моделей


user_groups = db.Table(
    'user_groups',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('group_id', db.Integer, db.ForeignKey('groups.id'), primary_key=True),
)

group_admins = db.Table(
    'group_admins',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('group_id', db.Integer, db.ForeignKey('groups.id'), primary_key=True),
)


class User(UserMixin, db.Model):
    """пользователи системы"""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='user')  # admin или user
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'), nullable=True)
    is_blocked = db.Column(db.Boolean, default=False, nullable=False)
    block_reason = db.Column(db.Text, nullable=True)
    blocked_until = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # связи с другими таблицами
    files = db.relationship('File', backref='uploader', lazy=True)
    peers = db.relationship('Peer', backref='user', lazy=True)
    groups = db.relationship('Group', secondary=user_groups, back_populates='users', lazy='select')
    admin_groups = db.relationship('Group', secondary=group_admins, back_populates='admins', lazy='select')

    def __repr__(self):
        return f'<User {self.email} ({self.role})>'

    def is_admin(self):
        return self.role in ('admin', 'superadmin')

    def is_superadmin(self):
        return self.role in ('admin', 'superadmin')


class File(db.Model):
    """файлы доступные для p2p скачивания"""
    __tablename__ = 'files'

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)  # байты
    piece_length = db.Column(db.Integer, nullable=False)  # размер куска байты
    piece_hashes = db.Column(db.Text, nullable=False)  # json список хешей
    content_hash = db.Column(db.String(64), nullable=True)
    uploader_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    # связи
    peers = db.relationship('Peer', backref='file', lazy=True, cascade='all, delete-orphan')
    pieces = db.relationship('Piece', backref='file', lazy=True, cascade='all, delete-orphan')
    visibilities = db.relationship('FileVisibility', backref='file', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<File {self.filename} ({self.file_size} bytes)>'

    def get_piece_hashes_list(self):
        """распарсить json в список python"""
        return json.loads(self.piece_hashes)

    def get_piece_count(self):
        """количество кусков в файле"""
        return len(json.loads(self.piece_hashes))

    def get_size_mb(self):
        """размер в мегабайтах для отображения"""
        return round(self.file_size / (1024 * 1024), 2)


class Peer(db.Model):
    """пиры - источники кусков"""
    __tablename__ = 'peers'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # null для сервера
    file_id = db.Column(db.Integer, db.ForeignKey('files.id'), nullable=False)
    peer_id = db.Column(db.String(100), nullable=False)  # генерит клиент
    has_all_pieces = db.Column(db.Boolean, default=False)  # стал сидом
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # связь с кусками
    pieces = db.relationship('Piece', backref='peer', lazy=True, cascade='all, delete-orphan')

    __table_args__ = (
        db.UniqueConstraint('file_id', 'peer_id', name='unique_file_peer'),
    )

    def __repr__(self):
        return f'<Peer {self.peer_id} file={self.file_id} all={self.has_all_pieces}>'

    def update_last_seen(self):
        """обновить время последней активности"""
        self.last_seen = datetime.utcnow()
        db.session.commit()

    def get_pieces_count(self):
        """сколько кусков есть у этого пира"""
        return Piece.query.filter_by(peer_id=self.id, has_piece=True).count()


class Piece(db.Model):
    """куски файлов у конкретных пиров"""
    __tablename__ = 'pieces'

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey('files.id'), nullable=False)
    peer_id = db.Column(db.Integer, db.ForeignKey('peers.id'), nullable=False)
    piece_index = db.Column(db.Integer, nullable=False)  # номер куска от 0
    has_piece = db.Column(db.Boolean, default=True)  # есть ли кусок
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('file_id', 'peer_id', 'piece_index', name='unique_piece_peer'),
    )

    def __repr__(self):
        return f'<Piece file={self.file_id} peer={self.peer_id} idx={self.piece_index}>'


class SignalingMessage(db.Model):
    """сигнальные сообщения для webrtc"""
    __tablename__ = 'signaling'

    id = db.Column(db.Integer, primary_key=True)
    from_peer = db.Column(db.String(100), nullable=False)
    to_peer = db.Column(db.String(100), nullable=False)
    data = db.Column(db.Text, nullable=False)  # json сообщения
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    delivered = db.Column(db.Boolean, default=False)  # доставлено ли

    def __repr__(self):
        return f'<Signal {self.from_peer} -> {self.to_peer}>'

    def get_data_json(self):
        """распарсить данные сообщения"""
        return json.loads(self.data)

    def mark_delivered(self):
        """пометить как доставленное"""
        self.delivered = True
        db.session.commit()


class Group(db.Model):
    """группы пользователей"""
    __tablename__ = 'groups'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    users = db.relationship('User', secondary=user_groups, back_populates='groups', lazy='select')
    admins = db.relationship('User', secondary=group_admins, back_populates='admin_groups', lazy='select')
    visibilities = db.relationship('FileVisibility', backref='group', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Group {self.name}>'


class FileVisibility(db.Model):
    """
    Отображение файла для конкретной группы.
    group_id = NULL => файл общий для всех.
    """
    __tablename__ = 'file_visibilities'

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey('files.id'), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'), nullable=True)
    display_name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('file_id', 'group_id', name='unique_file_group_visibility'),
    )

    def __repr__(self):
        return f'<FileVisibility file={self.file_id} group={self.group_id} name={self.display_name}>'


class NewsPost(db.Model):
    __tablename__ = 'news_posts'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'), nullable=True)
    author_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    group = db.relationship('Group', lazy='joined')
    author = db.relationship('User', lazy='joined')


class FileComment(db.Model):
    __tablename__ = 'file_comments'

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey('files.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    file = db.relationship('File', lazy='joined')
    user = db.relationship('User', lazy='joined')


# вспомогательные функции для работы с бд

def init_db():
    """создать все таблицы"""
    db.create_all()
    print('база данных создана')


def create_admin_if_not_exists(email='admin@local.local', password='admin123'):
    """
    создать админа по умолчанию если база пустая
    только для разработки
    """
    from werkzeug.security import generate_password_hash

    admin = User.query.filter_by(role='admin').first()
    if not admin:
        admin = User(
            email=email,
            password_hash=generate_password_hash(password),
            role='admin'
        )
        db.session.add(admin)
        db.session.commit()
        print(f'создан админ: {email} / {password}')
        return admin
    return admin


def get_file_peers(file_id, exclude_peer_id=None):
    """
    получить всех пиров для файла
    можно исключить конкретного пира по peer_id
    """
    query = Peer.query.filter_by(file_id=file_id)
    if exclude_peer_id:
        query = query.filter(Peer.peer_id != exclude_peer_id)
    return query.all()


def get_peer_by_id(file_id, peer_id):
    """найти пира по file_id и peer_id"""
    return Peer.query.filter_by(
        file_id=file_id,
        peer_id=peer_id
    ).first()


def get_or_create_peer(file_id, peer_id, user_id=None):
    """
    найти или создать запись пира
    возвращает кортеж (peer, created)
    """
    peer = get_peer_by_id(file_id, peer_id)
    created = False

    if not peer:
        peer = Peer(
            file_id=file_id,
            peer_id=peer_id,
            user_id=user_id,
            has_all_pieces=False
        )
        db.session.add(peer)
        db.session.commit()
        created = True

    return peer, created


def update_piece_status(file_id, peer_id, piece_index, has_piece=True):
    """
    обновить статус куска у пира
    создаёт запись если её нет
    """
    peer = get_peer_by_id(file_id, peer_id)
    if not peer:
        return None

    piece = Piece.query.filter_by(
        file_id=file_id,
        peer_id=peer.id,
        piece_index=piece_index
    ).first()

    if piece:
        piece.has_piece = has_piece
        piece.updated_at = datetime.utcnow()
    else:
        piece = Piece(
            file_id=file_id,
            peer_id=peer.id,
            piece_index=piece_index,
            has_piece=has_piece
        )
        db.session.add(piece)

    db.session.commit()
    return piece


def get_pending_signals(peer_id):
    """
    получить все непрочитанные сигналы для пира
    и пометить как доставленные
    """
    messages = SignalingMessage.query.filter_by(
        to_peer=peer_id,
        delivered=False
    ).order_by(SignalingMessage.created_at).all()

    result = []
    for msg in messages:
        result.append({
            'from_peer': msg.from_peer,
            'data': msg.get_data_json()
        })
        msg.delivered = True

    db.session.commit()
    return result


def add_signal_message(from_peer, to_peer, data):
    """добавить сигнальное сообщение в очередь"""
    msg = SignalingMessage(
        from_peer=from_peer,
        to_peer=to_peer,
        data=json.dumps(data)
    )
    db.session.add(msg)
    db.session.commit()
    return msg


def cleanup_old_peers(hours=24):
    """удалить пиров которые давно не появлялись"""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    old_peers = Peer.query.filter(Peer.last_seen < cutoff).all()

    count = len(old_peers)
    for peer in old_peers:
        # каскадное удаление pieces настроено в связи
        db.session.delete(peer)

    db.session.commit()
    print(f'удалено {count} старых пиров')
    return count


def cleanup_old_signals(hours=1):
    """удалить старые сигнальные сообщения"""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    count = SignalingMessage.query.filter(
        SignalingMessage.created_at < cutoff
    ).delete()

    db.session.commit()
    print(f'удалено {count} старых сигналов')
    return count
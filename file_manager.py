"""
file_manager.py - управление файлами для Secure P2P File Distributor

что тут происходит:
- админ заливает файл
- режем на куски по 1 мб
- считаем sha256 каждого куска
- сохраняем куски на диск
- пишем метаданные в бд
- удаление файлов
- отдача кусков серверным пиром
"""

import os
import json
import hashlib
from pathlib import Path
from typing import List, Optional
from werkzeug.utils import secure_filename
from flask import current_app
from extensions import db
from models import File, Peer, Piece, FileVisibility

# отложенные импорты чтоб не было циклических зависимостей
# db и модели подтянем через функции когда понадобятся

# константы проекта
PIECE_LENGTH = 1024 * 1024  # 1 мб - размер одного куска
UPLOADS_DIR = 'static/uploads'
PIECES_DIR = 'pieces'
SERVER_PEER_ID = 'server'  # жёсткий id для серверного источника


def _get_db():
    """ленивое получение db - циклические импорты не наш путь"""
    from extensions import db
    return db


def _get_models():
    """ленивое получение моделей"""
    from models import File, Peer, Piece, FileVisibility
    return File, Peer, Piece, FileVisibility


def ensure_directories():
    """создаём папки если их нет"""
    base_path = Path(current_app.root_path) / UPLOADS_DIR / PIECES_DIR
    base_path.mkdir(parents=True, exist_ok=True)
    return base_path


def calculate_piece_hash(data: bytes) -> str:
    """
    sha256 куска
    вход: байты
    выход: hex строка
    """
    return hashlib.sha256(data).hexdigest()


def calculate_file_hash(file_path: Path) -> str:
    """SHA-256 всего файла для дедупликации."""
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()


def split_file_and_save_pieces(file_path: Path, file_id: int) -> List[str]:
    """
    режем файл на куски и сохраняем на диск
    вход: путь к исходнику и id файла в бд
    выход: список хешей всех кусков
    """
    base_path = ensure_directories()
    file_pieces_dir = base_path / str(file_id)
    file_pieces_dir.mkdir(exist_ok=True)

    piece_hashes = []
    piece_index = 0

    with open(file_path, 'rb') as f:
        while True:
            piece_data = f.read(PIECE_LENGTH)
            if not piece_data:
                break

            # хеш считаем
            piece_hash = calculate_piece_hash(piece_data)
            piece_hashes.append(piece_hash)

            # кусок на диск
            piece_path = file_pieces_dir / f"{piece_index}.bin"
            with open(piece_path, 'wb') as piece_file:
                piece_file.write(piece_data)

            piece_index += 1

    return piece_hashes


def save_uploaded_file(uploaded_file, uploader_id: int, db=None, display_name=None, group_id=None):
    """
    главная функция загрузки от админа
    вход: объект файла из формы и id админа
    выход: объект File из бд или None если всё сломалось
    """
    if db is None:
        db = _get_db()

    File, Peer, Piece, FileVisibility = _get_models()

    # чистим имя файла от всякой дряни
    original_filename = uploaded_file.filename
    safe_filename = secure_filename(original_filename)

    # временный файл пока не порежем
    temp_path = Path(current_app.root_path) / UPLOADS_DIR / f"temp_{safe_filename}"
    temp_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        uploaded_file.save(temp_path)
        file_size = temp_path.stat().st_size
        file_hash = calculate_file_hash(temp_path)

        # Если такой файл уже есть - переиспользуем его и только настраиваем видимость/имя.
        existing_file = File.query.filter_by(content_hash=file_hash).first()
        if existing_file:
            resolved_name = display_name or safe_filename
            visibility = FileVisibility.query.filter_by(
                file_id=existing_file.id,
                group_id=group_id
            ).first()
            if visibility:
                visibility.display_name = resolved_name
            else:
                db.session.add(FileVisibility(
                    file_id=existing_file.id,
                    group_id=group_id,
                    display_name=resolved_name
                ))
            db.session.commit()
            return existing_file, True

        # запись в бд сначала без хешей
        new_file = File(
            filename=safe_filename,
            file_size=file_size,
            piece_length=PIECE_LENGTH,
            piece_hashes='[]',  # временная заглушка
            content_hash=file_hash,
            uploader_id=uploader_id
        )
        db.session.add(new_file)
        db.session.flush()  # нужен id для папки с кусками

        # режем и получаем хеши
        piece_hashes = split_file_and_save_pieces(temp_path, new_file.id)

        # обновляем хеши в бд
        new_file.piece_hashes = json.dumps(piece_hashes)
        db.session.add(FileVisibility(
            file_id=new_file.id,
            group_id=group_id,
            display_name=display_name or safe_filename
        ))

        # сервер как пир - у него всё есть
        server_peer = Peer(
            user_id=None,  # сервер не пользователь
            file_id=new_file.id,
            peer_id=SERVER_PEER_ID,
            has_all_pieces=True
        )
        db.session.add(server_peer)
        db.session.flush()  # нужен id пира

        # записываем все куски за сервером
        for piece_index in range(len(piece_hashes)):
            piece_record = Piece(
                file_id=new_file.id,
                peer_id=server_peer.id,
                piece_index=piece_index,
                has_piece=True
            )
            db.session.add(piece_record)

        db.session.commit()

        current_app.logger.info(
            f"файл '{safe_filename}' загружен | "
            f"размер {file_size} байт | кусков {len(piece_hashes)}"
        )

        return new_file, False

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"ошибка загрузки: {e}")
        raise
    finally:
        # темповый файл больше не нужен
        if temp_path.exists():
            temp_path.unlink()


def get_piece_data(file_id: int, piece_index: int) -> Optional[bytes]:
    """
    достать кусок с диска
    вход: id файла и номер куска
    выход: байты куска или None
    """
    base_path = ensure_directories()
    piece_path = base_path / str(file_id) / f"{piece_index}.bin"

    if not piece_path.exists():
        current_app.logger.warning(f"куска нет: {piece_path}")
        return None

    try:
        with open(piece_path, 'rb') as f:
            return f.read()
    except Exception as e:
        current_app.logger.error(f"ошибка чтения куска: {e}")
        return None


def get_file_piece_hashes(file_id: int) -> Optional[List[str]]:
    """
    список хешей кусков для файла
    вход: id файла
    выход: список hex строк или None
    """
    File, _, _, _ = _get_models()
    db = _get_db()

    file_record = db.session.get(File, file_id)
    if file_record:
        return json.loads(file_record.piece_hashes)
    return None


def delete_file(file_id: int, db=None) -> bool:
    """
    полное удаление файла из системы
    вход: id файла
    выход: True если ок False если провал
    """
    if db is None:
        db = _get_db()

    File, Peer, Piece, FileVisibility = _get_models()

    try:
        file_record = db.session.get(File, file_id)
        if not file_record:
            return False

        # чистим куски в бд
        Piece.query.filter_by(file_id=file_id).delete()

        # чистим пиров в бд
        Peer.query.filter_by(file_id=file_id).delete()

        # чистим видимость
        FileVisibility.query.filter_by(file_id=file_id).delete()

        # удаляем сам файл из бд
        db.session.delete(file_record)
        db.session.commit()

        # удаляем куски с диска
        base_path = ensure_directories()
        pieces_dir = base_path / str(file_id)
        if pieces_dir.exists():
            import shutil
            shutil.rmtree(pieces_dir)

        current_app.logger.info(f"файл id={file_id} удалён")
        return True

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"ошибка удаления: {e}")
        return False


def get_all_files():
    """все файлы из бд списком"""
    File, _, _, _ = _get_models()
    db = _get_db()
    return db.session.query(File).order_by(File.id.desc()).all()


def get_file_info(file_id: int) -> Optional[dict]:
    """
    инфо о файле для фронта
    вход: id файла
    выход: словарь с полями для клиента
    """
    File, _, _, _ = _get_models()
    db = _get_db()

    file_record = db.session.get(File, file_id)
    if not file_record:
        return None

    piece_hashes = json.loads(file_record.piece_hashes)

    return {
        'id': file_record.id,
        'filename': file_record.filename,
        'file_size': file_record.file_size,
        'piece_length': file_record.piece_length,
        'piece_count': len(piece_hashes),
        'piece_hashes': piece_hashes,
        'content_hash': file_record.content_hash
    }


def is_server_peer_registered(file_id: int) -> bool:
    """
    проверка есть ли серверный пир для файла
    """
    _, Peer, _, _ = _get_models()
    db = _get_db()

    server_peer = Peer.query.filter_by(
        file_id=file_id,
        peer_id=SERVER_PEER_ID
    ).first()

    return server_peer is not None


def register_server_peer(file_id: int, db=None) -> bool:
    """
    ручная регистрация серверного пира
    пригодится если файл залили в обход file_manager
    вход: id файла
    выход: True если получилось
    """
    if db is None:
        db = _get_db()

    _, Peer, Piece, _ = _get_models()

    if is_server_peer_registered(file_id):
        return True

    try:
        piece_hashes = get_file_piece_hashes(file_id)
        if not piece_hashes:
            return False

        server_peer = Peer(
            user_id=None,
            file_id=file_id,
            peer_id=SERVER_PEER_ID,
            has_all_pieces=True
        )
        db.session.add(server_peer)
        db.session.flush()

        for piece_index in range(len(piece_hashes)):
            piece_record = Piece(
                file_id=file_id,
                peer_id=server_peer.id,
                piece_index=piece_index,
                has_piece=True
            )
            db.session.add(piece_record)

        db.session.commit()
        return True

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"ошибка регистрации серверного пира: {e}")
        return False
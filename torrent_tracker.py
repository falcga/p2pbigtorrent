import base64
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from extensions import db
from models import Peer, Piece, FileVisibility
from file_manager import get_piece_data

tracker_bp = Blueprint('tracker', __name__)

# очередь сигнальных сообщений в памяти
# для продакшена лучше в бд или redis
signaling_queue = {}


def _can_access_file(file_id):
    if current_user.role == 'admin':
        return True
    user_group_ids = [group.id for group in getattr(current_user, 'groups', [])]
    return FileVisibility.query.filter(
        FileVisibility.file_id == file_id,
        ((FileVisibility.group_id.is_(None)) | (FileVisibility.group_id.in_(user_group_ids)))
    ).first() is not None


@tracker_bp.route('/api/announce')
@login_required
def announce():
    file_id = request.args.get('file_id', type=int)
    peer_id = request.args.get('peer_id')
    if file_id is None or not peer_id:
        return jsonify({'error': 'file_id и peer_id обязательны'}), 400
    if not _can_access_file(file_id):
        return jsonify({'error': 'доступ запрещен'}), 403

    access_row = FileVisibility.query.filter(
        FileVisibility.file_id == file_id
    ).first()
    if not access_row:
        return jsonify({'error': 'file not found'}), 404

    # все пиры для файла кроме себя
    peers = Peer.query.filter(
        Peer.file_id == file_id,
        Peer.peer_id != peer_id,
        Peer.peer_id != 'server'
    ).all()

    return jsonify({
        'peers': [{'peer_id': p.peer_id} for p in peers],
        'server_available': True
    })


@tracker_bp.route('/api/signaling', methods=['POST'])
@login_required
def post_signaling():
    data = request.json
    to_peer = data.get('to_peer')

    if to_peer not in signaling_queue:
        signaling_queue[to_peer] = []

    signaling_queue[to_peer].append({
        'from_peer': data.get('from_peer'),
        'data': data.get('data')
    })

    return jsonify({'status': 'ok'})


@tracker_bp.route('/api/signaling', methods=['GET'])
@login_required
def get_signaling():
    peer_id = request.args.get('peer_id')

    messages = signaling_queue.get(peer_id, [])
    signaling_queue[peer_id] = []  # очищаем очередь

    return jsonify(messages)


@tracker_bp.route('/api/peer_update', methods=['POST'])
@login_required
def peer_update():
    data = request.json
    peer = Peer.query.filter_by(
        file_id=data['file_id'],
        peer_id=data['peer_id']
    ).first()

    if peer:
        peer.has_all_pieces = data.get('has_all_pieces', False)
    else:
        peer = Peer(
            file_id=data['file_id'],
            peer_id=data['peer_id'],
            has_all_pieces=data.get('has_all_pieces', False)
        )
        db.session.add(peer)

    db.session.commit()
    return jsonify({'status': 'ok'})


@tracker_bp.route('/api/piece_update', methods=['POST'])
@login_required
def piece_update():
    data = request.json
    peer = Peer.query.filter_by(
        file_id=data['file_id'],
        peer_id=data['peer_id']
    ).first()

    if peer:
        piece = Piece.query.filter_by(
            file_id=data['file_id'],
            peer_id=peer.id,
            piece_index=data['piece_index']
        ).first()

        if piece:
            piece.has_piece = data.get('has_piece', True)
        else:
            piece = Piece(
                file_id=data['file_id'],
                peer_id=peer.id,
                piece_index=data['piece_index'],
                has_piece=data.get('has_piece', True)
            )
            db.session.add(piece)

        db.session.commit()

    return jsonify({'status': 'ok'})


@tracker_bp.route('/api/piece', methods=['GET'])
@login_required
def piece_data():
    file_id = request.args.get('file_id', type=int)
    piece_index = request.args.get('piece_index', type=int)

    if file_id is None or piece_index is None:
        return jsonify({'error': 'file_id и piece_index обязательны'}), 400
    if not _can_access_file(file_id):
        return jsonify({'error': 'доступ запрещен'}), 403

    piece = get_piece_data(file_id, piece_index)
    if piece is None:
        return jsonify({'error': 'кусок не найден'}), 404

    encoded = base64.b64encode(piece).decode('ascii')
    return jsonify({'index': piece_index, 'data': encoded})
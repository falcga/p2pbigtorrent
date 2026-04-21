from flask import Blueprint, request, jsonify
from extensions import db
from models import Peer, Piece

tracker_bp = Blueprint('tracker', __name__)

# очередь сигнальных сообщений в памяти
# для продакшена лучше в бд или redis
signaling_queue = {}


@tracker_bp.route('/api/announce')
def announce():
    file_id = request.args.get('file_id')
    peer_id = request.args.get('peer_id')

    # все пиры для файла кроме себя
    peers = Peer.query.filter(
        Peer.file_id == file_id,
        Peer.peer_id != peer_id
    ).all()

    return jsonify({
        'peers': [{'peer_id': p.peer_id} for p in peers]
    })


@tracker_bp.route('/api/signaling', methods=['POST'])
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
def get_signaling():
    peer_id = request.args.get('peer_id')

    messages = signaling_queue.get(peer_id, [])
    signaling_queue[peer_id] = []  # очищаем очередь

    return jsonify(messages)


@tracker_bp.route('/api/peer_update', methods=['POST'])
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
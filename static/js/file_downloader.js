/**
 * FileDownloader – загрузка одного файла с нескольких пиров.
 */

// Я тут накомментил потому что я уже сюда сам не вернусь мне этот фронтэнд сложен пока
class FileDownloader {
    constructor(fileInfo, localPeerId, trackerUrl) {
        this.fileInfo = fileInfo;          // { id, filename, piece_count, piece_hashes, piece_length, file_size }
        this.localPeerId = localPeerId;
        this.trackerUrl = trackerUrl;

        this.peers = new Map();             // peerId -> WebRTCPeer
        this.pieceMap = new Array(fileInfo.piece_count).fill(null);
        this.downloadedPieces = 0;
        this.isComplete = false;
        this.fileBlob = null;
        this.finalHashHex = null;
        this.isPaused = false;

        // Очередь запросов
        this.pieceQueue = [];
        for (let i = 0; i < fileInfo.piece_count; i++) {
            this.pieceQueue.push(i);
        }
        // Перемешиваем для равномерной загрузки
        this._shuffleArray(this.pieceQueue);

        // Статистика
        this.stats = {
            startTime: null,
            bytesDownloaded: 0,
        };

        this.onProgress = null;     // (percent, downloadedPieces, totalPieces)
        this.onComplete = null;     // (blob)
        this.onPeerConnected = null;
        this.onPeerDisconnected = null;
    }

    // Запуск загрузки: запрос к трекеру и установка соединений
    async start() {
        this.stats.startTime = Date.now();
        this.isPaused = false;
        const announce = await this._announceAndConnect();
        if (this.peers.size === 0 || announce.server_available) {
            await this._downloadFromServer();
            return;
        }
        setTimeout(() => {
            const openPeers = Array.from(this.peers.values()).filter(p => p.isOpen).length;
            if (!this.isComplete && openPeers === 0) {
                this._downloadFromServer();
            }
        }, 3500);
        this._requestNextPieces();
    }

    pause() {
        this.isPaused = true;
    }

    resume() {
        if (!this.isPaused || this.isComplete) {
            return;
        }
        this.isPaused = false;
        this._requestNextPieces();
        this._downloadFromServer();
    }

    async _announceAndConnect() {
        // Запрос к трекеру (announce)
        const url = `${this.trackerUrl}/api/announce?file_id=${this.fileInfo.id}&peer_id=${this.localPeerId}`;
        const resp = await fetch(url);
        const data = await resp.json();
        const peersList = data.peers; // [{ peer_id, ip? }] – не используем IP, только ID для сигналинга

        // Для каждого пира создаём WebRTCPeer (если ещё нет)
        for (let p of peersList) {
            if (p.peer_id === this.localPeerId) continue; // не соединяться с собой
            if (this.peers.has(p.peer_id)) continue;

            const peer = new WebRTCPeer(
                p.peer_id,
                this.localPeerId,
                this.fileInfo.id,
                (data) => this._handlePeerMessage(p.peer_id, data),
                () => this._onPeerOpen(p.peer_id),
                () => this._onPeerClose(p.peer_id)
            );

            // Регистрируем ICE-кандидатов и отправляем через сигнальный сервер
            peer.onIceCandidate((candidate) => {
                this._sendSignaling(p.peer_id, {
                    type: 'candidate',
                    candidate: candidate
                });
            });

            this.peers.set(p.peer_id, peer);

            // Начинаем соединение (мы инициатор)
            const offer = await peer.createOffer();
            this._sendSignaling(p.peer_id, {
                type: 'offer',
                offer: offer
            });
        }

        // Также подписываемся на входящие сигнальные сообщения (WebSocket или polling)
        // Для простоты используем периодический опрос /api/signaling?peer_id=...
        this._startSignalingPolling();
        return data;
    }

    // Отправка сигнального сообщения через трекер
    async _sendSignaling(targetPeerId, message) {
        await fetch(`${this.trackerUrl}/api/signaling`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                from_peer: this.localPeerId,
                to_peer: targetPeerId,
                data: message
            })
        });
    }

    // Периодический опрос входящих сигналов
    _startSignalingPolling() {
        const poll = async () => {
            if (this.isComplete) return;
            try {
                const resp = await fetch(`${this.trackerUrl}/api/signaling?peer_id=${this.localPeerId}`);
                const messages = await resp.json();
                for (let msg of messages) {
                    await this._handleSignaling(msg.from_peer, msg.data);
                }
            } catch (e) {
                console.warn('Signaling poll error', e);
            }
            setTimeout(poll, 2000);
        };
        poll();
    }

    async _handleSignaling(fromPeerId, data) {
        const peer = this.peers.get(fromPeerId);

        if (data.type === 'offer') {
            const newPeer = new WebRTCPeer(
                fromPeerId,
                this.localPeerId,
                this.fileInfo.id,
                (d) => this._handlePeerMessage(fromPeerId, d),
                () => this._onPeerOpen(fromPeerId),
                () => this._onPeerClose(fromPeerId)
            );
            newPeer.onIceCandidate((candidate) => {
                this._sendSignaling(fromPeerId, {
                    type: 'candidate',
                    candidate: candidate
                });
            });
            this.peers.set(fromPeerId, newPeer);
            const answer = await newPeer.handleOffer(data.offer);
            this._sendSignaling(fromPeerId, {
                type: 'answer',
                answer: answer
            });
        } else if (data.type === 'answer') {
            if (!peer) return;
            await peer.handleAnswer(data.answer);
        } else if (data.type === 'candidate') {
            if (!peer) return;
            await peer.addIceCandidate(data.candidate);
        }
    }

    _onPeerOpen(peerId) {
        console.log(`Peer ${peerId} connected`);
        if (this.onPeerConnected) this.onPeerConnected(peerId);
        // Как только соединение открыто, можно запрашивать куски
        this._requestNextPieces();
    }

    _onPeerClose(peerId) {
        console.log(`Peer ${peerId} disconnected`);
        if (this.onPeerDisconnected) this.onPeerDisconnected(peerId);
        // Удаляем пира и, возможно, перезапрашиваем недостающие куски
        const peer = this.peers.get(peerId);
        if (peer) peer.close();
        this.peers.delete(peerId);
    }

    // Обработка входящего сообщения от пира
    _handlePeerMessage(peerId, data) {
        if (typeof data === 'string') {
            // JSON-команда
            try {
                const msg = JSON.parse(data);
                if (msg.type === 'have') {
                    // Пиры могут сообщать о наличии кусков (пока не реализовано)
                } else if (msg.type === 'request') {
                    const response = this.servePiece(msg);
                    if (response) {
                        const peer = this.peers.get(peerId);
                        if (peer) {
                            peer.send(JSON.stringify(response));
                        }
                    }
                } else if (msg.type === 'piece') {
                    this._receivePiece(msg.index, msg.data);
                }
            } catch (e) {}
        } else if (data instanceof ArrayBuffer) {
            // Предполагаем, что это кусок (нужен индекс, но по ArrayBuffer не определить)
            // Поэтому в протоколе кусок всегда обёрнут в JSON с base64-данными
        }
    }

    // Запросить следующий кусок у любого доступного пира
    _requestNextPieces() {
        if (this.isComplete || this.isPaused) return;
        if (this.pieceQueue.length === 0) {
            // Проверить, всё ли скачано
            if (this.downloadedPieces === this.fileInfo.piece_count) {
                this._finalizeDownload();
            }
            return;
        }

        const pieceIndex = this.pieceQueue.shift();
        // Найти пира с открытым каналом
        const availablePeers = Array.from(this.peers.values()).filter(p => p.isOpen);
        if (availablePeers.length === 0) {
            // Если нет пиров, откладываем кусок обратно
            this.pieceQueue.unshift(pieceIndex);
            return;
        }

        // Выбираем случайного пира
        const peer = availablePeers[Math.floor(Math.random() * availablePeers.length)];
        // Отправляем запрос куска
        peer.send(JSON.stringify({
            type: 'request',
            index: pieceIndex
        }));

        // Продолжаем запрашивать, пока есть пиры и куски
        this._requestNextPieces();
    }

    _receivePiece(index, dataBase64) {
        // Декодируем base64 в ArrayBuffer
        const binary = atob(dataBase64);
        const buffer = new ArrayBuffer(binary.length);
        const view = new Uint8Array(buffer);
        for (let i = 0; i < binary.length; i++) {
            view[i] = binary.charCodeAt(i);
        }

        // Проверяем хэш куска (опционально, но обязательно для целостности)
        this._verifyPieceHash(index, buffer).then(valid => {
            if (!valid) {
                console.error(`Piece ${index} hash mismatch, discarding`);
                // Возвращаем кусок в очередь для повторной загрузки
                this.pieceQueue.push(index);
                return;
            }

            this.pieceMap[index] = buffer;
            this.downloadedPieces++;
            this.stats.bytesDownloaded += buffer.byteLength;

            if (this.onProgress) {
                this.onProgress(
                    (this.downloadedPieces / this.fileInfo.piece_count) * 100,
                    this.downloadedPieces,
                    this.fileInfo.piece_count
                );
            }

            // Сообщаем трекеру, что у нас есть этот кусок (необязательно, но для сидирования)
            this._updatePieceStatus(index, true);

            if (this.downloadedPieces === this.fileInfo.piece_count) {
                this._finalizeDownload();
            } else {
                this._requestNextPieces();
            }
        });
    }

    async _verifyPieceHash(index, buffer) {
        const expectedHashHex = this.fileInfo.piece_hashes[index];
        const hashBuffer = await crypto.subtle.digest('SHA-256', buffer);
        const hashArray = Array.from(new Uint8Array(hashBuffer));
        const hashHex = hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
        return hashHex === expectedHashHex;
    }

    async _finalizeDownload() {
        // Собираем файл из кусков
        const blobs = this.pieceMap.map(buf => new Blob([buf]));
        this.fileBlob = new Blob(blobs, { type: 'application/octet-stream' });
        this.isComplete = true;
        this.finalHashHex = await this._computeBlobSha256(this.fileBlob);

        // Сообщаем трекеру, что мы стали сидом
        this._becomeSeeder();

        if (this.onComplete) {
            this.onComplete(this.fileBlob);
        }
    }

    async _computeBlobSha256(blob) {
        const buffer = await blob.arrayBuffer();
        const digest = await crypto.subtle.digest('SHA-256', buffer);
        const arr = Array.from(new Uint8Array(digest));
        return arr.map(b => b.toString(16).padStart(2, '0')).join('');
    }

    async _becomeSeeder() {
        await fetch(`${this.trackerUrl}/api/peer_update`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                peer_id: this.localPeerId,
                file_id: this.fileInfo.id,
                has_all_pieces: true
            })
        });
    }

    async _updatePieceStatus(pieceIndex, has) {
        await fetch(`${this.trackerUrl}/api/piece_update`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                peer_id: this.localPeerId,
                file_id: this.fileInfo.id,
                piece_index: pieceIndex,
                has_piece: has
            })
        });
    }

    _shuffleArray(array) {
        for (let i = array.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [array[i], array[j]] = [array[j], array[i]];
        }
    }

    async _downloadFromServer() {
        while (this.pieceQueue.length > 0 && !this.isComplete && !this.isPaused) {
            const pieceIndex = this.pieceQueue.shift();
            try {
                const resp = await fetch(
                    `${this.trackerUrl}/api/piece?file_id=${this.fileInfo.id}&piece_index=${pieceIndex}`
                );
                if (!resp.ok) {
                    this.pieceQueue.push(pieceIndex);
                    continue;
                }
                const payload = await resp.json();
                this._receivePiece(payload.index, payload.data);
            } catch (e) {
                this.pieceQueue.push(pieceIndex);
                break;
            }
        }
    }

    // Предоставить кусок другому пиру (когда нас запрашивают)
    servePiece(requestMsg) {
        const index = requestMsg.index;
        const pieceBuffer = this.pieceMap[index];
        if (!pieceBuffer) return;

        // Преобразуем ArrayBuffer в base64
        const bytes = new Uint8Array(pieceBuffer);
        let binary = '';
        for (let i = 0; i < bytes.byteLength; i++) {
            binary += String.fromCharCode(bytes[i]);
        }
        const base64 = btoa(binary);

        // Отправляем ответ
        return {
            type: 'piece',
            index: index,
            data: base64
        };
    }
}
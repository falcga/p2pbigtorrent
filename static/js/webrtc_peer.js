/**
 * WebRTC Peer – управление соединением и DataChannel.
 */
class WebRTCPeer {
    constructor(remotePeerId, localPeerId, fileId, onMessage, onOpen, onClose) {
        this.remotePeerId = remotePeerId;
        this.localPeerId = localPeerId;
        this.fileId = fileId;
        this.onMessage = onMessage;       // (data) => {}
        this.onOpen = onOpen;             // () => {}
        this.onClose = onClose;           // () => {}

        this.pc = new RTCPeerConnection({
            iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
        });

        this.dataChannel = null;
        this.isInitiator = false;
        this.isOpen = false;
    }

    // Инициировать соединение (звонящий)
    async createOffer() {
        this.isInitiator = true;
        this.dataChannel = this.pc.createDataChannel('p2p-data');
        this._setupDataChannel();

        const offer = await this.pc.createOffer();
        await this.pc.setLocalDescription(offer);
        return offer;
    }

    // Принять входящий offer
    async handleOffer(offer) {
        this.isInitiator = false;
        this.pc.ondatachannel = (event) => {
            this.dataChannel = event.channel;
            this._setupDataChannel();
        };
        await this.pc.setRemoteDescription(offer);
        const answer = await this.pc.createAnswer();
        await this.pc.setLocalDescription(answer);
        return answer;
    }

    // Обработать answer от удалённой стороны
    async handleAnswer(answer) {
        await this.pc.setRemoteDescription(answer);
    }

    // Добавить ICE-кандидата
    async addIceCandidate(candidate) {
        if (candidate) {
            await this.pc.addIceCandidate(candidate);
        }
    }

    _setupDataChannel() {
        this.dataChannel.binaryType = 'arraybuffer'; // для бинарных кусков
        this.dataChannel.onopen = () => {
            this.isOpen = true;
            this.onOpen();
        };
        this.dataChannel.onclose = () => {
            this.isOpen = false;
            this.onClose();
        };
        this.dataChannel.onmessage = (event) => {
            this.onMessage(event.data);
        };
    }

    // Отправить данные (строка JSON или ArrayBuffer)
    send(data) {
        if (this.isOpen) {
            this.dataChannel.send(data);
        }
    }

    close() {
        if (this.dataChannel) this.dataChannel.close();
        this.pc.close();
    }

    // Подписка на ICE-кандидатов (вызывается снаружи)
    onIceCandidate(callback) {
        this.pc.onicecandidate = (event) => {
            if (event.candidate) {
                callback(event.candidate);
            }
        };
    }
}
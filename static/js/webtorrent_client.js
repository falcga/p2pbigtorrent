/**
 * WebTorrentClient – главный класс для управления P2P загрузками.
 */
class WebTorrentClient {
    constructor(options = {}) {
        this.trackerUrl = options.trackerUrl || '';
        this.localPeerId = this._generatePeerId();
        this.downloaders = new Map(); // fileId -> FileDownloader
    }

    _generatePeerId() {
        return 'peer-' + Math.random().toString(36).substr(2, 9);
    }

    // Начать загрузку файла
    startDownload(fileInfo, callbacks) {
        if (this.downloaders.has(fileInfo.id)) {
            console.warn('Download already in progress for file', fileInfo.id);
            return this.downloaders.get(fileInfo.id);
        }

        const downloader = new FileDownloader(
            fileInfo,
            this.localPeerId,
            this.trackerUrl
        );

        if (callbacks) {
            downloader.onProgress = callbacks.onProgress;
            downloader.onComplete = callbacks.onComplete;
            downloader.onPeerConnected = callbacks.onPeerConnected;
            downloader.onPeerDisconnected = callbacks.onPeerDisconnected;
        }

        this.downloaders.set(fileInfo.id, downloader);
        downloader.start().catch(err => {
            console.error('Download failed:', err);
        });

        return downloader;
    }

    // Получить текущий downloader по ID файла
    getDownloader(fileId) {
        return this.downloaders.get(fileId);
    }

    // Остановить все загрузки
    stopAll() {
        for (let d of this.downloaders.values()) {
            // TODO: закрыть соединения
        }
        this.downloaders.clear();
    }
}

// Глобальный экземпляр
window.p2pClient = new WebTorrentClient({
    trackerUrl: window.location.origin
});
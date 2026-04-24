document.addEventListener('DOMContentLoaded', function () {
    const client = window.p2pClient;

    document.querySelectorAll('.download-btn').forEach((btn) => {
        btn.addEventListener('click', function () {
            const fileId = parseInt(this.dataset.fileId, 10);
            const filename = this.dataset.filename;
            const pieceCount = parseInt(this.dataset.pieceCount, 10);
            const pieceLength = parseInt(this.dataset.pieceLength, 10);
            const fileSize = parseInt(this.dataset.fileSize, 10);
            const pieceHashes = JSON.parse(this.dataset.pieceHashes);
            const contentHash = this.dataset.contentHash;

            const fileInfo = {
                id: fileId,
                filename: filename,
                piece_count: pieceCount,
                piece_length: pieceLength,
                file_size: fileSize,
                piece_hashes: pieceHashes,
                content_hash: contentHash
            };

            const row = document.getElementById(`file-row-${fileId}`);
            const progressDiv = row.querySelector('.progress');
            const progressBar = progressDiv.querySelector('.progress-bar');
            const downloadLink = row.querySelector('.download-link');
            const checksumStatus = row.querySelector('.checksum-status');
            const downloadBtn = this;
            const pauseBtn = row.querySelector('.pause-btn');

            downloadBtn.disabled = true;
            progressDiv.style.display = 'block';
            pauseBtn.style.display = 'inline-block';
            pauseBtn.textContent = 'Пауза';

            const downloader = client.startDownload(fileInfo, {
                onProgress: (percent, downloaded, total) => {
                    progressBar.style.width = percent + '%';
                    progressBar.textContent = `${Math.round(percent)}% (${downloaded}/${total})`;
                },
                onComplete: () => {
                    progressBar.textContent = '100% - Готово!';
                    downloadLink.style.display = 'inline-block';
                    downloadLink.href = URL.createObjectURL(downloader.fileBlob);
                    downloadLink.download = filename;
                    downloadLink.textContent = 'Сохранить';
                    if (fileInfo.content_hash && downloader.finalHashHex) {
                        const ok = fileInfo.content_hash === downloader.finalHashHex;
                        checksumStatus.textContent = ok
                            ? `SHA-256 совпал: ${downloader.finalHashHex}`
                            : `SHA-256 не совпал! ожидался ${fileInfo.content_hash}, получен ${downloader.finalHashHex}`;
                        checksumStatus.className = `small mt-2 checksum-status ${ok ? 'text-success' : 'text-danger'}`;
                    }
                    downloadBtn.disabled = false;
                    pauseBtn.style.display = 'none';
                },
                onPeerConnected: (peerId) => {
                    console.log('Peer connected:', peerId);
                },
                onPeerDisconnected: (peerId) => {
                    console.log('Peer disconnected:', peerId);
                }
            });

            pauseBtn.onclick = function () {
                if (downloader.isPaused) {
                    downloader.resume();
                    pauseBtn.textContent = 'Пауза';
                } else {
                    downloader.pause();
                    pauseBtn.textContent = 'Продолжить';
                }
            };
        });
    });
});

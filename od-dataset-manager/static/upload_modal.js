// upload_modal.js — Shared upload modal logic for detection & classification pages.
// Exposes initUploadModal({ uploadUrl, onUploaded }) which binds the modal DOM
// (identical element IDs in index.html / classify.html) to upload behaviour.
// Pages inject uploadUrl (already carrying version_id via their own qp()) and
// onUploaded (refresh callback) — this module depends on neither.

function initUploadModal({ uploadUrl, onUploaded }) {
    const uploadModal = document.getElementById('upload-modal');
    const uploadFileInput = document.getElementById('upload-file-input');
    const uploadFilePreview = document.getElementById('upload-file-preview');
    const uploadResult = document.getElementById('upload-result');
    if (!uploadModal || !uploadFileInput) return;  // page without the modal

    const btnUpload = document.getElementById('btn-upload');
    const btnConfirm = document.getElementById('upload-confirm');
    const btnCancel = document.getElementById('upload-cancel');
    const btnClose = document.getElementById('upload-close');

    function openModal() {
        uploadFileInput.value = '';
        uploadFilePreview.innerHTML = '';
        uploadResult.style.display = 'none';
        uploadResult.innerHTML = '';
        uploadModal.style.display = 'flex';
    }

    function closeModal() {
        uploadModal.style.display = 'none';
    }

    if (btnUpload) btnUpload.addEventListener('click', openModal);

    uploadFileInput.addEventListener('change', () => {
        const files = Array.from(uploadFileInput.files);
        if (files.length === 0) {
            uploadFilePreview.innerHTML = '<span style="color:#999;">未选择文件</span>';
        } else {
            uploadFilePreview.innerHTML = `<strong>已选择 ${files.length} 个文件：</strong><br>` +
                files.map(f => f.name).join(', ');
        }
    });

    if (btnConfirm) btnConfirm.addEventListener('click', () => {
        const files = uploadFileInput.files;
        if (!files.length) {
            uploadResult.style.display = 'block';
            uploadResult.innerHTML = '<span style="color:#d32f2f;">请先选择要上传的图片文件</span>';
            return;
        }

        uploadResult.style.display = 'block';
        uploadResult.innerHTML = '<span style="color:#3098d8;">正在上传，请稍候...</span>';

        const formData = new FormData();
        for (let f of files) formData.append('images', f);

        fetch(uploadUrl, { method: 'POST', body: formData })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    uploadResult.innerHTML = `<span style="color:#388e3c;">✓ 上传成功！共上传 ${data.count || data.uploaded || files.length} 张图片</span>`;
                    setTimeout(() => {
                        if (typeof onUploaded === 'function') onUploaded();
                        closeModal();
                    }, 1500);
                } else {
                    uploadResult.innerHTML = `<span style="color:#d32f2f;">✗ 上传失败：${data.error || '未知错误'}</span>`;
                }
            })
            .catch(err => {
                uploadResult.innerHTML = `<span style="color:#d32f2f;">✗ 上传失败：${err.message}</span>`;
                console.error(err);
            });
    });

    if (btnCancel) btnCancel.addEventListener('click', closeModal);
    if (btnClose) btnClose.addEventListener('click', closeModal);
    uploadModal.addEventListener('click', (e) => {
        if (e.target === uploadModal) closeModal();
    });
}

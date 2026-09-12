// Versions management page logic (v2 - with progress bars)
console.log('[versions.js v2] loaded');

const versionModal = document.getElementById('version-modal');
const versionNameInput = document.getElementById('version-name');
const versionNoteInput = document.getElementById('version-note');
const sourceVersionSelect = document.getElementById('source-version');
const sourceVersionGroup = document.getElementById('source-version-group');
const versionZipInput = document.getElementById('version-zip');
const zipFileInfo = document.getElementById('zip-file-info');
const hasLabelsGroup = document.getElementById('has-labels-group');
const versionHasLabels = document.getElementById('version-has-labels');
const modalTitle = document.getElementById('version-modal-title');
const versionIdHolder = { id: null };
let isEditing = false;

// Progress bar elements
const progressRow = document.getElementById('version-progress');
const progressBar = document.getElementById('progress-bar');
const progressPct = document.getElementById('progress-pct');
const progressStatus = document.getElementById('progress-status');
let currentEventSource = null;

function openModal(editMode = false, versionData = null) {
    isEditing = editMode;
    versionIdHolder.id = null;
    closeEventSource();
    if (progressRow) progressRow.style.display = 'none';
    resetProgress();

    if (editMode && versionData) {
        modalTitle.textContent = '编辑版本';
        versionNameInput.value = versionData.name;
        versionNoteInput.value = versionData.note || '';
        sourceVersionGroup.style.display = 'none';
        versionZipInput.parentElement.style.display = 'none';
        if (hasLabelsGroup) hasLabelsGroup.style.display = 'none';
        versionIdHolder.id = versionData.id;
    } else {
        modalTitle.textContent = '创建新版本';
        versionNameInput.value = '';
        versionNoteInput.value = '';
        sourceVersionSelect.value = '';
        sourceVersionGroup.style.display = '';
        versionZipInput.parentElement.style.display = '';
        if (hasLabelsGroup) hasLabelsGroup.style.display = '';
        if (versionHasLabels) versionHasLabels.checked = true;
    }
    versionZipInput.value = '';
    zipFileInfo.style.display = 'none';
    zipFileInfo.textContent = '';
    versionModal.style.display = 'flex';
}

function closeModal() {
    versionModal.style.display = 'none';
    closeEventSource();
}

function closeEventSource() {
    if (currentEventSource) {
        currentEventSource.close();
        currentEventSource = null;
    }
}

function resetProgress() {
    if (!progressBar) return;
    progressBar.style.width = '0%';
    progressPct.textContent = '0%';
    progressStatus.textContent = '处理中…';
}

function showProgress(percent, message) {
    if (!progressRow) return;
    progressRow.style.display = 'block';
    progressBar.style.width = percent + '%';
    progressPct.textContent = percent + '%';
    progressStatus.textContent = message;
}

// Show selected zip file name
versionZipInput.addEventListener('change', () => {
    const file = versionZipInput.files[0];
    if (file) {
        const sizeMB = (file.size / (1024 * 1024)).toFixed(1);
        zipFileInfo.style.display = 'block';
        zipFileInfo.textContent = `✓ 已选择: ${file.name} (${sizeMB} MB)`;
    } else {
        zipFileInfo.style.display = 'none';
    }
});

document.getElementById('btn-create-version').addEventListener('click', () => openModal(false));
document.getElementById('version-modal-close').addEventListener('click', closeModal);
document.getElementById('version-modal-cancel').addEventListener('click', closeModal);
versionModal.addEventListener('click', (e) => { if (e.target === versionModal) closeModal(); });

document.getElementById('btn-save-version').addEventListener('click', async () => {
    const name = versionNameInput.value.trim();
    if (!name) { alert('请输入版本名称'); return; }
    const note = versionNoteInput.value.trim();
    const sourceVersionId = sourceVersionSelect.value;
    const zipFile = versionZipInput.files[0];

    const btn = document.getElementById('btn-save-version');

    try {
        if (isEditing) {
            const resp = await fetch(`/api/versions/${versionIdHolder.id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, note })
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || '操作失败');
            closeModal();
            location.reload();
            return;
        }

        if (!zipFile) {
            // No zip — normal JSON request
            const resp = await fetch(`/api/versions/${PROJECT_ID}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, note, source_version_id: sourceVersionId || null })
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || '操作失败');
            closeModal();
            location.reload();
            return;
        }

        // ── Zip upload with upload progress + SSE processing progress ──
        btn.disabled = true;
        btn.textContent = '上传中…';
        versionNameInput.disabled = true;
        versionNoteInput.disabled = true;
        sourceVersionSelect.disabled = true;
        versionZipInput.disabled = true;
        showProgress(0, '正在上传压缩包…');

        const formData = new FormData();
        formData.append('name', name);
        formData.append('note', note);
        if (sourceVersionId) formData.append('source_version_id', sourceVersionId);
        formData.append('has_labels', (versionHasLabels && versionHasLabels.checked) ? '1' : '0');
        formData.append('zip_file', zipFile);

        // Use XHR to get upload progress events
        const xhr = new XMLHttpRequest();
        xhr.open('POST', `/api/versions/${PROJECT_ID}`);

        // Upload progress
        xhr.upload.addEventListener('progress', (e) => {
            if (e.lengthComputable) {
                const pct = Math.round(e.loaded / e.total * 30); // upload = 0-30%
                const sizeMB = (e.loaded / (1024*1024)).toFixed(1);
                const totalMB = (e.total / (1024*1024)).toFixed(1);
                showProgress(pct, `正在上传 ${sizeMB}/${totalMB} MB…`);
            }
        });

        // Track last upload progress for transition
        xhr.upload.addEventListener('loadend', () => {
            showProgress(30, '上传完成，正在服务端处理…');
        });

        // Completion
        xhr.addEventListener('load', () => {
            try {
                const data = JSON.parse(xhr.responseText);
                if (xhr.status !== 200 || data.error) {
                    throw new Error(data.error || '操作失败');
                }
                const taskId = data.task_id;
                if (taskId) {
                    _watchProgress(taskId, btn);
                } else {
                    closeModal();
                    location.reload();
                }
            } catch (err) {
                alert(err.message);
                btn.disabled = false;
                btn.textContent = '保存';
                resetFormInputs();
                if (progressRow) progressRow.style.display = 'none';
            }
        });

        xhr.addEventListener('error', () => {
            alert('上传失败：网络错误');
            btn.disabled = false;
            btn.textContent = '保存';
            resetFormInputs();
            if (progressRow) progressRow.style.display = 'none';
        });

        xhr.send(formData);

    } catch (err) {
        alert(err.message);
        btn.disabled = false;
        btn.textContent = '保存';
        resetFormInputs();
        if (progressRow) progressRow.style.display = 'none';
    }
});

function resetFormInputs() {
    versionNameInput.disabled = false;
    versionNoteInput.disabled = false;
    sourceVersionSelect.disabled = false;
    versionZipInput.disabled = false;
}

function _watchProgress(taskId, btn) {
    closeEventSource();
    showProgress(35, '正在解压文件…');

    currentEventSource = new EventSource(`/api/task_progress/${taskId}`);

    currentEventSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);

            if (data.error) {
                showProgress(data.percent, '❌ ' + data.message);
                currentEventSource.close();
                currentEventSource = null;
                alert('处理失败：' + data.error);
                btn.disabled = false;
                btn.textContent = '保存';
                resetFormInputs();
                return;
            }

            if (data.done) {
                showProgress(100, '✅ ' + data.message);
                currentEventSource.close();
                currentEventSource = null;
                setTimeout(() => {
                    closeModal();
                    location.reload();
                }, 1000);
                return;
            }

            // Map server progress (35-100) to our display range (35-100)
            // Server reports 10-100, we shift to start from 35
            const displayPct = Math.max(35, data.percent);
            showProgress(displayPct, data.message);
        } catch (e) {
            console.error('Progress parse error:', e);
        }
    };

    currentEventSource.onerror = () => {
        // If connection drops, EventSource auto-reconnects.
        // Only treat as fatal if we're in CLOSED state and haven't received 'done'.
        if (currentEventSource && currentEventSource.readyState === EventSource.CLOSED) {
            currentEventSource = null;
            console.warn('[versions] SSE connection closed unexpectedly');
        }
    };
}

document.querySelectorAll('.btn-edit').forEach(btn => {
    btn.addEventListener('click', async () => {
        const vid = btn.dataset.versionId;
        const row = btn.closest('.version-row');
        openModal(true, {
            id: vid,
            name: row.querySelector('.v-name').textContent,
            note: row.querySelector('.v-note').textContent.replace('—', '').trim()
        });
    });
});

document.querySelectorAll('.btn-delete').forEach(btn => {
    btn.addEventListener('click', async () => {
        const vid = btn.dataset.versionId;
        const row = btn.closest('.version-row');
        const name = row.querySelector('.v-name').textContent;
        if (!confirm(`确定删除版本「${name}」？该版本的图片和标注数据将被删除，不可恢复。`)) return;
        try {
            const resp = await fetch(`/api/versions/${vid}`, { method: 'DELETE' });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || '删除失败');
            row.remove();
            const stats = document.getElementById('stats');
            const remaining = document.querySelectorAll('.version-row').length;
            stats.innerHTML = `版本数: <strong>${remaining}</strong>`;
            if (remaining === 0) {
                const wrap = document.getElementById('version-table-wrap');
                wrap.insertAdjacentHTML('afterend', '<div class="qc-empty">暂无版本，点击「创建新版本」开始</div>');
                wrap.style.display = 'none';
            }
        } catch (err) {
            alert(err.message);
        }
    });
});

document.querySelectorAll('.btn-copy').forEach(btn => {
    btn.addEventListener('click', () => {
        const vid = btn.dataset.versionId;
        const row = btn.closest('.version-row');
        const name = row.querySelector('.v-name').textContent;
        openModal(false);
        sourceVersionSelect.value = vid;
        versionNameInput.value = name + ' (副本)';
    });
});

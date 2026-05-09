// 通用API函数
const API = {
    async get(endpoint) {
        const res = await fetch(endpoint);
        if (!res.ok) {
            const text = await res.text();
            throw new Error(`HTTP ${res.status}: ${text}`);
        }
        return res.json();
    },
    async post(endpoint, data) {
        const options = data instanceof FormData
            ? { method: 'POST', body: data }
            : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
        const res = await fetch(endpoint, options);
        if (!res.ok) {
            const text = await res.text();
            throw new Error(`HTTP ${res.status}: ${text}`);
        }
        return res.json();
    }
};

// 显示消息
function showMessage(element, text, type = 'success') {
    element.innerHTML = `<div class="message message-${type}">${text}</div>`;
    setTimeout(() => element.innerHTML = '', 5000);
}

// 初始化上传区域
function initUploadArea(areaId, inputId, uploadUrl, onSuccess) {
    const area = document.getElementById(areaId);
    const input = document.getElementById(inputId);
    if (!area || !input) return;

    area.addEventListener('click', () => input.click());

    area.addEventListener('dragover', (e) => {
        e.preventDefault();
        area.classList.add('dragover');
    });

    area.addEventListener('dragleave', () => area.classList.remove('dragover'));

    area.addEventListener('drop', (e) => {
        e.preventDefault();
        area.classList.remove('dragover');
        const files = e.dataTransfer.files;
        handleFiles(files, uploadUrl, onSuccess);
    });

    input.addEventListener('change', () => {
        handleFiles(input.files, uploadUrl, onSuccess);
    });
}

async function handleFiles(files, uploadUrl, onSuccess) {
    const messageEl = document.getElementById('upload-message');

    for (const file of files) {
        const formData = new FormData();
        const fieldName = uploadUrl.includes('alerts') ? 'image' : 'video';
        formData.append(fieldName, file);

        messageEl.innerHTML = `<div class="message">上传中: ${file.name} <span class="loading"></span></div>`;

        try {
            const result = await API.post(uploadUrl, formData);
            if (result.success) {
                showMessage(messageEl, `上传成功: ${file.name}`, 'success');
                if (onSuccess) onSuccess();
            } else {
                showMessage(messageEl, `上传失败: ${result.error || '未知错误'}`, 'error');
            }
        } catch (err) {
            showMessage(messageEl, `上传错误: ${err.message}`, 'error');
        }
    }
}

// 告警图片页面
if (document.location.pathname.startsWith('/alerts')) {
    async function loadAlerts() {
        const listEl = document.getElementById('alert-list');
        try {
            const alerts = await API.get('/alerts/api');
            if (alerts.length === 0) {
                listEl.innerHTML = '<p>暂无告警图片</p>';
                return;
            }

            listEl.innerHTML = `
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>文件名</th>
                            <th>告警类型</th>
                            <th>最新OCR</th>
                            <th>验证结果</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${alerts.map(a => `
                            <tr>
                                <td>${a.id}</td>
                                <td>${a.filename}</td>
                                <td>${a.alert_type || a.alert_type_id || '-'}</td>
                                <td>${a.latest_ocr ? `ID: ${a.latest_ocr.video_id || '-'}, ${a.latest_ocr.timestamp_seconds || '-'}s` : '-'}</td>
                                <td>${renderVerdict(a.latest_verification)}</td>
                                <td>
                                    <button class="btn" onclick="runOCR(${a.id})">OCR</button>
                                    <button class="btn btn-success" onclick="runVerify(${a.id})">验证</button>
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        } catch (err) {
            listEl.innerHTML = `<p>加载失败: ${err.message}</p>`;
        }
    }

    function renderVerdict(v) {
        if (!v) return '-';
        const classes = {
            'correct': 'status-correct',
            'incorrect': 'status-incorrect',
            'unknown': 'status-unknown'
        };
        return `<span class="${classes[v.verdict] || ''}">${v.verdict}</span>`;
    }

    window.runOCR = async function(alertId) {
        const messageEl = document.getElementById('upload-message');
        messageEl.innerHTML = `<div class="message">OCR识别中 <span class="loading"></span></div>`;

        try {
            const result = await API.post(`/api/alerts/${alertId}/ocr`, {});
            if (result.success) {
                showMessage(messageEl, 'OCR识别成功!', 'success');
                loadAlerts();
            } else {
                showMessage(messageEl, `失败: ${result.error}`, 'error');
            }
        } catch (err) {
            showMessage(messageEl, `错误: ${err.message}`, 'error');
        }
    };

    window.runVerify = async function(alertId) {
        const messageEl = document.getElementById('upload-message');
        messageEl.innerHTML = `<div class="message">验证中 <span class="loading"></span></div>`;

        try {
            const result = await API.post(`/api/alerts/${alertId}/verify`, {
                mock_ocr: { video_id: "046", timestamp_seconds: 90 }
            });
            if (result.success) {
                showMessage(messageEl, `验证完成: ${result.verification_result.verdict}`, 'success');
                loadAlerts();
            } else {
                showMessage(messageEl, `失败: ${result.error}`, 'error');
            }
        } catch (err) {
            showMessage(messageEl, `错误: ${err.message}`, 'error');
        }
    };

    document.getElementById('batch-verify-btn')?.addEventListener('click', async () => {
        const messageEl = document.getElementById('upload-message');
        messageEl.innerHTML = `<div class="message">批量验证中 <span class="loading"></span></div>`;

        try {
            const result = await API.post('/api/verification/batch', {
                mock_ocr: { video_id: "046", timestamp_seconds: 90 }
            });
            showMessage(messageEl, '批量验证完成!', 'success');
            loadAlerts();
        } catch (err) {
            showMessage(messageEl, `错误: ${err.message}`, 'error');
        }
    });

    initUploadArea('alert-upload-area', 'alert-file-input', '/alerts/api/upload', loadAlerts);
    loadAlerts();
}

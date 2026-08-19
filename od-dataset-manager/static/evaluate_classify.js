// evaluate_classify.js — Image classification evaluation page logic

let _dirCallback = null;
let _dirCurrent = '';
let _selectedFile = '';   // selected CSV file path within browser
let allResults = [];      // full image list from last evaluate call
let lastMetrics = {};     // metrics from last evaluate call
let lastClasses = [];    // classes for confusion rendering
let filteredResults = [];
let currentFilter = 'all';
let currentPage = 0;
const PAGE_SIZE = 40;
let viewerIndex = -1;

// ── Dir browser (shows .csv files) ─────────────────────────────

function openDirBrowser(callback, startPath) {
    _dirCallback = callback;
    _selectedFile = '';
    loadDirBrowser(startPath || BASE_DIR);
    document.getElementById('dir-browser-modal').style.display = 'flex';
}

function loadDirBrowser(path) {
    fetch('/api/browse_dir?path=' + encodeURIComponent(path) + '&show_files=1&ext=.csv')
        .then(r => r.json()).then(data => {
            if (data.error) { setStatus(data.error, true); return; }
            _dirCurrent = data.path;
            _selectedFile = '';
            document.getElementById('dir-browser-path').textContent = data.path;
            const list = document.getElementById('dir-browser-list');
            list.innerHTML = '';
            if (data.parent !== data.path) {
                const li = document.createElement('li');
                li.className = 'dir-entry dir-up';
                li.textContent = '.. (上级目录)';
                li.addEventListener('click', () => loadDirBrowser(data.parent));
                list.appendChild(li);
            }
            data.entries.forEach(entry => {
                const li = document.createElement('li');
                li.className = entry.is_file ? 'dir-entry dir-file' : 'dir-entry';
                li.textContent = (entry.is_file ? '📄 ' : '📁 ') + entry.name;
                if (entry.is_file) {
                    li.addEventListener('click', () => {
                        _selectedFile = entry.path;
                        list.querySelectorAll('.dir-entry').forEach(e => e.style.background = '');
                        li.style.background = '#d4eded';
                    });
                } else {
                    li.addEventListener('click', () => loadDirBrowser(entry.path));
                }
                list.appendChild(li);
            });
        });
}

function closeDirBrowser() {
    document.getElementById('dir-browser-modal').style.display = 'none';
}

document.getElementById('dir-browser-select').addEventListener('click', () => {
    if (_dirCallback) _dirCallback(_selectedFile || _dirCurrent);
    closeDirBrowser();
});
document.getElementById('dir-browser-cancel').addEventListener('click', closeDirBrowser);
document.getElementById('dir-browser-close').addEventListener('click', closeDirBrowser);

document.getElementById('btn-browse-pred').addEventListener('click', () => {
    const cur = document.getElementById('eval-pred-dir').value.trim() || BASE_DIR;
    openDirBrowser(path => { document.getElementById('eval-pred-dir').value = path; }, cur);
});

// ── Save evaluation result (reuse detection endpoints) ────────

document.getElementById('btn-save-eval-result').addEventListener('click', () => {
    const name = document.getElementById('eval-result-name').value.trim();
    if (!name) { setStatus('请输入评估名称', true); return; }
    if (!allResults.length) { setStatus('请先运行评估', true); return; }

    fetch('/api/eval/save_result', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            version_id: VERSION_ID,
            name,
            pred_dir: document.getElementById('eval-pred-dir').value.trim(),
            conf_threshold: parseFloat(document.getElementById('conf-val').value),
            iou_threshold: 0.0,
            metrics: lastMetrics,
            images: allResults
        })
    }).then(r => r.json()).then(d => {
        if (d.error) { setStatus(d.error, true); return; }
        setStatus('评估结果已保存：' + name);
        document.getElementById('eval-result-name').value = '';
        loadResultList();
    }).catch(() => setStatus('保存失败', true));
});

function loadResultList() {
    fetch('/api/eval/list_results?version_id=' + encodeURIComponent(VERSION_ID))
        .then(r => r.json()).then(list => {
            const sel = document.getElementById('eval-result-select');
            sel.innerHTML = '<option value="">-- 选择历史结果 --</option>';
            list.forEach(r => {
                const opt = document.createElement('option');
                opt.value = r.id;
                opt.textContent = r.name + '  (' + r.created_at.slice(0, 10) + ')';
                sel.appendChild(opt);
            });
        });
}

document.getElementById('btn-load-eval-result').addEventListener('click', () => {
    const id = document.getElementById('eval-result-select').value;
    if (!id) { setStatus('请选择一条历史结果', true); return; }
    fetch('/api/eval/load_result?id=' + encodeURIComponent(id))
        .then(r => r.json()).then(d => {
            if (d.error) { setStatus(d.error, true); return; }
            allResults = d.images;
            lastMetrics = d.metrics;
            // classes from metrics.confusion_matrix size, fall back to CLASSES
            const cm = d.metrics.confusion_matrix;
            lastClasses = (cm && cm.length === CLASSES.length) ? CLASSES : CLASSES;
            renderMetrics(d.metrics, lastClasses);
            showGridWrap();
            currentPage = 0;
            applyFilter('all');
            document.getElementById('conf-val').value = d.conf_threshold;
            document.getElementById('conf-slider').value = d.conf_threshold;
            if (d.pred_dir) document.getElementById('eval-pred-dir').value = d.pred_dir;
            setStatus('已加载历史结果：' + d.name);
        }).catch(() => setStatus('加载失败', true));
});

document.getElementById('btn-delete-eval-result').addEventListener('click', () => {
    const id = document.getElementById('eval-result-select').value;
    const sel = document.getElementById('eval-result-select');
    const name = sel.options[sel.selectedIndex]?.text || '';
    if (!id) { setStatus('请先选择要删除的历史结果', true); return; }
    if (!confirm(`确认删除评估结果「${name}」？`)) return;
    fetch('/api/eval/delete_result/' + encodeURIComponent(id), {method: 'DELETE'})
        .then(r => r.json()).then(d => {
            if (d.error) { setStatus(d.error, true); return; }
            setStatus('已删除');
            loadResultList();
        }).catch(() => setStatus('删除失败', true));
});

// ── Threshold slider ───────────────────────────────────────────

function bindSlider(sliderId, inputId) {
    const slider = document.getElementById(sliderId);
    const input = document.getElementById(inputId);
    slider.addEventListener('input', () => { input.value = slider.value; });
    input.addEventListener('input', () => {
        let v = parseFloat(input.value);
        if (isNaN(v)) return;
        v = Math.max(0, Math.min(1, v));
        slider.value = v;
        input.value = v;
    });
}
bindSlider('conf-slider', 'conf-val');

// ── Status ─────────────────────────────────────────────────────

function setStatus(msg, isError) {
    const el = document.getElementById('eval-status');
    el.textContent = msg;
    el.style.color = isError ? '#d43f3a' : '#2b7a78';
}

// ── Run evaluation ────────────────────────────────────────────

document.getElementById('btn-run-eval').addEventListener('click', runEvaluate);

function runEvaluate() {
    const predPath = document.getElementById('eval-pred-dir').value.trim();
    if (!predPath) { setStatus('请先选择预测 CSV', true); return; }
    const conf = parseFloat(document.getElementById('conf-val').value);

    setStatus('评估中…');
    document.getElementById('btn-run-eval').disabled = true;

    fetch('/api/evaluate_classify', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            version_id: VERSION_ID,
            pred_dir: predPath,
            conf_threshold: conf
        })
    }).then(r => r.json()).then(data => {
        document.getElementById('btn-run-eval').disabled = false;
        if (data.error) { setStatus(data.error, true); return; }
        setStatus(`评估完成，共 ${data.images.length} 张图片`);
        allResults = data.images;
        lastMetrics = data.metrics;
        lastClasses = data.classes || CLASSES;
        renderMetrics(data.metrics, lastClasses);
        showGridWrap();
        currentPage = 0;
        applyFilter('all');
    }).catch(e => {
        document.getElementById('btn-run-eval').disabled = false;
        setStatus('请求失败: ' + e, true);
    });
}

// ── Metrics: accuracy + per-class table + confusion matrix ─────

function renderMetrics(metrics, classes) {
    const wrap = document.getElementById('eval-metrics-wrap');
    wrap.style.display = 'block';

    const accPct = (metrics.accuracy * 100).toFixed(1);
    document.getElementById('eval-accuracy').innerHTML =
        `<span class="acc-num">${accPct}%</span>` +
        `<span class="acc-sub">准确率（${metrics.correct}/${metrics.total}）` +
        `${metrics.unpredicted ? '，未预测 ' + metrics.unpredicted + ' 张' : ''}</span>`;

    const tbody = document.getElementById('eval-metrics-body');
    tbody.innerHTML = '';
    classes.forEach(cls => {
        const m = (metrics.per_class || {})[cls];
        if (!m) return;
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${escHtml(cls)}</td>
            <td class="metric-val">${(m.precision * 100).toFixed(1)}%</td>
            <td class="metric-val">${(m.recall * 100).toFixed(1)}%</td>
            <td class="metric-val">${(m.f1 * 100).toFixed(1)}%</td>
            <td>${m.tp}</td>
            <td class="metric-fp">${m.fp}</td>
            <td class="metric-fn">${m.fn}</td>`;
        tbody.appendChild(tr);
    });

    renderConfusion(metrics.confusion_matrix, classes);
}

function renderConfusion(matrix, classes) {
    const wrap = document.getElementById('eval-confusion-wrap');
    wrap.innerHTML = '';
    if (!matrix || !matrix.length) return;
    const N = classes.length;

    const table = document.createElement('table');
    table.className = 'confusion-table';
    const thead = document.createElement('thead');
    let headRow = '<tr><th>真值＼预测</th>';
    classes.forEach(c => headRow += `<th>${escHtml(c)}</th>`);
    headRow += '<th>合计</th></tr>';
    thead.innerHTML = headRow;
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    const colTotals = new Array(N).fill(0);
    for (let i = 0; i < N; i++) {
        let rowTotal = 0;
        let rowHtml = `<tr><th>${escHtml(classes[i])}</th>`;
        for (let j = 0; j < N; j++) {
            const v = matrix[i][j] || 0;
            rowTotal += v;
            colTotals[j] += v;
            const diag = (i === j);
            rowHtml += `<td class="${diag ? 'cm-diag' : (v ? 'cm-cell' : 'cm-zero')}">${v}</td>`;
        }
        rowHtml += `<td class="cm-total">${rowTotal}</td></tr>`;
        tbody.innerHTML += rowHtml;
    }
    let totalRow = '<tr><th>合计</th>';
    let grand = 0;
    for (let j = 0; j < N; j++) { totalRow += `<td class="cm-total">${colTotals[j]}</td>`; grand += colTotals[j]; }
    totalRow += `<td class="cm-total"><strong>${grand}</strong></td></tr>`;
    tbody.innerHTML += totalRow;
    table.appendChild(tbody);
    wrap.appendChild(table);
}

// ── Filter tabs ───────────────────────────────────────────────

function showGridWrap() {
    document.getElementById('eval-grid-wrap').style.display = 'block';
    document.getElementById('eval-viewer').style.display = 'none';
}

document.querySelectorAll('.filter-tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        currentPage = 0;
        applyFilter(tab.dataset.filter);
    });
});

function applyFilter(filter) {
    currentFilter = filter;
    const counts = { all: 0, correct: 0, wrong: 0 };
    allResults.forEach(img => {
        counts.all++;
        if (img.correct) counts.correct++; else counts.wrong++;
    });
    document.getElementById('cnt-all').textContent = counts.all;
    document.getElementById('cnt-correct').textContent = counts.correct;
    document.getElementById('cnt-wrong').textContent = counts.wrong;

    if (filter === 'all') {
        filteredResults = allResults.slice();
    } else if (filter === 'correct') {
        filteredResults = allResults.filter(img => img.correct);
    } else {
        filteredResults = allResults.filter(img => !img.correct);
    }
    renderGrid();
}

// ── Image grid ─────────────────────────────────────────────────

function renderGrid() {
    const grid = document.getElementById('eval-image-grid');
    grid.innerHTML = '';

    const totalPages = Math.max(1, Math.ceil(filteredResults.length / PAGE_SIZE));
    if (currentPage >= totalPages) currentPage = totalPages - 1;

    const start = currentPage * PAGE_SIZE;
    const pageItems = filteredResults.slice(start, start + PAGE_SIZE);

    const paginationEl = document.getElementById('eval-pagination');
    if (filteredResults.length > PAGE_SIZE) {
        paginationEl.style.display = 'flex';
        document.getElementById('eval-page-info').textContent =
            `第 ${currentPage + 1} 页 / 共 ${totalPages} 页（${filteredResults.length} 张）`;
        document.getElementById('btn-eval-prev-page').disabled = currentPage === 0;
        document.getElementById('btn-eval-next-page').disabled = currentPage >= totalPages - 1;
    } else {
        paginationEl.style.display = 'none';
    }

    pageItems.forEach((img, pageIdx) => {
        const globalIdx = start + pageIdx;
        const item = document.createElement('div');
        item.className = 'grid-item eval-thumb';
        item.title = img.filename;

        const imgEl = document.createElement('img');
        imgEl.src = '/image/' + encodeURIComponent(img.name) + '?version_id=' + encodeURIComponent(VERSION_ID);
        imgEl.alt = img.filename;
        imgEl.loading = 'lazy';
        item.appendChild(imgEl);

        // GT / Pred badge
        const badge = document.createElement('div');
        badge.className = 'grid-label-tag ' + (img.correct ? 'tag-ok' : 'tag-wrong');
        badge.textContent = `GT:${img.gt || '?'} → ${img.pred || '无'}`;
        item.appendChild(badge);

        const statusIcons = document.createElement('div');
        statusIcons.className = 'eval-status-icons';
        statusIcons.innerHTML = img.correct
            ? '<span class="si ok" title="正确">✓</span>'
            : '<span class="si fp" title="错误">!</span>';
        item.appendChild(statusIcons);

        const fname = document.createElement('div');
        fname.className = 'grid-filename';
        fname.textContent = img.filename;
        item.appendChild(fname);

        item.addEventListener('click', () => openViewer(globalIdx));
        grid.appendChild(item);
    });
}

// ── Pagination ─────────────────────────────────────────────────

document.getElementById('btn-eval-prev-page').addEventListener('click', () => {
    if (currentPage > 0) { currentPage--; renderGrid(); }
});
document.getElementById('btn-eval-next-page').addEventListener('click', () => {
    const totalPages = Math.ceil(filteredResults.length / PAGE_SIZE);
    if (currentPage < totalPages - 1) { currentPage++; renderGrid(); }
});

// ── Image viewer ───────────────────────────────────────────────

function openViewer(globalIdx) {
    viewerIndex = globalIdx;
    document.getElementById('eval-grid-wrap').style.display = 'none';
    document.getElementById('eval-viewer').style.display = 'block';
    loadViewerImage(globalIdx);
}

function loadViewerImage(idx) {
    const img = filteredResults[idx];
    if (!img) return;
    viewerIndex = idx;
    const imgEl = document.getElementById('eval-current-image');
    imgEl.src = '/image/' + encodeURIComponent(img.name) + '?version_id=' + encodeURIComponent(VERSION_ID);
    const mark = img.correct ? '✓ 正确' : '✕ 错误';
    document.getElementById('eval-viewer-title').textContent =
        `${img.filename}  [${idx + 1} / ${filteredResults.length}]`;
    document.getElementById('eval-box-legend').textContent =
        `真值：${img.gt || '?'}    预测：${img.pred || '（无）'}    置信度：${(img.conf * 100).toFixed(0)}%    ${mark}`;
}

document.getElementById('btn-eval-back-grid').addEventListener('click', () => {
    document.getElementById('eval-viewer').style.display = 'none';
    document.getElementById('eval-grid-wrap').style.display = 'block';
});
document.getElementById('btn-eval-prev').addEventListener('click', () => {
    if (viewerIndex > 0) loadViewerImage(viewerIndex - 1);
});
document.getElementById('btn-eval-next').addEventListener('click', () => {
    if (viewerIndex < filteredResults.length - 1) loadViewerImage(viewerIndex + 1);
});

document.addEventListener('keydown', e => {
    if (document.getElementById('eval-viewer').style.display === 'none') return;
    if (e.key === 'ArrowLeft' && viewerIndex > 0) loadViewerImage(viewerIndex - 1);
    if (e.key === 'ArrowRight' && viewerIndex < filteredResults.length - 1) loadViewerImage(viewerIndex + 1);
    if (e.key === 'Escape') {
        document.getElementById('eval-viewer').style.display = 'none';
        document.getElementById('eval-grid-wrap').style.display = 'block';
    }
});

// ── Helpers ────────────────────────────────────────────────────

function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Init ───────────────────────────────────────────────────────

loadResultList();

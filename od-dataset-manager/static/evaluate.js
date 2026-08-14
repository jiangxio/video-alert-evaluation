// evaluate.js — Detection result evaluation page logic

let _dirCallback = null;
let _dirCurrent = '';
let allResults = [];      // full image list from last evaluate call
let lastMetrics = {};     // metrics from last evaluate call
let filteredResults = []; // currently filtered list
let currentFilter = 'all';
let currentPage = 0;
const PAGE_SIZE = 40;     // 5 rows × 8 columns
let viewerIndex = -1;     // index in filteredResults currently shown

// ── Dir browser ─────────────────────────────────────────────

function openDirBrowser(callback, startPath) {
    _dirCallback = callback;
    loadDirBrowser(startPath || BASE_DIR);
    document.getElementById('dir-browser-modal').style.display = 'flex';
}

function loadDirBrowser(path) {
    fetch('/api/browse_dir?path=' + encodeURIComponent(path))
        .then(r => r.json()).then(data => {
            if (data.error) { setStatus(data.error, true); return; }
            _dirCurrent = data.path;
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
                li.className = 'dir-entry';
                li.textContent = '📁 ' + entry.name;
                li.addEventListener('click', () => loadDirBrowser(entry.path));
                list.appendChild(li);
            });
        });
}

function closeDirBrowser() {
    document.getElementById('dir-browser-modal').style.display = 'none';
}

document.getElementById('dir-browser-select').addEventListener('click', () => {
    if (_dirCallback) _dirCallback(_dirCurrent);
    closeDirBrowser();
});
document.getElementById('dir-browser-cancel').addEventListener('click', closeDirBrowser);
document.getElementById('dir-browser-close').addEventListener('click', closeDirBrowser);

document.getElementById('btn-browse-pred').addEventListener('click', () => {
    const cur = document.getElementById('eval-pred-dir').value.trim() || BASE_DIR;
    openDirBrowser(path => { document.getElementById('eval-pred-dir').value = path; }, cur);
});

// ── Save evaluation result ────────────────────────────────────

document.getElementById('btn-save-eval-result').addEventListener('click', () => {
    const name = document.getElementById('eval-result-name').value.trim();
    if (!name) { setStatus('请输入评估名称', true); return; }
    if (!allResults.length) { setStatus('请先运行评估', true); return; }

    fetch('/api/eval/save_result', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            project_id: PROJECT_ID,
            name,
            pred_dir: document.getElementById('eval-pred-dir').value.trim(),
            conf_threshold: parseFloat(document.getElementById('conf-val').value),
            iou_threshold: parseFloat(document.getElementById('iou-val').value),
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

// ── Load saved result list ────────────────────────────────────

function loadResultList() {
    fetch('/api/eval/list_results?project_id=' + encodeURIComponent(PROJECT_ID))
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
            renderMetrics(d.metrics, CLASSES);
            showGridWrap();
            currentPage = 0;
            applyFilter('all');
            document.getElementById('conf-val').value = d.conf_threshold;
            document.getElementById('conf-slider').value = d.conf_threshold;
            document.getElementById('iou-val').value = d.iou_threshold;
            document.getElementById('iou-slider').value = d.iou_threshold;
            if (d.pred_dir) document.getElementById('eval-pred-dir').value = d.pred_dir;
            document.getElementById('eval-legend').style.display = 'block';
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

// ── Class select dropdown ─────────────────────────────────────

function buildClassSelect() {
    const sel = document.getElementById('eval-class-select');
    sel.innerHTML = '';
    CLASSES.forEach(cls => {
        const opt = document.createElement('option');
        opt.value = cls;
        opt.textContent = cls;
        sel.appendChild(opt);
    });
}

function getSelectedClass() {
    return document.getElementById('eval-class-select').value;
}

// Re-render grid when class changes (after evaluation)
document.getElementById('eval-class-select').addEventListener('change', () => {
    if (!allResults.length) return;
    currentPage = 0;
    applyFilter(currentFilter);
});

// ── Threshold sliders ─────────────────────────────────────────

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
bindSlider('iou-slider', 'iou-val');

// ── Status / message ──────────────────────────────────────────

function setStatus(msg, isError) {
    const el = document.getElementById('eval-status');
    el.textContent = msg;
    el.style.color = isError ? '#d43f3a' : '#2b7a78';
}

// ── Per-class status for a single image ──────────────────────

function getImageClassStatus(imgData, cls) {
    const gtForClass = imgData.gt_boxes.filter(b => b.label === cls);
    const predForClass = imgData.pred_boxes.filter(b => b.label === cls);
    const hasFP = predForClass.some(p => !p.matched);
    const hasFN = gtForClass.some(g => !g.matched);
    if (!hasFP && !hasFN) return 'ok';
    if (hasFP && !hasFN) return 'fp';
    if (!hasFP && hasFN) return 'fn';
    return 'fp_fn';
}

// ── Run evaluation ────────────────────────────────────────────

document.getElementById('btn-run-eval').addEventListener('click', runEvaluate);

function runEvaluate() {
    const predDir = document.getElementById('eval-pred-dir').value.trim();
    if (!predDir) { setStatus('请先选择检测结果目录', true); return; }
    const conf = parseFloat(document.getElementById('conf-val').value);
    const iou = parseFloat(document.getElementById('iou-val').value);

    setStatus('评估中…');
    document.getElementById('btn-run-eval').disabled = true;

    fetch('/api/evaluate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            project_id: PROJECT_ID,
            pred_dir: predDir,
            conf_threshold: conf,
            iou_threshold: iou,
            classes: CLASSES
        })
    }).then(r => r.json()).then(data => {
        document.getElementById('btn-run-eval').disabled = false;
        if (data.error) { setStatus(data.error, true); return; }
        setStatus(`评估完成，共 ${data.images.length} 张图片`);
        allResults = data.images;
        lastMetrics = data.metrics;
        renderMetrics(data.metrics, CLASSES);
        showGridWrap();
        currentPage = 0;
        applyFilter('all');
        document.getElementById('eval-legend').style.display = 'block';
    }).catch(e => {
        document.getElementById('btn-run-eval').disabled = false;
        setStatus('请求失败: ' + e, true);
    });
}

// ── Metrics table ─────────────────────────────────────────────

function renderMetrics(metrics, classes) {
    const wrap = document.getElementById('eval-metrics-wrap');
    wrap.style.display = 'block';
    const tbody = document.getElementById('eval-metrics-body');
    tbody.innerHTML = '';

    classes.forEach(cls => {
        const m = metrics[cls];
        if (!m) return;
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${escHtml(cls)}</td>
            <td class="metric-val">${(m.precision * 100).toFixed(1)}%</td>
            <td class="metric-val">${(m.recall * 100).toFixed(1)}%</td>
            <td>${m.tp}</td>
            <td class="metric-fp">${m.fp}</td>
            <td class="metric-fn">${m.fn}</td>`;
        tbody.appendChild(tr);
    });

    const ov = metrics['_overall'];
    if (ov) {
        const tr = document.createElement('tr');
        tr.className = 'metric-overall';
        tr.innerHTML = `
            <td><strong>总计</strong></td>
            <td class="metric-val"><strong>${(ov.precision * 100).toFixed(1)}%</strong></td>
            <td class="metric-val"><strong>${(ov.recall * 100).toFixed(1)}%</strong></td>
            <td><strong>${ov.tp}</strong></td>
            <td class="metric-fp"><strong>${ov.fp}</strong></td>
            <td class="metric-fn"><strong>${ov.fn}</strong></td>`;
        tbody.appendChild(tr);
    }
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
    const cls = getSelectedClass();

    const counts = { all: 0, ok: 0, fp: 0, fn: 0, fp_fn: 0 };
    allResults.forEach(img => {
        const s = getImageClassStatus(img, cls);
        counts.all++;
        counts[s]++;
    });
    document.getElementById('cnt-all').textContent = counts.all;
    document.getElementById('cnt-ok').textContent = counts.ok;
    document.getElementById('cnt-fp').textContent = counts.fp;
    document.getElementById('cnt-fn').textContent = counts.fn;
    document.getElementById('cnt-fp-fn').textContent = counts.fp_fn;

    if (filter === 'all') {
        filteredResults = allResults.slice();
    } else {
        filteredResults = allResults.filter(img => getImageClassStatus(img, cls) === filter);
    }
    renderGrid();
}

// ── Image grid ────────────────────────────────────────────────

function renderGrid() {
    const cls = getSelectedClass();
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
        const clsStatus = getImageClassStatus(img, cls);

        const item = document.createElement('div');
        item.className = 'grid-item eval-thumb';
        item.title = img.filename;

        const imgEl = document.createElement('img');
        imgEl.src = '/image/' + encodeURIComponent(img.name) + '?project_id=' + encodeURIComponent(PROJECT_ID);
        imgEl.alt = img.filename;
        imgEl.loading = 'lazy';
        item.appendChild(imgEl);

        const svgEl = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svgEl.setAttribute('preserveAspectRatio', 'xMidYMid meet');
        svgEl.classList.add('thumb-canvas');
        item.appendChild(svgEl);

        function tryDraw() {
            if (imgEl.naturalWidth > 0) {
                drawThumbBoxes(svgEl, imgEl.naturalWidth, imgEl.naturalHeight, img, cls);
            }
        }
        imgEl.addEventListener('load', tryDraw);
        if (imgEl.complete && imgEl.naturalWidth > 0) tryDraw();

        item.appendChild(makeStatusIcons(clsStatus));

        const fname = document.createElement('div');
        fname.className = 'grid-filename';
        fname.textContent = img.filename;
        item.appendChild(fname);

        item.addEventListener('click', () => openViewer(globalIdx));
        grid.appendChild(item);
    });
}

function drawThumbBoxes(svgEl, natW, natH, imgData, cls) {
    svgEl.setAttribute('viewBox', `0 0 ${natW} ${natH}`);
    svgEl.innerHTML = '';

    const sw = Math.max(natW, natH) / 60;
    const dashLen = sw * 4;
    const gapLen = sw * 2;

    function addRect(x1, y1, x2, y2, color, dashed) {
        const rx = Math.min(x1, x2);
        const ry = Math.min(y1, y2);
        const rw = Math.abs(x2 - x1);
        const rh = Math.abs(y2 - y1);
        if (rw < 1 || rh < 1) return;
        const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('x', rx);
        rect.setAttribute('y', ry);
        rect.setAttribute('width', rw);
        rect.setAttribute('height', rh);
        rect.setAttribute('fill', 'none');
        rect.setAttribute('stroke', color);
        rect.setAttribute('stroke-width', sw);
        if (dashed) rect.setAttribute('stroke-dasharray', `${dashLen},${gapLen}`);
        svgEl.appendChild(rect);
    }

    imgData.gt_boxes.filter(b => b.label === cls).forEach(box => {
        const [p1, p2] = box.points;
        addRect(p1[0], p1[1], p2[0], p2[1], box.matched ? '#00c853' : '#00b7ff', !box.matched);
    });

    imgData.pred_boxes.filter(b => b.label === cls).forEach(box => {
        const [p1, p2] = box.points;
        addRect(p1[0], p1[1], p2[0], p2[1], box.matched ? '#ff6d00' : '#d43f3a', false);
    });
}

function makeStatusIcons(status) {
    const wrap = document.createElement('div');
    wrap.className = 'eval-status-icons';
    if (status === 'ok') {
        wrap.innerHTML = '<span class="si ok" title="检测OK">✓</span>';
    } else {
        if (status === 'fp' || status === 'fp_fn') {
            wrap.innerHTML += '<span class="si fp" title="有误检">!</span>';
        }
        if (status === 'fn' || status === 'fp_fn') {
            wrap.innerHTML += '<span class="si fn" title="有漏检">✕</span>';
        }
    }
    return wrap;
}

// ── Pagination ────────────────────────────────────────────────

document.getElementById('btn-eval-prev-page').addEventListener('click', () => {
    if (currentPage > 0) { currentPage--; renderGrid(); }
});
document.getElementById('btn-eval-next-page').addEventListener('click', () => {
    const totalPages = Math.ceil(filteredResults.length / PAGE_SIZE);
    if (currentPage < totalPages - 1) { currentPage++; renderGrid(); }
});

// ── Image viewer ──────────────────────────────────────────────

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
    const cls = getSelectedClass();
    const clsStatus = getImageClassStatus(img, cls);

    const imgEl = document.getElementById('eval-current-image');
    imgEl.src = '/image/' + encodeURIComponent(img.name) + '?project_id=' + encodeURIComponent(PROJECT_ID);
    document.getElementById('eval-viewer-title').textContent =
        `${img.filename}  [${idx + 1} / ${filteredResults.length}]  —  ${escHtml(cls)}  ${statusLabel(clsStatus)}`;

    imgEl.onload = () => drawOverlay(img, imgEl, cls);
    if (imgEl.complete && imgEl.naturalWidth) drawOverlay(img, imgEl, cls);
}

function drawOverlay(imgData, imgEl, cls) {
    const svg = document.getElementById('eval-overlay');
    svg.innerHTML = '';

    const dispW = imgEl.offsetWidth;
    const dispH = imgEl.offsetHeight;
    const natW = imgEl.naturalWidth || 1;
    const natH = imgEl.naturalHeight || 1;

    const scaleX = dispW / natW;
    const scaleY = dispH / natH;

    function makeRect(x1, y1, x2, y2, color, dashed, labelText) {
        const rx = Math.min(x1, x2) * scaleX;
        const ry = Math.min(y1, y2) * scaleY;
        const rw = Math.abs(x2 - x1) * scaleX;
        const rh = Math.abs(y2 - y1) * scaleY;

        const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('x', rx);
        rect.setAttribute('y', ry);
        rect.setAttribute('width', rw);
        rect.setAttribute('height', rh);
        rect.setAttribute('fill', 'none');
        rect.setAttribute('stroke', color);
        rect.setAttribute('stroke-width', '2');
        if (dashed) rect.setAttribute('stroke-dasharray', '6,3');
        svg.appendChild(rect);

        if (labelText) {
            const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            text.setAttribute('x', rx + 2);
            text.setAttribute('y', ry > 16 ? ry - 3 : ry + 14);
            text.setAttribute('fill', color);
            text.setAttribute('font-size', '12');
            text.setAttribute('font-family', 'Arial, sans-serif');
            text.setAttribute('paint-order', 'stroke');
            text.setAttribute('stroke', 'rgba(0,0,0,0.65)');
            text.setAttribute('stroke-width', '3');
            text.textContent = labelText;
            svg.appendChild(text);
        }
    }

    imgData.gt_boxes.filter(b => b.label === cls).forEach(box => {
        const [p1, p2] = box.points;
        if (box.matched) {
            makeRect(p1[0], p1[1], p2[0], p2[1], '#00c853', false, 'GT');
        } else {
            makeRect(p1[0], p1[1], p2[0], p2[1], '#00b7ff', true, 'FN');
        }
    });

    imgData.pred_boxes.filter(b => b.label === cls).forEach(box => {
        const [p1, p2] = box.points;
        const conf = (box.conf * 100).toFixed(0) + '%';
        if (box.matched) {
            makeRect(p1[0], p1[1], p2[0], p2[1], '#ff6d00', false, conf);
        } else {
            makeRect(p1[0], p1[1], p2[0], p2[1], '#d43f3a', false, conf);
        }
    });

    const gtCls = imgData.gt_boxes.filter(b => b.label === cls);
    const predCls = imgData.pred_boxes.filter(b => b.label === cls);
    const tp = predCls.filter(p => p.matched).length;
    const fp = predCls.filter(p => !p.matched).length;
    const fn = gtCls.filter(g => !g.matched).length;
    document.getElementById('eval-box-legend').textContent =
        `[${cls}]  GT标注: ${gtCls.length}  TP命中: ${tp}  FP误检: ${fp}  FN漏检: ${fn}`;
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

// ── Helpers ───────────────────────────────────────────────────

function statusLabel(status) {
    switch (status) {
        case 'ok': return '✓ 检测OK';
        case 'fp': return '! 误检';
        case 'fn': return '✕ 漏检';
        case 'fp_fn': return '! ✕ 误检+漏检';
        default: return status;
    }
}

function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Init ──────────────────────────────────────────────────────

buildClassSelect();
loadResultList();

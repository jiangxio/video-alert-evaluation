const statsEl = document.getElementById('stats');
const gridView = document.getElementById('grid-view');
const imageGrid = document.getElementById('image-grid');
const viewer = document.getElementById('viewer');
const imageEl = document.getElementById('current-image');
const overlay = document.getElementById('overlay');
const classSelect = document.getElementById('class-select');
const currentStatus = document.getElementById('current-status');
const messageEl = document.getElementById('message');

let allImages = [];
let currentFilter = 'all';
let currentClassFilter = 'all';
let currentPage = 1;
let imagesPerPage = 40;
let selectedImageName = null;
let selectedImageNames = new Set();
let currentImageIndex = -1;
let boxes = [];
let selectedIndex = -1;
let isDrawing = false;
let drawStart = {x: 0, y: 0};

// Per-class color palette
const CLASS_COLORS = [
    '#00b7ff', '#ff6d00', '#00c853', '#d43f3a',
    '#9c27b0', '#ff9800', '#2196f3', '#4caf50',
    '#e91e63', '#00bcd4'
];
function classColor(label) {
    const idx = CLASSES.indexOf(label);
    return idx >= 0 ? CLASS_COLORS[idx % CLASS_COLORS.length] : '#00b7ff';
}

// Append version_id query param to any API path
function qp(path) {
    const id = VERSION_ID || PROJECT_ID;
    if (!id) return path;
    const key = VERSION_ID ? 'version_id' : 'project_id';
    const sep = path.includes('?') ? '&' : '?';
    return path + sep + key + '=' + encodeURIComponent(id);
}

function resetOverlay() {
    while (overlay.firstChild) overlay.removeChild(overlay.firstChild);
}

function createOverlayBox(index, box) {
    const color = index === selectedIndex ? '#ffae00' : classColor(box.label);
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', box.x);
    rect.setAttribute('y', box.y);
    rect.setAttribute('width', box.w);
    rect.setAttribute('height', box.h);
    rect.setAttribute('fill', 'none');
    rect.setAttribute('stroke', color);
    rect.setAttribute('stroke-width', 2);
    rect.style.cursor = 'pointer';
    rect.addEventListener('click', (ev) => {
        ev.stopPropagation();
        selectedIndex = index;
        updateOverlay();
    });
    overlay.appendChild(rect);

    const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    label.setAttribute('x', box.x + 4);
    label.setAttribute('y', box.y + 16);
    label.setAttribute('fill', color);
    label.setAttribute('font-size', '14');
    label.setAttribute('paint-order', 'stroke');
    label.setAttribute('stroke', 'rgba(0,0,0,0.55)');
    label.setAttribute('stroke-width', '3');
    label.textContent = box.label;
    overlay.appendChild(label);
}

function setStatus(text) {
    currentStatus.textContent = text;
}

function showMessage(text, isError = false) {
    messageEl.textContent = text;
    messageEl.style.color = isError ? '#e04838' : '#3098d8';
    setTimeout(() => { messageEl.textContent = ''; }, 3000);
}

function autoSave(callback) {
    if (!selectedImageName) { callback(); return; }
    const payload = {
        shapes: boxes.map(b => ({
            label: b.label,
            points: [[b.x, b.y], [b.x + b.w, b.y + b.h]]
        }))
    };
    fetch(qp(`/api/labels/${selectedImageName}`), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    }).then(r => r.json()).then(data => {
        if (data.success) {
            const item = allImages.find(i => i.name === selectedImageName);
            if (item) item.has_label = true;
            setStatus(`已自动保存: ${selectedImageName}`);
        }
    }).catch(() => {}).finally(() => callback());
}

function deleteImage(name) {
    if (confirm('确认删除图像和标注文件？')) {
        fetch(qp(`/api/delete_image/${name}`), {method: 'DELETE'}).then(r => r.json()).then(data => {
            if (data.success) {
                showMessage('删除成功');
                reloadImageList();
            } else {
                showMessage('删除失败', true);
            }
        }).catch(err => {
            showMessage('删除接口出错', true);
            console.error(err);
        });
    }
}

function reloadImageList() {
    fetch(qp('/api/images')).then(r => r.json()).then(items => {
        allImages = items;
        renderGrid();
        updateStats();
    });
}

function updateStats() {
    let total = allImages.length;
    let labeled = allImages.filter(i => i.has_label).length;
    let unlabeled = total - labeled;
    statsEl.innerHTML = `总图片: <strong>${total}</strong>，已标注: <strong>${labeled}</strong>，未标注: <strong>${unlabeled}</strong>`;
}

function getFilteredImages() {
    let filtered = allImages;
    if (currentFilter === 'labeled') filtered = filtered.filter(i => i.has_label);
    else if (currentFilter === 'unlabeled') filtered = filtered.filter(i => !i.has_label);
    if (currentClassFilter !== 'all') {
        const idx = CLASSES.indexOf(currentClassFilter);
        if (idx >= 0) filtered = filtered.filter(i => i.classes && i.classes.includes(idx));
    }
    return filtered;
}

function renderGrid() {
    imageGrid.innerHTML = '';
    const filtered = getFilteredImages();
    const start = (currentPage - 1) * imagesPerPage;
    const pageImages = filtered.slice(start, start + imagesPerPage);

    pageImages.forEach(item => {
        const div = document.createElement('div');
        div.className = 'grid-item';
        div.classList.add(item.has_label ? 'labeled' : 'unlabeled');
        if (selectedImageNames.has(item.name)) div.classList.add('selected');

        const img = document.createElement('img');
        img.src = qp(`/image/${item.name}`);
        img.alt = item.filename;
        div.appendChild(img);

        const statusIcon = document.createElement('div');
        statusIcon.className = 'status-icon';
        div.appendChild(statusIcon);

        const nameLabel = document.createElement('div');
        nameLabel.className = 'grid-filename';
        nameLabel.textContent = item.filename;
        nameLabel.title = item.filename;
        div.appendChild(nameLabel);

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'grid-checkbox';
        checkbox.checked = selectedImageNames.has(item.name);
        checkbox.addEventListener('click', (e) => {
            e.stopPropagation();
            if (checkbox.checked) selectedImageNames.add(item.name);
            else selectedImageNames.delete(item.name);
            selectedImageName = item.name;
            renderGrid();
        });
        div.appendChild(checkbox);

        // Delete icon
        const delBtn = document.createElement('button');
        delBtn.className = 'grid-del-btn';
        delBtn.title = '删除';
        delBtn.textContent = '×';
        delBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            deleteImage(item.name);
        });
        div.appendChild(delBtn);

        const drawBoxes = (shapes) => {
            div.querySelectorAll('.grid-box').forEach(e => e.remove());
            if (!shapes || shapes.length === 0) return;
            const naturalW = img.naturalWidth;
            const naturalH = img.naturalHeight;
            const clientW = div.clientWidth;
            const clientH = div.clientHeight;
            const scale = Math.min(clientW / naturalW, clientH / naturalH);
            const offsetX = (clientW - naturalW * scale) / 2;
            const offsetY = (clientH - naturalH * scale) / 2;
            shapes.forEach(shape => {
                const pts = shape.points;
                if (pts && pts.length === 2) {
                    const x = Math.min(pts[0][0], pts[1][0]) * scale + offsetX;
                    const y = Math.min(pts[0][1], pts[1][1]) * scale + offsetY;
                    const w = Math.abs(pts[1][0] - pts[0][0]) * scale;
                    const h = Math.abs(pts[1][1] - pts[0][1]) * scale;
                    const boxDiv = document.createElement('div');
                    boxDiv.className = 'grid-box';
                    boxDiv.style.left = x + 'px';
                    boxDiv.style.top = y + 'px';
                    boxDiv.style.width = w + 'px';
                    boxDiv.style.height = h + 'px';
                    boxDiv.style.borderColor = classColor(shape.label);
                    div.appendChild(boxDiv);
                }
            });
        };

        let cachedShapes = [];
        fetch(qp(`/api/labels/${item.name}`)).then(r => r.json()).then(json => {
            if (json && json.shapes) cachedShapes = json.shapes;
            if (item.has_label) {
                statusIcon.classList.add('labeled');
                statusIcon.title = '已标注';
            } else {
                statusIcon.classList.add('unlabeled');
                statusIcon.title = '未标注';
            }
            if (img.complete) drawBoxes(cachedShapes);
        });

        img.addEventListener('load', () => { drawBoxes(cachedShapes); });

        div.addEventListener('click', (e) => {
            if (e.ctrlKey || e.metaKey) {
                if (selectedImageNames.has(item.name)) selectedImageNames.delete(item.name);
                else selectedImageNames.add(item.name);
                selectedImageName = item.name;
                renderGrid();
            } else {
                selectedImageName = item.name;
                openLightbox(item.name);
            }
        });
        imageGrid.appendChild(div);
    });

    updatePageInfo();
}

function updatePageInfo() {
    const filtered = getFilteredImages();
    const totalPages = Math.ceil(filtered.length / imagesPerPage);
    document.getElementById('page-info').textContent = `第 ${currentPage} 页 / 共 ${totalPages} 页`;
    document.getElementById('btn-prev-page').disabled = currentPage === 1;
    document.getElementById('btn-next-page').disabled = currentPage === totalPages;
}

function loadClassOptions() {
    classSelect.innerHTML = '';
    CLASSES.forEach(cls => {
        const opt = document.createElement('option');
        opt.value = cls;
        opt.textContent = cls;
        classSelect.appendChild(opt);
    });
}

function loadImage(name) {
    currentImageIndex = allImages.findIndex(i => i.name === name);
    boxes = [];
    selectedIndex = -1;

    fetch(qp(`/api/labels/${name}`)).then(r => r.json()).then(json => {
        if (!json) {
            imageEl.src = '';
            setStatus('未找到标注数据');
            resetOverlay();
            return;
        }

        let loadCandidate = json.imagePath || name;

        const tryImage = () => { imageEl.src = qp(`/image/${loadCandidate}`); };

        imageEl.onload = () => {
            setStatus(`当前: ${name} ( ${json.shapes?.length || 0} 框 )`);
            const imgW = imageEl.naturalWidth;
            const imgH = imageEl.naturalHeight;
            overlay.setAttribute('viewBox', `0 0 ${imgW} ${imgH}`);
            const displayW = imageEl.clientWidth;
            const displayH = imageEl.clientHeight;
            overlay.style.width = displayW + 'px';
            overlay.style.height = displayH + 'px';
            // Position overlay relative to image-holder
            const holderRect = imageHolder.getBoundingClientRect();
            const imgRect = imageEl.getBoundingClientRect();
            overlay.style.left = (imgRect.left - holderRect.left) + 'px';
            overlay.style.top = (imgRect.top - holderRect.top) + 'px';

            boxes = (json.shapes || []).map(shape => {
                const p1 = shape.points[0];
                const p2 = shape.points[1];
                const x = Math.min(p1[0], p2[0]);
                const y = Math.min(p1[1], p2[1]);
                const w = Math.abs(p2[0] - p1[0]);
                const h = Math.abs(p2[1] - p1[1]);
                return {x, y, w, h, label: shape.label};
            });
            selectedIndex = -1;
            updateOverlay();
            updateFilterIndicator();
        };

        imageEl.onerror = () => {
            if (loadCandidate !== name) {
                loadCandidate = name;
                tryImage();
                return;
            }
            setStatus('图像加载失败');
            resetOverlay();
        };

        tryImage();
    }).catch(err => {
        showMessage('加载标注失败', true);
        console.error(err);
    });
}

function imageRelativePos(evt) {
    const rect = imageEl.getBoundingClientRect();
    let x = Math.max(0, Math.min(rect.width, evt.clientX - rect.left));
    let y = Math.max(0, Math.min(rect.height, evt.clientY - rect.top));
    return {x: x * imageEl.naturalWidth / rect.width, y: y * imageEl.naturalHeight / rect.height};
}

function updateOverlay() {
    resetOverlay();
    boxes.forEach((box, idx) => createOverlayBox(idx, box));
}

const imageHolder = document.getElementById('image-holder');
imageHolder.addEventListener('mousedown', (evt) => {
    if (!selectedImageName) return;
    isDrawing = true;
    drawStart = imageRelativePos(evt);
    evt.preventDefault();
});

imageHolder.addEventListener('mousemove', (evt) => {
    if (!isDrawing) return;
    const current = imageRelativePos(evt);
    const x = Math.min(drawStart.x, current.x);
    const y = Math.min(drawStart.y, current.y);
    const w = Math.abs(current.x - drawStart.x);
    const h = Math.abs(current.y - drawStart.y);
    updateOverlay();
    createOverlayBox(-1, {x, y, w, h, label: classSelect.value});
});

window.addEventListener('mouseup', (evt) => {
    if (!isDrawing) return;
    isDrawing = false;
    const end = imageRelativePos(evt);
    const x = Math.min(drawStart.x, end.x);
    const y = Math.min(drawStart.y, end.y);
    const w = Math.abs(end.x - drawStart.x);
    const h = Math.abs(end.y - drawStart.y);
    if (w < 5 || h < 5) return;
    boxes.push({x, y, w, h, label: classSelect.value});
    selectedIndex = boxes.length - 1;
    updateOverlay();
});

document.getElementById('btn-delete-box').addEventListener('click', () => {
    if (selectedIndex === -1) { showMessage('请先选中一个框'); return; }
    boxes.splice(selectedIndex, 1);
    selectedIndex = -1;
    updateOverlay();
});

document.getElementById('btn-clear-all').addEventListener('click', () => {
    boxes = [];
    selectedIndex = -1;
    updateOverlay();
    setStatus('已清除所有框，可以重新标注');
});

document.getElementById('btn-save').addEventListener('click', () => {
    if (!selectedImageName) { showMessage('请先选择图片', true); return; }
    const payload = {
        shapes: boxes.map(b => ({label: b.label, points: [[b.x, b.y], [b.x + b.w, b.y + b.h]]}))
    };
    fetch(qp(`/api/labels/${selectedImageName}`), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    }).then(r => r.json()).then(data => {
        if (data.success) {
            showMessage('保存成功');
            setStatus(`已保存，共${boxes.length}个框`);
            reloadImageList();
        } else {
            showMessage('保存失败', true);
        }
    }).catch(err => { showMessage('保存接口出错', true); console.error(err); });
});

document.getElementById('btn-prev-page').addEventListener('click', () => {
    if (currentPage > 1) { currentPage--; renderGrid(); }
});

document.getElementById('btn-next-page').addEventListener('click', () => {
    const filtered = getFilteredImages();
    if (currentPage < Math.ceil(filtered.length / imagesPerPage)) { currentPage++; renderGrid(); }
});

document.getElementById('btn-annotate').addEventListener('click', () => {
    if (!selectedImageName) { showMessage('请先选择图片', true); return; }
    gridView.style.display = 'none';
    viewer.style.display = 'block';
    // Hide toolbar when entering annotation mode
    document.querySelector('.toolbar').style.display = 'none';
    loadImage(selectedImageName);
});

document.getElementById('btn-back-to-grid').addEventListener('click', () => {
    viewer.style.display = 'none';
    gridView.style.display = 'block';
    // Show toolbar when returning to grid
    document.querySelector('.toolbar').style.display = 'flex';
    renderGrid();
});

document.getElementById('btn-prev-image').addEventListener('click', () => {
    const filtered = getFilteredImages();
    const idx = filtered.findIndex(i => i.name === selectedImageName);
    if (idx > 0) {
        autoSave(() => {
            selectedImageName = filtered[idx - 1].name;
            loadImage(selectedImageName);
        });
    } else {
        showMessage('已是第一张', true);
    }
});

document.getElementById('btn-next-image').addEventListener('click', () => {
    const filtered = getFilteredImages();
    const idx = filtered.findIndex(i => i.name === selectedImageName);
    if (idx >= 0 && idx < filtered.length - 1) {
        autoSave(() => {
            selectedImageName = filtered[idx + 1].name;
            loadImage(selectedImageName);
        });
    } else {
        showMessage('已是最后一张', true);
    }
});

// 过滤按钮事件
function updateFilterIndicator() {
    const indicator = document.getElementById('filter-indicator');
    if (!indicator) return;
    const parts = [];
    if (currentFilter !== 'all') {
        const label = currentFilter === 'labeled' ? '已标注' : '未标注';
        parts.push(label);
    }
    if (currentClassFilter !== 'all') {
        parts.push(`类别: ${currentClassFilter}`);
    }
    if (parts.length === 0) {
        indicator.style.display = 'none';
    } else {
        indicator.style.display = 'inline-block';
        const filtered = getFilteredImages();
        indicator.textContent = `${parts.join(' | ')} (${filtered.length})`;
    }
}

document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentFilter = btn.dataset.filter;
        currentPage = 1;
        selectedImageNames.clear();
        selectedImageName = null;
        renderGrid();
        updateFilterIndicator();
    });
});

// 类别过滤
function initClassFilter() {
    const select = document.getElementById('class-filter-select');
    if (!select) return;
    CLASSES.forEach(cls => {
        const opt = document.createElement('option');
        opt.value = cls;
        opt.textContent = cls;
        select.appendChild(opt);
    });
    select.addEventListener('change', () => {
        currentClassFilter = select.value;
        currentPage = 1;
        selectedImageNames.clear();
        selectedImageName = null;
        renderGrid();
        updateFilterIndicator();
    });
}


document.getElementById('btn-delete-selected').addEventListener('click', () => {
    if (!selectedImageNames.size) { showMessage('请先选择要删除的图像', true); return; }
    if (!confirm(`确认删除 ${selectedImageNames.size} 张图像？`)) return;
    Promise.all([...selectedImageNames].map(name =>
        fetch(qp(`/api/delete_image/${name}`), {method: 'DELETE'}).then(r => r.json())
    )).then(() => {
        showMessage('批量删除完成');
        selectedImageNames.clear();
        selectedImageName = null;
        reloadImageList();
    }).catch(err => { showMessage('批量删除失败', true); console.error(err); });
});

document.getElementById('btn-refresh').addEventListener('click', () => {
    reloadImageList();
    showMessage('已刷新');
});

// 上传弹窗逻辑
const uploadModal = document.getElementById('upload-modal');
const uploadFileInput = document.getElementById('upload-file-input');
const uploadFilePreview = document.getElementById('upload-file-preview');
const uploadResult = document.getElementById('upload-result');

document.getElementById('btn-upload').addEventListener('click', () => {
    uploadFileInput.value = '';
    uploadFilePreview.innerHTML = '';
    uploadResult.style.display = 'none';
    uploadResult.innerHTML = '';
    uploadModal.style.display = 'flex';
});

uploadFileInput.addEventListener('change', () => {
    const files = Array.from(uploadFileInput.files);
    if (files.length === 0) {
        uploadFilePreview.innerHTML = '<span style="color:#999;">未选择文件</span>';
    } else {
        uploadFilePreview.innerHTML = `<strong>已选择 ${files.length} 个文件：</strong><br>` +
            files.map(f => f.name).join(', ');
    }
});

document.getElementById('upload-confirm').addEventListener('click', () => {
    const files = uploadFileInput.files;
    if (!files.length) {
        uploadResult.style.display = 'block';
        uploadResult.innerHTML = '<span style="color:#d32f2f;">请先选择要上传的图片文件</span>';
        return;
    }

    // 显示上传进度
    uploadResult.style.display = 'block';
    uploadResult.innerHTML = '<span style="color:#3098d8;">正在上传，请稍候...</span>';

    const formData = new FormData();
    for (let f of files) formData.append('images', f);

    fetch(qp('/api/upload_images'), {
        method: 'POST',
        body: formData
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            uploadResult.innerHTML = `<span style="color:#388e3c;">✓ 上传成功！共上传 ${data.count || files.length} 张图片</span>`;
            // 延迟刷新列表并关闭弹窗
            setTimeout(() => {
                reloadImageList();
                uploadModal.style.display = 'none';
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

document.getElementById('upload-cancel').addEventListener('click', () => {
    uploadModal.style.display = 'none';
});

document.getElementById('upload-close').addEventListener('click', () => {
    uploadModal.style.display = 'none';
});

// 点击遮罩层关闭弹窗
uploadModal.addEventListener('click', (e) => {
    if (e.target === uploadModal) {
        uploadModal.style.display = 'none';
    }
});

// ── Class color legend ──────────────────────────────────────
function buildColorLegend() {
    const wrap = document.getElementById('class-color-legend');
    if (!wrap) return;
    wrap.innerHTML = '';
    CLASSES.forEach(cls => {
        const item = document.createElement('div');
        item.className = 'legend-cls-item';
        const swatch = document.createElement('div');
        swatch.className = 'legend-cls-swatch';
        swatch.style.background = classColor(cls);
        const label = document.createElement('span');
        label.textContent = cls;
        item.appendChild(swatch);
        item.appendChild(label);
        wrap.appendChild(item);
    });
}

// ── Dir browser (for export path selection) ────────────────
let _dirCallback = null;
let _dirCurrent = '';

function openDirBrowser(callback, startPath) {
    _dirCallback = callback;
    loadDirBrowser(startPath || BASE_DIR);
    document.getElementById('dir-browser-modal').style.display = 'flex';
}

function loadDirBrowser(path) {
    fetch('/api/browse_dir?path=' + encodeURIComponent(path))
        .then(r => r.json()).then(data => {
            if (data.error) { showMessage(data.error, true); return; }
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
        }).catch(() => showMessage('无法加载目录', true));
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

// ── YOLO export/import ─────────────────────────────────────
document.getElementById('btn-browse-yolo-import').addEventListener('click', () => {
    openDirBrowser(path => { document.getElementById('yolo-import-dir').value = path; },
                   document.getElementById('yolo-import-dir').value || BASE_DIR);
});

document.getElementById('btn-import-yolo-dir').addEventListener('click', () => {
    const dir = document.getElementById('yolo-import-dir').value.trim();
    if (!dir) { showMessage('请先选择 YOLO 标注目录', true); return; }
    fetch(qp('/api/import/yolo_dir'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({src_dir: dir})})
        .then(r => r.json()).then(j => {
            if (j.success) {
                const skip = j.skipped?.length ? `（跳过 ${j.skipped.length} 张）` : '';
                showMessage(`YOLO 导入完成，共导入 ${j.imported_count} 张${skip}`);
                reloadImageList();
            } else showMessage(j.error || '导入失败', true);
        }).catch(() => showMessage('导入失败', true));
});

document.getElementById('btn-browse-yolo-export').addEventListener('click', () => {
    openDirBrowser(path => { document.getElementById('yolo-export-dir').value = path; },
                   document.getElementById('yolo-export-dir').value || BASE_DIR);
});

document.getElementById('btn-export-yolo').addEventListener('click', () => {
    const dir = document.getElementById('yolo-export-dir').value.trim();
    const body = dir ? {output_dir: dir} : {};
    fetch(qp('/api/export/yolo'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
        .then(r => r.json()).then(j => {
            if (j.success) showMessage(`YOLO 已导出 ${j.exported_count} 张到: ${j.output_dir}`);
            else showMessage(j.error || '导出失败', true);
        }).catch(() => showMessage('导出失败', true));
});

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
    if (viewer.style.display !== 'none') {
        if (e.key === 'ArrowLeft') { e.preventDefault(); document.getElementById('btn-prev-image').click(); }
        else if (e.key === 'ArrowRight') { e.preventDefault(); document.getElementById('btn-next-image').click(); }
        else if (e.ctrlKey && e.key === 's') { e.preventDefault(); document.getElementById('btn-save').click(); }
    }
});

// Resize functionality
let originalBoxes = null;
let originalDimensions = null;

document.querySelectorAll('.resize-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.getElementById('resize-ratio').value = btn.dataset.ratio;
        document.querySelectorAll('.resize-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
    });
});

document.getElementById('btn-resize-preview').addEventListener('click', () => {
    if (!selectedImageName) {
        showMessage('请先选择图片', true);
        return;
    }

    const ratio = parseFloat(document.getElementById('resize-ratio').value);
    if (!ratio || ratio <= 0 || ratio > 5) {
        showMessage('比例必须在 0.1 到 5.0 之间', true);
        return;
    }

    // Save original state if not already saved
    if (!originalBoxes) {
        originalBoxes = JSON.parse(JSON.stringify(boxes));
        originalDimensions = {
            width: imageEl.naturalWidth,
            height: imageEl.naturalHeight
        };
        document.getElementById('btn-resize-reset').style.display = 'inline-block';
    }

    // Apply CSS transform for visual preview
    imageEl.style.transform = `scaleY(${ratio})`;
    imageEl.style.transformOrigin = 'top left';

    // Update overlay viewBox to match stretched dimensions
    const newH = originalDimensions.height * ratio;
    overlay.setAttribute('viewBox', `0 0 ${originalDimensions.width} ${newH}`);

    // Scale boxes Y coordinates for preview
    boxes = originalBoxes.map(b => ({
        ...b,
        y: b.y * ratio,
        h: b.h * ratio
    }));
    updateOverlay();

    const newHeight = Math.round(originalDimensions.height * ratio);
    setStatus(`预览: ${originalDimensions.width}×${newHeight} (比例 ${ratio})`);
    showMessage(`预览: ${originalDimensions.width}×${newHeight}`);
});

document.getElementById('btn-resize-apply').addEventListener('click', () => {
    if (!selectedImageName) {
        showMessage('请先选择图片', true);
        return;
    }

    const ratio = parseFloat(document.getElementById('resize-ratio').value);
    if (!ratio || ratio <= 0 || ratio > 5) {
        showMessage('比例必须在 0.1 到 5.0 之间', true);
        return;
    }

    if (!confirm(`确认将图像高度调整为 ${ratio} 倍？此操作将修改图像文件和标注数据。`)) {
        return;
    }

    fetch(qp(`/api/resize_image/${selectedImageName}`), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ratio: ratio, preview: false})
    }).then(r => r.json()).then(data => {
        if (data.success) {
            showMessage(`已应用: ${data.new_width}×${data.new_height}`);
            // Reset preview state
            originalBoxes = null;
            originalDimensions = null;
            document.getElementById('btn-resize-reset').style.display = 'none';
            // Reload image
            loadImage(selectedImageName);
            reloadImageList();
        } else {
            showMessage(data.error || '应用失败', true);
        }
    }).catch(err => {
        showMessage('应用接口出错', true);
        console.error(err);
    });
});

document.getElementById('btn-resize-reset').addEventListener('click', () => {
    if (originalBoxes && originalDimensions) {
        boxes = originalBoxes;
        originalBoxes = null;
        originalDimensions = null;
        document.getElementById('btn-resize-reset').style.display = 'none';
        // Clear CSS transform
        imageEl.style.transform = '';
        imageEl.style.transformOrigin = '';
        // Restore original viewBox and overlay
        const imgW = imageEl.naturalWidth;
        const imgH = imageEl.naturalHeight;
        overlay.setAttribute('viewBox', `0 0 ${imgW} ${imgH}`);
        overlay.style.width = imageEl.clientWidth + 'px';
        overlay.style.height = imageEl.clientHeight + 'px';
        updateOverlay();
        setStatus(`已重置: ${imgW}×${imgH}`);
        showMessage('已重置预览');
    }
});

// Scale functionality (black padding)
let scaleOriginalBoxes = null;
let scaleOriginalSrc = null;

document.querySelectorAll('.scale-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.getElementById('scale-ratio').value = btn.dataset.ratio;
        document.querySelectorAll('.scale-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
    });
});

document.getElementById('btn-scale-preview').addEventListener('click', () => {
    if (!selectedImageName) {
        showMessage('请先选择图片', true);
        return;
    }

    const ratio = parseFloat(document.getElementById('scale-ratio').value);
    if (!ratio || ratio <= 0 || ratio > 5) {
        showMessage('比例必须在 0.1 到 5.0 之间', true);
        return;
    }

    const imgW = imageEl.naturalWidth;
    const imgH = imageEl.naturalHeight;

    // Save original state
    if (!scaleOriginalBoxes) {
        scaleOriginalBoxes = JSON.parse(JSON.stringify(boxes));
        scaleOriginalSrc = imageEl.src;
        document.getElementById('btn-scale-reset').style.display = 'inline-block';
    }

    // Create canvas for preview
    const canvas = document.createElement('canvas');
    canvas.width = imgW;
    canvas.height = imgH;
    const ctx = canvas.getContext('2d');

    // Fill with black
    ctx.fillStyle = 'black';
    ctx.fillRect(0, 0, imgW, imgH);

    // Calculate scaled dimensions and offset
    const newW = Math.round(imgW * ratio);
    const newH = Math.round(imgH * ratio);
    const offsetX = Math.round((imgW - newW) / 2);
    const offsetY = Math.round((imgH - newH) / 2);

    // Draw scaled image centered
    ctx.drawImage(imageEl, offsetX, offsetY, newW, newH);

    // Update image src to canvas data
    imageEl.src = canvas.toDataURL();

    // Update boxes with scaled coordinates
    boxes = scaleOriginalBoxes.map(b => ({
        ...b,
        x: b.x * ratio + offsetX,
        y: b.y * ratio + offsetY,
        w: b.w * ratio,
        h: b.h * ratio
    }));
    updateOverlay();

    const action = ratio < 1 ? '缩小' : '放大';
    setStatus(`预览: ${action} ${ratio}倍 (黑边填充)`);
    showMessage(`预览: ${action} ${ratio}倍`);
});

document.getElementById('btn-scale-apply').addEventListener('click', () => {
    if (!selectedImageName) {
        showMessage('请先选择图片', true);
        return;
    }

    const ratio = parseFloat(document.getElementById('scale-ratio').value);
    if (!ratio || ratio <= 0 || ratio > 5) {
        showMessage('比例必须在 0.1 到 5.0 之间', true);
        return;
    }

    if (!confirm(`确认将图像${ratio < 1 ? '缩小' : '放大'} ${ratio} 倍？画布尺寸保持不变。`)) {
        return;
    }

    fetch(qp(`/api/scale_image/${selectedImageName}`), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ratio: ratio, preview: false})
    }).then(r => r.json()).then(data => {
        if (data.success) {
            const action = ratio < 1 ? '缩小' : '放大';
            showMessage(`已${action}: ${data.canvas_width}×${data.canvas_height}`);
            // Reset preview state
            scaleOriginalBoxes = null;
            scaleOriginalSrc = null;
            document.getElementById('btn-scale-reset').style.display = 'none';
            // Reload image
            loadImage(selectedImageName);
            reloadImageList();
        } else {
            showMessage(data.error || '应用失败', true);
        }
    }).catch(err => {
        showMessage('应用接口出错', true);
        console.error(err);
    });
});

document.getElementById('btn-scale-reset').addEventListener('click', () => {
    if (scaleOriginalBoxes && scaleOriginalSrc) {
        boxes = scaleOriginalBoxes;
        scaleOriginalBoxes = null;
        document.getElementById('btn-scale-reset').style.display = 'none';
        // Restore original image
        imageEl.src = scaleOriginalSrc;
        scaleOriginalSrc = null;
        // Restore overlay
        const imgW = imageEl.naturalWidth;
        const imgH = imageEl.naturalHeight;
        overlay.setAttribute('viewBox', `0 0 ${imgW} ${imgH}`);
        overlay.style.width = imageEl.clientWidth + 'px';
        overlay.style.height = imageEl.clientHeight + 'px';
        updateOverlay();
        setStatus(`已重置: ${imgW}×${imgH}`);
        showMessage('已重置预览');
    }
});

// ── Lightbox (large image preview) ─────────────────────────
const lightboxModal = document.getElementById('lightbox-modal');
const lightboxImage = document.getElementById('lightbox-image');
const lightboxTitle = document.getElementById('lightbox-title');
const lightboxCounter = document.getElementById('lightbox-counter');
let lightboxName = null;

function openLightbox(name) {
    lightboxName = name;
    const filtered = getFilteredImages();
    const idx = filtered.findIndex(i => i.name === name);
    lightboxImage.src = qp(`/image/${name}`);
    lightboxTitle.textContent = filtered[idx]?.filename || name;
    lightboxCounter.textContent = `${idx + 1} / ${filtered.length}`;
    lightboxModal.style.display = 'flex';
}

function closeLightbox() {
    lightboxModal.style.display = 'none';
    lightboxName = null;
}

function lightboxNavigate(delta) {
    if (!lightboxName) return;
    const filtered = getFilteredImages();
    let idx = filtered.findIndex(i => i.name === lightboxName);
    if (idx < 0) return;
    idx = idx + delta;
    if (idx < 0) idx = filtered.length - 1;
    if (idx >= filtered.length) idx = 0;
    const item = filtered[idx];
    lightboxName = item.name;
    lightboxImage.src = qp(`/image/${item.name}`);
    lightboxTitle.textContent = item.filename;
    lightboxCounter.textContent = `${idx + 1} / ${filtered.length}`;
}

document.getElementById('lightbox-close').addEventListener('click', closeLightbox);
document.getElementById('lightbox-prev').addEventListener('click', () => lightboxNavigate(-1));
document.getElementById('lightbox-next').addEventListener('click', () => lightboxNavigate(1));
document.getElementById('lightbox-annotate').addEventListener('click', () => {
    if (!lightboxName) return;
    const name = lightboxName;
    closeLightbox();
    gridView.style.display = 'none';
    viewer.style.display = 'block';
    // Hide toolbar when entering annotation mode
    document.querySelector('.toolbar').style.display = 'none';
    loadImage(name);
});
lightboxModal.addEventListener('click', (e) => {
    if (e.target === lightboxModal) closeLightbox();
});

// init
loadClassOptions();
initClassFilter();
buildColorLegend();
reloadImageList();
setStatus('请在网格中选择图片，然后点击标注');

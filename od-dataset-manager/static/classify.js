// classify.js — Image classification annotation page logic
// Adapted from main.js: no SVG boxes, just pick a whole-image label per image.
// Layout aligned with index.html (toolbar + filters + grid), upload via shared upload_modal.js.

const statsEl = document.getElementById('stats');
const gridView = document.getElementById('grid-view');
const imageGrid = document.getElementById('image-grid');
const viewer = document.getElementById('viewer');
const imageEl = document.getElementById('current-image');
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

// Append version_id query param to any API path
function qp(path) {
    if (!VERSION_ID) return path;
    const sep = path.includes('?') ? '&' : '?';
    return path + sep + 'version_id=' + encodeURIComponent(VERSION_ID);
}

function setStatus(text) {
    currentStatus.textContent = text;
}

function showMessage(text, isError = false) {
    messageEl.textContent = text;
    messageEl.style.color = isError ? '#d43f3a' : '#2b7a78';
    setTimeout(() => { messageEl.textContent = ''; }, 3000);
}

// Classification "labeled" = has a non-null class_label.
// NOTE: do NOT use item.has_label — clearing a label only UPDATEs the row
// (class_label=NULL) without deleting it, so has_label stays true.
function isLabeled(item) {
    return !!item.class_label;
}

function deleteImage(name) {
    if (confirm('确认删除图像和标注？')) {
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

function getFilteredImages() {
    let filtered = allImages;
    if (currentFilter === 'labeled') filtered = filtered.filter(i => isLabeled(i));
    else if (currentFilter === 'unlabeled') filtered = filtered.filter(i => !isLabeled(i));
    if (currentClassFilter !== 'all') {
        filtered = filtered.filter(i => i.class_label === currentClassFilter);
    }
    return filtered;
}

function reloadImageList() {
    fetch(qp('/api/images')).then(r => r.json()).then(items => {
        allImages = items;
        renderGrid();
        updateStats();
        updateFilterIndicator();
    });
}

function updateStats() {
    const total = allImages.length;
    const labeled = allImages.filter(isLabeled).length;
    const unlabeled = total - labeled;
    statsEl.innerHTML = `总图片: <strong>${total}</strong>，已标注: <strong>${labeled}</strong>，未标注: <strong>${unlabeled}</strong>`;
}

function renderGrid() {
    imageGrid.innerHTML = '';
    const filtered = getFilteredImages();
    const start = (currentPage - 1) * imagesPerPage;
    const pageImages = filtered.slice(start, start + imagesPerPage);

    pageImages.forEach(item => {
        const div = document.createElement('div');
        div.className = 'grid-item';
        div.classList.add(isLabeled(item) ? 'labeled' : 'unlabeled');
        if (selectedImageNames.has(item.name)) div.classList.add('selected');

        const img = document.createElement('img');
        img.src = qp(`/image/${item.name}`);
        img.alt = item.filename;
        img.loading = 'lazy';
        div.appendChild(img);

        const statusIcon = document.createElement('div');
        statusIcon.className = 'status-icon';
        div.appendChild(statusIcon);

        // Current label badge (top-left) — classification-specific
        if (item.class_label) {
            const tag = document.createElement('div');
            tag.className = 'grid-label-tag';
            tag.textContent = item.class_label;
            div.appendChild(tag);
        }

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

        const delBtn = document.createElement('button');
        delBtn.className = 'grid-del-btn';
        delBtn.title = '删除';
        delBtn.textContent = '×';
        delBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            deleteImage(item.name);
        });
        div.appendChild(delBtn);

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
    updateFilterIndicator();
}

function updatePageInfo() {
    const filtered = getFilteredImages();
    const totalPages = Math.max(1, Math.ceil(filtered.length / imagesPerPage));
    document.getElementById('page-info').textContent = `第 ${currentPage} 页 / 共 ${totalPages} 页`;
    document.getElementById('btn-prev-page').disabled = currentPage === 1;
    document.getElementById('btn-next-page').disabled = currentPage === totalPages;
}

function updateFilterIndicator() {
    const indicator = document.getElementById('filter-indicator');
    if (!indicator) return;
    const parts = [];
    if (currentFilter !== 'all') {
        parts.push(currentFilter === 'labeled' ? '已标注' : '未标注');
    }
    if (currentClassFilter !== 'all') {
        parts.push(`类别: ${currentClassFilter}`);
    }
    if (parts.length === 0) {
        indicator.style.display = 'none';
    } else {
        indicator.style.display = 'inline-block';
        indicator.textContent = `${parts.join(' | ')} (${getFilteredImages().length})`;
    }
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
    });
}

function loadImage(name) {
    const filtered = getFilteredImages();
    currentImageIndex = filtered.findIndex(i => i.name === name);
    const item = filtered[currentImageIndex] || allImages.find(i => i.name === name);
    if (!item) return;

    if (item.class_label && CLASSES.includes(item.class_label)) {
        classSelect.value = item.class_label;
    } else if (CLASSES.length) {
        classSelect.value = CLASSES[0];
    }

    imageEl.src = qp(`/image/${item.name}`);
    const lbl = item.class_label ? `（当前标签：${item.class_label}）` : '（未标注）';
    setStatus(`当前: ${name} ${lbl}`);
}

function autoSave(callback) {
    if (!selectedImageName) { callback(); return; }
    const cls = classSelect.value;
    fetch(qp(`/api/classify/${selectedImageName}`), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({class_label: cls})
    }).then(r => r.json()).then(data => {
        if (data.success) {
            const item = allImages.find(i => i.name === selectedImageName);
            if (item) item.class_label = cls;
            setStatus(`已自动保存: ${selectedImageName} -> ${cls}`);
        }
    }).catch(() => {}).finally(() => callback());
}

document.getElementById('btn-save').addEventListener('click', () => {
    if (!selectedImageName) { showMessage('请先选择图片', true); return; }
    const cls = classSelect.value;
    fetch(qp(`/api/classify/${selectedImageName}`), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({class_label: cls})
    }).then(r => r.json()).then(data => {
        if (data.success) {
            const item = allImages.find(i => i.name === selectedImageName);
            if (item) item.class_label = cls;
            showMessage('保存成功');
            setStatus(`已保存：${selectedImageName} -> ${cls}`);
            reloadImageList();
        } else {
            showMessage(data.error || '保存失败', true);
        }
    }).catch(err => { showMessage('保存接口出错', true); console.error(err); });
});

document.getElementById('btn-clear-label').addEventListener('click', () => {
    if (!selectedImageName) { showMessage('请先选择图片', true); return; }
    fetch(qp(`/api/classify/${selectedImageName}`), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({class_label: ''})
    }).then(r => r.json()).then(data => {
        if (data.success) {
            const item = allImages.find(i => i.name === selectedImageName);
            if (item) item.class_label = null;
            showMessage('已清除标签');
            setStatus(`已清除：${selectedImageName}`);
            reloadImageList();
        } else {
            showMessage(data.error || '清除失败', true);
        }
    }).catch(err => { showMessage('清除接口出错', true); console.error(err); });
});

document.getElementById('btn-prev-page').addEventListener('click', () => {
    if (currentPage > 1) { currentPage--; renderGrid(); }
});

document.getElementById('btn-next-page').addEventListener('click', () => {
    const filtered = getFilteredImages();
    if (currentPage < Math.ceil(filtered.length / imagesPerPage)) { currentPage++; renderGrid(); }
});

document.getElementById('btn-annotate').addEventListener('click', () => {
    const filtered = getFilteredImages();
    if (!filtered.length) { showMessage('没有可标注的图片', true); return; }
    // Annotate the selected image if any, otherwise start from the first of the filtered set
    if (!selectedImageName || !filtered.find(i => i.name === selectedImageName)) {
        selectedImageName = filtered[0].name;
    }
    gridView.style.display = 'none';
    viewer.style.display = 'block';
    document.querySelector('.toolbar').style.display = 'none';
    loadImage(selectedImageName);
});

document.getElementById('btn-back-to-grid').addEventListener('click', () => {
    viewer.style.display = 'none';
    gridView.style.display = 'block';
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

document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentFilter = btn.dataset.filter;
        currentPage = 1;
        selectedImageNames.clear();
        selectedImageName = null;
        renderGrid();
    });
});

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

document.getElementById('btn-refresh').addEventListener('click', reloadImageList);

// ── Dir browser (for export/import path selection) ───────
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

// ── CSV export / import ───────────────────────────────────
document.getElementById('btn-browse-classify-import').addEventListener('click', () => {
    openDirBrowser(path => { document.getElementById('classify-import-dir').value = path; },
                   document.getElementById('classify-import-dir').value || BASE_DIR);
});

document.getElementById('btn-import-csv').addEventListener('click', () => {
    const dir = document.getElementById('classify-import-dir').value.trim();
    if (!dir) { showMessage('请先选择 CSV 所在目录', true); return; }
    fetch(qp('/api/import/classify_csv'), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({version_id: VERSION_ID, src_dir: dir})
    }).then(r => r.json()).then(data => {
        if (data.success) {
            showMessage(`导入 ${data.imported} 条（跳过：未知类 ${data.skipped_unknown}，未找到 ${data.skipped_not_found}）← ${data.csv_file}`);
            reloadImageList();
        } else {
            showMessage(data.error || '导入失败', true);
        }
    }).catch(err => { showMessage('导入接口出错', true); console.error(err); });
});

document.getElementById('btn-browse-classify-export').addEventListener('click', () => {
    openDirBrowser(path => { document.getElementById('classify-export-dir').value = path; },
                   document.getElementById('classify-export-dir').value || BASE_DIR);
});

document.getElementById('btn-export-csv').addEventListener('click', () => {
    const dir = document.getElementById('classify-export-dir').value.trim();
    if (!dir) { showMessage('请先选择输出目录', true); return; }
    fetch(qp('/api/export/classify_csv'), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({version_id: VERSION_ID, output_dir: dir})
    }).then(r => r.json()).then(data => {
        if (data.success) showMessage(`已导出 ${data.exported_count} 条 → ${data.output_dir}`);
        else showMessage(data.error || '导出失败', true);
    }).catch(err => { showMessage('导出接口出错', true); console.error(err); });
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
    selectedImageName = lightboxName;
    closeLightbox();
    gridView.style.display = 'none';
    viewer.style.display = 'block';
    document.querySelector('.toolbar').style.display = 'none';
    loadImage(selectedImageName);
});
lightboxModal.addEventListener('click', (e) => {
    if (e.target === lightboxModal) closeLightbox();
});

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
    if (viewer.style.display !== 'none') {
        if (e.key === 'ArrowLeft') { e.preventDefault(); document.getElementById('btn-prev-image').click(); }
        else if (e.key === 'ArrowRight') { e.preventDefault(); document.getElementById('btn-next-image').click(); }
        else if (e.ctrlKey && e.key === 's') { e.preventDefault(); document.getElementById('btn-save').click(); }
    }
});

// init
loadClassOptions();
initClassFilter();
initUploadModal({ uploadUrl: qp('/api/upload_images'), onUploaded: reloadImageList });
reloadImageList();
setStatus('请在网格中选择图片，然后点击标注');

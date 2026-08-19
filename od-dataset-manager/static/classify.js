// classify.js — Image classification annotation page logic
// Adapted from main.js: no SVG boxes, just pick a whole-image label per image.

const imageListEl = document.getElementById('image-list');
const statsEl = document.getElementById('stats');
const gridView = document.getElementById('grid-view');
const imageGrid = document.getElementById('image-grid');
const viewer = document.getElementById('viewer');
const imageEl = document.getElementById('current-image');
const classSelect = document.getElementById('class-select');
const currentStatus = document.getElementById('current-status');
const messageEl = document.getElementById('message');

let allImages = [];
let currentPage = 1;
let imagesPerPage = 40;
let selectedImageName = null;
let selectedImageNames = new Set();
let currentImageIndex = -1;
let viewAllImages = true;   // false when filtering to unlabeled only

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

function isLabeled(item) {
    return !!item.class_label;
}

function reloadImageList() {
    fetch(qp('/api/images')).then(r => r.json()).then(items => {
        allImages = viewAllImages ? items : items.filter(i => !isLabeled(i));
        renderGrid();
        renderList();
        updateStats();
    });
}

function renderList() {
    imageListEl.innerHTML = '';
    allImages.forEach(i => {
        const li = document.createElement('li');
        li.textContent = i.filename;
        if (!isLabeled(i)) li.classList.add('unlabeled');
        else li.title = '标签：' + i.class_label;
        li.addEventListener('click', (e) => {
            if (e.ctrlKey || e.metaKey) {
                if (selectedImageNames.has(i.name)) selectedImageNames.delete(i.name);
                else selectedImageNames.add(i.name);
            } else {
                selectedImageNames.clear();
                selectedImageNames.add(i.name);
            }
            selectedImageName = i.name;
            renderGrid();
            renderList();
        });
        if (selectedImageNames.has(i.name)) li.classList.add('selected');
        imageListEl.appendChild(li);
    });
}

function updateStats() {
    const total = allImages.length;
    const labeled = allImages.filter(isLabeled).length;
    statsEl.textContent = `总图片: ${total}，已标注: ${labeled}，未标注: ${total - labeled}`;
}

function renderGrid() {
    imageGrid.innerHTML = '';
    const start = (currentPage - 1) * imagesPerPage;
    const pageImages = allImages.slice(start, start + imagesPerPage);

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

        // Current label badge (top-left)
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
            renderList();
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
            } else {
                selectedImageNames.clear();
                selectedImageNames.add(item.name);
            }
            selectedImageName = item.name;
            renderGrid();
            renderList();
        });
        imageGrid.appendChild(div);
    });

    updatePageInfo();
}

function updatePageInfo() {
    const totalPages = Math.max(1, Math.ceil(allImages.length / imagesPerPage));
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
    const item = allImages[currentImageIndex];
    if (!item) return;

    // Pre-select dropdown to current label (if any)
    if (item.class_label && CLASSES.includes(item.class_label)) {
        classSelect.value = item.class_label;
    } else if (CLASSES.length) {
        classSelect.value = CLASSES[0];
    }

    imageEl.src = qp(`/image/${item.filename}`);
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
    if (currentPage < Math.ceil(allImages.length / imagesPerPage)) { currentPage++; renderGrid(); }
});

document.getElementById('btn-annotate').addEventListener('click', () => {
    if (!selectedImageName) { showMessage('请先选择图片', true); return; }
    gridView.style.display = 'none';
    viewer.style.display = 'block';
    loadImage(selectedImageName);
});

document.getElementById('btn-back-to-grid').addEventListener('click', () => {
    viewer.style.display = 'none';
    gridView.style.display = 'block';
    renderGrid();
});

document.getElementById('btn-prev-image').addEventListener('click', () => {
    if (currentImageIndex > 0) {
        autoSave(() => {
            selectedImageName = allImages[currentImageIndex - 1].name;
            loadImage(selectedImageName);
        });
    }
});

document.getElementById('btn-next-image').addEventListener('click', () => {
    if (currentImageIndex < allImages.length - 1) {
        autoSave(() => {
            selectedImageName = allImages[currentImageIndex + 1].name;
            loadImage(selectedImageName);
        });
    }
});

document.getElementById('btn-unlabeled').addEventListener('click', () => {
    viewAllImages = !viewAllImages;
    document.getElementById('btn-unlabeled').textContent = viewAllImages ? '未标注' : '显示全部';
    reloadImageList();
});

document.getElementById('btn-annotate-all').addEventListener('click', () => {
    if (!allImages.length) { showMessage('没有图片', true); return; }
    const startIndex = selectedImageName
        ? Math.max(0, allImages.findIndex(i => i.name === selectedImageName))
        : 0;
    selectedImageName = allImages[startIndex].name;
    gridView.style.display = 'none';
    viewer.style.display = 'block';
    loadImage(selectedImageName);
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

document.getElementById('btn-select-upload').addEventListener('click', () => {
    document.getElementById('upload-images').click();
});

document.getElementById('btn-upload').addEventListener('click', () => {
    const files = document.getElementById('upload-images').files;
    if (!files.length) { showMessage('请先选择图像文件', true); return; }
    const formData = new FormData();
    for (let f of files) formData.append('images', f);
    fetch(qp('/api/upload_images'), {method: 'POST', body: formData})
        .then(r => r.json()).then(data => {
            if (data.success) { showMessage('上传成功'); reloadImageList(); }
            else showMessage('上传失败', true);
        }).catch(err => { showMessage('上传接口出错', true); console.error(err); });
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
reloadImageList();
setStatus('请在网格中选择图片，然后点击标注');

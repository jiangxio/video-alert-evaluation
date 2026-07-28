/* 误检复核工作台前端逻辑 */
(function () {
    'use strict';

    const TASK_ID = window.TASK_ID;
    const TYPE_NAMES = window.TYPE_NAMES || {};

    let allAlerts = [];       // 全量告警
    let filteredAlerts = [];  // 筛选后（扁平，带原索引）
    let currentIndex = -1;    // 当前在 filteredAlerts 中的索引
    let currentImgIdx = 0;    // 多图组内当前展示的图索引
    const selectedIds = new Set();  // 批量审查选中的告警 id

    const PAGE_SIZE = 50;
    let listPage = 1;

    const STATUS_LABEL = { correct: '正确', false_positive: '误检', ignored: '忽略', auto: '自动' };

    // ── 初始化 ──────────────────────────────────────────────────────────────
    async function init() {
        const resp = await fetch(`/review/api/${TASK_ID}/alerts`);
        const data = await resp.json();
        if (!data.success) {
            document.getElementById('alert-list').innerHTML = '<div style="padding:1rem;color:#e74c3c;">加载失败：' + (data.error || '') + '</div>';
            return;
        }
        allAlerts = data.alerts;
        buildFilterOptions();
        renderList();
    }

    function buildFilterOptions() {
        const videos = [...new Set(allAlerts.map(a => a.video_id))].sort();
        const types = [...new Set(allAlerts.map(a => a.event_type))].sort();

        const vSel = document.getElementById('filter-video');
        vSel.innerHTML = '<option value="">全部视频</option>' +
            videos.map(v => `<option value="${v}">${v}</option>`).join('');

        const tSel = document.getElementById('filter-type');
        tSel.innerHTML = '<option value="">全部类型</option>' +
            types.map(t => `<option value="${t}">${TYPE_NAMES[t] || t}</option>`).join('');
    }

    // ── 筛选 ──────────────────────────────────────────────────────────────────
    // AI审查判定与自动判定是否一致（只有 verdict 为 correct/false_positive 才算有效审查）
    function reviewRelation(a) {
        // 返回 'none'(未审查) / 'agree'(一致) / 'disagree'(不一致) / 'error'(审查出错)
        const s = a.ai_suggestion;
        if (!s) return 'none';
        if (s.verdict === 'error') return 'error';
        // 自动判定 effective_status: correct / false_positive / ignored
        // AI verdict: correct / false_positive
        const auto = a.effective_status === 'ignored' ? null : a.effective_status;
        if (auto === null) return 'none'; // 忽略的不比较
        return s.verdict === auto ? 'agree' : 'disagree';
    }

    function getFiltered() {
        const st = document.getElementById('filter-status').value;
        const rv = document.getElementById('filter-review').value;
        const vid = document.getElementById('filter-video').value;
        const et = document.getElementById('filter-type').value;
        const multi = document.getElementById('filter-multi').checked;
        const kw = document.getElementById('filter-search').value.trim().toLowerCase();

        return allAlerts.filter(a => {
            if (vid && a.video_id !== vid) return false;
            if (et && a.event_type !== et) return false;
            if (multi && (a.image_ids || []).length <= 1) return false;
            if (kw && !String(a.video_id).toLowerCase().includes(kw)) return false;
            if (st !== 'all' && a.effective_status !== st) return false;
            // 审查筛选
            if (rv !== 'all') {
                const rel = reviewRelation(a);
                if (rv === 'reviewed' && rel === 'none') return false;
                if (rv === 'unreviewed' && rel !== 'none') return false;
                if (rv === 'agree' && rel !== 'agree') return false;
                if (rv === 'disagree' && rel !== 'disagree') return false;
            }
            return true;
        });
    }

    // ── 列表渲染（按事件类型分组，分页展示） ────────────────────────────────────────────
    window.renderList = function () {
        filteredAlerts = getFiltered();
        document.getElementById('list-summary').textContent =
            `共 ${filteredAlerts.length} 条 / 全部 ${allAlerts.length} 条`;

        const listEl = document.getElementById('alert-list');
        if (!filteredAlerts.length) {
            listEl.innerHTML = '<div style="padding:1rem;color:#999;">无匹配告警</div>';
            renderListPagination(1, 1);
            return;
        }

        const totalPages = Math.ceil(filteredAlerts.length / PAGE_SIZE) || 1;
        if (listPage > totalPages) listPage = totalPages;
        if (listPage < 1) listPage = 1;
        const pageStart = (listPage - 1) * PAGE_SIZE;
        const pagedAlerts = filteredAlerts.slice(pageStart, pageStart + PAGE_SIZE);

        // 按事件类型分组（仅当前页）
        const groups = {};
        pagedAlerts.forEach((a, offset) => {
            const idx = pageStart + offset;
            (groups[a.event_type] = groups[a.event_type] || []).push({ a, idx });
        });

        let html = '';
        // 全选行
        html += `<div class="select-all-row"><label><input type="checkbox" id="select-all" onchange="toggleSelectAll(this.checked)"> 全选当前列表 (${filteredAlerts.length})</label> <span class="selected-count" id="selected-count"></span></div>`;
        for (const etype of Object.keys(groups)) {
            html += `<div class="group-header">${TYPE_NAMES[etype] || etype} (${groups[etype].length})</div>`;
            for (const { a, idx } of groups[etype]) {
                const cls = ['alert-item'];
                if (idx === currentIndex) cls.push('active');
                if (a.ai_suggestion) cls.push('has-suggestion');
                if (selectedIds.has(a.id)) cls.push('selected');
                const badge = statusBadge(a.effective_status);
                const ts = fmtTs(a.ts_start, a.ts_end);
                const rel = reviewRelation(a);
                let aiLine = '';
                if (a.ai_suggestion) {
                    const vText = { correct: '审查正确', false_positive: '审查误检', error: '审查异常' }[a.ai_suggestion.verdict] || a.ai_suggestion.verdict;
                    const vCls = { correct: 'rv-ok', false_positive: 'rv-fp', error: 'rv-err' }[a.ai_suggestion.verdict] || 'rv-err';
                    let relTag = '';
                    if (rel === 'agree') relTag = ' <span class="rv-tag rv-agree">一致</span>';
                    else if (rel === 'disagree') relTag = ' <span class="rv-tag rv-disagree">不一致</span>';
                    aiLine = `<div class="ai"><span class="rv-badge ${vCls}">${vText}</span>${relTag} <span class="rv-reason">${a.ai_suggestion.reason || ''}</span></div>`;
                }
                const imgCount = (a.image_ids || []).length;
                const checked = selectedIds.has(a.id) ? 'checked' : '';
                html += `<div class="${cls.join(' ')}" data-idx="${idx}">
                    <input type="checkbox" class="item-check" data-idx="${idx}" data-id="${a.id}" ${checked} onclick="event.stopPropagation()" onchange="toggleSelect(${a.id}, this.checked)">
                    <img class="alert-thumb" src="${thumbUrl(a.representative_image_id, 120)}" loading="lazy" onerror="this.style.visibility='hidden'" onclick="selectAlert(${idx})">
                    <div class="alert-info" onclick="selectAlert(${idx})">
                        <div>${a.video_id} ${badge} ${imgCount > 1 ? `<span style="color:#888;">×${imgCount}</span>` : ''}</div>
                        <div class="ts">${ts}</div>
                        ${aiLine}
                    </div>
                </div>`;
            }
        }
        listEl.innerHTML = html;
        updateSelectedCount();
        renderListPagination(listPage, totalPages);
    }

    window.onFilterChange = function () {
        currentIndex = -1;
        listPage = 1;
        renderList();
    }

    function setListPage(p) {
        listPage = p;
        renderList();
        const listEl = document.getElementById('alert-list');
        if (listEl) listEl.scrollIntoView({ block: 'nearest' });
    }

    function renderListPagination(page, totalPages) {
        const el = document.getElementById('list-pagination');
        if (!el) return;
        if (totalPages <= 1) {
            el.innerHTML = '';
            return;
        }
        el.innerHTML = `
            <div style="display:flex;justify-content:center;align-items:center;gap:0.5rem;padding:0.5rem;font-size:0.85rem;">
                <button class="btn btn-sm" onclick="window.setListPage(${page - 1})" ${page <= 1 ? 'disabled' : ''}>上一页</button>
                <span>第 ${page} / ${totalPages} 页（每页 ${PAGE_SIZE} 条）</span>
                <button class="btn btn-sm" onclick="window.setListPage(${page + 1})" ${page >= totalPages ? 'disabled' : ''}>下一页</button>
            </div>
        `;
    }

    window.setListPage = setListPage;

    function statusBadge(st) {
        const map = { correct: ['badge-correct', '正确'], false_positive: ['badge-fp', '误检'], ignored: ['badge-ignored', '忽略'] };
        const [cls, label] = map[st] || ['badge-ignored', st];
        return `<span class="badge ${cls}">${label}</span>`;
    }

    function aiVerdictText(s) {
        if (!s) return '';
        const v = { correct: '正确', false_positive: '误检', error: '出错' }[s.verdict] || s.verdict;
        return `${v} - ${s.reason || ''}`;
    }

    // ── 选中告警 ──────────────────────────────────────────────────────────────
    window.selectAlert = function (idx) {
        currentIndex = idx;
        currentImgIdx = 0;
        if (currentIndex >= 0 && filteredAlerts.length) {
            listPage = Math.floor(currentIndex / PAGE_SIZE) + 1;
        }
        renderList();
        renderCenter();
        renderTimeline();
        // 滚动到当前项
        const el = document.querySelector(`.alert-item[data-idx="${idx}"]`);
        if (el) el.scrollIntoView({ block: 'nearest' });
    };

    function currentAlert() {
        return currentIndex >= 0 ? filteredAlerts[currentIndex] : null;
    }

    // ── 中栏渲染 ──────────────────────────────────────────────────────────────
    function renderCenter() {
        const a = currentAlert();
        if (!a) {
            document.getElementById('center-empty').classList.remove('hidden');
            document.getElementById('center-content').classList.add('hidden');
            return;
        }
        document.getElementById('center-empty').classList.add('hidden');
        document.getElementById('center-content').classList.remove('hidden');

        const imgs = a.image_ids || [a.representative_image_id];
        const curImg = imgs[currentImgIdx] || a.representative_image_id;
        document.getElementById('big-image').src = imgUrl(curImg);

        // 缩略图条（多图时）
        const strip = document.getElementById('thumb-strip');
        if (imgs.length > 1) {
            strip.innerHTML = imgs.map((id, i) =>
                `<img src="${thumbUrl(id, 120)}" class="${i === currentImgIdx ? 'active' : ''}" onclick="selectImage(${i})" loading="lazy">`
            ).join('');
            strip.classList.remove('hidden');
        } else {
            strip.innerHTML = '';
            strip.classList.add('hidden');
        }

        // 详情
        const meta = document.getElementById('detail-meta');
        const gtInfo = a.matched_gt_event_id ? `命中GT#${a.matched_gt_event_id}` : '无匹配GT';
        const ocrTs = a.timestamp_seconds != null ? `${a.timestamp_seconds}s` : '-';
        meta.innerHTML = `
            <div class="row"><span class="k">视频ID</span><span>${a.video_id}</span></div>
            <div class="row"><span class="k">事件类型</span><span>${TYPE_NAMES[a.event_type] || a.event_type}</span></div>
            <div class="row"><span class="k">告警时间</span><span>${fmtTs(a.ts_start, a.ts_end)}</span></div>
            <div class="row"><span class="k">OCR时间戳</span><span>${ocrTs}</span></div>
            <div class="row"><span class="k">自动判定</span><span>${a.is_false_positive ? '误检' : '正确'}</span></div>
            <div class="row"><span class="k">当前状态</span><span>${statusBadge(a.effective_status)} (manual: ${STATUS_LABEL[a.manual_status] || a.manual_status})</span></div>
            <div class="row"><span class="k">GT匹配</span><span>${gtInfo}</span></div>
            <div class="row"><span class="k">位置</span><span>${currentIndex + 1} / ${filteredAlerts.length}</span></div>
        `;

        renderAiSuggestion(a);
    }

    window.selectImage = function (i) {
        currentImgIdx = i;
        renderCenter();
    };

    function renderAiSuggestion(a) {
        const box = document.getElementById('ai-suggestion-box');
        if (!a.ai_suggestion) {
            box.classList.add('hidden');
            return;
        }
        box.classList.remove('hidden');
        const s = a.ai_suggestion;
        const cls = { correct: 'ai-correct', false_positive: 'ai-fp', error: 'ai-error' }[s.verdict] || 'ai-error';
        const label = { correct: 'AI建议：审查正确', false_positive: 'AI建议：审查误检', error: 'AI审查异常' }[s.verdict] || 'AI建议';
        const rel = reviewRelation(a);
        let relLine = '';
        if (rel === 'agree') relLine = '<div style="color:#27ae60;font-size:0.8rem;margin-top:0.3rem;">✓ 与自动判定一致</div>';
        else if (rel === 'disagree') relLine = '<div style="color:#e67e22;font-size:0.8rem;margin-top:0.3rem;">⚠ 与自动判定不一致（自动：' + (a.effective_status === 'false_positive' ? '误检' : '正确') + '）</div>';
        box.className = 'ai-suggestion-box ' + cls;
        box.innerHTML = `<div class="label">${label}</div><div>${s.reason || ''}</div>${relLine}`;
    }

    // ── 改判（复用现有接口） ──────────────────────────────────────────────────
    window.setStatus = async function (status) {
        const a = currentAlert();
        if (!a) return;
        const resp = await fetch(`/evaluation/api/tasks/${TASK_ID}/merged-events/${a.id}/status`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ manual_status: status }),
        });
        const result = await resp.json();
        if (result.success) {
            a.manual_status = status;
            a.effective_status = status === 'auto' ? (a.is_false_positive ? 'false_positive' : 'correct') : status;
            renderList();
            renderCenter();
            nextAlert();
        } else {
            alert('改判失败：' + (result.error || ''));
        }
    };

    function nextAlert() { if (currentIndex < filteredAlerts.length - 1) selectAlert(currentIndex + 1); }
    function prevAlert() { if (currentIndex > 0) selectAlert(currentIndex - 1); }

    // ── 时间轴 ────────────────────────────────────────────────────────────────
    async function renderTimeline() {
        const a = currentAlert();
        if (!a) return;
        const resp = await fetch(`/review/api/${TASK_ID}/gt-context?video_id=${encodeURIComponent(a.video_id)}`);
        const data = await resp.json();
        if (!data.success) return;

        document.getElementById('timeline-empty').classList.add('hidden');
        document.getElementById('timeline-content').classList.remove('hidden');
        document.getElementById('timeline-video-label').textContent = `视频 ${a.video_id}`;

        // 计算时间范围
        const pts = [];
        data.gt_events.forEach(g => { pts.push(g.start_sec); pts.push(g.end_sec); });
        data.alerts.forEach(al => { if (al.ts_start != null) pts.push(al.ts_start); if (al.ts_end != null) pts.push(al.ts_end); });
        if (pts.length === 0) {
            document.getElementById('timeline').innerHTML = '<div style="padding:1rem;color:#999;font-size:0.8rem;">无时间数据</div>';
            return;
        }
        const minT = Math.min(...pts);
        const maxT = Math.max(...pts);
        const span = Math.max(maxT - minT, 1);
        const pad = span * 0.05;

        const tl = document.getElementById('timeline');
        let html = '';
        // GT 区间
        data.gt_events.forEach(g => {
            const left = ((g.start_sec - minT + pad) / (span + 2 * pad)) * 100;
            const width = Math.max(((g.end_sec - g.start_sec) / (span + 2 * pad)) * 100, 0.5);
            html += `<div class="gt-bar" style="left:${left}%;width:${width}%;" title="GT ${g.event_type} ${g.start_sec}~${g.end_sec}s"></div>`;
        });
        // 告警点
        data.alerts.forEach(al => {
            if (al.ts_start == null) return;
            const left = ((al.ts_start - minT + pad) / (span + 2 * pad)) * 100;
            const isCur = al.id === a.id;
            const cls = isCur ? 'pt-cur' : (al.effective_status === 'false_positive' ? 'pt-fp' : al.effective_status === 'ignored' ? 'pt-ignored' : 'pt-correct');
            html += `<div class="alert-pt ${cls}" style="left:${left}%;" title="${al.event_type} ${al.ts_start}s"></div>`;
        });
        tl.innerHTML = html;
    }

    // ── 智能审查（批量异步） ────────────────────────────────────────────────────
    // 选择控制
    window.toggleSelect = function (id, checked) {
        if (checked) selectedIds.add(id); else selectedIds.delete(id);
        updateSelectedCount();
    };
    window.toggleSelectAll = function (checked) {
        if (checked) filteredAlerts.forEach(a => selectedIds.add(a.id));
        else filteredAlerts.forEach(a => selectedIds.delete(a.id));
        renderList();
    };
    function updateSelectedCount() {
        const el = document.getElementById('selected-count');
        if (el) el.textContent = selectedIds.size ? `（已选 ${selectedIds.size}）` : '';
    }

    // 批量进度浮层
    let _aiTimer = null;
    let _aiStart = 0;

    function showAiProgress(total) {
        const ov = document.getElementById('ai-progress-overlay');
        ov.classList.remove('hidden', 'ai-done', 'ai-fail');
        document.getElementById('ai-progress-title').textContent = `正在批量审查 ${total} 张告警…`;
        document.getElementById('ai-progress-fill').style.width = '0%';
        document.getElementById('ai-progress-elapsed').textContent = `0 / ${total}`;
        document.querySelector('.ai-progress-hint').textContent = '后台运行中，可继续操作页面，完成后自动刷新结果';
        _aiStart = Date.now();
    }

    function updateAiProgress(done, total) {
        const pct = total > 0 ? Math.round((done / total) * 100) : 0;
        document.getElementById('ai-progress-fill').style.width = pct + '%';
        const elapsed = Math.floor((Date.now() - _aiStart) / 1000);
        document.getElementById('ai-progress-elapsed').textContent = `${done} / ${total}（${elapsed}s）`;
    }

    function hideAiProgress(success, msg) {
        if (_aiTimer) { clearInterval(_aiTimer); _aiTimer = null; }
        const ov = document.getElementById('ai-progress-overlay');
        const title = document.getElementById('ai-progress-title');
        const fill = document.getElementById('ai-progress-fill');
        if (success) {
            fill.style.width = '100%';
            ov.classList.add('ai-done');
            title.textContent = msg || '审查完成';
        } else {
            ov.classList.add('ai-fail');
            title.textContent = msg || '审查失败';
        }
        setTimeout(() => ov.classList.add('hidden'), 1200);
    }

    // 把后端返回的结果合并进 allAlerts 并刷新当前选中项
    function applyBatchResults(results) {
        const map = {};
        results.forEach(r => { map[r.merged_id] = r.suggestion; });
        allAlerts.forEach(a => {
            if (map[a.id]) a.ai_suggestion = map[a.id];
        });
        renderList();
        const cur = currentAlert();
        if (cur && map[cur.id]) renderAiSuggestion(cur);
    }

    // 提交批量审查并轮询
    async function runBatchCheck(ids) {
        if (!ids.length) { alert('请先选择告警'); return; }
        setBatchButtonsDisabled(true);
        showAiProgress(ids.length);
        try {
            const resp = await fetch(`/review/api/${TASK_ID}/ai-check`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ merged_ids: ids }),
            });
            const data = await resp.json();
            if (!data.success) {
                hideAiProgress(false, '提交失败：' + (data.error || ''));
                setBatchButtonsDisabled(false);
                return;
            }
            const batchId = data.batch_id;
            const total = data.total;
            // 轮询进度
            _aiTimer = setInterval(async () => {
                try {
                    const sr = await fetch(`/review/api/${TASK_ID}/ai-check/status?batch_id=${batchId}`);
                    const sd = await sr.json();
                    if (!sd.success) return;
                    updateAiProgress(sd.done, total);
                    // 实时刷新已完成的结果
                    if (sd.results && sd.results.length) applyBatchResults(sd.results);
                    if (sd.status === 'done' || sd.status === 'error') {
                        if (sd.status === 'error') {
                            hideAiProgress(false, '审查出错：' + (sd.error || ''));
                        } else {
                            hideAiProgress(true, `完成：已审查 ${sd.done} / ${total} 张`);
                        }
                        setBatchButtonsDisabled(false);
                        // 清空选中
                        selectedIds.clear();
                        renderList();
                    }
                } catch (e) { /* 忽略单次轮询失败 */ }
            }, 1500);
        } catch (e) {
            hideAiProgress(false, '请求失败：' + e);
            setBatchButtonsDisabled(false);
        }
    }

    function setBatchButtonsDisabled(disabled) {
        ['btn-ai-check', 'btn-ai-batch', 'btn-ai-batch-all'].forEach(id => {
            const b = document.getElementById(id);
            if (b) b.disabled = disabled;
        });
    }

    window.aiCheckCurrent = function () {
        const a = currentAlert();
        if (!a) return;
        runBatchCheck([a.id]);
    };
    window.aiCheckBatch = function () {
        if (!selectedIds.size) { alert('请先勾选要审查的告警（左侧列表复选框）'); return; }
        runBatchCheck([...selectedIds]);
    };
    window.aiCheckFiltered = function () {
        if (!filteredAlerts.length) { alert('当前筛选结果为空'); return; }
        if (!confirm(`将审查当前筛选出的 ${filteredAlerts.length} 张告警，可能耗时较长，后台运行中可继续操作。确认开始？`)) return;
        runBatchCheck(filteredAlerts.map(a => a.id));
    };

    // 按AI建议批量改判选中告警
    window.applyAiToSelected = async function () {
        if (!selectedIds.size) { alert('请先勾选要改判的告警（左侧列表复选框）'); return; }
        // 收集选中的、有有效AI建议的告警，按 verdict 分组
        const byVerdict = { correct: [], false_positive: [] };
        let noSuggestion = 0;
        allAlerts.forEach(a => {
            if (!selectedIds.has(a.id)) return;
            const s = a.ai_suggestion;
            if (s && (s.verdict === 'correct' || s.verdict === 'false_positive')) {
                byVerdict[s.verdict].push(a.id);
            } else {
                noSuggestion++;
            }
        });
        const total = byVerdict.correct.length + byVerdict.false_positive.length;
        if (total === 0) { alert('选中的告警都没有有效的AI审查结果，请先审查'); return; }
        const skipMsg = noSuggestion ? `\n（另有 ${noSuggestion} 张未审查或审查异常，将跳过）` : '';
        if (!confirm(`将把 ${total} 张选中告警的人工状态改为AI建议结果：\n  · ${byVerdict.correct.length} 张改为"正确"\n  · ${byVerdict.false_positive.length} 张改为"误检"${skipMsg}\n\n确认改判？`)) return;

        // 分组调用批量改判接口
        let okCount = 0;
        for (const [verdict, ids] of Object.entries(byVerdict)) {
            if (!ids.length) continue;
            try {
                const resp = await fetch(`/evaluation/api/tasks/${TASK_ID}/merged-events/batch-status`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ merged_ids: ids, manual_status: verdict }),
                });
                const data = await resp.json();
                if (data.success) {
                    okCount += data.updated_count || ids.length;
                    // 更新本地状态
                    ids.forEach(id => {
                        const a = allAlerts.find(x => x.id === id);
                        if (a) {
                            a.manual_status = verdict;
                            a.effective_status = verdict;
                        }
                    });
                }
            } catch (e) { /* 忽略单组失败 */ }
        }
        renderList();
        const cur = currentAlert();
        if (cur) renderCenter();
        selectedIds.clear();
        renderList();
        alert(`改判完成：${okCount} 张已按AI建议更新`);
    };

    // ── 大图 lightbox ──────────────────────────────────────────────────────────
    window.openLightbox = function () {
        const src = document.getElementById('big-image').src;
        if (!src) return;
        document.getElementById('lightbox-img').src = src;
        document.getElementById('lightbox').classList.remove('hidden');
    };
    window.closeLightbox = function () {
        document.getElementById('lightbox').classList.add('hidden');
    };

    // ── 键盘快捷键 ────────────────────────────────────────────────────────────
    document.addEventListener('keydown', function (e) {
        // 输入框聚焦时不触发
        const tag = e.target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        if (!document.getElementById('lightbox').classList.contains('hidden')) {
            if (e.key === 'Escape') closeLightbox();
            return;
        }
        switch (e.key) {
            case 'ArrowLeft': e.preventDefault(); prevAlert(); break;
            case 'ArrowRight': e.preventDefault(); nextAlert(); break;
            case 'c': case 'C': e.preventDefault(); setStatus('correct'); break;
            case 'f': case 'F': e.preventDefault(); setStatus('false_positive'); break;
            case 'i': case 'I': e.preventDefault(); setStatus('ignored'); break;
            case 'a': case 'A': e.preventDefault(); setStatus('auto'); break;
            case ' ': e.preventDefault(); aiCheckCurrent(); break;
        }
    });

    // ── 工具函数 ──────────────────────────────────────────────────────────────
    function imgUrl(imageId) {
        return imageId ? `/alerts/api/images/${imageId}/file` : '';
    }
    function thumbUrl(imageId, width) {
        const url = imgUrl(imageId);
        return url ? `${url}?w=${width}` : '';
    }
    function fmtTs(s, e) {
        if (s == null) return '-';
        if (e != null && e !== s) return `${s}s ~ ${e}s`;
        return `${s}s`;
    }

    init();
})();

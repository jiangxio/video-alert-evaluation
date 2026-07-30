(function() {
    'use strict';

    const fab = document.getElementById('assistant-fab');
    const panel = document.getElementById('assistant-panel');
    const closeBtn = document.getElementById('assistant-close');
    const clearBtn = document.getElementById('assistant-clear');
    const tasksToggleBtn = document.getElementById('assistant-tasks-toggle');
    const tasksPanel = document.getElementById('assistant-tasks-panel');
    const tasksRefreshBtn = document.getElementById('assistant-tasks-refresh');
    const tasksListEl = document.getElementById('assistant-tasks-list');
    const messagesEl = document.getElementById('assistant-messages');
    const inputEl = document.getElementById('assistant-input');
    const sendBtn = document.getElementById('assistant-send');

    let isOpen = false;
    let isLoading = false;
    let tasksPollInterval = null;

    function togglePanel() {
        isOpen = !isOpen;
        panel.classList.toggle('open', isOpen);
        if (isOpen) {
            inputEl.focus();
            loadTasks();
        } else {
            if (tasksPollInterval) {
                clearInterval(tasksPollInterval);
                tasksPollInterval = null;
            }
        }
    }

    function scrollToBottom() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function appendMessage(role, content, type) {
        const div = document.createElement('div');
        div.className = `assistant-message ${role}`;
        if (type === 'error') div.classList.add('error');
        div.innerHTML = `<div class="assistant-bubble">${escapeHtml(content)}</div>`;
        messagesEl.appendChild(div);
        scrollToBottom();
        return div;
    }

    function appendTyping() {
        const div = document.createElement('div');
        div.className = 'assistant-message assistant';
        div.id = 'assistant-typing';
        div.innerHTML = `
            <div class="assistant-bubble assistant-typing">
                <span></span><span></span><span></span>
            </div>`;
        messagesEl.appendChild(div);
        scrollToBottom();
    }

    function removeTyping() {
        const el = document.getElementById('assistant-typing');
        if (el) el.remove();
    }

    function appendToolResult(role, toolCalls) {
        if (!toolCalls || toolCalls.length === 0) return;
        const div = document.createElement('div');
        div.className = `assistant-message ${role}`;
        let html = '';
        toolCalls.forEach((tc, idx) => {
            const resultId = `tool-result-${Date.now()}-${idx}`;
            html += `
                <div class="assistant-tool-result">
                    <button class="assistant-tool-toggle" data-target="${resultId}">
                        ↓ 查看 ${escapeHtml(tc.name)} 原始结果
                    </button>
                    <div id="${resultId}" class="assistant-tool-details">
                        <pre>${escapeHtml(JSON.stringify(tc.result, null, 2))}</pre>
                    </div>
                </div>
            `;
        });
        div.innerHTML = html;
        messagesEl.appendChild(div);

        div.querySelectorAll('.assistant-tool-toggle').forEach(btn => {
            btn.addEventListener('click', function() {
                const target = document.getElementById(this.dataset.target);
                target.classList.toggle('open');
                this.textContent = target.classList.contains('open')
                    ? this.textContent.replace('↓', '↑')
                    : this.textContent.replace('↑', '↓');
            });
        });
        scrollToBottom();
    }

    function appendConfirmation(confirmation) {
        const div = document.createElement('div');
        div.className = 'assistant-message assistant';
        let affectedHtml = '';
        if (confirmation.affected && confirmation.affected.length > 0) {
            affectedHtml = '<ul>' + confirmation.affected.map(a =>
                `<li>${escapeHtml(a.name || String(a.id))}</li>`
            ).join('') + '</ul>';
        }
        div.innerHTML = `
            <div class="assistant-confirmation" data-confirmation-id="${escapeHtml(confirmation.id)}">
                <h4>⚠️ 请确认操作</h4>
                <p>${escapeHtml(confirmation.summary)}</p>
                <p>影响数量：${confirmation.affected_count}</p>
                ${affectedHtml}
                <div class="assistant-confirmation-actions">
                    <button class="assistant-confirm-btn">确认执行</button>
                    <button class="assistant-cancel-btn">取消</button>
                </div>
            </div>
        `;
        messagesEl.appendChild(div);

        div.querySelector('.assistant-confirm-btn').addEventListener('click', function() {
            handleConfirm(confirmation.id);
        });
        div.querySelector('.assistant-cancel-btn').addEventListener('click', function() {
            handleCancel(confirmation.id, div);
        });
        scrollToBottom();
    }

    function escapeHtml(text) {
        if (text === null || text === undefined) return '';
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    async function loadHistory() {
        try {
            const resp = await fetch('/assistant/api/history');
            const data = await resp.json();
            const messages = data.messages || [];
            if (messages.length === 0) return;
            messagesEl.innerHTML = '';
            messages.forEach(msg => {
                if (msg.role === 'user') {
                    appendMessage('user', msg.content);
                } else if (msg.role === 'assistant') {
                    if (msg.content && msg.content.trim()) {
                        appendMessage('assistant', msg.content);
                    }
                    if (msg.tool_calls) {
                        appendToolResult('assistant', msg.tool_calls);
                    }
                }
            });
            scrollToBottom();
        } catch (err) {
            // 历史加载失败时静默，保留欢迎语
        }
    }

    function setLoading(loading) {
        isLoading = loading;
        sendBtn.disabled = loading;
        inputEl.disabled = loading;
        if (loading) {
            appendTyping();
        } else {
            removeTyping();
        }
    }

    async function sendMessage() {
        const text = inputEl.value.trim();
        if (!text || isLoading) return;

        appendMessage('user', text);
        inputEl.value = '';
        setLoading(true);

        try {
            const resp = await fetch('/assistant/api/chat', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({message: text}),
            });
            const data = await resp.json();
            handleResponse(data);
        } catch (err) {
            appendMessage('assistant', '请求失败：' + err.message, 'error');
        } finally {
            setLoading(false);
        }
    }

    function handleResponse(data) {
        if (data.type === 'error') {
            appendMessage('assistant', data.message.content, 'error');
            return;
        }
        if (data.type === 'confirmation_required') {
            appendMessage('assistant', data.message.content);
            appendConfirmation(data.confirmation);
            return;
        }
        if (data.message) {
            appendMessage('assistant', data.message.content);
        }
        if (data.message && data.message.tool_calls) {
            appendToolResult('assistant', data.message.tool_calls);
        }
    }

    async function handleConfirm(confirmationId) {
        setLoading(true);
        try {
            const resp = await fetch('/assistant/api/confirm', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({confirmation_id: confirmationId}),
            });
            const data = await resp.json();
            handleResponse(data);
        } catch (err) {
            appendMessage('assistant', '确认失败：' + err.message, 'error');
        } finally {
            setLoading(false);
        }
    }

    async function handleCancel(confirmationId, el) {
        try {
            await fetch('/assistant/api/cancel', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({confirmation_id: confirmationId}),
            });
            if (el) el.remove();
            appendMessage('assistant', '已取消操作。');
        } catch (err) {
            appendMessage('assistant', '取消失败：' + err.message, 'error');
        }
    }

    async function loadTasks() {
        try {
            const resp = await fetch('/assistant/api/tasks');
            const data = await resp.json();
            renderTasks(data.tasks || []);
            startTasksPolling();
        } catch (err) {
            tasksListEl.innerHTML = '<div class="assistant-task-empty">加载失败：' + escapeHtml(err.message) + '</div>';
        }
    }

    function renderTasks(tasks) {
        if (!tasks || tasks.length === 0) {
            tasksListEl.innerHTML = '<div class="assistant-task-empty">暂无任务</div>';
            return;
        }

        const statusMap = {
            'pending': '等待中',
            'running': '运行中',
            'done': '已完成',
            'failed': '失败'
        };

        tasksListEl.innerHTML = tasks.map(t => {
            const progress = t.progress || {};
            const pct = progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0;
            const summary = t.result_summary || t.error_message || '';
            return `
                <div class="assistant-task-item">
                    <div>
                        <span class="assistant-task-type">#${t.id} ${escapeHtml(t.task_type)}</span>
                        <span class="assistant-task-status ${t.status}">${statusMap[t.status] || t.status}</span>
                    </div>
                    <div class="assistant-task-progress">
                        <div class="assistant-task-progress-bar" style="width: ${pct}%"></div>
                    </div>
                    ${summary ? '<div class="assistant-task-summary">' + escapeHtml(summary) + '</div>' : ''}
                </div>
            `;
        }).join('');
    }

    function startTasksPolling() {
        if (tasksPollInterval) clearInterval(tasksPollInterval);
        tasksPollInterval = setInterval(async () => {
            if (!isOpen) return;
            try {
                const resp = await fetch('/assistant/api/tasks');
                const data = await resp.json();
                renderTasks(data.tasks || []);
                const hasRunning = (data.tasks || []).some(t => t.status === 'pending' || t.status === 'running');
                if (!hasRunning) {
                    clearInterval(tasksPollInterval);
                    tasksPollInterval = null;
                }
            } catch (err) {
                // ignore polling errors
            }
        }, 3000);
    }

    function toggleTasksPanel() {
        tasksPanel.classList.toggle('open');
        if (tasksPanel.classList.contains('open')) {
            loadTasks();
        }
    }

    async function clearConversation() {
        try {
            await fetch('/assistant/api/clear', { method: 'POST' });
            messagesEl.innerHTML = `
                <div class="assistant-message assistant">
                    <div class="assistant-bubble">对话已清除。这里是 AI 助手，支持查询视频/告警/事件类型/评测结果、视频打标签/删除/水印、批量 OCR、修改复核状态、启动评测、导出报告等操作，请直接描述您的需求。</div>
                </div>
            `;
        } catch (err) {
            appendMessage('assistant', '清除失败：' + err.message, 'error');
        }
    }

    if (fab) fab.addEventListener('click', togglePanel);
    if (closeBtn) closeBtn.addEventListener('click', togglePanel);
    if (clearBtn) clearBtn.addEventListener('click', clearConversation);
    if (tasksToggleBtn) tasksToggleBtn.addEventListener('click', toggleTasksPanel);
    if (tasksRefreshBtn) tasksRefreshBtn.addEventListener('click', loadTasks);
    if (sendBtn) sendBtn.addEventListener('click', sendMessage);
    if (inputEl) {
        inputEl.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });
    }

    loadHistory();
})();

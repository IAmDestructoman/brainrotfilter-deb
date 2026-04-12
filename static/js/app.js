/**
 * BrainrotFilter Admin Panel — app.js
 * General utilities: API helpers, table management, modals, toasts, forms
 */

'use strict';

/* ── API Helpers ───────────────────────────────────────────────── */

const BASE = '';  // Same-origin API

async function fetchJSON(url, options = {}) {
  const res = await fetch(BASE + url, {
    headers: { 'Accept': 'application/json', ...options.headers },
    ...options,
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const body = await res.json(); msg = body.detail || body.message || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

async function postJSON(url, data) {
  return fetchJSON(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

async function putJSON(url, data) {
  return fetchJSON(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

async function deleteJSON(url) {
  return fetchJSON(url, { method: 'DELETE' });
}

/* ── Toast System ──────────────────────────────────────────────── */

const toastContainer = (() => {
  let el = document.getElementById('toast-container');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast-container';
    el.className = 'toast-container';
    document.body.appendChild(el);
  }
  return el;
})();

function showToast(message, type = 'info', duration = 4000) {
  const icons = { success: '✓', error: '✕', warning: '⚠', info: 'ℹ' };
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span class="toast-icon">${icons[type] || 'ℹ'}</span><span class="toast-msg">${escapeHtml(message)}</span>`;
  toastContainer.appendChild(toast);

  setTimeout(() => {
    toast.classList.add('removing');
    toast.addEventListener('animationend', () => toast.remove());
  }, duration);
}

/* ── Modal System ──────────────────────────────────────────────── */

class Modal {
  constructor(id) {
    this.overlay = document.getElementById(id);
    if (!this.overlay) return;
    this.overlay.addEventListener('click', (e) => {
      if (e.target === this.overlay) this.close();
    });
    const closeBtn = this.overlay.querySelector('.modal-close');
    if (closeBtn) closeBtn.addEventListener('click', () => this.close());
  }

  open() { this.overlay?.classList.add('open'); }
  close() { this.overlay?.classList.remove('open'); }
}

function openModal(id) {
  document.getElementById(id)?.classList.add('open');
}

function closeModal(id) {
  document.getElementById(id)?.classList.remove('open');
}

/* ── Confirmation Dialog ───────────────────────────────────────── */

function confirmAction(message, onConfirm, dangerous = true) {
  const existing = document.getElementById('confirm-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'confirm-modal';
  modal.className = 'modal-overlay open';
  modal.innerHTML = `
    <div class="modal" style="max-width:380px">
      <div class="modal-header">
        <span class="modal-title">${dangerous ? '⚠ Confirm Action' : 'Confirm'}</span>
        <button class="modal-close" onclick="document.getElementById('confirm-modal').remove()">×</button>
      </div>
      <div class="modal-body">
        <p style="color:var(--text-muted);font-size:0.9rem;line-height:1.6">${escapeHtml(message)}</p>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" onclick="document.getElementById('confirm-modal').remove()">Cancel</button>
        <button class="btn ${dangerous ? 'btn-danger' : 'btn-primary'}" id="confirm-ok">Confirm</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  document.getElementById('confirm-ok').addEventListener('click', () => {
    modal.remove();
    onConfirm();
  });
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove(); });
}

/* ── Dropdown Menus ────────────────────────────────────────────── */

document.addEventListener('click', (e) => {
  // Close all dropdowns when clicking outside
  if (!e.target.closest('.dropdown')) {
    document.querySelectorAll('.dropdown-menu.open').forEach(m => m.classList.remove('open'));
  }
});

function toggleDropdown(btn) {
  const menu = btn.nextElementSibling;
  const isOpen = menu.classList.contains('open');
  document.querySelectorAll('.dropdown-menu.open').forEach(m => m.classList.remove('open'));
  if (!isOpen) menu.classList.add('open');
}

/* ── Table Sorting ─────────────────────────────────────────────── */

class TableSorter {
  constructor(tableEl, data, renderFn) {
    this.table = tableEl;
    this.data = [...data];
    this.sorted = [...data];
    this.render = renderFn;
    this.sortKey = null;
    this.sortDir = 1;
    this._bindHeaders();
  }

  _bindHeaders() {
    this.table.querySelectorAll('thead th.sortable').forEach(th => {
      th.addEventListener('click', () => this.sortBy(th.dataset.key));
    });
  }

  sortBy(key) {
    if (this.sortKey === key) {
      this.sortDir *= -1;
    } else {
      this.sortKey = key;
      this.sortDir = 1;
    }

    this.sorted = [...this.data].sort((a, b) => {
      let av = a[key], bv = b[key];
      if (av == null) av = '';
      if (bv == null) bv = '';
      if (typeof av === 'string') return av.localeCompare(bv) * this.sortDir;
      return (av - bv) * this.sortDir;
    });

    // Update header indicators
    this.table.querySelectorAll('thead th').forEach(th => {
      th.classList.remove('sorted-asc', 'sorted-desc');
      if (th.dataset.key === key) {
        th.classList.add(this.sortDir === 1 ? 'sorted-asc' : 'sorted-desc');
      }
    });

    this.render(this.sorted);
  }

  updateData(data) {
    this.data = [...data];
    this.sorted = this.sortKey
      ? [...data].sort((a, b) => {
          let av = a[this.sortKey] ?? '', bv = b[this.sortKey] ?? '';
          if (typeof av === 'string') return av.localeCompare(bv) * this.sortDir;
          return (av - bv) * this.sortDir;
        })
      : [...data];
    this.render(this.sorted);
  }
}

/* ── Pagination ────────────────────────────────────────────────── */

class Paginator {
  constructor({ container, pageSize = 25, onPage }) {
    this.container = container;
    this.pageSize = pageSize;
    this.onPage = onPage;
    this.total = 0;
    this.current = 1;
  }

  setTotal(total) {
    this.total = total;
    this.render();
  }

  get pageCount() { return Math.max(1, Math.ceil(this.total / this.pageSize)); }

  goTo(page) {
    this.current = Math.max(1, Math.min(page, this.pageCount));
    this.onPage(this.current, this.pageSize);
    this.render();
  }

  render() {
    if (!this.container) return;
    const pc = this.pageCount;
    const c = this.current;

    let pages = [];
    if (pc <= 7) {
      for (let i = 1; i <= pc; i++) pages.push(i);
    } else {
      pages = [1];
      if (c > 3) pages.push('…');
      for (let i = Math.max(2, c - 1); i <= Math.min(pc - 1, c + 1); i++) pages.push(i);
      if (c < pc - 2) pages.push('…');
      pages.push(pc);
    }

    const start = (c - 1) * this.pageSize + 1;
    const end = Math.min(c * this.pageSize, this.total);

    this.container.innerHTML = `
      <span class="pagination-info">${start}–${end} of ${this.total}</span>
      <button class="page-btn" ${c === 1 ? 'disabled' : ''} data-page="${c - 1}">‹</button>
      ${pages.map(p => p === '…'
        ? `<span style="color:var(--text-dim);padding:0 4px">…</span>`
        : `<button class="page-btn ${p === c ? 'active' : ''}" data-page="${p}">${p}</button>`
      ).join('')}
      <button class="page-btn" ${c === pc ? 'disabled' : ''} data-page="${c + 1}">›</button>
    `;

    this.container.querySelectorAll('.page-btn[data-page]').forEach(btn => {
      btn.addEventListener('click', () => this.goTo(parseInt(btn.dataset.page)));
    });
  }
}

/* ── Expandable Rows ───────────────────────────────────────────── */

function makeExpandable(table, contentFn) {
  table.addEventListener('click', (e) => {
    const row = e.target.closest('tr.expand-row');
    if (!row || e.target.closest('button, a, .dropdown')) return;

    const nextRow = row.nextElementSibling;
    const isExpand = nextRow?.classList.contains('expand-content');

    // Collapse all
    table.querySelectorAll('tr.expand-row.open').forEach(r => {
      r.classList.remove('open');
      const nr = r.nextElementSibling;
      if (nr?.classList.contains('expand-content')) nr.classList.remove('open');
    });

    if (isExpand && !row.classList.contains('open')) {
      row.classList.add('open');
      nextRow.classList.add('open');
    }
  });
}

/* ── Slider Value Display ──────────────────────────────────────── */

function initSliders(container = document) {
  container.querySelectorAll('input[type="range"]').forEach(slider => {
    const display = document.getElementById(slider.id + '-val') ||
                    slider.parentElement?.querySelector('.slider-value');
    if (display) {
      display.textContent = slider.value;
      slider.addEventListener('input', () => { display.textContent = slider.value; });
    }
  });
}

/* ── Weight Sum Validator ──────────────────────────────────────── */

function initWeightValidator(ids, sumDisplayId) {
  const display = document.getElementById(sumDisplayId);
  const inputs = ids.map(id => document.getElementById(id)).filter(Boolean);

  function update() {
    const sum = inputs.reduce((acc, el) => acc + parseFloat(el.value || 0), 0);
    const rounded = Math.round(sum * 100) / 100;
    if (display) {
      display.textContent = rounded.toFixed(2);
      display.className = 'weight-sum-value ' + (Math.abs(rounded - 1.0) < 0.001 ? 'valid' : 'invalid');
    }
  }

  inputs.forEach(el => el.addEventListener('input', update));
  update();
}

/* ── Export to CSV ─────────────────────────────────────────────── */

function exportTableToCSV(tableId, filename) {
  const table = document.getElementById(tableId);
  if (!table) return;

  const rows = [];
  table.querySelectorAll('tr').forEach(row => {
    const cells = [];
    row.querySelectorAll('th, td').forEach(cell => {
      const text = cell.innerText.replace(/[\n\r]/g, ' ').replace(/"/g, '""');
      cells.push(`"${text}"`);
    });
    rows.push(cells.join(','));
  });

  const csv = rows.join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || 'export.csv';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  showToast('CSV exported successfully', 'success');
}

function exportDataToCSV(data, columns, filename) {
  const header = columns.map(c => `"${c.label}"`).join(',');
  const rows = data.map(row =>
    columns.map(c => {
      const val = c.key.split('.').reduce((o, k) => o?.[k], row) ?? '';
      return `"${String(val).replace(/"/g, '""')}"`;
    }).join(',')
  );
  const csv = [header, ...rows].join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || 'export.csv';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  showToast('CSV exported', 'success');
}

/* ── Sidebar Toggle ────────────────────────────────────────────── */

function initSidebar() {
  const toggle = document.getElementById('sidebar-toggle');
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');

  if (!toggle || !sidebar) return;

  toggle.addEventListener('click', () => {
    sidebar.classList.toggle('open');
    backdrop?.classList.toggle('open');
  });

  backdrop?.addEventListener('click', () => {
    sidebar.classList.remove('open');
    backdrop.classList.remove('open');
  });
}

/* ── Status Polling ────────────────────────────────────────────── */

async function updateServiceStatus() {
  const dot = document.getElementById('status-dot');
  const label = document.getElementById('status-label');
  try {
    const data = await fetchJSON('/api/stats');
    if (dot)   { dot.className = 'status-dot'; }
    if (label) { label.textContent = 'Service Online'; }
  } catch {
    if (dot)   { dot.className = 'status-dot offline'; }
    if (label) { label.textContent = 'Service Offline'; }
  }
}

/* ── Score Utilities ───────────────────────────────────────────── */

function scoreClass(score) {
  if (score < 30) return 'allow';
  if (score < 50) return 'monitor';
  if (score < 70) return 'soft-block';
  return 'block';
}

function statusBadge(status) {
  const labels = {
    allow: 'Allow',
    monitor: 'Monitor',
    soft_block: 'Soft Block',
    block: 'Block',
  };
  const cssClass = status === 'soft_block' ? 'soft-block' : status;
  return `<span class="badge badge-${cssClass}">${labels[status] || status}</span>`;
}

function scoreColorClass(score) {
  if (score < 30) return 'score-allow';
  if (score < 50) return 'score-monitor';
  if (score < 70) return 'score-soft-block';
  return 'score-block';
}

function scoreBar(score) {
  const cls = scoreClass(score);
  return `
    <div class="score-bar">
      <span class="score ${scoreColorClass(score)}">${score}</span>
      <div class="score-bar-track">
        <div class="score-bar-fill ${cls}" style="width:${score}%"></div>
      </div>
    </div>`;
}

function scoreMeter(label, score) {
  const cls = scoreClass(score);
  return `
    <div class="score-meter">
      <div class="score-meter-label"><span>${label}</span><span class="score ${scoreColorClass(score)}">${score}</span></div>
      <div class="score-meter-track">
        <div class="score-meter-fill ${cls}" style="width:${score}%"></div>
      </div>
    </div>`;
}

/* ── HTML Escape ───────────────────────────────────────────────── */

function escapeHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/* ── Relative Time ─────────────────────────────────────────────── */

function relativeTime(dateStr) {
  if (!dateStr) return '—';
  const date = new Date(dateStr);
  const diff = (Date.now() - date) / 1000;
  if (diff < 60)   return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  return `${Math.floor(diff/86400)}d ago`;
}

function formatDate(dateStr) {
  if (!dateStr) return '—';
  return new Date(dateStr).toLocaleString();
}

/* ── Loading helpers ───────────────────────────────────────────── */

function showLoading(container, msg = 'Loading…') {
  container.innerHTML = `<div class="loading-overlay"><div class="spinner lg"></div><span>${msg}</span></div>`;
}

function showError(container, msg = 'Failed to load data') {
  container.innerHTML = `
    <div class="empty-state">
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
      <h3>Error</h3>
      <p>${escapeHtml(msg)}</p>
    </div>`;
}

/* ── Tab System ────────────────────────────────────────────────── */

function initTabs(container) {
  const buttons = container.querySelectorAll('.tab-btn');
  const panels  = container.querySelectorAll('.tab-panel');

  buttons.forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.tab;
      buttons.forEach(b => b.classList.remove('active'));
      panels.forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      const panel = container.querySelector(`.tab-panel[data-tab="${target}"]`);
      if (panel) panel.classList.add('active');
    });
  });
}

/* ── Override / Whitelist Actions ──────────────────────────────── */

async function overrideVideo(videoId, action) {
  try {
    await postJSON('/api/override', { video_id: videoId, action });
    showToast(`Video set to: ${action}`, 'success');
    return true;
  } catch (e) {
    showToast(`Override failed: ${e.message}`, 'error');
    return false;
  }
}

async function whitelistItem(type, id, reason) {
  try {
    await postJSON('/api/whitelist', { type, target_id: id, reason });
    showToast(`Added to whitelist`, 'success');
    return true;
  } catch (e) {
    showToast(`Whitelist failed: ${e.message}`, 'error');
    return false;
  }
}

async function removeWhitelist(id) {
  try {
    await deleteJSON(`/api/whitelist/${id}`);
    showToast('Removed from whitelist', 'success');
    return true;
  } catch (e) {
    showToast(`Remove failed: ${e.message}`, 'error');
    return false;
  }
}

/* ── Bulk Actions ──────────────────────────────────────────────── */

class BulkSelector {
  constructor(tableId, bulkBarId, onAction) {
    this.table = document.getElementById(tableId);
    this.bar = document.getElementById(bulkBarId);
    this.selected = new Set();
    this.onAction = onAction;
    this._init();
  }

  _init() {
    if (!this.table) return;

    this.table.addEventListener('change', (e) => {
      if (e.target.type !== 'checkbox') return;
      if (e.target.dataset.all !== undefined) {
        this._selectAll(e.target.checked);
      } else {
        const id = e.target.dataset.id;
        if (e.target.checked) this.selected.add(id);
        else this.selected.delete(id);
        this._updateBar();
      }
    });

    if (this.bar) {
      this.bar.querySelectorAll('[data-bulk-action]').forEach(btn => {
        btn.addEventListener('click', () => this.onAction(this.selected, btn.dataset.bulkAction));
      });
    }
  }

  _selectAll(checked) {
    this.table.querySelectorAll('input[type="checkbox"][data-id]').forEach(cb => {
      cb.checked = checked;
      if (checked) this.selected.add(cb.dataset.id);
      else this.selected.delete(cb.dataset.id);
    });
    this._updateBar();
  }

  _updateBar() {
    if (!this.bar) return;
    const count = this.selected.size;
    this.bar.classList.toggle('visible', count > 0);
    const countEl = this.bar.querySelector('.bulk-count');
    if (countEl) countEl.textContent = `${count} item${count !== 1 ? 's' : ''} selected`;
  }

  clear() {
    this.selected.clear();
    this.table?.querySelectorAll('input[type="checkbox"]').forEach(cb => cb.checked = false);
    this._updateBar();
  }
}

/* ── Settings Form ─────────────────────────────────────────────── */

async function loadSettings() {
  try {
    const data = await fetchJSON('/api/settings');
    return data;
  } catch (e) {
    showToast(`Failed to load settings: ${e.message}`, 'error');
    return null;
  }
}

async function saveSettings(formData) {
  try {
    await putJSON('/api/settings', formData);
    showToast('Settings saved successfully', 'success');
    return true;
  } catch (e) {
    showToast(`Save failed: ${e.message}`, 'error');
    return false;
  }
}

function populateForm(formEl, data, prefix = '') {
  Object.entries(data).forEach(([key, val]) => {
    const fullKey = prefix ? `${prefix}_${key}` : key;
    const el = formEl.elements[fullKey] || document.getElementById(fullKey);
    if (!el) return;
    if (el.type === 'checkbox') el.checked = Boolean(val);
    else el.value = val ?? '';
  });
}

function collectForm(formEl) {
  const data = {};
  new FormData(formEl).forEach((val, key) => {
    // Try to cast numbers
    const num = parseFloat(val);
    data[key] = isNaN(num) ? val : num;
  });
  // Handle unchecked checkboxes
  formEl.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    if (!(cb.name in data)) data[cb.name] = false;
    else data[cb.name] = true;
  });
  return data;
}

/* ── Real-time Log Polling ─────────────────────────────────────── */

class LogPoller {
  constructor(renderFn, interval = 10000) {
    this.render = renderFn;
    this.interval = interval;
    this._timer = null;
    this._running = false;
    this.filters = {};
    this.page = 1;
    this.pageSize = 50;
  }

  start() {
    if (this._running) return;
    this._running = true;
    this._poll();
    this._timer = setInterval(() => this._poll(), this.interval);
  }

  stop() {
    this._running = false;
    clearInterval(this._timer);
  }

  setFilters(filters) {
    this.filters = filters;
    this.page = 1;
    this._poll();
  }

  async _poll() {
    const params = new URLSearchParams({
      page: this.page,
      limit: this.pageSize,
      ...this.filters,
    });
    try {
      const data = await fetchJSON(`/api/logs?${params}`);
      this.render(data);
    } catch (e) {
      console.error('Log poll failed:', e.message);
    }
  }
}

/* ── Number Formatting ─────────────────────────────────────────── */

function fmtNum(n) {
  if (n == null) return '—';
  if (n >= 1e6) return (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
  return String(n);
}

function fmtDuration(secs) {
  if (!secs) return '—';
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function fmtPct(val, decimals = 1) {
  return val != null ? `${parseFloat(val).toFixed(decimals)}%` : '—';
}

/* ── Thumbnail with fallback ───────────────────────────────────── */

function thumbEl(url, alt) {
  if (!url) {
    return `<div class="video-thumb" style="display:flex;align-items:center;justify-content:center;background:var(--bg-primary)">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--text-dim)" stroke-width="1.5"><rect x="2" y="4" width="20" height="16" rx="2"/><polygon points="10,8 16,12 10,16"/></svg>
    </div>`;
  }
  return `<img class="video-thumb" src="${escapeHtml(url)}" alt="${escapeHtml(alt || '')}" loading="lazy" onerror="this.style.display='none'">`;
}

/* ── Init on DOMContentLoaded ──────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  initSidebar();
  initSliders();
  updateServiceStatus();
  setInterval(updateServiceStatus, 30000);
});

/* ── Global exports ────────────────────────────────────────────── */

window.BF = {
  fetchJSON, postJSON, putJSON, deleteJSON,
  showToast, Modal, openModal, closeModal, confirmAction,
  TableSorter, Paginator, BulkSelector, LogPoller,
  toggleDropdown, makeExpandable, initSliders, initTabs,
  initWeightValidator, exportTableToCSV, exportDataToCSV,
  scoreClass, statusBadge, scoreColorClass, scoreBar, scoreMeter,
  escapeHtml, relativeTime, formatDate, showLoading, showError,
  overrideVideo, whitelistItem, removeWhitelist,
  loadSettings, saveSettings, populateForm, collectForm,
  fmtNum, fmtDuration, fmtPct, thumbEl,
};

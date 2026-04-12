/**
 * BrainrotFilter Admin Panel — dashboard.js
 * Chart.js dashboard: score distribution, tier doughnut, time series, activity feed
 */

'use strict';

/* ── Chart defaults ────────────────────────────────────────────── */

Chart.defaults.color = '#8899b0';
Chart.defaults.borderColor = 'rgba(255,255,255,0.06)';
Chart.defaults.font.family = "'Inter','Segoe UI',system-ui,sans-serif";
Chart.defaults.font.size = 12;

const THEME = {
  accent:    '#00b4d8',
  danger:    '#e63946',
  warning:   '#f4a261',
  success:   '#2a9d8f',
  orange:    '#e76f51',
  muted:     '#8899b0',
  dim:       '#5c7091',
  grid:      'rgba(255,255,255,0.05)',
  card:      '#0f3460',
};

const TIER_COLORS = {
  allow:      THEME.success,
  monitor:    THEME.accent,
  soft_block: THEME.warning,
  block:      THEME.danger,
};

/* ── Store chart instances for cleanup/resize ──────────────────── */

const charts = {};

function destroyChart(key) {
  if (charts[key]) {
    charts[key].destroy();
    delete charts[key];
  }
}

/* ── Score Distribution Bar Chart ──────────────────────────────── */

function renderScoreDistribution(data) {
  const ctx = document.getElementById('chart-score-dist');
  if (!ctx) return;
  destroyChart('scoreDist');

  // data: { buckets: [{range:"0-10", count:5}, ...] }
  const buckets = data?.buckets || Array.from({length: 10}, (_, i) => ({
    range: `${i*10}-${i*10+9}`,
    count: 0,
  }));

  const labels = buckets.map(b => b.range);
  const counts = buckets.map(b => b.count);

  // Color bars by score range
  const bgColors = buckets.map(b => {
    const mid = parseInt(b.range.split('-')[0]) + 5;
    if (mid < 30) return THEME.success + 'cc';
    if (mid < 50) return THEME.accent  + 'cc';
    if (mid < 70) return THEME.warning + 'cc';
    return THEME.danger + 'cc';
  });

  charts.scoreDist = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Videos',
        data: counts,
        backgroundColor: bgColors,
        borderColor: bgColors.map(c => c.replace('cc', '')),
        borderWidth: 1,
        borderRadius: 4,
        borderSkipped: false,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: items => `Score ${items[0].label}`,
            label:  item => ` ${item.raw} video${item.raw !== 1 ? 's' : ''}`,
          },
        },
      },
      scales: {
        x: {
          grid: { color: THEME.grid },
          ticks: { color: THEME.muted },
        },
        y: {
          grid: { color: THEME.grid },
          ticks: { color: THEME.muted, precision: 0 },
          beginAtZero: true,
        },
      },
    },
  });
}

/* ── Tier Distribution Doughnut ────────────────────────────────── */

function renderTierDist(data) {
  const ctx = document.getElementById('chart-tier-dist');
  if (!ctx) return;
  destroyChart('tierDist');

  // data: { allow: N, monitor: N, soft_block: N, block: N }
  const tiers   = ['allow', 'monitor', 'soft_block', 'block'];
  const labels  = ['Allow', 'Monitor', 'Soft Block', 'Block'];
  const counts  = tiers.map(t => data?.[t] ?? 0);
  const colors  = tiers.map(t => TIER_COLORS[t]);

  charts.tierDist = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{
        data: counts,
        backgroundColor: colors.map(c => c + 'cc'),
        borderColor:     colors,
        borderWidth: 2,
        hoverOffset: 6,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '65%',
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            padding: 16,
            usePointStyle: true,
            pointStyleWidth: 10,
            color: THEME.muted,
            font: { size: 11 },
          },
        },
        tooltip: {
          callbacks: {
            label: item => ` ${item.label}: ${item.raw} videos`,
          },
        },
      },
    },
  });
}

/* ── Time Series Line Chart ─────────────────────────────────────── */

function renderTimeSeries(data) {
  const ctx = document.getElementById('chart-time-series');
  if (!ctx) return;
  destroyChart('timeSeries');

  // data: { days: [{date:"2024-01-01", total: N, blocked: N}, ...] }
  const days    = data?.days || [];
  const labels  = days.map(d => {
    const dt = new Date(d.date);
    return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  });
  const totals  = days.map(d => d.total   || 0);
  const blocked = days.map(d => d.blocked || 0);

  const makeGradient = (ctx2, color) => {
    const g = ctx2.createLinearGradient(0, 0, 0, 280);
    g.addColorStop(0, color + '50');
    g.addColorStop(1, color + '00');
    return g;
  };

  charts.timeSeries = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Total Analyzed',
          data: totals,
          borderColor: THEME.accent,
          backgroundColor: (ctx2) => makeGradient(ctx2.chart.ctx, THEME.accent),
          borderWidth: 2,
          pointRadius: 3,
          pointBackgroundColor: THEME.accent,
          tension: 0.35,
          fill: true,
        },
        {
          label: 'Blocked',
          data: blocked,
          borderColor: THEME.danger,
          backgroundColor: (ctx2) => makeGradient(ctx2.chart.ctx, THEME.danger),
          borderWidth: 2,
          pointRadius: 3,
          pointBackgroundColor: THEME.danger,
          tension: 0.35,
          fill: true,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          position: 'top',
          align: 'end',
          labels: {
            boxWidth: 12,
            usePointStyle: true,
            pointStyleWidth: 10,
            color: THEME.muted,
            font: { size: 11 },
          },
        },
        tooltip: {
          callbacks: {
            label: item => ` ${item.dataset.label}: ${item.raw}`,
          },
        },
      },
      scales: {
        x: {
          grid: { color: THEME.grid },
          ticks: {
            color: THEME.muted,
            maxTicksLimit: 10,
            maxRotation: 0,
          },
        },
        y: {
          grid: { color: THEME.grid },
          ticks: { color: THEME.muted, precision: 0 },
          beginAtZero: true,
        },
      },
    },
  });
}

/* ── Stats Cards ────────────────────────────────────────────────── */

function updateStatCards(stats) {
  const set = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = val != null ? BF.fmtNum(val) : '—';
  };

  set('stat-total-videos',    stats.total_videos);
  set('stat-blocked',         stats.total_blocked);
  set('stat-channels-flagged', stats.channels_flagged);
  set('stat-active-monitors', stats.active_monitors);
  set('stat-requests-today',  stats.requests_today);
}

/* ── Activity Feed ──────────────────────────────────────────────── */

function renderActivityFeed(videos) {
  const feed = document.getElementById('activity-feed');
  if (!feed) return;

  if (!videos?.length) {
    feed.innerHTML = `
      <div class="empty-state" style="padding:32px">
        <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M9 3H5a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-4"/><path d="M13 3h8v8"/><path d="M13 3l8 8"/></svg>
        <p>No flagged videos yet</p>
      </div>`;
    return;
  }

  feed.innerHTML = videos.map(v => {
    const scoreCls = BF.scoreColorClass(v.combined_score);
    return `
      <div class="activity-item">
        ${v.thumbnail_url
          ? `<img class="activity-thumb" src="${BF.escapeHtml(v.thumbnail_url)}" alt="" loading="lazy" onerror="this.style.display='none'">`
          : `<div class="activity-thumb-placeholder">▶</div>`}
        <div class="activity-info">
          <div class="activity-title" title="${BF.escapeHtml(v.title)}">${BF.escapeHtml(v.title || 'Unknown')}</div>
          <div class="activity-meta">${BF.escapeHtml(v.channel_name || v.channel_id || '—')} · ${BF.relativeTime(v.analyzed_at)}</div>
        </div>
        ${BF.statusBadge(v.status)}
        <span class="activity-score ${scoreCls}">${v.combined_score}</span>
      </div>`;
  }).join('');
}

/* ── Top Flagged Channels Table ─────────────────────────────────── */

function renderTopChannels(channels) {
  const tbody = document.getElementById('top-channels-tbody');
  if (!tbody) return;

  if (!channels?.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="text-center text-dim" style="padding:24px">No data</td></tr>`;
    return;
  }

  tbody.innerHTML = channels.slice(0, 10).map(ch => `
    <tr>
      <td style="font-weight:500">${BF.escapeHtml(ch.channel_name || ch.channel_id)}</td>
      <td class="text-mono">${ch.videos_flagged} / ${ch.videos_analyzed}</td>
      <td class="text-mono">${BF.fmtPct(ch.flagged_percentage)}</td>
      <td>${BF.statusBadge(ch.tier)}</td>
    </tr>
  `).join('');
}

/* ── Main Load & Refresh ────────────────────────────────────────── */

async function loadDashboard() {
  try {
    // Load stats
    const [stats, videosResp, channelsResp] = await Promise.allSettled([
      BF.fetchJSON('/api/stats'),
      BF.fetchJSON('/api/videos?limit=20&sort=recent&status=block,soft_block,monitor'),
      BF.fetchJSON('/api/channels?sort=flagged&limit=10'),
    ]);

    if (stats.status === 'fulfilled') {
      const s = stats.value;
      updateStatCards(s);
      renderScoreDistribution(s.score_distribution);
      renderTierDist(s.tier_distribution);
      renderTimeSeries(s.time_series);
    }

    if (videosResp.status === 'fulfilled') {
      const videos = videosResp.value?.items || videosResp.value || [];
      renderActivityFeed(videos);
    }

    if (channelsResp.status === 'fulfilled') {
      const channels = channelsResp.value?.items || channelsResp.value || [];
      renderTopChannels(channels);
    }

  } catch (e) {
    console.error('Dashboard load error:', e);
    BF.showToast('Failed to load dashboard data', 'error');
  }
}

/* ── Auto-refresh ───────────────────────────────────────────────── */

let refreshTimer = null;

function startAutoRefresh(intervalMs = 30000) {
  stopAutoRefresh();
  refreshTimer = setInterval(loadDashboard, intervalMs);
}

function stopAutoRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

/* ── Responsive chart resize ────────────────────────────────────── */

window.addEventListener('resize', () => {
  Object.values(charts).forEach(c => c.resize());
});

/* ── Init ───────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  loadDashboard();
  startAutoRefresh(30000);

  const refreshBtn = document.getElementById('btn-refresh-dashboard');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      refreshBtn.disabled = true;
      refreshBtn.innerHTML = '<div class="spinner"></div> Refreshing…';
      await loadDashboard();
      refreshBtn.disabled = false;
      refreshBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> Refresh`;
    });
  }
});

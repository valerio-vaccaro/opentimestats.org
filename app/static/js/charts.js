/* charts.js — loaded only on /charts */

let rateChart = null;
let calChart  = null;
let tlChart   = null;
let allCalTlChart = null;

const COLORS = {
  complete: 'rgba(40,167,69,0.85)',
  partial:  'rgba(255,193,7,0.85)',
  pending:  'rgba(108,117,125,0.7)',
};

// ── calendar summary boxes ─────────────────────────────────────────────────

function renderCalSummary(data) {
  const container = document.getElementById('cal-summary');
  container.innerHTML = '';
  data.forEach(d => {
    const total = d.confirmed_count + d.pending_count;
    const rate  = total > 0 ? ((d.confirmed_count / total) * 100).toFixed(1) : '0.0';
    const rateNum = parseFloat(rate);
    const badgeClass = rateNum >= 80 ? 'bg-success' : rateNum >= 40 ? 'bg-warning text-dark' : 'bg-danger';
    const avgStr = d.avg_delta !== null ? fmtDelta(d.avg_delta) : '—';

    const col = document.createElement('div');
    col.className = 'col-6 col-md-4 col-lg-3';
    col.innerHTML = `
      <div class="card border-0 shadow-sm h-100 text-center p-3">
        <div class="fw-semibold text-truncate mb-2" title="${d.calendar_url}">${d.calendar_name}</div>
        <div class="display-6 fw-bold mb-1">${rate}<small class="fs-6 fw-normal text-muted">%</small></div>
        <span class="badge ${badgeClass} mb-2">confirmation rate</span>
        <div class="fw-semibold mb-1">${avgStr}</div>
        <span class="badge bg-secondary mb-3">avg confirm time</span>
        <div class="row g-0 text-muted small border-top pt-2 mt-auto">
          <div class="col border-end">
            <div class="fw-semibold text-dark">${d.confirmed_count}</div>
            <div>confirmed</div>
          </div>
          <div class="col border-end">
            <div class="fw-semibold text-dark">${total}</div>
            <div>requests</div>
          </div>
          <div class="col">
            <div class="fw-semibold text-dark">${d.distinct_block_count}</div>
            <div>blocks</div>
          </div>
        </div>
      </div>`;
    container.appendChild(col);
  });
}

// ── confirmation rate chart ────────────────────────────────────────────────

function renderRateChart(data) {
  const noData = document.getElementById('no-rate-data');
  if (!data.length) {
    noData.classList.remove('d-none');
    document.getElementById('rateChart').classList.add('d-none');
    return;
  }
  noData.classList.add('d-none');
  document.getElementById('rateChart').classList.remove('d-none');

  const labels = data.map(d => d.calendar_name);
  const conf   = data.map(d => d.confirmed_count);
  const pend   = data.map(d => d.pending_count);
  const rates  = data.map(d => {
    const total = d.confirmed_count + d.pending_count;
    return total > 0 ? +((d.confirmed_count / total) * 100).toFixed(1) : 0;
  });

  if (rateChart) rateChart.destroy();
  rateChart = new Chart(document.getElementById('rateChart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          label: 'Confirmed',
          data: conf,
          backgroundColor: 'rgba(40,167,69,0.75)',
          borderColor:     'rgba(40,167,69,1)',
          borderWidth: 1,
        },
        {
          label: 'Pending / not confirmed',
          data: pend,
          backgroundColor: 'rgba(220,53,69,0.45)',
          borderColor:     'rgba(220,53,69,0.8)',
          borderWidth: 1,
        },
      ],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      plugins: {
        tooltip: {
          callbacks: {
            afterBody: items => {
              const i   = items[0].dataIndex;
              const tot = conf[i] + pend[i];
              return [`Confirmation rate: ${rates[i]}%  (${conf[i]} / ${tot})`];
            },
          },
        },
      },
      scales: {
        x: {
          stacked: true,
          title: { display: true, text: 'Requests' },
          ticks: { precision: 0 },
        },
        y: {
          stacked: true,
        },
      },
    },
  });
}

// ── calendar performance chart ─────────────────────────────────────────────

function renderCalChart(data) {
  const confirmed = data.filter(d => d.avg_delta !== null);

  const noData = document.getElementById('no-cal-data');
  if (!confirmed.length) {
    noData.classList.remove('d-none');
    document.getElementById('calChart').classList.add('d-none');
    return;
  }
  noData.classList.add('d-none');
  document.getElementById('calChart').classList.remove('d-none');

  const labels = confirmed.map(d => d.calendar_name);
  const toMin  = v => v !== null ? +(v / 60).toFixed(2) : null;

  if (calChart) calChart.destroy();
  calChart = new Chart(document.getElementById('calChart'), {
    type: 'bar',
    plugins: [ChartDataLabels],
    data: {
      labels,
      datasets: [
        {
          label: 'Min',
          data: confirmed.map(d => toMin(d.min_delta)),
          backgroundColor: 'rgba(75,192,192,0.6)',
          borderColor:     'rgba(75,192,192,1)',
          borderWidth: 1,
        },
        {
          label: 'Median',
          data: confirmed.map(d => toMin(d.median_delta)),
          backgroundColor: 'rgba(255,206,86,0.7)',
          borderColor:     'rgba(255,206,86,1)',
          borderWidth: 1,
        },
        {
          label: 'Average',
          data: confirmed.map(d => toMin(d.avg_delta)),
          backgroundColor: 'rgba(54,162,235,0.7)',
          borderColor:     'rgba(54,162,235,1)',
          borderWidth: 1,
        },
        {
          label: 'Max',
          data: confirmed.map(d => toMin(d.max_delta)),
          backgroundColor: 'rgba(255,99,132,0.5)',
          borderColor:     'rgba(255,99,132,1)',
          borderWidth: 1,
        },
      ],
    },
    options: {
      responsive: true,
      plugins: {
        tooltip: {
          callbacks: {
            label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y?.toFixed(1) ?? '—'} min`,
          },
        },
        datalabels: {
          anchor: 'end',
          align: 'top',
          formatter: value => value !== null ? value.toFixed(1) : '',
          font: { size: 10 },
          color: '#555',
        },
      },
      scales: {
        y: {
          beginAtZero: true,
          title: { display: true, text: 'Minutes' },
        },
        x: {
          ticks: { maxRotation: 30 },
        },
      },
    },
  });
}

// ── fetch calendar stats once, render all dependent views ──────────────────

async function loadCalendarStats(dateFrom, dateTo) {
  let url = '/api/calendar-stats';
  const p = [];
  if (dateFrom) p.push('date_from=' + dateFrom);
  if (dateTo)   p.push('date_to='   + dateTo);
  if (p.length) url += '?' + p.join('&');

  const data = await fetch(url).then(r => r.json());
  renderCalSummary(data);
  renderRateChart(data);
  renderCalChart(data);
}

// ── timeline scatter chart ─────────────────────────────────────────────────

async function loadTlChart(dateFrom, dateTo) {
  let url = '/api/timeline';
  const p = [];
  if (dateFrom) p.push('date_from=' + dateFrom);
  if (dateTo)   p.push('date_to='   + dateTo);
  if (p.length) url += '?' + p.join('&');

  const resp    = await fetch(url).then(r => r.json());
  const total   = resp.total ?? 0;
  const data    = resp.requests ?? [];

  // Update global stat box
  const confirmed = data.length;
  const rate      = total > 0 ? ((confirmed / total) * 100).toFixed(1) : '0.0';
  const rateNum   = parseFloat(rate);
  const badgeClass = rateNum >= 80 ? 'bg-success' : rateNum >= 40 ? 'bg-warning text-dark' : 'bg-danger';

  document.getElementById('global-rate').textContent        = rate;
  document.getElementById('global-rate-badge').className    = `badge ${badgeClass} mb-2`;
  document.getElementById('global-confirmed').textContent   = confirmed;
  document.getElementById('global-total').textContent       = total;

  const avgFirstEl = document.getElementById('avg-first-confirm');
  if (avgFirstEl) {
    avgFirstEl.textContent = confirmed > 0
      ? fmtDelta(data.reduce((s, d) => s + d.first_delta, 0) / confirmed)
      : '—';
  }

  const noData = document.getElementById('no-tl-data');

  if (!data.length) {
    noData.classList.remove('d-none');
    document.getElementById('tlChart').classList.add('d-none');
    return;
  }
  noData.classList.add('d-none');
  document.getElementById('tlChart').classList.remove('d-none');

  // Split by status for distinct colors
  const byStatus = { complete: [], partial: [], pending: [] };
  data.forEach(d => {
    const key = d.status in byStatus ? d.status : 'pending';
    byStatus[key].push({
      x: new Date(d.created_at).getTime(),
      y: +(d.first_delta / 60).toFixed(2),
      filename: d.filename,
    });
  });

  // Tick formatter: ms → local date string
  function msToDate(ms) {
    const d = new Date(ms);
    const date = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    return `${date} ${time}`;
  }

  if (tlChart) tlChart.destroy();
  tlChart = new Chart(document.getElementById('tlChart'), {
    type: 'scatter',
    data: {
      datasets: Object.entries(byStatus)
        .filter(([, pts]) => pts.length > 0)
        .map(([status, pts]) => ({
          label: status.charAt(0).toUpperCase() + status.slice(1),
          data: pts,
          backgroundColor: COLORS[status],
          borderColor: COLORS[status],
          pointRadius: 5,
          pointHoverRadius: 7,
          showLine: true,
          tension: 0.3,
        })),
    },
    options: {
      responsive: true,
      plugins: {
        tooltip: {
          callbacks: {
            label: ctx => [
              ctx.raw.filename,
              `First confirm: ${ctx.parsed.y.toFixed(1)} min`,
            ],
          },
        },
      },
      scales: {
        x: {
          type: 'linear',
          ticks: {
            callback: val => msToDate(val),
            maxTicksLimit: 8,
          },
          title: { display: true, text: 'File creation date' },
        },
        y: {
          title: { display: true, text: 'Minutes to first confirmation' },
          beginAtZero: true,
        },
      },
    },
  });
}

// ── combined all-calendars timeline chart ──────────────────────────────────

function calColor(idx, alpha = 0.85) {
  const hue = (idx * 47) % 360;
  return `hsla(${hue}, 70%, 48%, ${alpha})`;
}

async function loadAllCalTimelines(dateFrom, dateTo) {
  let url = '/api/calendar-timeline';
  const p = [];
  if (dateFrom) p.push('date_from=' + dateFrom);
  if (dateTo)   p.push('date_to='   + dateTo);
  if (p.length) url += '?' + p.join('&');

  const data = await fetch(url).then(r => r.json());
  const noData = document.getElementById('no-all-cal-tl-data');
  const canvas = document.getElementById('allCalTlChart');

  function msToDate(ms) {
    const d = new Date(ms);
    const date = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    return `${date} ${time}`;
  }

  const datasets = data
    .map((cal, idx) => {
      const pts = (cal.points || []).map(p => ({
        x: new Date(p.created_at).getTime(),
        y: +(p.delta_seconds / 60).toFixed(2),
        filename: p.filename,
      }));
      if (!pts.length) return null;
      const color = calColor(idx);
      return {
        label: cal.calendar_name,
        data: pts,
        backgroundColor: color,
        borderColor: color,
        pointRadius: 4,
        pointHoverRadius: 6,
        showLine: true,
        tension: 0.25,
      };
    })
    .filter(Boolean);

  if (!datasets.length) {
    noData.classList.remove('d-none');
    canvas.classList.add('d-none');
    if (allCalTlChart) {
      allCalTlChart.destroy();
      allCalTlChart = null;
    }
    return;
  }

  noData.classList.add('d-none');
  canvas.classList.remove('d-none');

  if (allCalTlChart) allCalTlChart.destroy();
  allCalTlChart = new Chart(canvas, {
    type: 'scatter',
    data: { datasets },
    options: {
      responsive: true,
      plugins: {
        legend: { display: true, position: 'bottom' },
        tooltip: {
          callbacks: {
            label: ctx => [
              `${ctx.dataset.label} - ${ctx.raw.filename}`,
              `Confirmed in: ${ctx.parsed.y.toFixed(1)} min`,
            ],
          },
        },
      },
      scales: {
        x: {
          type: 'linear',
          ticks: {
            callback: val => msToDate(val),
            maxTicksLimit: 8,
          },
          title: { display: true, text: 'File creation date' },
        },
        y: {
          title: { display: true, text: 'Minutes to confirmation' },
          beginAtZero: true,
        },
      },
    },
  });
}

// ── entry point ────────────────────────────────────────────────────────────

async function loadCharts() {
  const dateFrom = document.getElementById('cf-from').value;
  const dateTo   = document.getElementById('cf-to').value;
  await Promise.all([
    loadCalendarStats(dateFrom, dateTo),
    loadTlChart(dateFrom, dateTo),
    loadAllCalTimelines(dateFrom, dateTo),
  ]);
}

// loadCharts() is called by the template after preset dates are applied.

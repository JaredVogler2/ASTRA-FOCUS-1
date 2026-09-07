/* ============================================================================
   BURNDOWN CHART - Management View
   Combined stacked bar + line chart showing schedule burndown over time.
   Color by aircraft (default), team, or task type. Filterable.

   Features:
   - Today's date indicator (vertical dashed line)
   - Per-aircraft CS 744 delivery deadline markers
   - X-axis starts at today's date (matching first scheduled job)
   - Remaining-jobs line starts at total minus already-completed jobs
   - Filters adjust both stacked bars AND remaining-jobs line
   ============================================================================ */

// ---------- State ----------
let burndownRawData = null;       // Raw API response
let burndownColorMode = 'aircraft'; // 'aircraft' | 'team' | 'type'
let burndownFilters = { aircraft: [], team: [], type: [] };
let burndownChartInstance = null;

// ---------- Color Palettes ----------
const BURNDOWN_PALETTE = [
    '#0033A0', '#E31837', '#2E7D32', '#F57C00', '#7B1FA2',
    '#00838F', '#C62828', '#1565C0', '#9E9D24', '#AD1457',
    '#00695C', '#EF6C00', '#4527A0', '#558B2F', '#D84315',
    '#6A1B9A', '#00796B', '#283593', '#BF360C', '#1B5E20',
    '#FF6F00', '#4A148C', '#004D40', '#880E4F', '#E65100'
];

// Distinct colors for delivery deadline markers (triangles / lines)
const DEADLINE_COLORS = [
    '#FF1744', '#2979FF', '#00C853', '#FF9100', '#D500F9',
    '#00B8D4', '#C51162', '#304FFE', '#64DD17', '#FF6D00',
    '#AA00FF', '#00BFA5', '#DD2C00', '#6200EA', '#AEEA00'
];

function getBurndownColor(index) {
    return BURNDOWN_PALETTE[index % BURNDOWN_PALETTE.length];
}

function getDeadlineColor(index) {
    return DEADLINE_COLORS[index % DEADLINE_COLORS.length];
}

// ---------- Date helpers ----------
function parseDate(str) {
    // Parse YYYY-MM-DD to a Date at noon local to avoid timezone issues
    const parts = str.split('-');
    return new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]), 12, 0, 0);
}

function formatDateLabel(str) {
    const dt = parseDate(str);
    return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function todayStr() {
    const now = new Date();
    const y = now.getFullYear();
    const m = String(now.getMonth() + 1).padStart(2, '0');
    const d = String(now.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + d;
}

// ---------- Tab Switching ----------
function switchMgmtTab(tabId) {
    document.querySelectorAll('.mgmt-tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.mgmtTab === tabId);
    });
    document.querySelectorAll('.mgmt-tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === 'mgmt-panel-' + tabId);
    });
    if (tabId === 'burndown' && !burndownRawData) {
        loadBurndownData();
    }
}

// ---------- Color Mode Toggle ----------
function switchBurndownColorMode(mode) {
    burndownColorMode = mode;
    document.querySelectorAll('.burndown-color-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.colorMode === mode);
    });
    const subtitleEl = document.getElementById('burndownChartSubtitle');
    if (subtitleEl) {
        const labels = { aircraft: 'Aircraft', team: 'Team', type: 'Task Type' };
        subtitleEl.textContent = 'Colored by ' + labels[mode];
    }
    if (burndownRawData) {
        renderBurndownChart();
    }
}

// ---------- Filter Logic ----------
function applyBurndownFilters() {
    if (!burndownRawData) return;

    const aircraftSel = document.getElementById('burndownAircraftFilter');
    const teamSel = document.getElementById('burndownTeamFilter');
    const typeSel = document.getElementById('burndownTypeFilter');

    burndownFilters.aircraft = getSelectedValues(aircraftSel);
    burndownFilters.team = getSelectedValues(teamSel);
    burndownFilters.type = getSelectedValues(typeSel);

    renderBurndownChart();
    updateBurndownStats();
}

function resetBurndownFilters() {
    burndownFilters = { aircraft: [], team: [], type: [] };

    ['burndownAircraftFilter', 'burndownTeamFilter', 'burndownTypeFilter'].forEach(id => {
        const sel = document.getElementById(id);
        if (sel) {
            Array.from(sel.options).forEach(opt => {
                opt.selected = opt.value === 'all';
            });
        }
    });

    if (burndownRawData) {
        renderBurndownChart();
        updateBurndownStats();
    }
}

function getSelectedValues(selectEl) {
    if (!selectEl) return [];
    const selected = Array.from(selectEl.selectedOptions).map(o => o.value);
    if (selected.includes('all') || selected.length === 0) return [];
    return selected;
}

// ---------- Data Loading ----------
async function loadBurndownData() {
    const loading = document.getElementById('burndownLoading');
    const noData = document.getElementById('burndownNoData');
    const chartContainer = document.getElementById('burndownChart');

    if (loading) loading.style.display = 'flex';
    if (noData) noData.style.display = 'none';
    if (chartContainer) chartContainer.style.display = 'none';

    try {
        const resp = await fetch('/api/scenario/3stage/burndown');
        if (!resp.ok) {
            throw new Error('Failed to load burndown data: ' + resp.status);
        }
        burndownRawData = await resp.json();
        console.log('Burndown API response:', {
            today: burndownRawData.today,
            total_jobs: burndownRawData.total_jobs,
            completed_before_today: burndownRawData.completed_before_today,
            num_days: (burndownRawData.days || []).length,
            aircraft: burndownRawData.aircraft_list
        });

        if (burndownRawData.error) {
            throw new Error(burndownRawData.error);
        }

        if (!burndownRawData.days || burndownRawData.days.length === 0) {
            throw new Error('No scheduled days in burndown data');
        }

        populateBurndownFilters();
        updateBurndownStats();

        // Hide loading, show canvas
        if (loading) loading.style.display = 'none';
        if (chartContainer) chartContainer.style.display = 'block';

        // CRITICAL: Use requestAnimationFrame to ensure the browser has
        // processed the display:block change and the canvas has non-zero
        // dimensions before Chart.js tries to render.
        requestAnimationFrame(function() {
            requestAnimationFrame(function() {
                renderBurndownChart();
            });
        });

    } catch (err) {
        console.error('Burndown load error:', err);
        if (loading) loading.style.display = 'none';
        if (noData) {
            noData.style.display = 'block';
            noData.innerHTML = '<p style="font-size: 16px; font-weight: 600;">No Schedule Data</p>' +
                '<p style="font-size: 13px; color: #6B7280;">Load a 3-stage schedule to view the burndown chart.</p>' +
                '<p style="font-size: 12px; color: #9CA3AF; margin-top: 8px;">Error: ' + err.message + '</p>';
        }
    }
}

// ---------- Populate Filter Dropdowns ----------
function populateBurndownFilters() {
    if (!burndownRawData) return;

    const aircraftSel = document.getElementById('burndownAircraftFilter');
    const teamSel = document.getElementById('burndownTeamFilter');
    const typeSel = document.getElementById('burndownTypeFilter');

    if (aircraftSel) {
        aircraftSel.innerHTML = '<option value="all" selected>All Aircraft</option>';
        burndownRawData.aircraft_list.forEach(ac => {
            const opt = document.createElement('option');
            opt.value = String(ac);
            opt.textContent = 'AC ' + ac;
            aircraftSel.appendChild(opt);
        });
    }

    if (teamSel) {
        teamSel.innerHTML = '<option value="all" selected>All Teams</option>';
        burndownRawData.team_list.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t;
            opt.textContent = t;
            teamSel.appendChild(opt);
        });
    }

    if (typeSel) {
        typeSel.innerHTML = '<option value="all" selected>All Types</option>';
        burndownRawData.type_list.forEach(tp => {
            const opt = document.createElement('option');
            opt.value = tp;
            opt.textContent = tp;
            typeSel.appendChild(opt);
        });
    }
}

// ---------- Stats Update ----------
function updateBurndownStats() {
    if (!burndownRawData) return;

    const { days: filteredDays, totalJobs } = getFilteredDays();
    const total = totalJobs !== undefined ? totalJobs : burndownRawData.total_jobs;
    const days = filteredDays || burndownRawData.days || [];

    const totalJobsEl = document.getElementById('burndownTotalJobs');
    const spanEl = document.getElementById('burndownSpan');
    const peakDayEl = document.getElementById('burndownPeakDay');
    const peakDateEl = document.getElementById('burndownPeakDate');
    const avgDayEl = document.getElementById('burndownAvgDay');

    if (totalJobsEl) totalJobsEl.textContent = total.toLocaleString();
    if (spanEl) spanEl.textContent = days.length;

    let peakCount = 0, peakDate = '';
    days.forEach(d => {
        if (d.scheduled > peakCount) {
            peakCount = d.scheduled;
            peakDate = d.date;
        }
    });
    if (peakDayEl) peakDayEl.textContent = peakCount;
    if (peakDateEl) peakDateEl.textContent = peakDate ? ('on ' + peakDate) : 'jobs in one day';

    const activeDays = days.filter(d => d.scheduled > 0);
    const avg = activeDays.length > 0 ? (total / activeDays.length).toFixed(1) : 0;
    if (avgDayEl) avgDayEl.textContent = avg;
}

// ---------- Determine Active Aircraft (for deadlines) ----------
function getActiveAircraft() {
    if (burndownFilters.aircraft.length > 0) {
        return burndownFilters.aircraft.map(String);
    }
    return (burndownRawData.aircraft_list || []).map(String);
}

// ---------- Get delivery deadlines for active/filtered aircraft ----------
function getActiveDeadlines() {
    if (!burndownRawData || !burndownRawData.aircraft_deadlines) return {};
    const deadlines = burndownRawData.aircraft_deadlines;
    const active = getActiveAircraft();
    const result = {};
    active.forEach(ac => {
        if (deadlines[ac]) {
            result[ac] = deadlines[ac];
        }
    });
    return result;
}

// ---------- Filter Data ----------
function getFilteredDays() {
    if (!burndownRawData) return { days: [], categories: [], totalJobs: 0 };

    // The API already returns days starting from today.
    // Use all days directly (no x-axis start filtering needed here).
    const days = burndownRawData.days;
    const hasAcFilter = burndownFilters.aircraft.length > 0;
    const hasTeamFilter = burndownFilters.team.length > 0;
    const hasTypeFilter = burndownFilters.type.length > 0;
    const hasAnyFilter = hasAcFilter || hasTeamFilter || hasTypeFilter;

    const catKey = burndownColorMode === 'aircraft' ? 'by_aircraft' :
                   burndownColorMode === 'team' ? 'by_team' : 'by_type';

    if (!hasAnyFilter) {
        const catSet = new Set();
        days.forEach(d => {
            Object.keys(d[catKey] || {}).forEach(k => catSet.add(k));
        });
        return { days, categories: Array.from(catSet).sort(), totalJobs: burndownRawData.total_jobs };
    }

    // Build filter sets for each dimension
    const acAllowed = hasAcFilter ? new Set(burndownFilters.aircraft) : null;
    const teamAllowed = hasTeamFilter ? new Set(burndownFilters.team) : null;
    const typeAllowed = hasTypeFilter ? new Set(burndownFilters.type) : null;

    // For the current color mode, determine which category keys to keep
    let allowedCategories = null;
    if (burndownColorMode === 'aircraft' && acAllowed) {
        allowedCategories = acAllowed;
    } else if (burndownColorMode === 'team' && teamAllowed) {
        allowedCategories = teamAllowed;
    } else if (burndownColorMode === 'type' && typeAllowed) {
        allowedCategories = typeAllowed;
    }

    // First pass: compute total scheduled in filtered data
    const catSet = new Set();
    let runningTotal = 0;
    days.forEach(d => {
        const breakdown = d[catKey] || {};
        Object.entries(breakdown).forEach(([k, v]) => {
            if (!allowedCategories || allowedCategories.has(k)) {
                runningTotal += v;
            }
        });
    });

    const totalFilteredJobs = runningTotal;
    let remaining = totalFilteredJobs;

    const filteredDays = days.map(d => {
        const breakdown = d[catKey] || {};
        const filteredBreakdown = {};
        let dayTotal = 0;

        Object.entries(breakdown).forEach(([k, v]) => {
            if (!allowedCategories || allowedCategories.has(k)) {
                filteredBreakdown[k] = v;
                dayTotal += v;
                catSet.add(k);
            }
        });

        // Store remaining BEFORE subtracting so the line starts at the full count
        const currentRemaining = remaining;
        remaining -= dayTotal;

        return {
            ...d,
            scheduled: dayTotal,
            remaining: Math.max(0, currentRemaining),
            [catKey]: filteredBreakdown
        };
    });

    return { days: filteredDays, categories: Array.from(catSet).sort(), totalJobs: totalFilteredJobs };
}

// ---------- Render Chart ----------
function renderBurndownChart() {
    if (!burndownRawData) {
        console.warn('Burndown: no raw data');
        return;
    }

    const canvas = document.getElementById('burndownChart');
    if (!canvas) {
        console.warn('Burndown: canvas element not found');
        return;
    }

    // Verify the canvas has usable dimensions
    const parentRect = canvas.parentElement ? canvas.parentElement.getBoundingClientRect() : null;
    if (parentRect && (parentRect.width === 0 || parentRect.height === 0)) {
        console.warn('Burndown: canvas parent has zero dimensions, deferring render');
        requestAnimationFrame(function() { renderBurndownChart(); });
        return;
    }

    if (burndownChartInstance) {
        burndownChartInstance.destroy();
        burndownChartInstance = null;
    }

    const { days, categories, totalJobs } = getFilteredDays();
    console.log('Burndown render: days=' + days.length + ', categories=' + categories.length + ', totalJobs=' + totalJobs);
    if (!days || days.length === 0) {
        console.warn('Burndown: no days to render');
        return;
    }

    const catKey = burndownColorMode === 'aircraft' ? 'by_aircraft' :
                   burndownColorMode === 'team' ? 'by_team' : 'by_type';

    // X-axis labels (API already starts from today)
    const labels = days.map(d => formatDateLabel(d.date));
    const dateStrings = days.map(d => d.date);

    // Today's date info
    const today = burndownRawData.today || todayStr();
    const todayIndex = dateStrings.indexOf(today);

    // Delivery deadlines for active/filtered aircraft
    const deadlines = getActiveDeadlines();

    // Build color map for categories
    const colorMap = {};
    categories.forEach((cat, i) => {
        colorMap[cat] = getBurndownColor(i);
    });

    // Stacked bar datasets (jobs scheduled per day, by category)
    const barDatasets = categories.map(cat => ({
        label: burndownColorMode === 'aircraft' ? ('AC ' + cat) : cat,
        type: 'bar',
        data: days.map(d => (d[catKey] || {})[cat] || 0),
        backgroundColor: colorMap[cat] + 'CC',
        borderColor: colorMap[cat],
        borderWidth: 1,
        stack: 'jobs',
        order: 2,
        yAxisID: 'y'
    }));

    // Burndown line dataset (remaining jobs at start of each day)
    // Each point shows remaining jobs BEFORE that day's scheduled work begins.
    const lineDataset = {
        label: 'Remaining Jobs',
        type: 'line',
        data: days.map(d => d.remaining),
        borderColor: '#1F2937',
        backgroundColor: 'rgba(31, 41, 55, 0.1)',
        borderWidth: 3,
        pointRadius: 3,
        pointHoverRadius: 6,
        fill: false,
        tension: 0.2,
        order: 1,
        yAxisID: 'y',
        stack: 'remaining'
    };

    const datasets = [...barDatasets, lineDataset];

    // Custom plugin to draw vertical lines for today and delivery deadlines
    const verticalLinesPlugin = {
        id: 'burndownVerticalLines',
        afterDraw: function(chart) {
            const ctx = chart.ctx;
            const xAxis = chart.scales.x;
            const yAxis = chart.scales.y;

            if (!xAxis || !yAxis) return;

            // Draw today line
            if (todayIndex >= 0) {
                const x = xAxis.getPixelForValue(todayIndex);
                ctx.save();
                ctx.beginPath();
                ctx.setLineDash([6, 4]);
                ctx.strokeStyle = '#10B981';
                ctx.lineWidth = 2.5;
                ctx.moveTo(x, yAxis.top);
                ctx.lineTo(x, yAxis.bottom);
                ctx.stroke();
                ctx.setLineDash([]);

                // Label
                ctx.fillStyle = '#10B981';
                ctx.font = 'bold 11px -apple-system, BlinkMacSystemFont, sans-serif';
                ctx.textAlign = 'center';
                const labelWidth = ctx.measureText('TODAY').width + 12;
                const labelY = yAxis.top - 4;
                roundRect(ctx, x - labelWidth / 2, labelY - 16, labelWidth, 18, 4);
                ctx.fill();
                ctx.fillStyle = '#FFFFFF';
                ctx.fillText('TODAY', x, labelY - 3);
                ctx.restore();
            }

            // Draw delivery deadline lines
            let dlIdx = 0;
            Object.entries(deadlines).forEach(([ac, dateStr]) => {
                const idx = dateStrings.indexOf(dateStr);
                if (idx >= 0) {
                    const x = xAxis.getPixelForValue(idx);
                    const color = getDeadlineColor(dlIdx);

                    ctx.save();
                    ctx.beginPath();
                    ctx.setLineDash([3, 3]);
                    ctx.strokeStyle = color;
                    ctx.lineWidth = 2;
                    ctx.moveTo(x, yAxis.top);
                    ctx.lineTo(x, yAxis.bottom);
                    ctx.stroke();
                    ctx.setLineDash([]);

                    // Label at bottom
                    const labelText = 'AC ' + ac;
                    ctx.font = 'bold 10px -apple-system, BlinkMacSystemFont, sans-serif';
                    const tw = ctx.measureText(labelText).width + 10;
                    const ly = yAxis.bottom + 4;
                    ctx.fillStyle = color;
                    roundRect(ctx, x - tw / 2, ly, tw, 16, 3);
                    ctx.fill();
                    ctx.fillStyle = '#FFFFFF';
                    ctx.textAlign = 'center';
                    ctx.fillText(labelText, x, ly + 12);

                    // Small triangle at top
                    ctx.beginPath();
                    ctx.fillStyle = color;
                    ctx.moveTo(x, yAxis.top);
                    ctx.lineTo(x - 5, yAxis.top - 8);
                    ctx.lineTo(x + 5, yAxis.top - 8);
                    ctx.closePath();
                    ctx.fill();

                    ctx.restore();
                }
                dlIdx++;
            });
        }
    };

    try {
        burndownChartInstance = new Chart(canvas.getContext('2d'), {
            type: 'bar',
            data: { labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                layout: {
                    padding: { top: 20, bottom: 24 }
                },
                interaction: {
                    mode: 'index',
                    intersect: false
                },
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            boxWidth: 14,
                            padding: 12,
                            font: { size: 11 },
                            usePointStyle: true
                        }
                    },
                    tooltip: {
                        backgroundColor: 'rgba(0,0,0,0.85)',
                        titleFont: { size: 13 },
                        bodyFont: { size: 12 },
                        padding: 12,
                        callbacks: {
                            title: function(tooltipItems) {
                                if (tooltipItems.length > 0) {
                                    const idx = tooltipItems[0].dataIndex;
                                    const dateLabel = days[idx].date;
                                    const isToday = dateLabel === today;
                                    return dateLabel + (isToday ? '  (TODAY)' : '');
                                }
                                return '';
                            },
                            afterTitle: function(tooltipItems) {
                                if (tooltipItems.length > 0) {
                                    const idx = tooltipItems[0].dataIndex;
                                    let info = 'Scheduled: ' + days[idx].scheduled + ' | Remaining: ' + days[idx].remaining;
                                    // Check if this date is a deadline
                                    const dateStr = days[idx].date;
                                    const deadlineAc = Object.entries(deadlines)
                                        .filter(([ac, dd]) => dd === dateStr)
                                        .map(([ac]) => 'AC ' + ac);
                                    if (deadlineAc.length > 0) {
                                        info += '\nDelivery Due: ' + deadlineAc.join(', ');
                                    }
                                    return info;
                                }
                                return '';
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        stacked: true,
                        grid: { display: false },
                        ticks: {
                            font: { size: 10 },
                            maxRotation: 45,
                            autoSkip: true,
                            maxTicksLimit: 30
                        }
                    },
                    y: {
                        stacked: true,
                        position: 'left',
                        title: {
                            display: true,
                            text: 'Jobs',
                            font: { size: 12, weight: 'bold' }
                        },
                        beginAtZero: true,
                        suggestedMax: totalJobs,
                        grid: { color: '#E5E7EB' },
                        ticks: { font: { size: 11 } }
                    }
                }
            },
            plugins: [verticalLinesPlugin]
        });
        console.log('Burndown chart rendered successfully');
    } catch (chartErr) {
        console.error('Burndown Chart.js error:', chartErr);
    }

    renderBurndownSummaryTable(categories, days, catKey, colorMap);
}

// ---------- Rounded rect helper for canvas labels ----------
function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
}

// ---------- Summary Table ----------
function renderBurndownSummaryTable(categories, days, catKey, colorMap) {
    const container = document.getElementById('burndownSummaryTable');
    if (!container) return;

    if (categories.length === 0) {
        container.innerHTML = '<p style="color: #6B7280; font-size: 13px;">No data to display.</p>';
        return;
    }

    const catTotals = {};
    categories.forEach(cat => { catTotals[cat] = 0; });
    days.forEach(d => {
        const bd = d[catKey] || {};
        Object.entries(bd).forEach(([k, v]) => {
            if (catTotals[k] !== undefined) catTotals[k] += v;
        });
    });

    const totalJobs = Object.values(catTotals).reduce((a, b) => a + b, 0);
    const modeLabel = burndownColorMode === 'aircraft' ? 'Aircraft' :
                      burndownColorMode === 'team' ? 'Team' : 'Type';

    // Include delivery deadline info when in aircraft mode
    const deadlines = burndownRawData.aircraft_deadlines || {};
    const showDeadline = burndownColorMode === 'aircraft';

    let html = '<table style="width: 100%; border-collapse: collapse; font-size: 13px;">';
    html += '<thead><tr style="border-bottom: 2px solid #E5E7EB;">';
    html += '<th style="text-align: left; padding: 8px;">' + modeLabel + '</th>';
    if (showDeadline) html += '<th style="text-align: center; padding: 8px;">Delivery Date</th>';
    html += '<th style="text-align: right; padding: 8px;">Jobs</th>';
    html += '<th style="text-align: right; padding: 8px;">% of Total</th>';
    html += '<th style="text-align: left; padding: 8px 8px 8px 16px;">Distribution</th>';
    html += '</tr></thead><tbody>';

    const sorted = categories.slice().sort((a, b) => catTotals[b] - catTotals[a]);

    sorted.forEach(cat => {
        const count = catTotals[cat];
        const pct = totalJobs > 0 ? ((count / totalJobs) * 100).toFixed(1) : '0.0';
        const barWidth = totalJobs > 0 ? ((count / totalJobs) * 100) : 0;
        const label = burndownColorMode === 'aircraft' ? ('AC ' + cat) : cat;

        html += '<tr style="border-bottom: 1px solid #F3F4F6;">';
        html += '<td style="padding: 8px;">';
        html += '<span style="display: inline-block; width: 12px; height: 12px; border-radius: 3px; background: ' + (colorMap[cat] || '#999') + '; margin-right: 8px; vertical-align: middle;"></span>';
        html += '<span style="vertical-align: middle;">' + label + '</span></td>';
        if (showDeadline) {
            const dd = deadlines[cat];
            html += '<td style="text-align: center; padding: 8px; color: #6B7280; font-size: 12px;">' + (dd || '-') + '</td>';
        }
        html += '<td style="text-align: right; padding: 8px; font-weight: 600;">' + count.toLocaleString() + '</td>';
        html += '<td style="text-align: right; padding: 8px; color: #6B7280;">' + pct + '%</td>';
        html += '<td style="padding: 8px 8px 8px 16px;">';
        html += '<div style="background: #E5E7EB; border-radius: 4px; height: 8px; width: 100%; max-width: 200px;">';
        html += '<div style="background: ' + (colorMap[cat] || '#999') + '; height: 100%; width: ' + barWidth + '%; border-radius: 4px;"></div>';
        html += '</div></td>';
        html += '</tr>';
    });

    html += '</tbody></table>';
    container.innerHTML = html;
}

// ---------- Expose globally ----------
window.switchMgmtTab = switchMgmtTab;
window.switchBurndownColorMode = switchBurndownColorMode;
window.applyBurndownFilters = applyBurndownFilters;
window.resetBurndownFilters = resetBurndownFilters;
window.loadBurndownData = loadBurndownData;

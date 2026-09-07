// ============================================================================
// SHIFT PERFORMANCE DASHBOARD - Three-Dimensional "Transcript" Model
// ============================================================================
// Three metrics: Throughput (quantity) + Alignment (quality) + Impact (outcome)
// Combined into Performance Index: Throughput x Alignment x Impact Factor
// Four audience tabs: Floor Manager, Production Manager, Executive, Schedule Impact

// Module-level state
let spPerformanceData = null;
let spShiftDate = null;
let spShiftNumber = null;
let spCurrentTab = 'floor-manager';
let spActiveWorkGroup = 'mechanic';  // Default to Production
let spCharts = {};
let spBeforeScheduleFile = null;  // Filename of "before" (plan) schedule for task search

// ============================================================================
// INITIALIZATION
// ============================================================================

document.addEventListener('DOMContentLoaded', function() {
    setupShiftPerformanceListeners();
    loadDelayReasonCategories();
});

function setupShiftPerformanceListeners() {
    const refreshBtn = document.getElementById('refreshPerformanceBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', autoLoadLatestPerformance);
    }

    const delayForm = document.getElementById('delayReasonForm');
    if (delayForm) {
        delayForm.addEventListener('submit', submitDelayReason);
    }

    document.querySelectorAll('.sp-tab-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const tabId = this.getAttribute('data-sp-tab');
            switchSpTab(tabId);
        });
    });
}

// ============================================================================
// SUB-TAB SWITCHING
// ============================================================================

function switchSpTab(tabId) {
    spCurrentTab = tabId;
    document.querySelectorAll('.sp-tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-sp-tab') === tabId);
    });
    document.querySelectorAll('.sp-tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === `sp-${tabId}`);
    });
    if (spPerformanceData) renderTabContent(tabId);
}

// ============================================================================
// DATA LOADING
// ============================================================================

/**
 * Find the best before/after schedule pair for performance comparison.
 *
 * Work day shift order:  3rd → 1st → 2nd
 * Performance for shift N = compare N's schedule (before/plan) vs next shift's
 * schedule (after/remaining work).
 *
 *   3rd shift perf:  before = S3(work day D),  after = S1(work day D)
 *   1st shift perf:  before = S1(work day D),  after = S2(work day D)
 *   2nd shift perf:  before = S2(work day D),  after = S3(work day D+1)
 *
 * The most recent schedule is the "after" (just produced).  We look backward
 * in the shift sequence to find the matching "before".
 */
function findPerformancePair(schedules) {
    // Only consider schedules that have shift info (new format)
    const withShift = schedules.filter(s => s.shift_number && s.work_day);

    if (withShift.length >= 2) {
        // Sort newest first by mtime
        withShift.sort((a, b) => b.modified - a.modified);

        const after = withShift[0];  // Most recent = "after" schedule
        const afterShift = after.shift_number;
        const afterWorkDay = after.work_day;

        // Determine what the "before" shift should be based on the sequence
        // 3rd → 1st → 2nd.  The "before" is the prior shift in the sequence.
        // after=S1 → before should be S3 of SAME work day
        // after=S2 → before should be S1 of SAME work day
        // after=S3 → before should be S2 of PREVIOUS work day
        let beforeShift, beforeWorkDay;
        if (afterShift === 1) {
            beforeShift = 3;
            beforeWorkDay = afterWorkDay;  // S3 is same work day (ran prior evening)
        } else if (afterShift === 2) {
            beforeShift = 1;
            beforeWorkDay = afterWorkDay;
        } else {
            // afterShift === 3
            beforeShift = 2;
            // S3 of work day D+1 follows S2 of work day D
            // Subtract one day from afterWorkDay to get the S2 work day
            const d = new Date(afterWorkDay.replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3'));
            d.setDate(d.getDate() - 1);
            const y = d.getFullYear();
            const m = String(d.getMonth() + 1).padStart(2, '0');
            const day = String(d.getDate()).padStart(2, '0');
            beforeWorkDay = `${y}${m}${day}`;
        }

        // Find the best matching "before" schedule
        const beforeCandidates = withShift.filter(s =>
            s.shift_number === beforeShift && s.work_day === beforeWorkDay
        );

        if (beforeCandidates.length > 0) {
            // Use the most recent one matching the criteria
            const before = beforeCandidates[0];

            // The shift being evaluated is the one BETWEEN before and after
            // before=S3 after=S1 → evaluating 3rd shift performance
            // before=S1 after=S2 → evaluating 1st shift performance
            // before=S2 after=S3 → evaluating 2nd shift performance
            const evaluatedShift = beforeShift;
            const evaluatedWorkDay = (beforeShift === 2 && afterShift === 3) ? beforeWorkDay : afterWorkDay;

            return {
                before: before,
                after: after,
                evaluatedShift: evaluatedShift,
                evaluatedWorkDay: evaluatedWorkDay,
                method: 'shift-paired'
            };
        }
    }

    // Fallback: no shift info or no matching pair — use legacy mtime-based pairing
    const sorted = [...schedules].sort((a, b) => {
        const aTime = a.run_timestamp ? new Date(a.run_timestamp).getTime() : (a.modified * 1000);
        const bTime = b.run_timestamp ? new Date(b.run_timestamp).getTime() : (b.modified * 1000);
        return bTime - aTime;
    });

    if (sorted.length < 2) return null;

    return {
        before: sorted[1],
        after: sorted[0],
        evaluatedShift: sorted[1].shift_number || 1,
        evaluatedWorkDay: sorted[1].work_day || sorted[1].date_str || sorted[0].date_str,
        method: 'legacy-mtime'
    };
}

async function autoLoadLatestPerformance() {
    try {
        showPerformanceLoading(true);

        const schedulesResponse = await fetch('/api/shift-performance/available-schedules');
        if (!schedulesResponse.ok) throw new Error('Failed to load schedules');

        const schedulesData = await schedulesResponse.json();
        const schedules = schedulesData.schedules;

        if (schedules.length < 2) {
            showPerformanceNoData(true, 'Not enough schedules for comparison. Need at least 2 schedule files.');
            return;
        }

        // Find the best before/after pair using shift-aware logic
        const pair = findPerformancePair(schedules);
        if (!pair) {
            showPerformanceNoData(true, 'Could not find a valid schedule pair for comparison.');
            return;
        }

        const { before: previousSchedule, after: currentSchedule, evaluatedShift, evaluatedWorkDay } = pair;

        document.getElementById('currentScheduleName').textContent =
            currentSchedule.display_name || `${currentSchedule.display_date} - ${currentSchedule.identifier}`;
        document.getElementById('previousScheduleName').textContent =
            previousSchedule.display_name || `${previousSchedule.display_date} - ${previousSchedule.identifier}`;

        spShiftDate = evaluatedWorkDay;
        spShiftNumber = evaluatedShift;

        const shiftNames = {1: '1st Shift', 2: '2nd Shift', 3: '3rd Shift'};
        const displayDate = evaluatedWorkDay.replace(/(\d{4})(\d{2})(\d{2})/, '$2/$3/$1');
        document.getElementById('performanceShiftInfo').textContent =
            `${shiftNames[spShiftNumber] || 'Shift ' + spShiftNumber} — Work Day ${displayDate}`;

        if (pair.method === 'shift-paired') {
            console.log(`Performance pair (shift-aware): evaluating ${shiftNames[evaluatedShift]} on ${displayDate}`);
            console.log(`  Before (plan): ${previousSchedule.filename}`);
            console.log(`  After (remaining): ${currentSchedule.filename}`);
        } else {
            console.log('Performance pair (legacy mtime fallback)');
        }

        const calculateResponse = await fetch(
            `/api/shift-performance/calculate/${spShiftDate}/${spShiftNumber}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                before_schedule_file: previousSchedule.filename,
                after_schedule_file: currentSchedule.filename
            })
        });

        if (calculateResponse.ok) {
            const calcData = await calculateResponse.json();
            if (calcData.performance) {
                spPerformanceData = calcData.performance;
                spBeforeScheduleFile = previousSchedule.filename;
                spActiveWorkGroup = calcData.performance.active_work_group || 'mechanic';
                showPerformanceDashboard();
                renderScheduleOverview(calcData.performance.schedule_overview);
                renderAircraftChangeBanner(calcData.performance.aircraft_changes);
                renderResourceTypeSelector();
                renderAllTabs();
                return;
            }
        }

        showPerformanceNoData(true, 'No performance data available for this shift.');

    } catch (error) {
        console.error('Error loading performance:', error);
        showPerformanceNoData(true, `Error: ${error.message}`);
    } finally {
        showPerformanceLoading(false);
    }
}

// ============================================================================
// UI STATE HELPERS
// ============================================================================

function showPerformanceLoading(show) {
    const el = document.getElementById('performanceLoading');
    const noData = document.getElementById('performanceNoData');
    const dash = document.getElementById('performanceDashboard');
    if (el) el.style.display = show ? 'flex' : 'none';
    if (show && noData) noData.style.display = 'none';
    if (show && dash) dash.style.display = 'none';
}

function showPerformanceNoData(show, message) {
    const el = document.getElementById('performanceNoData');
    const loading = document.getElementById('performanceLoading');
    const dash = document.getElementById('performanceDashboard');
    if (el) {
        el.style.display = show ? 'block' : 'none';
        if (message) {
            el.innerHTML = `<div class="ios-empty-icon">&#128202;</div>
                <h3 class="ios-empty-title">No Performance Data</h3>
                <p class="ios-empty-description">${message}</p>`;
        }
    }
    if (show && loading) loading.style.display = 'none';
    if (show && dash) dash.style.display = 'none';
}

function showPerformanceDashboard() {
    document.getElementById('performanceLoading').style.display = 'none';
    document.getElementById('performanceNoData').style.display = 'none';
    document.getElementById('performanceDashboard').style.display = 'block';
}

// ============================================================================
// SCHEDULE OVERVIEW (before vs after task count comparison)
// ============================================================================

function renderScheduleOverview(overview) {
    const container = document.getElementById('spScheduleOverview');
    if (!container) return;

    if (!overview) {
        container.innerHTML = '';
        return;
    }

    const shiftNames = {1: '1st', 2: '2nd', 3: '3rd'};
    const removedColor = overview.tasks_removed > 0 ? '#059669' : (overview.tasks_removed < 0 ? '#dc2626' : '#6b7280');
    const completedColor = overview.truly_completed > 0 ? '#059669' : '#6b7280';
    const addedColor = overview.newly_added > 0 ? '#2563eb' : '#6b7280';

    // Determine if this looks like identical data (sanity check)
    const isIdentical = overview.truly_completed === 0 && overview.newly_added === 0
        && overview.before_total === overview.after_total;

    let html = `<div style="margin: 0 0 16px 0; padding: 14px 18px; border-radius: 10px;
        background: linear-gradient(135deg, #f8fafc, #f1f5f9); border: 1px solid #e2e8f0;">
        <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; margin-bottom:10px;">
            <div style="font-weight:700; font-size:15px; color:#0f172a;">Schedule Overview</div>
            <div style="display:flex; gap:18px; font-size:13px;">
                <span>Before: <strong>${overview.before_total}</strong> tasks</span>
                <span>After: <strong>${overview.after_total}</strong> tasks</span>
                <span style="color:${completedColor}; font-weight:700;">
                    ${overview.truly_completed > 0 ? overview.truly_completed + ' completed' : 'None completed'}
                </span>`;

    if (overview.newly_added > 0) {
        html += `<span style="color:${addedColor}; font-weight:600;">+${overview.newly_added} new</span>`;
    }

    html += `
                <span style="color:${removedColor}; font-weight:700;">
                    Net: ${overview.tasks_removed > 0 ? '-' : ''}${Math.abs(overview.tasks_removed)} tasks
                </span>
            </div>
        </div>`;

    if (isIdentical) {
        html += `<div style="padding:6px 12px; background:#fef3c7; border-radius:6px; color:#92400e;
            font-size:12px; font-weight:600; margin-bottom:10px;">
            Same task set in both schedules — no work was completed between these runs.
        </div>`;
    }

    // Per-shift breakdown table
    html += `<table style="width:100%; border-collapse:collapse; font-size:13px;">
        <thead><tr style="border-bottom:2px solid #cbd5e1;">
            <th style="text-align:left; padding:4px 8px; color:#475569;">Shift</th>
            <th style="text-align:right; padding:4px 8px; color:#475569;">Before (Plan)</th>
            <th style="text-align:right; padding:4px 8px; color:#475569;">After</th>
            <th style="text-align:right; padding:4px 8px; color:#475569;">Delta</th>
        </tr></thead><tbody>`;

    for (const s of (overview.shift_breakdown || [])) {
        const deltaColor = s.delta < 0 ? '#059669' : (s.delta > 0 ? '#dc2626' : '#6b7280');
        const deltaStr = s.delta === 0 ? '—' : (s.delta > 0 ? `+${s.delta}` : `${s.delta}`);
        const rowBg = s.is_evaluated ? 'background:#e0f2fe;' : '';
        const evalBadge = s.is_evaluated ? ' <span style="font-size:10px;color:#0369a1;font-weight:600;">(evaluated)</span>' : '';

        html += `<tr style="border-bottom:1px solid #e2e8f0; ${rowBg}">
            <td style="padding:4px 8px; font-weight:600;">${shiftNames[s.shift] || 'S' + s.shift} Shift${evalBadge}</td>
            <td style="text-align:right; padding:4px 8px;">${s.before}</td>
            <td style="text-align:right; padding:4px 8px;">${s.after}</td>
            <td style="text-align:right; padding:4px 8px; font-weight:700; color:${deltaColor};">${deltaStr}</td>
        </tr>`;
    }

    // Total row
    html += `<tr style="border-top:2px solid #94a3b8; font-weight:700;">
        <td style="padding:4px 8px;">Total</td>
        <td style="text-align:right; padding:4px 8px;">${overview.before_total}</td>
        <td style="text-align:right; padding:4px 8px;">${overview.after_total}</td>
        <td style="text-align:right; padding:4px 8px; color:${removedColor};">
            ${overview.tasks_removed === 0 ? '—' : (overview.tasks_removed > 0 ? '-' + overview.tasks_removed : '+' + Math.abs(overview.tasks_removed))}
        </td>
    </tr>`;

    html += `</tbody></table>`;

    // Evaluated shift today breakdown
    if (overview.before_today_shift > 0 || overview.after_today_shift > 0) {
        const todayDelta = overview.today_shift_delta;
        const todayDeltaColor = todayDelta < 0 ? '#059669' : (todayDelta > 0 ? '#dc2626' : '#6b7280');
        const cleared = overview.before_today_shift - overview.after_today_shift;
        html += `<div style="margin-top:8px; padding:6px 12px; background:#f0fdf4; border-radius:6px; font-size:12px; color:#166534;">
            <strong>${shiftNames[overview.evaluated_shift] || ''} Shift today (day ${overview.today_day}):</strong>
            ${overview.before_today_shift} planned &rarr; ${overview.after_today_shift} remaining
            ${cleared > 0 ? `&nbsp;|&nbsp; <strong>${cleared} cleared</strong>` : ''}
        </div>`;
    }

    html += '</div>';
    container.innerHTML = html;
}

// ============================================================================
// AIRCRAFT SET CHANGE BANNER
// ============================================================================

function renderAircraftChangeBanner(changes) {
    // Remove any existing banner
    const existing = document.getElementById('aircraftChangeBanner');
    if (existing) existing.remove();

    if (!changes || !changes.has_changes) return;

    const banner = document.createElement('div');
    banner.id = 'aircraftChangeBanner';
    banner.style.cssText = 'margin: 0 0 16px 0; padding: 12px 16px; border-radius: 8px; ' +
        'background: #fffbeb; border: 1px solid #f59e0b; font-size: 14px;';

    let html = '<div style="font-weight: 600; margin-bottom: 6px; color: #92400e;">Aircraft Set Changes</div>';

    if (changes.completed_aircraft && changes.completed_aircraft.length > 0) {
        for (const ac of changes.completed_aircraft) {
            html += `<div style="color: #059669; margin: 2px 0;">` +
                `Line ${ac.line_number} complete! (${ac.task_count} tasks finished)</div>`;
        }
    }

    if (changes.new_aircraft && changes.new_aircraft.length > 0) {
        for (const ac of changes.new_aircraft) {
            html += `<div style="color: #2563eb; margin: 2px 0;">` +
                `NEW! Line ${ac.line_number} (${ac.task_count} tasks added to schedule)</div>`;
        }
    }

    banner.innerHTML = html;

    // Insert at top of the performance dashboard
    const dashboard = document.getElementById('performanceDashboard');
    if (dashboard) {
        dashboard.insertBefore(banner, dashboard.firstChild);
    }
}

// ============================================================================
// RENDERING - DISPATCH
// ============================================================================

function renderAllTabs() {
    renderTabContent('floor-manager');
    renderTabContent('executive');
    renderTabContent('production-manager');
    renderTabContent('schedule-impact');
}

// ============================================================================
// RESOURCE TYPE SELECTOR
// ============================================================================

function renderResourceTypeSelector() {
    const container = document.getElementById('spResourceTypeSelector');
    if (!container || !spPerformanceData) return;

    const labels = spPerformanceData.work_group_labels || {
        mechanic: 'Production', quality: 'Quality (QA)',
        customer: 'Customer', vendor: 'Vendor', all: 'All Resources'
    };
    const summary = spPerformanceData.work_group_summary || {};

    const workGroups = ['mechanic', 'quality', 'customer', 'vendor', 'all'];
    let html = '<div class="sp-resource-type-bar">';
    for (const wg of workGroups) {
        const label = labels[wg] || wg;
        const wgInfo = summary[wg] || {};
        const planned = wgInfo.total_tasks_planned || 0;
        const piGrade = wgInfo.pi_grade || '';
        const isActive = wg === spActiveWorkGroup;
        const badgeText = wg !== 'all' ? ` (${planned})` : '';
        const gradeText = piGrade && piGrade !== 'N/A' && wg !== 'all' ? ` ${piGrade}` : '';

        html += `<button class="sp-resource-btn ${isActive ? 'active' : ''} ${wg === 'mechanic' ? 'sp-resource-primary' : ''}"
            onclick="switchResourceType('${wg}')" title="${label}">
            ${label}${badgeText}${gradeText ? ' <span class="sp-resource-grade" style="color:' + gradeColor(piGrade) + ';">' + gradeText + '</span>' : ''}
        </button>`;
    }
    html += '</div>';
    container.innerHTML = html;
}

function switchResourceType(workGroup) {
    if (!spPerformanceData || !spPerformanceData.by_resource_type) return;
    spActiveWorkGroup = workGroup;

    const wgData = spPerformanceData.by_resource_type[workGroup];
    if (!wgData) return;

    // Swap the active view data
    spPerformanceData.teams = wgData.teams || [];
    spPerformanceData.superintendents = wgData.superintendents || [];
    spPerformanceData.factory = wgData.factory || {};
    spPerformanceData.team_grade_distribution = wgData.team_grade_distribution || {};
    spPerformanceData.delay_reason_breakdown = wgData.delay_reason_breakdown || {};

    // Re-render selector and all tabs
    renderResourceTypeSelector();
    renderAllTabs();
}


function renderTabContent(tabId) {
    if (!spPerformanceData && tabId !== 'shift-progress') return;
    switch(tabId) {
        case 'floor-manager': renderFloorManager(); break;
        case 'production-manager': renderProductionManager(); break;
        case 'executive': renderExecutive(); break;
        case 'schedule-impact': renderScheduleImpact(); break;
        case 'shift-progress': renderShiftProgress(); break;
    }
}

// ============================================================================
// UTILITY HELPERS
// ============================================================================

function gradeColor(grade) {
    if (!grade || grade === 'N/A') return '#6c757d';
    const g = grade.replace(/[+-]/g, '').charAt(0);
    const colors = { 'A': '#059669', 'B': '#2563eb', 'C': '#d97706', 'D': '#ea580c', 'F': '#dc2626' };
    return colors[g] || '#6c757d';
}

function gradeBg(grade) {
    if (!grade || grade === 'N/A') return '#f3f4f6';
    const g = grade.replace(/[+-]/g, '').charAt(0);
    const bgs = { 'A': '#ecfdf5', 'B': '#eff6ff', 'C': '#fffbeb', 'D': '#fff7ed', 'F': '#fef2f2' };
    return bgs[g] || '#f3f4f6';
}

function destroyChart(key) {
    if (spCharts[key]) { spCharts[key].destroy(); delete spCharts[key]; }
}

function safe(val, fallback) {
    return (val !== undefined && val !== null) ? val : fallback;
}

function deltaArrow(val) {
    if (val > 0) return `<span style="color:#059669;">+${val}</span>`;
    if (val < 0) return `<span style="color:#dc2626;">${val}</span>`;
    return `<span style="color:#6b7280;">0</span>`;
}

function impactBadge(impact) {
    const improved = safe(impact.aircraft_improved, 0);
    const held = safe(impact.aircraft_held, 0);
    const worsened = safe(impact.aircraft_worsened, 0);
    let parts = [];
    if (improved > 0) parts.push(`<span style="color:#059669;">${improved} improved</span>`);
    if (held > 0) parts.push(`<span style="color:#6b7280;">${held} held</span>`);
    if (worsened > 0) parts.push(`<span style="color:#dc2626;">${worsened} worsened</span>`);
    return parts.join(' / ') || 'No data';
}

// ============================================================================
// TAB 1: FLOOR MANAGER VIEW
// ============================================================================

function renderFloorManager() {
    const data = spPerformanceData;
    const container = document.getElementById('sp-floor-manager-content');
    if (!container) return;

    const factory = data.factory || {};
    const throughput = factory.throughput || {};
    const alignment = factory.alignment || {};
    const impact = factory.impact || {};
    const teams = (data.teams || []).filter(t => !t.no_tasks_planned);
    const wgLabels = data.work_group_labels || {};
    const activeLabel = wgLabels[spActiveWorkGroup] || spActiveWorkGroup;

    const sortedTeams = [...teams].sort((a, b) => {
        return (a.performance_index || 0) - (b.performance_index || 0);
    });

    let html = '';

    // Work group summary cards (show all resource types at a glance)
    const wgSummary = data.work_group_summary || {};
    if (Object.keys(wgSummary).length > 0) {
        html += `<div class="sp-section" style="padding:12px 16px; margin-bottom:16px; background:#f8fafc;">
            <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px;">
                <strong style="font-size:13px; color:#475569;">Resource Type Summary</strong>
                <span class="sp-wg-label sp-wg-${spActiveWorkGroup}">Viewing: ${activeLabel}</span>
            </div>
            <div style="display:flex; gap:16px; flex-wrap:wrap;">`;
        for (const [wg, info] of Object.entries(wgSummary)) {
            const isActive = wg === spActiveWorkGroup;
            html += `<div style="font-size:12px; color:${isActive ? '#1e293b' : '#94a3b8'}; ${isActive ? 'font-weight:700;' : ''}">
                ${info.label}: ${info.tasks_completed}/${info.total_tasks_planned} tasks
                ${info.pi_grade && info.pi_grade !== 'N/A' ? `<span style="color:${gradeColor(info.pi_grade)};font-weight:700;">${info.pi_grade}</span>` : ''}
            </div>`;
        }
        html += `</div></div>`;
    }

    // Three-metric summary row
    html += `<div class="sp-stats-row">
        <div class="sp-stat-card">
            <div class="sp-stat-value">${safe(throughput.completion_rate, 0).toFixed(0)}%</div>
            <div class="sp-stat-label">Throughput</div>
            <div class="sp-stat-sublabel">${safe(throughput.tasks_completed, 0)} / ${safe(throughput.tasks_planned, 0)} tasks today</div>
        </div>
        <div class="sp-stat-card">
            <div class="sp-stat-value">${safe(alignment.alignment_score, 0).toFixed(0)}%</div>
            <div class="sp-stat-label">Alignment</div>
            <div class="sp-stat-sublabel">${safe(alignment.critical_path_completed, 0)}/${safe(alignment.critical_path_total, 0)} critical path</div>
        </div>
        <div class="sp-stat-card">
            <div class="sp-stat-value">${impactBadge(impact)}</div>
            <div class="sp-stat-label">Delivery Impact</div>
            <div class="sp-stat-sublabel">Avg slide/aircraft: ${safe(impact.avg_hours_per_aircraft, 0) > 0 ? '+' : ''}${safe(impact.avg_hours_per_aircraft, 0)} hrs</div>
        </div>
        <div class="sp-stat-card" style="border-left: 4px solid ${gradeColor(factory.pi_grade)};">
            <div class="sp-stat-value" style="color: ${gradeColor(factory.pi_grade)};">${safe(factory.performance_index, 0).toFixed(0)}</div>
            <div class="sp-stat-label">Performance Index</div>
            <div class="sp-stat-sublabel">Grade: ${safe(factory.pi_grade, 'N/A')}</div>
        </div>
    </div>`;

    // Superintendent filter
    const supers = data.superintendents || [];
    html += `<div class="sp-filter-bar">
        <label>Filter by Superintendent:</label>
        <select id="spSuperFilter" onchange="filterFloorManagerTeams(this.value)">
            <option value="all">All Superintendents (${teams.length} teams)</option>
            ${supers.map(s => `<option value="${s.superintendent}">${s.superintendent} (${s.team_count} teams)</option>`).join('')}
        </select>
    </div>`;

    // Team cards grid
    html += '<div class="sp-team-grid" id="spTeamGrid">';
    for (const team of sortedTeams) {
        const tt = team.throughput || {};
        const ta = team.alignment || {};
        const piGrade = team.pi_grade || 'N/A';
        const superintendent = team.superintendent || 'Unknown';

        html += `<div class="sp-team-card" data-superintendent="${superintendent}" onclick="showTeamDrillDown('${team.team}')">
            <div class="sp-team-header">
                <div class="sp-team-name">${team.team}</div>
                <div class="sp-grade-badge" style="background: ${gradeBg(piGrade)}; color: ${gradeColor(piGrade)};">${piGrade}</div>
            </div>
            <div class="sp-team-super">${superintendent}</div>
            <div class="sp-team-metrics">
                <div class="sp-metric-row">
                    <span class="sp-metric-label">Throughput</span>
                    <div class="sp-bar-container">
                        <div class="sp-bar" style="width: ${Math.min(100, safe(tt.completion_rate, 0))}%; background: ${gradeColor(tt.throughput_grade)};"></div>
                    </div>
                    <span class="sp-metric-val">${safe(tt.completion_rate, 0).toFixed(0)}%</span>
                </div>
                <div class="sp-metric-row">
                    <span class="sp-metric-label">Alignment</span>
                    <div class="sp-bar-container">
                        <div class="sp-bar" style="width: ${Math.min(100, safe(ta.alignment_score, 0))}%; background: #6366f1;"></div>
                    </div>
                    <span class="sp-metric-val">${safe(ta.alignment_score, 0).toFixed(0)}%</span>
                </div>
                <div class="sp-metric-row">
                    <span class="sp-metric-label">PI</span>
                    <div class="sp-bar-container">
                        <div class="sp-bar" style="width: ${Math.min(100, safe(team.performance_index, 0))}%; background: ${gradeColor(piGrade)};"></div>
                    </div>
                    <span class="sp-metric-val">${safe(team.performance_index, 0).toFixed(0)}</span>
                </div>
            </div>
            <div class="sp-team-footer">
                <span>${team.tasks_completed}/${team.total_tasks_planned} tasks</span>
                ${team.unscheduled_tasks_completed > 0 ? `<span class="sp-ok">+${team.unscheduled_tasks_completed} extra</span>` : ''}
                ${team.tasks_incomplete_no_reason > 0
                    ? `<span class="sp-warning">${team.tasks_incomplete_no_reason} no reason</span>`
                    : ''}
            </div>
        </div>`;
    }
    html += '</div>';

    // Team comparison chart
    html += `<div class="sp-chart-section">
        <h4>Team Comparison</h4>
        <div class="sp-chart-wrapper"><canvas id="spTeamComparisonChart"></canvas></div>
    </div>`;

    container.innerHTML = html;
    renderTeamComparisonChart(sortedTeams);
}

function filterFloorManagerTeams(superintendent) {
    const cards = document.querySelectorAll('#spTeamGrid .sp-team-card');
    cards.forEach(card => {
        if (superintendent === 'all') {
            card.style.display = '';
        } else {
            card.style.display = card.getAttribute('data-superintendent') === superintendent ? '' : 'none';
        }
    });
}

function renderTeamComparisonChart(teams) {
    const ctx = document.getElementById('spTeamComparisonChart');
    if (!ctx) return;
    destroyChart('teamComparison');

    const top20 = teams.slice(0, 20);
    const labels = top20.map(t => t.team.replace('FGI-CF-', '').replace('QA-FGI-CF-', 'QA-').substring(0, 18));
    const throughputData = top20.map(t => (t.throughput || {}).completion_rate || 0);
    const alignmentData = top20.map(t => (t.alignment || {}).alignment_score || 0);
    const piData = top20.map(t => t.performance_index || 0);

    spCharts['teamComparison'] = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [
                { label: 'Throughput %', data: throughputData, backgroundColor: 'rgba(37, 99, 235, 0.7)', borderRadius: 3 },
                { label: 'Alignment %', data: alignmentData, backgroundColor: 'rgba(99, 102, 241, 0.7)', borderRadius: 3 },
                { label: 'Perf. Index', data: piData, backgroundColor: 'rgba(5, 150, 105, 0.7)', borderRadius: 3 }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { position: 'top' } },
            scales: { y: { beginAtZero: true, max: 110, title: { display: true, text: 'Score' } } }
        }
    });
}

// ============================================================================
// TAB 2: PRODUCTION MANAGER VIEW
// ============================================================================

function renderProductionManager() {
    const data = spPerformanceData;
    const container = document.getElementById('sp-production-manager-content');
    if (!container) return;

    const supers = (data.superintendents || []).sort((a, b) => {
        return (b.performance_index || 0) - (a.performance_index || 0);
    });

    let html = '';

    // Superintendent cards
    html += '<div class="sp-super-grid">';
    for (const sup of supers) {
        const st = sup.throughput || {};
        const sa = sup.alignment || {};
        const si = sup.impact || {};
        const piGrade = sup.pi_grade || 'N/A';

        html += `<div class="sp-super-card">
            <div class="sp-super-header">
                <div>
                    <div class="sp-super-name">${sup.superintendent}</div>
                    <div class="sp-super-info">${sup.team_count} teams | ${sup.total_tasks_planned} tasks planned</div>
                </div>
                <div class="sp-grade-badge sp-grade-lg" style="background: ${gradeBg(piGrade)}; color: ${gradeColor(piGrade)};">${piGrade}</div>
            </div>
            <div class="sp-super-metrics">
                <div class="sp-dual-metric">
                    <div class="sp-dual-metric-item">
                        <div class="sp-dual-val">${safe(st.completion_rate, 0).toFixed(0)}%</div>
                        <div class="sp-dual-label">Throughput</div>
                    </div>
                    <div class="sp-dual-divider"></div>
                    <div class="sp-dual-metric-item">
                        <div class="sp-dual-val">${safe(sa.alignment_score, 0).toFixed(0)}%</div>
                        <div class="sp-dual-label">Alignment</div>
                    </div>
                    <div class="sp-dual-divider"></div>
                    <div class="sp-dual-metric-item">
                        <div class="sp-dual-val" style="color: ${gradeColor(piGrade)}; font-weight: 700;">${safe(sup.performance_index, 0).toFixed(0)}</div>
                        <div class="sp-dual-label">Perf. Index</div>
                    </div>
                </div>
                <div class="sp-super-stats">
                    <span>Done: ${sup.tasks_completed}/${sup.total_tasks_planned}</span>
                    <span>Impact: ${impactBadge(si)}</span>
                </div>
            </div>
        </div>`;
    }
    html += '</div>';

    // Bottom performers table
    const allTeams = (data.teams || []).filter(t => !t.no_tasks_planned);
    const bottom5 = [...allTeams].sort((a, b) => {
        return (a.performance_index || 0) - (b.performance_index || 0);
    }).slice(0, 5);

    html += `<div class="sp-section">
        <h4>Needs Improvement (Bottom 5 Teams)</h4>
        <table class="sp-table">
            <thead><tr>
                <th>Team</th><th>Superintendent</th><th>Throughput</th><th>Alignment</th><th>PI</th><th>No Reason</th>
            </tr></thead>
            <tbody>
                ${bottom5.map(t => {
                    const tt = t.throughput || {};
                    const ta = t.alignment || {};
                    return `<tr>
                        <td><strong>${t.team}</strong></td>
                        <td>${t.superintendent || '-'}</td>
                        <td>${safe(tt.completion_rate, 0).toFixed(0)}%</td>
                        <td>${safe(ta.alignment_score, 0).toFixed(0)}%</td>
                        <td style="color: ${gradeColor(t.pi_grade)}; font-weight: 700;">${safe(t.performance_index, 0).toFixed(0)} (${t.pi_grade || '-'})</td>
                        <td class="${t.tasks_incomplete_no_reason > 0 ? 'sp-cell-warning' : ''}">${t.tasks_incomplete_no_reason}</td>
                    </tr>`;
                }).join('')}
            </tbody>
        </table>
    </div>`;

    // Charts
    const delayBreakdown = data.delay_reason_breakdown || {};
    if (Object.keys(delayBreakdown).length > 0) {
        html += `<div class="sp-section sp-charts-row">
            <div class="sp-chart-half">
                <h4>Delay Reasons</h4>
                <div class="sp-chart-wrapper sp-chart-short"><canvas id="spDelayReasonChart"></canvas></div>
            </div>
            <div class="sp-chart-half">
                <h4>Superintendent Comparison</h4>
                <div class="sp-chart-wrapper sp-chart-short"><canvas id="spSuperComparisonChart"></canvas></div>
            </div>
        </div>`;
    } else {
        html += `<div class="sp-section">
            <h4>Superintendent Comparison</h4>
            <div class="sp-chart-wrapper"><canvas id="spSuperComparisonChart"></canvas></div>
        </div>`;
    }

    container.innerHTML = html;
    renderSuperComparisonChart(supers);
    if (Object.keys(delayBreakdown).length > 0) renderDelayReasonChart(delayBreakdown);
}

function renderSuperComparisonChart(supers) {
    const ctx = document.getElementById('spSuperComparisonChart');
    if (!ctx) return;
    destroyChart('superComparison');

    const labels = supers.map(s => s.superintendent.substring(0, 20));
    const piData = supers.map(s => s.performance_index || 0);
    const throughputData = supers.map(s => (s.throughput || {}).completion_rate || 0);
    const alignmentData = supers.map(s => (s.alignment || {}).alignment_score || 0);

    spCharts['superComparison'] = new Chart(ctx, {
        type: 'bar',
        data: {
            labels,
            datasets: [
                { label: 'Perf. Index', data: piData, backgroundColor: 'rgba(5, 150, 105, 0.8)', borderRadius: 4 },
                { label: 'Throughput %', data: throughputData, backgroundColor: 'rgba(37, 99, 235, 0.5)', borderRadius: 4 },
                { label: 'Alignment %', data: alignmentData, backgroundColor: 'rgba(99, 102, 241, 0.5)', borderRadius: 4 }
            ]
        },
        options: {
            responsive: true, maintainAspectRatio: false, indexAxis: 'y',
            plugins: { legend: { position: 'top' } },
            scales: { x: { beginAtZero: true, max: 110 } }
        }
    });
}

function renderDelayReasonChart(breakdown) {
    const ctx = document.getElementById('spDelayReasonChart');
    if (!ctx) return;
    destroyChart('delayReason');

    const labels = Object.keys(breakdown).map(k => k.replace(/_/g, ' '));
    const values = Object.values(breakdown);
    const colors = ['#ef4444','#f97316','#eab308','#22c55e','#3b82f6','#8b5cf6','#ec4899','#6b7280','#14b8a6','#f43f5e','#a855f7','#64748b'];

    spCharts['delayReason'] = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels,
            datasets: [{ data: values, backgroundColor: colors.slice(0, labels.length), borderWidth: 2, borderColor: '#fff' }]
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { position: 'right', labels: { boxWidth: 12, font: { size: 11 } } } }
        }
    });
}

// ============================================================================
// TAB 3: EXECUTIVE VIEW
// ============================================================================

function renderExecutive() {
    const data = spPerformanceData;
    const container = document.getElementById('sp-executive-content');
    if (!container) return;

    const factory = data.factory || {};
    const throughput = factory.throughput || {};
    const alignment = factory.alignment || {};
    const impact = factory.impact || {};
    const schedImpact = factory.schedule_impact || {};
    const piGrade = factory.pi_grade || 'N/A';
    const wgLabels = data.work_group_labels || {};
    const activeLabel = wgLabels[spActiveWorkGroup] || spActiveWorkGroup;

    let html = '';

    // Hero: PI circle + three-metric breakdown
    html += `<div class="sp-exec-hero">
        <div class="sp-exec-gpa-circle" style="border-color: ${gradeColor(piGrade)};">
            <div class="sp-exec-gpa-value" style="color: ${gradeColor(piGrade)};">${safe(factory.performance_index, 0).toFixed(0)}</div>
            <div class="sp-exec-gpa-label">Perf. Index</div>
        </div>
        <div class="sp-exec-hero-details">
            <div class="sp-exec-grade-row">
                <span class="sp-exec-grade" style="background: ${gradeBg(piGrade)}; color: ${gradeColor(piGrade)};">${piGrade}</span>
                <span class="sp-exec-grade-label">Factory Grade</span>
                <span class="sp-wg-label sp-wg-${spActiveWorkGroup}" style="margin-left:12px;">${activeLabel}</span>
            </div>
            <div class="sp-exec-dual-row">
                <div class="sp-exec-dual-item">
                    <div class="sp-exec-dual-val">${safe(throughput.completion_rate, 0).toFixed(0)}%</div>
                    <div class="sp-exec-dual-lbl">Throughput</div>
                    <div class="sp-exec-dual-sub">${safe(throughput.tasks_completed, 0)} / ${safe(throughput.tasks_planned, 0)} tasks today</div>
                </div>
                <div class="sp-exec-dual-item">
                    <div class="sp-exec-dual-val">${safe(alignment.alignment_score, 0).toFixed(0)}%</div>
                    <div class="sp-exec-dual-lbl">Alignment</div>
                    <div class="sp-exec-dual-sub">${safe(alignment.critical_path_completed, 0)}/${safe(alignment.critical_path_total, 0)} critical path tasks</div>
                </div>
                <div class="sp-exec-dual-item">
                    <div class="sp-exec-dual-val">${safe(impact.avg_hours_per_aircraft, 0) > 0 ? '+' : ''}${safe(impact.avg_hours_per_aircraft, 0)} hrs/aircraft</div>
                    <div class="sp-exec-dual-lbl">Delivery Impact</div>
                    <div class="sp-exec-dual-sub">${impactBadge(impact)}</div>
                </div>
            </div>
        </div>
    </div>`;

    // Key numbers row
    html += `<div class="sp-exec-numbers">
        <div class="sp-exec-num-card">
            <div class="sp-exec-num">${safe(factory.total_tasks_planned, 0)}</div>
            <div class="sp-exec-num-label">Tasks Planned Today</div>
        </div>
        <div class="sp-exec-num-card" style="border-left-color: #059669;">
            <div class="sp-exec-num" style="color: #059669;">${safe(factory.tasks_completed, 0)}</div>
            <div class="sp-exec-num-label">Completed</div>
        </div>
        <div class="sp-exec-num-card" style="border-left-color: #2563eb;">
            <div class="sp-exec-num" style="color: #2563eb;">${safe(factory.unscheduled_tasks_completed, 0)}</div>
            <div class="sp-exec-num-label">Extra (Unscheduled)</div>
        </div>
        <div class="sp-exec-num-card" style="border-left-color: #dc2626;">
            <div class="sp-exec-num" style="color: #dc2626;">${safe(factory.tasks_incomplete_no_reason, 0)}</div>
            <div class="sp-exec-num-label">No Reason Given</div>
        </div>
        <div class="sp-exec-num-card" style="border-left-color: #d97706;">
            <div class="sp-exec-num" style="color: #d97706;">${safe(schedImpact.total_blocked_successors, 0)}</div>
            <div class="sp-exec-num-label">Downstream Blocked</div>
        </div>
        <div class="sp-exec-num-card">
            <div class="sp-exec-num">${safe(factory.team_count, 0)}</div>
            <div class="sp-exec-num-label">Active Teams</div>
        </div>
    </div>`;

    // Aircraft delivery impact table
    const deltas = impact.aircraft_deltas || [];
    if (deltas.length > 0) {
        html += `<div class="sp-section">
            <h4>Aircraft Delivery Impact</h4>
            <table class="sp-table">
                <thead><tr>
                    <th>Aircraft</th><th>Delivery Date</th><th>Tasks Removed</th>
                    <th>Before Projected</th><th>After Projected</th>
                    <th>Schedule Delta</th><th>CP Duration (diag)</th>
                </tr></thead>
                <tbody>
                    ${deltas.map(d => {
                        const hrs = d.hours_delta;
                        const hasHrs = hrs !== undefined;
                        // hours_delta: negative = pulled earlier (good), positive = pushed later (bad)
                        const deltaLabel = hasHrs
                            ? (Math.abs(hrs) >= 0.1 ? `${hrs > 0 ? '+' : ''}${hrs} hrs` : 'held')
                            : (d.lateness_delta !== 0 ? `${deltaArrow(d.lateness_delta)} days` : 'held');
                        const deltaColor = hasHrs
                            ? (hrs < -0.1 ? '#059669' : hrs > 0.1 ? '#dc2626' : '#6b7280')
                            : (d.lateness_delta > 0 ? '#059669' : d.lateness_delta < 0 ? '#dc2626' : '#6b7280');
                        const cpBefore = d.before_cp_duration !== undefined ? `${(d.before_cp_duration/60).toFixed(1)}h` : d.before_cp;
                        const cpAfter = d.after_cp_duration !== undefined ? `${(d.after_cp_duration/60).toFixed(1)}h` : d.after_cp;
                        const cpDelta = d.cp_reduction !== undefined ? `${d.cp_reduction > 0 ? '-' : '+'}${Math.abs(d.cp_reduction)}m` : deltaArrow(d.cp_delta || 0);
                        return `<tr>
                        <td><strong>${d.name}</strong></td>
                        <td>${d.delivery_date}</td>
                        <td style="text-align:center;">${d.tasks_removed}</td>
                        <td>${d.before_projected}</td>
                        <td>${d.after_projected}</td>
                        <td style="text-align:center; font-weight:700; color:${deltaColor};">${deltaLabel}</td>
                        <td style="text-align:center;">${cpBefore} &rarr; ${cpAfter} (${cpDelta})</td>
                    </tr>`;
                    }).join('')}
                </tbody>
            </table>
        </div>`;
    }

    // Grade distribution + trend
    html += `<div class="sp-section sp-charts-row">
        <div class="sp-chart-half">
            <h4>Team Grade Distribution</h4>
            <div class="sp-chart-wrapper sp-chart-short"><canvas id="spGradeDistChart"></canvas></div>
        </div>
        <div class="sp-chart-half">
            <h4>Performance Trend (30 Days)</h4>
            <div class="sp-chart-wrapper sp-chart-short"><canvas id="spTrendChart"></canvas></div>
        </div>
    </div>`;

    container.innerHTML = html;

    renderGradeDistChart(data.team_grade_distribution || {});
    loadAndRenderTrend();
}

function renderGradeDistChart(distribution) {
    const ctx = document.getElementById('spGradeDistChart');
    if (!ctx) return;
    destroyChart('gradeDist');

    const grades = ['A', 'B', 'C', 'D', 'F'];
    const values = grades.map(g => distribution[g] || 0);
    const colors = ['#059669', '#2563eb', '#d97706', '#ea580c', '#dc2626'];

    spCharts['gradeDist'] = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: grades,
            datasets: [{
                label: 'Teams',
                data: values,
                backgroundColor: colors,
                borderRadius: 6,
                borderSkipped: false
            }]
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                y: { beginAtZero: true, ticks: { stepSize: 1 }, title: { display: true, text: 'Number of Teams' } },
                x: { title: { display: true, text: 'Performance Index Grade' } }
            }
        }
    });
}

async function loadAndRenderTrend() {
    const ctx = document.getElementById('spTrendChart');
    if (!ctx) return;
    destroyChart('trend');

    try {
        const response = await fetch('/api/shift-performance/factory-trend?days=30');
        if (!response.ok) throw new Error('No trend data');
        const data = await response.json();

        if (!data.trend || data.trend.length === 0) {
            spCharts['trend'] = new Chart(ctx, {
                type: 'line',
                data: { labels: ['No data'], datasets: [{ label: 'PI', data: [0] }] },
                options: { responsive: true, maintainAspectRatio: false }
            });
            return;
        }

        const labels = data.trend.map(t => `${t.shift_date} S${t.shift_number}`);
        const scores = data.trend.map(t => t.score_percentage);

        spCharts['trend'] = new Chart(ctx, {
            type: 'line',
            data: {
                labels,
                datasets: [{
                    label: 'Performance Index',
                    data: scores,
                    borderColor: '#2563eb',
                    backgroundColor: 'rgba(37, 99, 235, 0.1)',
                    fill: true, tension: 0.3, pointRadius: 3
                }]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    y: { beginAtZero: true, max: 110, title: { display: true, text: 'Performance Index' } },
                    x: { ticks: { maxTicksLimit: 10, font: { size: 10 } } }
                }
            }
        });
    } catch (e) {
        console.log('No trend data available yet');
    }
}

// ============================================================================
// TAB 4: SCHEDULE IMPACT VIEW
// ============================================================================

function renderScheduleImpact() {
    const data = spPerformanceData;
    const container = document.getElementById('sp-schedule-impact-content');
    if (!container) return;

    const factory = data.factory || {};
    const impact = factory.schedule_impact || {};
    const deliveryImpact = factory.impact || {};
    const allTeams = (data.teams || []).filter(t => !t.no_tasks_planned);

    let allImpact = [];
    for (const team of allTeams) {
        const si = team.schedule_impact || {};
        allImpact = allImpact.concat(si.all_incomplete_impact || []);
    }
    allImpact.sort((a, b) => (b.cascade_blocked || 0) - (a.cascade_blocked || 0));

    let html = '';

    // Impact summary
    html += `<div class="sp-impact-summary">
        <div class="sp-impact-stat">
            <div class="sp-impact-num" style="color: #dc2626;">${safe(impact.total_incomplete_tasks, 0)}</div>
            <div class="sp-impact-label">Tasks Not Completed</div>
        </div>
        <div class="sp-impact-stat">
            <div class="sp-impact-num" style="color: #d97706;">${safe(impact.total_blocked_successors, 0)}</div>
            <div class="sp-impact-label">Downstream Tasks Blocked</div>
        </div>
        <div class="sp-impact-stat">
            <div class="sp-impact-num">${(deliveryImpact.aircraft_deltas || []).length}</div>
            <div class="sp-impact-label">Aircraft Tracked</div>
        </div>
        <div class="sp-impact-stat">
            <div class="sp-impact-num" style="color: ${safe(deliveryImpact.net_hours, 0) <= 0 ? '#059669' : '#dc2626'};">
                ${safe(deliveryImpact.net_hours, 0) > 0 ? '+' : ''}${safe(deliveryImpact.net_hours, 0)} hrs
            </div>
            <div class="sp-impact-label">Net Schedule Slide (total)</div>
        </div>
        <div class="sp-impact-stat">
            <div class="sp-impact-num" style="color: ${safe(deliveryImpact.avg_hours_per_aircraft, 0) <= 0 ? '#059669' : '#dc2626'};">
                ${safe(deliveryImpact.avg_hours_per_aircraft, 0) > 0 ? '+' : ''}${safe(deliveryImpact.avg_hours_per_aircraft, 0)} hrs
            </div>
            <div class="sp-impact-label">Avg Slide per Aircraft</div>
        </div>
    </div>`;

    // Aircraft delivery table
    const deltas = deliveryImpact.aircraft_deltas || [];
    if (deltas.length > 0) {
        html += `<div class="sp-section">
            <h4>Aircraft Delivery Projections</h4>
            <p class="sp-section-desc">Compares each aircraft's last scheduled job completion between before and after optimizer runs. Hours Slide is the wall-clock time difference.</p>
            <table class="sp-table">
                <thead><tr>
                    <th>Aircraft</th><th>Delivery Due</th>
                    <th>Before Completion</th><th>After Completion</th>
                    <th>Hours Slide</th><th>Lateness</th><th>Tasks Completed</th>
                </tr></thead>
                <tbody>
                    ${deltas.map(d => {
                        const hrs = d.hours_delta;
                        const hasHrs = hrs !== undefined;
                        // hours_delta: wall-clock hours; positive = pushed later, negative = pulled earlier
                        const slideLabel = hasHrs
                            ? (Math.abs(hrs) >= 0.1 ? `${hrs > 0 ? '+' : ''}${hrs} hrs` : 'held')
                            : `${deltaArrow(d.lateness_delta)} days`;
                        const slideColor = hasHrs
                            ? (hrs < -0.1 ? '#059669' : hrs > 0.1 ? '#dc2626' : '#6b7280')
                            : (d.lateness_delta > 0 ? '#059669' : d.lateness_delta < 0 ? '#dc2626' : '#6b7280');
                        const beforeLabel = d.before_completion_dt || '—';
                        const afterLabel = d.after_completion_dt || '—';
                        return `<tr>
                        <td><strong>${d.name}</strong></td>
                        <td>${d.delivery_date}</td>
                        <td>${beforeLabel}</td>
                        <td>${afterLabel}</td>
                        <td style="font-weight:700; color:${slideColor};">${slideLabel}</td>
                        <td>${d.before_lateness}d → ${d.after_lateness}d</td>
                        <td style="text-align:center;">${d.tasks_removed}</td>
                    </tr>`;
                    }).join('')}
                </tbody>
            </table>
        </div>`;
    }

    // Most impactful misses
    const topMisses = (impact.most_impactful_misses || []).slice(0, 10);
    if (topMisses.length > 0) {
        html += `<div class="sp-section">
            <h4>Most Impactful Misses (Top 10)</h4>
            <p class="sp-section-desc">Incomplete tasks blocking the most downstream work. Prioritize resolving these first.</p>
            <table class="sp-table">
                <thead><tr>
                    <th>SOI</th><th>Aircraft</th><th>Team</th><th>Schedule Value</th>
                    <th>Critical?</th><th>Cascade Blocked</th><th>Delay Reason</th>
                </tr></thead>
                <tbody>
                    ${topMisses.map(m => `<tr>
                        <td><strong>${m.soi}</strong></td>
                        <td>Line ${m.line_number}</td>
                        <td>${m.team}</td>
                        <td>${(m.priority_score || 0).toLocaleString()}</td>
                        <td style="text-align:center;">${m.isCritical ? '<span style="color:#dc2626;font-weight:700;">YES</span>' : '-'}</td>
                        <td style="text-align:center; font-weight: 700; color: ${m.cascade_blocked > 5 ? '#dc2626' : '#d97706'};">${m.cascade_blocked}</td>
                        <td>${m.has_delay_reason ? `<span class="sp-badge-info">${m.delay_reason}</span>` : '<span class="sp-badge-warn">None</span>'}</td>
                    </tr>`).join('')}
                </tbody>
            </table>
        </div>`;
    }

    // Full incomplete list (collapsible)
    if (allImpact.length > 0) {
        html += `<div class="sp-section">
            <h4>All Incomplete Tasks (${allImpact.length})</h4>
            <button class="sp-btn-secondary" onclick="toggleElement('spFullImpactTable')">Show/Hide Full List</button>
            <div id="spFullImpactTable" style="display: none; margin-top: 12px;">
                <table class="sp-table sp-table-compact">
                    <thead><tr>
                        <th>SOI</th><th>Aircraft</th><th>Team</th><th>Duration</th><th>Value</th>
                        <th>Blocked</th><th>Reason</th>
                    </tr></thead>
                    <tbody>
                        ${allImpact.map(m => `<tr>
                            <td>${m.soi}</td>
                            <td>Ln ${m.line_number}</td>
                            <td>${m.team}</td>
                            <td>${m.duration} min</td>
                            <td>${(m.priority_score || 0).toLocaleString()}</td>
                            <td style="text-align:center;">${m.cascade_blocked}</td>
                            <td>${m.has_delay_reason ? m.delay_reason : '<span class="sp-badge-warn">None</span>'}</td>
                        </tr>`).join('')}
                    </tbody>
                </table>
            </div>
        </div>`;
    }

    // Aircraft blocking breakdown
    const aircraftImpact = impact.aircraft_impact || {};
    const aircraftEntries = Object.entries(aircraftImpact).sort((a, b) => b[1].blocked_count - a[1].blocked_count);
    if (aircraftEntries.length > 0) {
        html += `<div class="sp-section">
            <h4>Aircraft Blocking Breakdown</h4>
            <div class="sp-aircraft-impact-detail">
                ${aircraftEntries.map(([ln, info]) => `
                    <div class="sp-aircraft-detail-card">
                        <div class="sp-aircraft-detail-header">
                            <span class="sp-aircraft-detail-ln">Line ${ln}</span>
                            <span class="sp-aircraft-detail-count" style="color: ${info.blocked_count > 5 ? '#dc2626' : '#d97706'};">
                                ${info.blocked_count} blocked downstream
                            </span>
                        </div>
                        <div class="sp-aircraft-detail-tasks">
                            ${info.tasks.length} incomplete task(s): ${info.tasks.slice(0, 5).join(', ')}${info.tasks.length > 5 ? '...' : ''}
                        </div>
                    </div>
                `).join('')}
            </div>
        </div>`;
    }

    // ---- SCHEDULE SLIDE TRACKER ----
    // Shows cumulative slide per aircraft from the stored completion history.
    // Data comes from the slide_summary attached to the performance response
    // or fetched from /api/shift-performance/slide-summary.
    html += `<div class="sp-section">
        <h4>Schedule Slide Tracker</h4>
        <p class="sp-section-desc">Tracks how each aircraft's projected completion has moved over time.
            Slide = current projected completion minus best-ever projection (high-water mark).
            Positive slide means the schedule has pushed later due to shifts not completing optimized plans.</p>
        <div id="spSlideTrackerContent"><div style="padding:16px;color:#94a3b8;">Loading slide data...</div></div>
    </div>`;

    // Chart placeholder for slide sparklines
    html += `<div class="sp-section" id="spSlideChartSection" style="display:none;">
        <h4>Completion Projection Trend</h4>
        <p class="sp-section-desc">Per-aircraft projected completion over recent shifts. Flat or downward = on track. Upward = sliding.</p>
        <div class="sp-chart-wrapper" style="height:300px;"><canvas id="spSlideChart"></canvas></div>
    </div>`;

    container.innerHTML = html;

    // Load slide data asynchronously after rendering the rest of the tab
    loadSlideTracker();
}

async function loadSlideTracker() {
    const slideContainer = document.getElementById('spSlideTrackerContent');
    if (!slideContainer) return;

    try {
        // Try slide_summary from performance data first, otherwise fetch
        let aircraft = (spPerformanceData && spPerformanceData.slide_summary)
            ? spPerformanceData.slide_summary
            : null;

        if (!aircraft) {
            const resp = await fetch('/api/shift-performance/slide-summary');
            if (resp.ok) {
                const data = await resp.json();
                aircraft = data.aircraft || [];
            }
        }

        if (!aircraft || aircraft.length === 0) {
            slideContainer.innerHTML = '<div style="padding:16px;color:#94a3b8;">No slide history recorded yet. Slide tracking begins after the first performance calculation.</div>';
            return;
        }

        // Render slide summary table
        let html = `<table class="sp-table">
            <thead><tr>
                <th>Aircraft</th>
                <th>Delivery Date</th>
                <th>Current Projected</th>
                <th>Best-Ever (High Water)</th>
                <th>Set On</th>
                <th>Cumulative Slide</th>
                <th>Last Shift Delta</th>
                <th>Tasks Remaining</th>
                <th style="width:40px;"></th>
            </tr></thead>
            <tbody>`;

        for (const ac of aircraft) {
            const slideMin = ac.cumulative_slide_minutes || 0;
            const slideHrs = (slideMin / 60.0).toFixed(1);
            const deltaMin = ac.shift_delta_minutes || 0;
            const deltaHrs = (deltaMin / 60.0).toFixed(1);

            // Color coding: green = no slide, yellow = minor, orange = moderate, red = severe
            let slideColor = '#059669';  // green
            if (slideMin > 480) slideColor = '#dc2626';       // red: >8 hrs
            else if (slideMin > 240) slideColor = '#ea580c';  // orange: >4 hrs
            else if (slideMin > 60) slideColor = '#d97706';   // yellow: >1 hr

            let deltaColor = '#6b7280';
            if (deltaMin < -5) deltaColor = '#059669';   // improved
            else if (deltaMin > 5) deltaColor = '#dc2626'; // worsened

            const hwDate = ac.high_water_shift_date || '';
            const hwShift = ac.high_water_shift_number || '';
            const hwLabel = hwDate ? `${hwDate} S${hwShift}` : '—';

            html += `<tr>
                <td><strong>Line ${ac.line_number}</strong></td>
                <td>${ac.delivery_date || '—'}</td>
                <td>${ac.current_projected || '—'}</td>
                <td>${ac.high_water_projected || '—'}</td>
                <td style="font-size:11px;color:#64748b;">${hwLabel}</td>
                <td style="font-weight:700; color:${slideColor};">
                    ${slideMin === 0 ? 'On track' : `+${slideHrs} hrs`}
                    ${slideMin > 0 ? `<span style="font-size:10px;color:#94a3b8;"> (${slideMin}m)</span>` : ''}
                </td>
                <td style="font-weight:600; color:${deltaColor};">
                    ${Math.abs(deltaMin) <= 5 ? 'held' : `${deltaMin > 0 ? '+' : ''}${deltaHrs} hrs`}
                </td>
                <td style="text-align:center;">${ac.total_tasks_remaining || '—'}</td>
                <td><button class="sp-btn-secondary" style="padding:2px 8px;font-size:11px;"
                    onclick="loadSlideHistory(${ac.line_number})">trend</button></td>
            </tr>`;
        }
        html += '</tbody></table>';
        slideContainer.innerHTML = html;

    } catch (e) {
        console.error('Error loading slide tracker:', e);
        slideContainer.innerHTML = `<div style="padding:16px;color:#dc2626;">Error loading slide data: ${e.message}</div>`;
    }
}

async function loadSlideHistory(lineNumber) {
    const chartSection = document.getElementById('spSlideChartSection');
    if (!chartSection) return;
    chartSection.style.display = 'block';
    chartSection.scrollIntoView({ behavior: 'smooth' });

    try {
        const resp = await fetch(`/api/shift-performance/slide-history/${lineNumber}?shifts=90`);
        if (!resp.ok) throw new Error('Failed to load history');
        const data = await resp.json();

        if (!data.history || data.history.length === 0) {
            chartSection.innerHTML = `<h4>Completion Projection Trend</h4>
                <p style="color:#94a3b8;">No history for Line ${lineNumber}</p>`;
            return;
        }

        // Re-render the section with title
        const hwAbs = data.high_water ? data.high_water.completion_abs_minutes : null;
        const slideHrs = (data.cumulative_slide_minutes / 60.0).toFixed(1);
        chartSection.innerHTML = `
            <h4>Line ${lineNumber} — Projected Completion Over Time</h4>
            <p class="sp-section-desc">
                High-water (best): <strong>${data.high_water ? data.high_water.projected_completion : '—'}</strong>
                (set ${data.high_water ? data.high_water.shift_date + ' S' + data.high_water.shift_number : '—'})
                &nbsp;|&nbsp; Current: <strong>${data.current ? data.current.projected_completion : '—'}</strong>
                &nbsp;|&nbsp; Cumulative slide: <strong style="color:${data.cumulative_slide_minutes > 0 ? '#dc2626' : '#059669'};">
                    ${data.cumulative_slide_minutes === 0 ? 'none' : '+' + slideHrs + ' hrs'}</strong>
            </p>
            <div class="sp-chart-wrapper" style="height:300px;"><canvas id="spSlideChart"></canvas></div>`;

        const ctx = document.getElementById('spSlideChart');
        if (!ctx) return;
        destroyChart('slideHistory');

        const labels = data.history.map(h => {
            const d = h.shift_date || '';
            const shortDate = d.length >= 10 ? d.substring(5) : d;  // MM-DD
            return `${shortDate} S${h.shift_number}`;
        });
        const absValues = data.history.map(h => h.completion_abs_minutes);

        const datasets = [{
            label: `Line ${lineNumber} Completion (abs min)`,
            data: absValues,
            borderColor: '#2563eb',
            backgroundColor: 'rgba(37, 99, 235, 0.1)',
            fill: true,
            tension: 0.3,
            pointRadius: 3
        }];

        // Add high-water reference line
        if (hwAbs !== null) {
            datasets.push({
                label: 'High Water (best)',
                data: Array(labels.length).fill(hwAbs),
                borderColor: '#059669',
                borderDash: [6, 3],
                pointRadius: 0,
                fill: false
            });
        }

        spCharts['slideHistory'] = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: 'top' } },
                scales: {
                    y: { title: { display: true, text: 'Completion (absolute minutes)' } },
                    x: { ticks: { maxTicksLimit: 15, font: { size: 10 } } }
                }
            }
        });

    } catch (e) {
        console.error('Error loading slide history:', e);
        chartSection.innerHTML = `<h4>Error</h4><p style="color:#dc2626;">${e.message}</p>`;
    }
}

// ============================================================================
// TAB 5: SHIFT PROGRESS (Intra-Shift Timeline)
// ============================================================================

let spProgressData = null;  // Cached progress timeline data

async function renderShiftProgress() {
    const container = document.getElementById('sp-shift-progress-content');
    if (!container) return;

    // Show loading
    container.innerHTML = '<div style="text-align:center;padding:40px;color:#6b7280;">Loading shift progress...</div>';

    try {
        // Determine which shift to show progress for
        let url = '/api/shift-performance/shift-progress/latest';
        if (spShiftDate && spShiftNumber) {
            url = `/api/shift-performance/shift-progress/${spShiftDate}/${spShiftNumber}`;
        }

        const response = await fetch(url);
        if (!response.ok) throw new Error('Failed to load shift progress');

        const data = await response.json();
        spProgressData = data;

        const timeline = data.timeline || [];
        const shiftLabel = data.shift_label || '?';
        const workDay = data.work_day || '';
        const displayDate = workDay.replace(/(\d{4})(\d{2})(\d{2})/, '$2/$3/$1');

        let html = '';

        // Header
        html += `<div class="sp-progress-header">
            <h3 class="sp-section-title">${shiftLabel} Shift Progress — Work Day ${displayDate}</h3>
            <div class="sp-progress-plan-info">Plan: ${data.plan_filename || 'N/A'}</div>
        </div>`;

        if (timeline.length === 0) {
            html += `<div class="sp-progress-empty">
                <p>No progress data yet for this shift.</p>
                <p style="color:#6b7280;font-size:13px;">
                    Run <code>python capture_snapshot.py</code> during the shift to capture progress checkpoints.
                </p>
            </div>`;
            container.innerHTML = html;
            return;
        }

        // Summary cards (if we have snapshots)
        const latest = data.latest || {};
        const pace = data.pace || {};
        const paceColors = {ahead: '#059669', on_pace: '#2563eb', behind: '#dc2626', no_data: '#6b7280'};
        const paceLabels = {ahead: 'Ahead of Pace', on_pace: 'On Pace', behind: 'Behind Pace', no_data: 'No Data'};
        const paceColor = paceColors[pace.status] || '#6b7280';

        html += `<div class="sp-progress-summary">
            <div class="sp-progress-card">
                <div class="sp-progress-card-val">${latest.completed || 0}</div>
                <div class="sp-progress-card-lbl">Tasks Completed</div>
            </div>
            <div class="sp-progress-card">
                <div class="sp-progress-card-val">${latest.total_planned || 0}</div>
                <div class="sp-progress-card-lbl">Tasks Planned</div>
            </div>
            <div class="sp-progress-card">
                <div class="sp-progress-card-val" style="color:${paceColor};">${latest.completion_pct || 0}%</div>
                <div class="sp-progress-card-lbl">Completion</div>
            </div>
            <div class="sp-progress-card">
                <div class="sp-progress-card-val" style="color:${paceColor};">${paceLabels[pace.status] || '—'}</div>
                <div class="sp-progress-card-lbl">Pace (${pace.delta_pct > 0 ? '+' : ''}${(pace.delta_pct || 0).toFixed(0)}%)</div>
            </div>
            <div class="sp-progress-card">
                <div class="sp-progress-card-val">${data.total_checkpoints || 0}</div>
                <div class="sp-progress-card-lbl">Check-ins</div>
            </div>
        </div>`;

        // Progress bar
        const pct = latest.completion_pct || 0;
        html += `<div class="sp-progress-bar-container">
            <div class="sp-progress-bar-track">
                <div class="sp-progress-bar-fill" style="width:${pct}%;background:${paceColor};"></div>
            </div>
            <div class="sp-progress-bar-labels">
                <span>0%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span>
            </div>
        </div>`;

        // Timeline visualization
        html += '<div class="sp-progress-timeline">';
        const totalPlanned = timeline[0]?.total_planned || 1;

        for (let i = 0; i < timeline.length; i++) {
            const point = timeline[i];
            const isPlan = point.type === 'plan';
            const prevPct = i > 0 ? timeline[i - 1].completion_pct : 0;
            const deltaTasks = point.completed - (i > 0 ? timeline[i - 1].completed : 0);

            html += `<div class="sp-timeline-point ${isPlan ? 'sp-timeline-plan' : 'sp-timeline-snapshot'}">
                <div class="sp-timeline-dot ${isPlan ? 'sp-timeline-dot-plan' : ''}"></div>
                <div class="sp-timeline-content">
                    <div class="sp-timeline-label">${point.label}</div>
                    <div class="sp-timeline-metrics">
                        <span class="sp-timeline-pct" style="color:${isPlan ? '#6b7280' : paceColor};">
                            ${point.completion_pct.toFixed(1)}%
                        </span>
                        <span class="sp-timeline-tasks">
                            ${point.completed}/${point.total_planned} tasks
                        </span>
                        ${!isPlan && deltaTasks > 0 ? `<span class="sp-timeline-delta">+${deltaTasks} since last</span>` : ''}
                        ${point.new_tasks > 0 ? `<span class="sp-timeline-new">${point.new_tasks} new tasks</span>` : ''}
                    </div>
                    ${!isPlan ? `<div class="sp-timeline-bar">
                        <div class="sp-timeline-bar-fill" style="width:${point.completion_pct}%;background:${paceColor};"></div>
                    </div>` : ''}
                </div>
            </div>`;

            // Connector line between points
            if (i < timeline.length - 1) {
                html += '<div class="sp-timeline-connector"></div>';
            }
        }
        html += '</div>';

        // Checkpoint table
        if (timeline.length > 1) {
            html += `<div class="sp-progress-table-section">
                <h4>Checkpoint Details</h4>
                <table class="sp-table">
                    <thead>
                        <tr>
                            <th>Time (UTC)</th>
                            <th>Completed</th>
                            <th>Remaining</th>
                            <th>Progress</th>
                            <th>New Work</th>
                            <th>File</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${timeline.map(p => `
                            <tr>
                                <td>${p.type === 'plan' ? 'Shift Start' : p.time_utc}</td>
                                <td>${p.completed}</td>
                                <td>${p.remaining}</td>
                                <td style="color:${paceColor};font-weight:600;">${p.completion_pct.toFixed(1)}%</td>
                                <td>${p.new_tasks > 0 ? '+' + p.new_tasks : '—'}</td>
                                <td style="font-size:11px;color:#6b7280;">${p.filename}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>`;
        }

        container.innerHTML = html;

    } catch (error) {
        console.error('Error loading shift progress:', error);
        container.innerHTML = `<div style="text-align:center;padding:40px;color:#dc2626;">
            Error loading shift progress: ${error.message}
        </div>`;
    }
}

// ============================================================================
// TEAM DRILL-DOWN MODAL
// ============================================================================

function showTeamDrillDown(teamName) {
    if (!spPerformanceData) return;

    const team = (spPerformanceData.teams || []).find(t => t.team === teamName);
    if (!team) return;

    const details = team.task_details || {};
    const throughput = team.throughput || {};
    const alignment = team.alignment || {};
    const impact = team.impact || {};
    const piGrade = team.pi_grade || 'N/A';

    let html = '';

    // Team header: three-metric stats
    html += `<div class="sp-modal-stats">
        <div class="sp-modal-stat">
            <div class="sp-modal-stat-val" style="color: ${gradeColor(piGrade)};">${safe(team.performance_index, 0).toFixed(0)}</div>
            <div class="sp-modal-stat-lbl">Performance Index (${piGrade})</div>
        </div>
        <div class="sp-modal-stat">
            <div class="sp-modal-stat-val">${safe(throughput.completion_rate, 0).toFixed(0)}%</div>
            <div class="sp-modal-stat-lbl">Throughput</div>
        </div>
        <div class="sp-modal-stat">
            <div class="sp-modal-stat-val">${safe(alignment.alignment_score, 0).toFixed(0)}%</div>
            <div class="sp-modal-stat-lbl">Alignment</div>
        </div>
        <div class="sp-modal-stat">
            <div class="sp-modal-stat-val">${team.tasks_completed}/${team.total_tasks_planned}</div>
            <div class="sp-modal-stat-lbl">Tasks</div>
        </div>
    </div>`;

    // PI component breakdown
    const comp = team.pi_components || {};
    html += `<div class="sp-modal-section" style="background:#f8fafc;padding:12px;border-radius:8px;margin-bottom:16px;">
        <strong>PI Breakdown:</strong>
        Throughput ${safe(comp.throughput, 0).toFixed(0)}%
        &times; Alignment ${safe(comp.alignment, 0).toFixed(0)}%
        &times; Impact ${safe(comp.impact_factor, 1).toFixed(2)}x
        = <strong style="color:${gradeColor(piGrade)};">${safe(team.performance_index, 0).toFixed(1)}</strong>
    </div>`;

    // Completed tasks
    if (details.completed && details.completed.length > 0) {
        html += `<div class="sp-modal-section">
            <h4 style="color: #059669;">Completed Tasks (${details.completed.length})</h4>
            <table class="sp-table sp-table-compact">
                <thead><tr><th>SOI</th><th>Aircraft</th><th>Duration</th><th>Schedule Value</th><th>Critical?</th><th>Successors</th></tr></thead>
                <tbody>
                    ${details.completed.map(t => `<tr>
                        <td>${t.soi}</td>
                        <td>Ln ${t.line_number}</td>
                        <td>${t.duration_minutes || t.duration || '-'} min</td>
                        <td>${(t.priority_score || 0).toLocaleString()}</td>
                        <td>${t.isCritical ? '<span style="color:#dc2626;font-weight:700;">YES</span>' : '-'}</td>
                        <td>${t.successor_count || 0}</td>
                    </tr>`).join('')}
                </tbody>
            </table>
        </div>`;
    }

    // Incomplete tasks
    if (details.incomplete && details.incomplete.length > 0) {
        html += `<div class="sp-modal-section">
            <h4 style="color: #dc2626;">Incomplete Tasks (${details.incomplete.length})</h4>
            <table class="sp-table sp-table-compact">
                <thead><tr><th>SOI</th><th>Aircraft</th><th>Duration</th><th>Schedule Value</th><th>Critical?</th><th>Delay Reason</th><th></th></tr></thead>
                <tbody>
                    ${details.incomplete.map(t => `<tr>
                        <td>${t.soi}</td>
                        <td>Ln ${t.line_number}</td>
                        <td>${t.duration_minutes || t.duration || '-'} min</td>
                        <td>${(t.priority_score || 0).toLocaleString()}</td>
                        <td>${t.isCritical ? '<span style="color:#dc2626;font-weight:700;">YES</span>' : '-'}</td>
                        <td>${t.has_delay_reason
                            ? `<span class="sp-badge-info">${t.delay_reason}</span>`
                            : '<span class="sp-badge-warn">None</span>'}</td>
                        <td>${!t.has_delay_reason
                            ? `<button class="sp-btn-sm" onclick="closeTaskDetailsModal(); showDelayReasonModal('${t.soi}', ${t.line_number}, '${teamName}', '${spShiftDate}', ${spShiftNumber});">Add Reason</button>`
                            : ''}</td>
                    </tr>`).join('')}
                </tbody>
            </table>
        </div>`;
    }

    // Unscheduled
    if (details.unscheduled && details.unscheduled.length > 0) {
        html += `<div class="sp-modal-section">
            <h4 style="color: #2563eb;">Unscheduled Completed (${details.unscheduled.length})</h4>
            <p style="font-size:12px;color:#6b7280;">Extra work not on today's plan. Tracked separately, does not inflate throughput.</p>
            <table class="sp-table sp-table-compact">
                <thead><tr><th>SOI</th><th>Aircraft</th><th>Duration</th></tr></thead>
                <tbody>
                    ${details.unscheduled.map(t => `<tr>
                        <td>${t.soi}</td>
                        <td>Ln ${t.line_number}</td>
                        <td>${t.duration_minutes || t.duration || '-'} min</td>
                    </tr>`).join('')}
                </tbody>
            </table>
        </div>`;
    }

    document.getElementById('modalTeamName').textContent = teamName;
    document.getElementById('taskDetailsContent').innerHTML = html;
    document.getElementById('taskDetailsModal').style.display = 'block';
}

function closeTaskDetailsModal() {
    document.getElementById('taskDetailsModal').style.display = 'none';
}

// ============================================================================
// TOGGLE HELPER
// ============================================================================

function toggleElement(id) {
    const el = document.getElementById(id);
    if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

// ============================================================================
// DELAY REASON HANDLING
// ============================================================================

async function loadDelayReasonCategories() {
    try {
        const response = await fetch('/api/shift-performance/delay-reasons/categories');
        if (!response.ok) return;
        const data = await response.json();
        const select = document.getElementById('delayReasonSelect');
        if (select) {
            select.innerHTML = '<option value="">Select reason...</option>' +
                data.categories.map(cat =>
                    `<option value="${cat.category_code}">${cat.category_name}</option>`
                ).join('');
        }
    } catch (e) {
        console.error('Error loading delay reasons:', e);
    }
}

function handleDelayReasonChange() {
    const delayReason = document.getElementById('delayReasonSelect').value;
    const container = document.getElementById('heldPredecessorContainer');
    if (delayReason === 'HELD_PREDECESSOR') {
        container.style.display = 'block';
        const list = document.getElementById('predecessorEntriesList');
        if (list.children.length === 0) addDelayPredecessorEntry();
    } else {
        container.style.display = 'none';
    }
}

let predecessorEntryCounter = 0;
let _predSearchTimers = {};   // Debounce timers per entry

function addDelayPredecessorEntry() {
    const list = document.getElementById('predecessorEntriesList');
    const entryId = predecessorEntryCounter++;
    list.insertAdjacentHTML('beforeend', `
        <div class="predecessor-entry" id="pred-entry-${entryId}" style="display:flex;gap:10px;align-items:start;background:#f0f8ff;padding:10px;border:1px solid #d1e9ff;border-radius:6px;margin-bottom:8px;">
            <div style="flex:1.5;position:relative;">
                <label style="font-size:11px;display:block;margin-bottom:4px;">Predecessor SOI (Optional)</label>
                <input type="text" class="pred-task-soi" id="pred-soi-input-${entryId}"
                    placeholder="Type 2+ chars to search..."
                    autocomplete="off"
                    oninput="onPredSOIInput(${entryId})"
                    style="width:100%;padding:6px;border:1px solid #d1d5db;border-radius:4px;font-size:13px;">
                <input type="hidden" class="pred-task-line" id="pred-line-input-${entryId}" value="">
                <div id="pred-soi-dropdown-${entryId}" class="pred-soi-dropdown" style="display:none;position:absolute;top:100%;left:0;right:0;z-index:1000;max-height:200px;overflow-y:auto;background:white;border:1px solid #d1d5db;border-top:none;border-radius:0 0 6px 6px;box-shadow:0 4px 12px rgba(0,0,0,0.15);"></div>
                <div id="pred-soi-selected-${entryId}" style="display:none;margin-top:4px;font-size:11px;padding:4px 8px;background:#dbeafe;border-radius:4px;color:#1e40af;">
                </div>
                <span style="font-size:10px;color:#6b7280;display:block;margin-top:2px;">Same aircraft — search by SOI</span>
            </div>
            <div style="flex:2;"><label style="font-size:11px;display:block;margin-bottom:4px;">What are you waiting for? (Required)</label>
                <textarea class="pred-notes" required placeholder="Describe what this task is waiting for..." style="width:100%;height:40px;padding:6px;border:1px solid #d1d5db;border-radius:4px;resize:vertical;"></textarea></div>
            <button type="button" onclick="removePredecessorEntry(${entryId})" style="background:#ef4444;color:white;border:none;border-radius:50%;width:24px;height:24px;cursor:pointer;font-size:14px;margin-top:22px;flex-shrink:0;">&times;</button>
        </div>`);
}

function removePredecessorEntry(id) {
    const el = document.getElementById(`pred-entry-${id}`);
    if (el) el.remove();
    if (_predSearchTimers[id]) clearTimeout(_predSearchTimers[id]);
}

function onPredSOIInput(entryId) {
    // Debounce: wait 250ms after typing stops before searching
    if (_predSearchTimers[entryId]) clearTimeout(_predSearchTimers[entryId]);
    _predSearchTimers[entryId] = setTimeout(() => searchPredecessorSOI(entryId), 250);

    // Clear any previous selection when user edits the input
    const hiddenLine = document.getElementById(`pred-line-input-${entryId}`);
    if (hiddenLine) hiddenLine.value = '';
    const selectedBadge = document.getElementById(`pred-soi-selected-${entryId}`);
    if (selectedBadge) selectedBadge.style.display = 'none';
}

async function searchPredecessorSOI(entryId) {
    const input = document.getElementById(`pred-soi-input-${entryId}`);
    const dropdown = document.getElementById(`pred-soi-dropdown-${entryId}`);
    if (!input || !dropdown) return;

    const query = input.value.trim();
    if (query.length < 2) {
        dropdown.style.display = 'none';
        return;
    }

    // Get the line_number of the delayed task (predecessor is often on same aircraft)
    const delayLineEl = document.getElementById('delayLineNumber');
    const delayLine = delayLineEl ? delayLineEl.value : '';

    // Search the before-schedule for matching tasks
    if (!spBeforeScheduleFile) {
        dropdown.innerHTML = '<div style="padding:8px;color:#6b7280;font-size:12px;">No schedule loaded</div>';
        dropdown.style.display = 'block';
        return;
    }

    try {
        // Predecessors must be on the same aircraft — filter by line_number
        const lineParam = delayLine ? `&line_number=${encodeURIComponent(delayLine)}` : '';
        const url = `/api/shift-performance/search-tasks?schedule=${encodeURIComponent(spBeforeScheduleFile)}&q=${encodeURIComponent(query)}${lineParam}`;
        const response = await fetch(url);
        if (!response.ok) throw new Error('Search failed');
        const data = await response.json();
        const results = data.results || [];

        if (results.length === 0) {
            dropdown.innerHTML = `<div style="padding:8px;color:#6b7280;font-size:12px;">No matching tasks on Line ${delayLine}</div>`;
            dropdown.style.display = 'block';
            return;
        }

        results.sort((a, b) => a.soi.localeCompare(b.soi));

        let html = `<div style="padding:4px 8px;background:#dbeafe;font-size:10px;font-weight:700;color:#1e40af;border-bottom:1px solid #e2e8f0;">
            Line ${delayLine} — ${results.length} match${results.length !== 1 ? 'es' : ''}</div>`;
        for (const r of results) {
            html += `<div class="pred-soi-option" onclick="selectPredecessorSOI(${entryId}, '${r.soi.replace(/'/g, "\\'")}', ${r.line_number}, '${(r.team || '').replace(/'/g, "\\'")}')"
                style="padding:6px 8px;cursor:pointer;font-size:12px;border-bottom:1px solid #f1f5f9;display:flex;justify-content:space-between;align-items:center;"
                onmouseenter="this.style.background='#eff6ff'" onmouseleave="this.style.background='white'">
                <span style="font-weight:600;color:#1e293b;">${r.soi}</span>
                <span style="color:#6b7280;font-size:11px;">${r.team ? r.team.replace('FGI-CF-','').substring(0,15) : ''} ${r.duration ? r.duration + 'min' : ''}</span>
            </div>`;
        }

        dropdown.innerHTML = html;
        dropdown.style.display = 'block';
    } catch (e) {
        console.error('Predecessor search error:', e);
        dropdown.innerHTML = '<div style="padding:8px;color:#dc2626;font-size:12px;">Search error</div>';
        dropdown.style.display = 'block';
    }
}

function selectPredecessorSOI(entryId, soi, lineNumber, team) {
    const input = document.getElementById(`pred-soi-input-${entryId}`);
    const hiddenLine = document.getElementById(`pred-line-input-${entryId}`);
    const dropdown = document.getElementById(`pred-soi-dropdown-${entryId}`);
    const selectedBadge = document.getElementById(`pred-soi-selected-${entryId}`);

    if (input) input.value = soi;
    if (hiddenLine) hiddenLine.value = lineNumber;
    if (dropdown) dropdown.style.display = 'none';
    if (selectedBadge) {
        selectedBadge.innerHTML = `<strong>${soi}</strong> — Line ${lineNumber}` + (team ? ` (${team.replace('FGI-CF-','')})` : '');
        selectedBadge.style.display = 'block';
    }
}

// Close predecessor dropdowns when clicking outside
document.addEventListener('click', function(e) {
    if (!e.target.closest('.predecessor-entry')) {
        document.querySelectorAll('.pred-soi-dropdown').forEach(d => d.style.display = 'none');
    }
});

function showDelayReasonModal(taskSOI, lineNumber, team, shiftDate, shiftNumber) {
    document.getElementById('delayTaskSOI').value = taskSOI;
    document.getElementById('delayLineNumber').value = lineNumber;
    document.getElementById('delayShiftDate').value = shiftDate;
    document.getElementById('delayShiftNumber').value = shiftNumber;
    document.getElementById('delayTeam').value = team;
    document.getElementById('delayTaskDisplay').textContent = taskSOI;
    document.getElementById('delayLineDisplay').textContent = `Line ${lineNumber}`;
    document.getElementById('delayReasonSelect').value = '';
    document.getElementById('delayNotes').value = '';
    document.getElementById('delayEnteredBy').value = '';
    document.getElementById('heldPredecessorContainer').style.display = 'none';
    document.getElementById('predecessorEntriesList').innerHTML = '';
    predecessorEntryCounter = 0;
    document.getElementById('delayReasonModal').style.display = 'block';
}

function closeDelayReasonModal() {
    document.getElementById('delayReasonModal').style.display = 'none';
}

async function submitDelayReason(event) {
    event.preventDefault();
    const delayReason = document.getElementById('delayReasonSelect').value;
    const enteredBy = document.getElementById('delayEnteredBy').value.trim();
    if (!enteredBy) { alert('Please enter who is submitting'); document.getElementById('delayEnteredBy').focus(); return; }

    const formData = {
        task_soi: document.getElementById('delayTaskSOI').value,
        line_number: parseInt(document.getElementById('delayLineNumber').value),
        shift_date: document.getElementById('delayShiftDate').value,
        shift_number: parseInt(document.getElementById('delayShiftNumber').value),
        team: document.getElementById('delayTeam').value,
        delay_reason: delayReason,
        notes: document.getElementById('delayNotes').value,
        entered_by: enteredBy
    };

    if (delayReason === 'HELD_PREDECESSOR') {
        const entries = document.querySelectorAll('.predecessor-entry');
        if (entries.length === 0) { alert('Add at least one predecessor entry'); return; }
        const preds = [];
        for (const entry of entries) {
            const notes = entry.querySelector('.pred-notes').value.trim();
            if (!notes) { alert('Each predecessor needs notes'); entry.querySelector('.pred-notes').focus(); return; }
            const predSOI = entry.querySelector('.pred-task-soi').value.trim() || null;
            const predLine = entry.querySelector('.pred-task-line');
            const predLineNum = predLine && predLine.value ? parseInt(predLine.value) : null;
            preds.push({
                predecessor_task_soi: predSOI,
                predecessor_line_number: predLineNum,
                notes
            });
        }
        formData.held_predecessors = preds;
    }

    try {
        const response = await fetch('/api/shift-performance/delay-reasons', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(formData)
        });
        if (!response.ok) { const err = await response.json(); throw new Error(err.error || 'Failed'); }
        alert('Delay reason saved successfully');
        closeDelayReasonModal();
        autoLoadLatestPerformance();
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

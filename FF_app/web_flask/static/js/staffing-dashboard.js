// staffing-dashboard.js - Staffing Dashboard with Peak Demand and Utilization Analysis

// Global state for staffing dashboard
let staffingState = {
    filterOptions: null,
    peakDemandChart: null,
    utilizationChart: null,
    referenceDate: null,   // populated from API response
    currentFilters: {
        superintendents: [],
        shifts: [],
        teams: [],
        roles: []
    }
};

/**
 * Convert a day-number (offset from reference_date) to a short date string.
 * Returns "Mon Jun 23" style if referenceDate is set, else "Day 150".
 */
function dayToDateLabel(dayNum) {
    if (!staffingState.referenceDate) return `Day ${dayNum}`;
    const d = new Date(staffingState.referenceDate);
    d.setDate(d.getDate() + parseInt(dayNum));
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    return `${days[d.getDay()]} ${months[d.getMonth()]} ${d.getDate()}`;
}

// Color palette for teams (stacked bar chart)
const TEAM_COLORS = [
    '#003581', '#0066CC', '#3399FF', '#66CCFF',
    '#FF6B35', '#FF9F1C', '#FFBF69', '#F4A261',
    '#2A9D8F', '#264653', '#E76F51', '#E9C46A',
    '#9D4EDD', '#7209B7', '#560BAD', '#3C096C',
    '#06FFA5', '#1B998B', '#2D6A4F', '#40916C',
    '#EF476F', '#F78C6B', '#FFD166', '#06D6A0'
];

// Initialize staffing dashboard
function initializeStaffingDashboard() {
    // Guard: don't reinitialize if already set up for the current scenario
    // This prevents filter dropdowns from being cleared every time the view is shown
    const staffingView = document.getElementById('staffing-view');
    const currentScenarioId = (typeof currentScenario !== 'undefined') ? currentScenario : '';
    if (staffingView && staffingView.getAttribute('data-staffing-init') === currentScenarioId) {
        console.log('Staffing Dashboard already initialized for this scenario, skipping');
        return;
    }

    console.log('Initializing Staffing Dashboard...');

    // Load filter options
    loadFilterOptions();

    // Set up event listeners
    setupStaffingEventListeners();

    // Mark as initialized for this scenario
    if (staffingView) {
        staffingView.setAttribute('data-staffing-init', currentScenarioId);
    }
}

// Set up event listeners for staffing dashboard
function setupStaffingEventListeners() {
    // Apply Filters button
    const applyBtn = document.getElementById('applyFiltersBtn');
    if (applyBtn) {
        applyBtn.addEventListener('click', applyStaffingFilters);
    }

    // Clear Filters button
    const clearBtn = document.getElementById('clearFiltersBtn');
    if (clearBtn) {
        clearBtn.addEventListener('click', clearStaffingFilters);
    }

    // Handle select-all functionality for each multi-select
    const selects = ['superFilter', 'shiftFilter', 'teamFilter', 'roleFilter'];
    selects.forEach(selectId => {
        const select = document.getElementById(selectId);
        if (select) {
            select.addEventListener('change', (e) => handleMultiSelectChange(e, selectId));
        }
    });
}

// Handle multi-select change (select/deselect ALL)
// Track previous selection state to detect which option was just toggled
const _prevSelections = {};

function handleMultiSelectChange(event, selectId) {
    const select = event.target;
    const options = Array.from(select.options);
    const allOption = options.find(opt => opt.value === 'ALL');

    if (!allOption) return;

    const selectedValues = Array.from(select.selectedOptions).map(opt => opt.value);
    const prevValues = _prevSelections[selectId] || ['ALL'];

    // Determine what just changed
    const allWasSelected = prevValues.includes('ALL');
    const allIsSelected = selectedValues.includes('ALL');
    const nonAllValues = selectedValues.filter(v => v !== 'ALL');

    if (allIsSelected && nonAllValues.length > 0) {
        if (allWasSelected) {
            // ALL was already selected, user added a specific item → deselect ALL, keep specific
            allOption.selected = false;
        } else {
            // User just added ALL while specific items selected → switch to ALL only
            options.forEach(opt => { opt.selected = false; });
            allOption.selected = true;
        }
    }

    // If nothing is selected at all, revert to ALL
    if (Array.from(select.selectedOptions).length === 0) {
        allOption.selected = true;
    }

    // Store current selection for next comparison
    _prevSelections[selectId] = Array.from(select.selectedOptions).map(opt => opt.value);
}

// Load filter options from API
async function loadFilterOptions() {
    try {
        const response = await fetch('/api/staffing-dashboard/filter-options');
        if (!response.ok) {
            throw new Error('Failed to load filter options');
        }

        const data = await response.json();
        staffingState.filterOptions = data;

        // Populate filter dropdowns
        populateFilterDropdown('superFilter', data.superintendents);
        populateFilterDropdown('teamFilter', data.teams);

        console.log('Filter options loaded:', data);
    } catch (error) {
        console.error('Error loading filter options:', error);
    }
}

// Populate a filter dropdown with options (preserving current selections)
function populateFilterDropdown(selectId, options) {
    const select = document.getElementById(selectId);
    if (!select) return;

    // Save current selections before rebuilding
    const savedSelections = Array.from(select.selectedOptions).map(opt => opt.value);

    // Keep the ALL option, add new options
    const allOption = select.querySelector('option[value="ALL"]');
    select.innerHTML = '';
    if (allOption) {
        select.appendChild(allOption);
    }

    options.forEach(option => {
        const optionEl = document.createElement('option');
        optionEl.value = option;
        optionEl.textContent = option;
        select.appendChild(optionEl);
    });

    // Restore saved selections
    if (savedSelections.length > 0) {
        const allOptions = Array.from(select.options);
        let restored = false;
        savedSelections.forEach(val => {
            const match = allOptions.find(opt => opt.value === val);
            if (match) {
                match.selected = true;
                restored = true;
            }
        });
        // If none of the saved values were found, select ALL
        if (!restored && allOption) {
            allOption.selected = true;
        }
    }
}

// Apply staffing filters
async function applyStaffingFilters() {
    console.log('Applying staffing filters...');

    // Show loading state
    showStaffingLoading(true);

    // Get selected filters
    const filters = getSelectedFilters();
    staffingState.currentFilters = filters;

    try {
        // Fetch peak demand and utilization data
        const response = await fetch('/api/staffing-dashboard/peak-demand-utilization', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(filters)
        });

        if (!response.ok) {
            throw new Error('Failed to fetch staffing data');
        }

        const data = await response.json();

        // Store reference_date for day-to-date conversion
        if (data.reference_date) {
            staffingState.referenceDate = data.reference_date;
        }

        // Update UI with data
        updateSummaryCards(data.summary);
        updatePeakDemandChart(data.peak_demand_by_day);
        updateUtilizationChart(data.utilization_by_day);
        updateDataTable(data.utilization_by_day, data.peak_demand_by_day);

        // Update utilization alerts (v1.4.2 enhancement)
        if (typeof updateUtilizationAlerts === 'function' && data.utilization_by_team) {
            updateUtilizationAlerts(data.utilization_by_team);
        }

        console.log('Staffing data loaded:', data);
    } catch (error) {
        console.error('Error loading staffing data:', error);
        alert('Failed to load staffing data. Please try again.');
    } finally {
        showStaffingLoading(false);
    }
}

// Get selected filters from dropdowns
function getSelectedFilters() {
    const filters = {
        superintendents: [],
        shifts: [],
        teams: [],
        roles: []
    };

    // Superintendent
    const superSelect = document.getElementById('superFilter');
    if (superSelect) {
        const selected = Array.from(superSelect.selectedOptions).map(opt => opt.value);
        if (!selected.includes('ALL')) {
            filters.superintendents = selected;
        }
    }

    // Shift (convert to numbers)
    const shiftSelect = document.getElementById('shiftFilter');
    if (shiftSelect) {
        const selected = Array.from(shiftSelect.selectedOptions).map(opt => opt.value);
        if (!selected.includes('ALL')) {
            filters.shifts = selected.map(Number);
        }
    }

    // Team
    const teamSelect = document.getElementById('teamFilter');
    if (teamSelect) {
        const selected = Array.from(teamSelect.selectedOptions).map(opt => opt.value);
        if (!selected.includes('ALL')) {
            filters.teams = selected;
        }
    }

    // Role
    const roleSelect = document.getElementById('roleFilter');
    if (roleSelect) {
        const selected = Array.from(roleSelect.selectedOptions).map(opt => opt.value);
        if (!selected.includes('ALL')) {
            filters.roles = selected;
        }
    }

    return filters;
}

// Clear all filters
function clearStaffingFilters() {
    // Reset all selects to "ALL"
    const selects = ['superFilter', 'shiftFilter', 'teamFilter', 'roleFilter'];
    selects.forEach(selectId => {
        const select = document.getElementById(selectId);
        if (select) {
            Array.from(select.options).forEach(opt => opt.selected = false);
            const allOption = select.querySelector('option[value="ALL"]');
            if (allOption) {
                allOption.selected = true;
            }
        }
    });

    // Clear current data
    staffingState.currentFilters = {
        superintendents: [],
        shifts: [],
        teams: [],
        roles: []
    };

    // Clear summary cards
    document.getElementById('avgPeakDemand').textContent = '-';
    document.getElementById('maxPeakDemand').textContent = '-';
    document.getElementById('staffingAvgUtilization').textContent = '-';
    document.getElementById('totalWorkHours').textContent = '-';

    // Clear charts
    if (staffingState.peakDemandChart) {
        staffingState.peakDemandChart.destroy();
        staffingState.peakDemandChart = null;
    }
    if (staffingState.utilizationChart) {
        staffingState.utilizationChart.destroy();
        staffingState.utilizationChart = null;
    }

    // Clear table
    const tableBody = document.getElementById('staffingTableBody');
    if (tableBody) {
        tableBody.innerHTML = '<tr><td colspan="5" style="padding: 20px; text-align: center; color: #999;">Click "Apply Filters" to load data</td></tr>';
    }
}

// Update summary cards
function updateSummaryCards(summary) {
    document.getElementById('avgPeakDemand').textContent = summary.avg_peak_demand.toFixed(1);
    document.getElementById('maxPeakDemand').textContent = summary.max_peak_demand;
    document.getElementById('staffingAvgUtilization').textContent = summary.avg_utilization.toFixed(1) + '%';
    document.getElementById('totalWorkHours').textContent = summary.total_work_hours.toLocaleString();
}

// Update peak demand stacked bar chart
function updatePeakDemandChart(peakDemandByDay) {
    const ctx = document.getElementById('peakDemandChart');
    if (!ctx) return;

    // Destroy existing chart
    if (staffingState.peakDemandChart) {
        staffingState.peakDemandChart.destroy();
    }

    // Prepare data for stacked bar chart
    const days = Object.keys(peakDemandByDay).sort((a, b) => parseInt(a) - parseInt(b));
    const teams = new Set();

    // Collect all unique teams
    days.forEach(day => {
        const dayData = peakDemandByDay[day];
        if (dayData && dayData.by_team) {
            Object.keys(dayData.by_team).forEach(team => teams.add(team));
        }
    });

    const teamsList = Array.from(teams).sort();

    // Build datasets (one per team)
    const datasets = teamsList.map((team, index) => {
        const data = days.map(day => {
            const dayData = peakDemandByDay[day];
            return dayData && dayData.by_team ? (dayData.by_team[team] || 0) : 0;
        });

        return {
            label: team,
            data: data,
            backgroundColor: TEAM_COLORS[index % TEAM_COLORS.length],
            borderColor: TEAM_COLORS[index % TEAM_COLORS.length],
            borderWidth: 1
        };
    });

    // Create chart
    staffingState.peakDemandChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: days.map(d => dayToDateLabel(d)),
            datasets: datasets
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            scales: {
                x: {
                    stacked: true,
                    title: {
                        display: true,
                        text: 'Date'
                    }
                },
                y: {
                    stacked: true,
                    beginAtZero: true,
                    title: {
                        display: true,
                        text: 'Peak Demand (Mechanics)'
                    },
                    ticks: {
                        precision: 0
                    }
                }
            },
            plugins: {
                legend: {
                    display: true,
                    position: 'bottom',
                    labels: {
                        boxWidth: 15,
                        padding: 10,
                        font: {
                            size: 10
                        }
                    }
                },
                title: {
                    display: false
                },
                tooltip: {
                    mode: 'index',
                    intersect: false
                }
            }
        }
    });
}

// Update utilization line chart
function updateUtilizationChart(utilizationByDay) {
    const ctx = document.getElementById('utilizationChart');
    if (!ctx) return;

    // Destroy existing chart
    if (staffingState.utilizationChart) {
        staffingState.utilizationChart.destroy();
    }

    // Prepare data
    const days = Object.keys(utilizationByDay).sort((a, b) => parseInt(a) - parseInt(b));
    const utilizationData = days.map(day => utilizationByDay[day].utilization_pct);

    // Create chart
    staffingState.utilizationChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: days.map(d => dayToDateLabel(d)),
            datasets: [{
                label: 'Utilization %',
                data: utilizationData,
                borderColor: '#28a745',
                backgroundColor: 'rgba(40, 167, 69, 0.1)',
                fill: true,
                tension: 0.3,
                pointRadius: 4,
                pointHoverRadius: 6
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            scales: {
                x: {
                    title: {
                        display: true,
                        text: 'Date'
                    }
                },
                y: {
                    beginAtZero: true,
                    max: 100,
                    title: {
                        display: true,
                        text: 'Utilization %'
                    },
                    ticks: {
                        callback: function(value) {
                            return value + '%';
                        }
                    }
                }
            },
            plugins: {
                legend: {
                    display: true
                },
                title: {
                    display: false
                },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            return 'Utilization: ' + context.parsed.y.toFixed(2) + '%';
                        }
                    }
                }
            }
        }
    });
}

// Update data table
function updateDataTable(utilizationByDay, peakDemandByDay) {
    const tableBody = document.getElementById('staffingTableBody');
    if (!tableBody) return;

    const days = Object.keys(utilizationByDay).sort((a, b) => parseInt(a) - parseInt(b));

    const rows = days.map(day => {
        const util = utilizationByDay[day];
        const peak = peakDemandByDay[day];

        return `
            <tr style="border-bottom: 1px solid #dee2e6;">
                <td style="padding: 12px; text-align: left;">${dayToDateLabel(day)}</td>
                <td style="padding: 12px; text-align: right;">${peak.total}</td>
                <td style="padding: 12px; text-align: right;">${util.work_minutes.toFixed(0)}</td>
                <td style="padding: 12px; text-align: right;">${util.capacity_minutes.toFixed(0)}</td>
                <td style="padding: 12px; text-align: right; font-weight: 600; color: ${util.utilization_pct > 100 ? '#dc3545' : '#28a745'};">
                    ${util.utilization_pct.toFixed(2)}%
                </td>
            </tr>
        `;
    }).join('');

    tableBody.innerHTML = rows;
}

// Show/hide loading state
function showStaffingLoading(show) {
    const loading = document.getElementById('staffingLoadingState');
    if (loading) {
        loading.style.display = show ? 'block' : 'none';
    }
}

// Initialize when staffing view becomes active
document.addEventListener('DOMContentLoaded', function() {
    // Initialize staffing dashboard when view is shown
    const viewTabs = document.querySelectorAll('.view-tab[data-view="staffing"]');
    viewTabs.forEach(tab => {
        tab.addEventListener('click', function() {
            // Small delay to ensure DOM is ready
            setTimeout(initializeStaffingDashboard, 100);
        });
    });
});

// Make functions globally available for dashboard-js.js integration
window.initializeStaffingDashboard = initializeStaffingDashboard;
window.applyStaffingFilters = applyStaffingFilters;
window.clearStaffingFilters = clearStaffingFilters;
window.exportBudgetToExcel = typeof exportBudgetToExcel !== 'undefined' ? exportBudgetToExcel : undefined;

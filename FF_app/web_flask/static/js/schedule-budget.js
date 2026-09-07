/**
 * Schedule Budget View - JavaScript
 * Handles budget chart visualization, filtering, and Excel export
 */

// Global variables
let budgetData = [];
let budgetChart = null;
let jobCountChart = null;
let allAircraft = [];
let allDates = [];
let dateRange = { min: null, max: null };

// Color scheme for budget types
const BUDGET_COLORS = {
    'Baseline': '#4CAF50',
    'Rework': '#FF9800',
    'Rework-NC': '#FF5722',
    'Rework-PU': '#FF6F00',
    'Rework-CI': '#E65100',
    'Rework-Other': '#BF360C',
    'QA Inspection': '#2196F3',
    'QA Inspection-in-process': '#1976D2',
    'QA Inspection-final': '#0D47A1',
    'QA Inspection-general': '#64B5F6',
    'Customer Inspection': '#9C27B0'
};

/**
 * Initialize the Schedule Budget view
 */
async function initScheduleBudget() {
    console.log('Initializing Schedule Budget view...');

    try {
        // Load budget data from API
        const response = await fetch('/api/schedule/budget');
        if (!response.ok) {
            throw new Error(`API error: ${response.status}`);
        }

        const data = await response.json();
        console.log('Budget data loaded:', data);

        budgetData = data.budgetData || [];
        allAircraft = data.aircraft || [];
        dateRange = data.dateRange || { min: null, max: null };

        // Extract all unique dates from budget data
        allDates = [...new Set(budgetData.map(r => r.date))].sort();

        // Initialize aircraft filters
        initializeAircraftFilters();

        // Initialize date range slider
        initializeDateRangeSlider();

        // Apply initial filters and render chart
        applyBudgetFilters();

        console.log('Schedule Budget initialized successfully');
    } catch (error) {
        console.error('Error loading budget data:', error);
        alert('Failed to load budget data. Please check the console for details.');
    }
}

/**
 * Initialize aircraft filter checkboxes
 */
function initializeAircraftFilters() {
    const container = document.getElementById('aircraftFilters');
    container.innerHTML = '';

    allAircraft.forEach(aircraft => {
        const label = document.createElement('label');
        label.innerHTML = `
            <input type="checkbox" value="${aircraft}" checked>
            Line ${aircraft}
        `;
        container.appendChild(label);
    });
}

/**
 * Initialize date range slider
 */
function initializeDateRangeSlider() {
    if (allDates.length === 0) {
        return;
    }

    const slider = document.getElementById('dateRangeSlider');
    const sliderMinLabel = document.getElementById('sliderMinDate');
    const sliderMaxLabel = document.getElementById('sliderMaxDate');

    // Set slider range (0 to number of dates - 1)
    slider.min = 0;
    slider.max = allDates.length - 1;
    slider.value = allDates.length - 1; // Default to showing all dates

    // Set labels
    sliderMinLabel.textContent = allDates[0];
    sliderMaxLabel.textContent = allDates[allDates.length - 1];

    // Update date range display
    updateDateRangeDisplay(0, allDates.length - 1);

    // Add slider event listener
    slider.addEventListener('input', function() {
        updateDateRangeDisplay(0, parseInt(this.value));
    });
}

/**
 * Update date range display based on slider position
 */
function updateDateRangeDisplay(startIdx, endIdx) {
    const startDateEl = document.getElementById('dateRangeStart');
    const endDateEl = document.getElementById('dateRangeEnd');

    startDateEl.textContent = allDates[startIdx] || '-';
    endDateEl.textContent = allDates[endIdx] || '-';

    // Update slider background gradient
    const slider = document.getElementById('dateRangeSlider');
    const percent = (endIdx / (allDates.length - 1)) * 100;
    slider.style.background = `linear-gradient(to right, #4CAF50 0%, #4CAF50 ${percent}%, #ddd ${percent}%, #ddd 100%)`;
}

/**
 * Select all aircraft
 */
function selectAllAircraft() {
    const checkboxes = document.querySelectorAll('#aircraftFilters input[type="checkbox"]');
    checkboxes.forEach(cb => cb.checked = true);
}

/**
 * Deselect all aircraft
 */
function deselectAllAircraft() {
    const checkboxes = document.querySelectorAll('#aircraftFilters input[type="checkbox"]');
    checkboxes.forEach(cb => cb.checked = false);
}

/**
 * Apply budget filters and update chart
 */
function applyBudgetFilters() {
    // Get selected budget types
    const selectedTypes = [];
    document.querySelectorAll('#budgetTypeFilters input:checked').forEach(cb => {
        selectedTypes.push(cb.value);
    });

    // Get selected rework subtypes
    const selectedReworkSubtypes = [];
    document.querySelectorAll('#reworkSubtypeFilters input:checked').forEach(cb => {
        selectedReworkSubtypes.push(cb.value);
    });

    // Get selected QA subtypes
    const selectedQASubtypes = [];
    document.querySelectorAll('#qaSubtypeFilters input:checked').forEach(cb => {
        selectedQASubtypes.push(cb.value);
    });

    // Get selected aircraft (keep as strings to match record.lineNumber type)
    const selectedAircraft = [];
    document.querySelectorAll('#aircraftFilters input:checked').forEach(cb => {
        const val = cb.value;
        // Support both integer and string line numbers
        selectedAircraft.push(val);
        const parsed = parseInt(val);
        if (!isNaN(parsed)) {
            selectedAircraft.push(parsed);
        }
    });

    // Get date range from slider
    const sliderValue = parseInt(document.getElementById('dateRangeSlider').value);
    const startDateIdx = 0;
    const endDateIdx = sliderValue;
    const startDate = allDates[startDateIdx];
    const endDate = allDates[endDateIdx];

    // Filter budget data
    const filteredData = budgetData.filter(record => {
        // Filter by budget type
        if (!selectedTypes.includes(record.budgetType)) {
            return false;
        }

        // Filter by rework subtype
        if (record.budgetType === 'Rework' && record.budgetSubtype) {
            if (!selectedReworkSubtypes.includes(record.budgetSubtype)) {
                return false;
            }
        }

        // Filter by QA subtype
        if (record.budgetType === 'QA Inspection' && record.budgetSubtype) {
            if (!selectedQASubtypes.includes(record.budgetSubtype)) {
                return false;
            }
        }

        // Filter by aircraft
        if (!selectedAircraft.includes(record.lineNumber)) {
            return false;
        }

        // Filter by date range
        if (record.date < startDate || record.date > endDate) {
            return false;
        }

        return true;
    });

    // Update summary stats
    updateSummaryStats(filteredData);

    // Render chart
    renderBudgetChart(filteredData, startDate, endDate);

    // Update table
    updateBudgetTable(filteredData);
}

/**
 * Update summary statistics
 */
function updateSummaryStats(filteredData) {
    // Calculate total hours
    const totalHours = filteredData.reduce((sum, r) => sum + r.totalHours, 0);

    // Calculate baseline hours
    const baselineHours = filteredData
        .filter(r => r.budgetType === 'Baseline')
        .reduce((sum, r) => sum + r.totalHours, 0);

    // Calculate QA hours
    const qaHours = filteredData
        .filter(r => r.budgetType === 'QA Inspection')
        .reduce((sum, r) => sum + r.totalHours, 0);

    // Calculate date range string
    const dates = [...new Set(filteredData.map(r => r.date))].sort();
    const dateRangeStr = dates.length > 0
        ? `${dates[0]} to ${dates[dates.length - 1]}`
        : '-';

    // Update DOM
    document.getElementById('budget-total-hours').textContent = Math.round(totalHours);
    document.getElementById('budget-baseline-hours').textContent = Math.round(baselineHours);
    document.getElementById('budget-qa-hours').textContent = Math.round(qaHours);
    document.getElementById('budget-date-range').textContent = dateRangeStr;
}

/**
 * Render the stacked bar chart
 */
function renderBudgetChart(filteredData, startDate, endDate) {
    // Aggregate data by date and budget type/subtype
    const dataByDate = {};

    filteredData.forEach(record => {
        const date = record.date;
        if (!dataByDate[date]) {
            dataByDate[date] = {};
        }

        // Create unique key for budget type + subtype
        let key = record.budgetType;
        if (record.budgetSubtype) {
            key = `${record.budgetType}-${record.budgetSubtype}`;
        }

        if (!dataByDate[date][key]) {
            dataByDate[date][key] = 0;
        }

        dataByDate[date][key] += record.totalHours;
    });

    // Get all dates in range
    const dates = allDates.filter(d => d >= startDate && d <= endDate);

    // Get all unique budget keys
    const budgetKeys = new Set();
    Object.values(dataByDate).forEach(dateData => {
        Object.keys(dateData).forEach(key => budgetKeys.add(key));
    });

    // Sort budget keys for consistent stacking order
    const sortedBudgetKeys = Array.from(budgetKeys).sort();

    // Build datasets for Chart.js
    const datasets = sortedBudgetKeys.map(key => {
        return {
            label: key.replace('-', ' - '),
            data: dates.map(date => dataByDate[date]?.[key] || 0),
            backgroundColor: BUDGET_COLORS[key] || '#999',
            borderColor: BUDGET_COLORS[key] || '#999',
            borderWidth: 1
        };
    });

    // Destroy existing chart if it exists
    if (budgetChart) {
        budgetChart.destroy();
    }

    // Create new chart
    const budgetCanvas = document.getElementById('budgetChart');
    if (!budgetCanvas) {
        console.error('budgetChart canvas element not found');
        return;
    }
    const ctx = budgetCanvas.getContext('2d');
    budgetChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: dates,
            datasets: datasets
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                title: {
                    display: true,
                    text: 'Schedule Budget - Hours per Day by Task Type',
                    font: {
                        size: 18
                    }
                },
                legend: {
                    display: true,
                    position: 'bottom'
                },
                tooltip: {
                    mode: 'index',
                    intersect: false,
                    callbacks: {
                        footer: function(tooltipItems) {
                            let total = 0;
                            tooltipItems.forEach(item => {
                                total += item.parsed.y;
                            });
                            return 'Total: ' + Math.round(total) + ' hours';
                        }
                    }
                }
            },
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
                    title: {
                        display: true,
                        text: 'Total Hours'
                    },
                    beginAtZero: true
                }
            }
        }
    });

    // Also update the job count chart
    updateJobCountChart(filteredData);
}

/**
 * Update the job count chart (number of jobs per day)
 */
function updateJobCountChart(filteredData) {
    // Count jobs per day
    const jobsByDate = {};
    filteredData.forEach(record => {
        if (!jobsByDate[record.date]) {
            jobsByDate[record.date] = 0;
        }
        jobsByDate[record.date]++;
    });

    // Sort dates
    const dates = Object.keys(jobsByDate).sort();
    const jobCounts = dates.map(date => jobsByDate[date]);

    // Destroy existing chart if it exists
    if (jobCountChart) {
        jobCountChart.destroy();
    }

    // Create new chart
    const jobCountCanvas = document.getElementById('jobCountChart');
    if (!jobCountCanvas) {
        console.error('jobCountChart canvas element not found');
        return;
    }
    const ctx = jobCountCanvas.getContext('2d');
    jobCountChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: dates,
            datasets: [{
                label: 'Number of Jobs',
                data: jobCounts,
                backgroundColor: 'rgba(59, 130, 246, 0.7)',
                borderColor: 'rgba(59, 130, 246, 1)',
                borderWidth: 1
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                title: {
                    display: false  // Title is in HTML
                },
                legend: {
                    display: false  // Only one dataset, no legend needed
                },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            return context.parsed.y + ' jobs';
                        }
                    }
                }
            },
            scales: {
                x: {
                    title: {
                        display: true,
                        text: 'Date'
                    }
                },
                y: {
                    title: {
                        display: true,
                        text: 'Number of Jobs'
                    },
                    beginAtZero: true,
                    ticks: {
                        stepSize: 1  // Integer steps for job counts
                    }
                }
            }
        }
    });
}

/**
 * Update the budget data table
 */
function updateBudgetTable(filteredData) {
    const tbody = document.getElementById('budgetTableBody');
    const info = document.getElementById('budgetTableInfo');

    tbody.innerHTML = '';

    // Sort by date, then line number
    const sorted = [...filteredData].sort((a, b) => {
        if (a.date !== b.date) return a.date.localeCompare(b.date);
        return a.lineNumber - b.lineNumber;
    });

    // Aggregate data for display
    const aggregated = {};
    sorted.forEach(record => {
        const key = `${record.date}_${record.lineNumber}_${record.budgetType}_${record.budgetSubtype || 'none'}`;
        if (!aggregated[key]) {
            aggregated[key] = {
                date: record.date,
                lineNumber: record.lineNumber,
                budgetType: record.budgetType,
                budgetSubtype: record.budgetSubtype || '-',
                totalHours: 0
            };
        }
        aggregated[key].totalHours += record.totalHours;
    });

    // Convert to array and sort
    const rows = Object.values(aggregated).sort((a, b) => {
        if (a.date !== b.date) return a.date.localeCompare(b.date);
        if (a.lineNumber !== b.lineNumber) return a.lineNumber - b.lineNumber;
        return a.budgetType.localeCompare(b.budgetType);
    });

    // Populate table
    rows.forEach(row => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${row.date}</td>
            <td>${row.lineNumber}</td>
            <td>${row.budgetType}</td>
            <td>${row.budgetSubtype}</td>
            <td>${Math.round(row.totalHours * 100) / 100}</td>
        `;
        tbody.appendChild(tr);
    });

    // Update info
    info.textContent = `Showing ${rows.length} records`;
}

/**
 * Reset all filters to default
 */
function resetBudgetFilters() {
    // Check all budget type filters
    document.querySelectorAll('#budgetTypeFilters input').forEach(cb => cb.checked = true);

    // Check all rework subtype filters
    document.querySelectorAll('#reworkSubtypeFilters input').forEach(cb => cb.checked = true);

    // Check all QA subtype filters
    document.querySelectorAll('#qaSubtypeFilters input').forEach(cb => cb.checked = true);

    // Check all aircraft
    selectAllAircraft();

    // Reset date slider to max
    const slider = document.getElementById('dateRangeSlider');
    slider.value = slider.max;
    updateDateRangeDisplay(0, parseInt(slider.max));

    // Apply filters
    applyBudgetFilters();
}

/**
 * Export budget data to Excel
 */
function exportBudgetToExcel() {
    if (typeof XLSX === 'undefined') {
        alert('Excel export library (XLSX) is not loaded. Please ensure the page has fully loaded and try again.');
        return;
    }

    // Get filtered data from the current table
    const table = document.getElementById('budgetTable');
    const rows = table.querySelectorAll('tbody tr');

    if (rows.length === 0) {
        alert('No data to export');
        return;
    }

    // Build worksheet data
    const wsData = [
        ['Line Number', 'Budget Type', 'Budget Subtype', 'Date', 'Total Hours']
    ];

    rows.forEach(row => {
        const cells = row.querySelectorAll('td');
        // Parse hours as a number so Excel recognizes it as numeric
        const hoursText = cells[4].textContent.trim();
        const hours = parseFloat(hoursText) || 0;

        wsData.push([
            cells[1].textContent, // Line Number
            cells[2].textContent, // Budget Type
            cells[3].textContent, // Budget Subtype
            cells[0].textContent, // Date
            hours                 // Total Hours (as number, not text)
        ]);
    });

    // Create workbook
    const wb = XLSX.utils.book_new();
    const ws = XLSX.utils.aoa_to_sheet(wsData);

    // Add worksheet to workbook
    XLSX.utils.book_append_sheet(wb, ws, 'Schedule Budget');

    // Generate filename with current date
    const now = new Date();
    const filename = `schedule_budget_${now.getFullYear()}${(now.getMonth()+1).toString().padStart(2,'0')}${now.getDate().toString().padStart(2,'0')}.xlsx`;

    // Download file
    XLSX.writeFile(wb, filename);

    console.log('Excel file exported:', filename);
}

// Initialize when view becomes active
function onScheduleBudgetViewActivated() {
    if (budgetData.length === 0) {
        initScheduleBudget();
    }
}

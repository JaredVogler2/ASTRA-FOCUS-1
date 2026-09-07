// dashboard.js - Enhanced Client-side JavaScript for Production Scheduling Dashboard
// Compatible with product-specific late parts and rework tasks
let mechanicOptionsCache = {};
let lastFilterKey = null;
let currentScenario = '3stage';  // Default to 3-stage scheduler (99.9% success)
let currentView = 'team-lead';
let selectedTeams = ['all'];  // Changed to array for multi-select support
let selectedSuperintendent = 'all';  // New superintendent filter
let selectedSkill = 'all';
let selectedShift = 'all';
let selectedWorkGroup = 'all';  // Work group filter (mechanic, quality, customer)
let selectedProduct = 'all';
let selectedCustomer = 'all';  // Customer (airline code) filter
let jobSearchTerm = '';  // Job/Task search filter
let searchDebounceTimer = null;  // Debounce timer for search input
let selectedSuggestionIndex = -1;  // Current selected autocomplete suggestion
let scenarioData = {};
let allScenarios = {};
let mechanicAvailability = {};
let taskAssignments = {};
let latePartsData = {};
let supplyChainMetrics = {};

// Pagination state for 3-stage scenario
let pagination3stage = {
    offset: 0,
    limit: 500,  // Load 500 tasks at a time
    total: 0,
    filtered_total: 0,
    has_more: false,
    loading: false
};

// Client-side filter cache for instant filtering
let filterCache = {
    data: null,              // Cached filtered tasks
    filterKey: null,         // Current filter combination
    timestamp: null,         // Cache timestamp
    pendingRequest: null,    // In-flight request (for deduplication)

    // Generate unique key from current filters
    getFilterKey() {
        return JSON.stringify({
            superintendent: selectedSuperintendent,
            teams: selectedTeams.slice().sort(),
            skill: selectedSkill,
            shift: selectedShift,
            workGroup: selectedWorkGroup,
            product: selectedProduct,
            search: jobSearchTerm  // Include search term in cache key
        });
    },

    // Check if cache is valid for current filters
    isValid() {
        return this.data !== null &&
               this.filterKey === this.getFilterKey() &&
               this.timestamp &&
               (Date.now() - this.timestamp) < 300000; // 5 min TTL
    },

    // Store filtered data
    set(tasks) {
        this.data = tasks;
        this.filterKey = this.getFilterKey();
        this.timestamp = Date.now();
    },

    // Clear cache
    clear() {
        this.data = null;
        this.filterKey = null;
        this.timestamp = null;
    }
};

// Helper function to create safe IDs for query selectors
function sanitizeForQuerySelector(key) {
    if (!key) return '';
    return key.replace(/[^a-zA-Z0-9-_]/g, '_');
}

/**
 * Parse a teamSkill key into its components.
 * Handles both the shift-aware format "TEAM S{N} (SKILL)" and the
 * legacy format "TEAM (SKILL)".
 *
 * @param {string} teamSkill - e.g. "FGI-CF-CUSREADY S1 (D85)" or "FGI-CF-CUSREADY (D85)"
 * @returns {{baseTeam: string, shift: number|null, skill: string|null}}
 */
function parseTeamSkill(teamSkill) {
    if (!teamSkill) return { baseTeam: 'UNKNOWN', shift: null, skill: null };

    // Try shift-aware format first: "TEAM S{N} (SKILL)"
    const shiftMatch = teamSkill.match(/^(.+?)\s+S(\d)\s*\((.+?)\)\s*$/);
    if (shiftMatch) {
        return {
            baseTeam: shiftMatch[1].trim(),
            shift: parseInt(shiftMatch[2], 10),
            skill: shiftMatch[3].trim()
        };
    }

    // Fallback: legacy format "TEAM (SKILL)"
    const legacyMatch = teamSkill.match(/^(.+?)\s*\((.+?)\)\s*$/);
    if (legacyMatch) {
        return {
            baseTeam: legacyMatch[1].trim(),
            shift: null,
            skill: legacyMatch[2].trim()
        };
    }

    // No match — entire string is the team
    return { baseTeam: teamSkill.trim(), shift: null, skill: null };
}

/**
 * Build a display label for a resource.
 * Format: "{RoleLabel} #{index} {baseTeam} S{shift} ({skill})"
 *
 * @param {string} baseTeam - Team name
 * @param {number} index - Worker number (1-based)
 * @param {number|null} shift - Shift number (1, 2, 3) or null
 * @param {string|null} skill - Skill code or null
 * @returns {string}
 */
function buildResourceLabel(baseTeam, index, shift, skill) {
    let roleLabel = 'Mechanic';
    if (typeof isCustomerTeam === 'function' && isCustomerTeam(baseTeam)) roleLabel = 'Customer';
    else if (typeof isQualityTeam === 'function' && isQualityTeam(baseTeam)) roleLabel = 'Inspector';
    else if (typeof isVendorTeam === 'function' && isVendorTeam(baseTeam)) roleLabel = 'Vendor';

    const shiftStr = shift ? ` S${shift}` : '';
    const skillStr = skill ? ` (${skill})` : '';
    return `${roleLabel} #${index} ${baseTeam}${shiftStr}${skillStr}`;
}

// Debounce helper function
function debounce(func, delay) {
    let timeout;
    return function(...args) {
        const context = this;
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(context, args), delay);
    };
}

let savedAssignments = {}; // Store assignments per scenario

// Initialize savedAssignments structure
function initializeSavedAssignments() {
    if (!savedAssignments) {
        savedAssignments = {};
    }

    // Clear stale assignments from before shift-aware resource naming.
    // Old worker IDs used "TEAM (SKILL)_N" format; new format is "TEAM S{N} (SKILL)_N".
    const SA_VERSION_KEY = '__sa_version';
    const CURRENT_VERSION = 2;  // Bump this if the worker ID format changes again
    if (savedAssignments[SA_VERSION_KEY] !== CURRENT_VERSION) {
        console.log('savedAssignments format changed (shift-aware resource IDs) — clearing stale data');
        savedAssignments = { [SA_VERSION_KEY]: CURRENT_VERSION };
        try { localStorage.removeItem('savedAssignments'); } catch(e) { /* noop */ }
    }

    if (!savedAssignments[currentScenario]) {
        savedAssignments[currentScenario] = {};
    }
    // The mechanicSchedules object will now be generated on-the-fly, not stored.
}

/**
 * Auto-populate savedAssignments from the optimizer's mechanic_id on each task.
 * This bridges the gap between Stage 3 mechanic assignment (which sets mechanic_id
 * on every task) and the Individual view (which reads from savedAssignments).
 * Skipped if savedAssignments already has data (e.g. loaded from localStorage).
 */
function populateAssignmentsFromOptimizer() {
    if (!scenarioData || !scenarioData.tasks || scenarioData.tasks.length === 0) return;

    // Skip if assignments already exist for this scenario (e.g. from localStorage)
    const existing = savedAssignments[currentScenario];
    if (existing && Object.keys(existing).length > 0) {
        console.log('populateAssignmentsFromOptimizer: assignments already present, skipping');
        return;
    }

    // Build the worker pool from teamCapacities (same logic as autoAssign)
    const workerPool = {};
    const teamCapacities = scenarioData.teamCapacities || {};

    Object.keys(teamCapacities).forEach(teamSkill => {
        const capacity = teamCapacities[teamSkill] || 0;
        const parsed = parseTeamSkill(teamSkill);
        const baseTeam = parsed.baseTeam;
        const skill = parsed.skill;
        const shift = parsed.shift;

        for (let i = 1; i <= capacity; i++) {
            const workerId = `${teamSkill}_${i}`;
            const isCustomer = typeof isCustomerTeam === 'function' && isCustomerTeam(baseTeam);
            const isQuality = typeof isQualityTeam === 'function' && isQualityTeam(baseTeam);
            const isVendor = typeof isVendorTeam === 'function' && isVendorTeam(baseTeam);
            const displayName = buildResourceLabel(baseTeam, i, shift, skill);
            workerPool[workerId] = { id: workerId, baseTeam, skill, shift, displayName, isQuality, isCustomer, isVendor };
        }
    });

    // Use the existing core assignment logic that maps optimizer mechanic_ids to worker IDs
    const assignmentResult = generateAssignments(scenarioData.tasks, workerPool);

    // Convert mechanic-keyed schedules to task-keyed assignments (same as autoAssign)
    const newAssignmentsByTask = {};
    Object.entries(assignmentResult.schedules).forEach(([mechanicId, schedule]) => {
        schedule.tasks.forEach(task => {
            if (!newAssignmentsByTask[task.taskId]) {
                const originalTask = scenarioData.tasks.find(t => t.taskId === task.taskId);
                if (!originalTask) return;
                newAssignmentsByTask[task.taskId] = {
                    mechanics: [],
                    team: originalTask.team,
                    mechanicsNeeded: originalTask.mechanics || 1
                };
            }
            newAssignmentsByTask[task.taskId].mechanics.push(mechanicId);
        });
    });

    if (!savedAssignments[currentScenario]) savedAssignments[currentScenario] = {};
    Object.assign(savedAssignments[currentScenario], newAssignmentsByTask);

    console.log(`populateAssignmentsFromOptimizer: auto-populated ${Object.keys(newAssignmentsByTask).length} task assignments from optimizer mechanic_ids (${assignmentResult.successCount} successful)`);
}

// Initialize dashboard on page load
document.addEventListener('DOMContentLoaded', async function() {
    console.log('Initializing Production Scheduling Dashboard...');

    // Initialize data structures first
    initializeSavedAssignments();

    // Load available schedules first (await to ensure dropdown is populated)
    await loadAvailableSchedules();

    loadAllScenarios();
    setupEventListeners();
    setupProductFilter();
    setupRefreshButton();
});

// Load available schedules from filesystem
async function loadAvailableSchedules() {
    try {
        console.log('Fetching available schedules from /api/available-schedules...');
        const response = await fetch('/api/available-schedules');

        if (!response.ok) {
            console.error(`API returned status ${response.status}`);
            throw new Error(`Failed to load available schedules: ${response.status}`);
        }

        const data = await response.json();
        console.log('API response:', data);
        const schedules = data.schedules || [];

        // Populate scenario selector with available schedules
        const scenarioSelect = document.getElementById('scenarioSelect');
        if (!scenarioSelect) {
            console.error('scenarioSelect element not found!');
            return;
        }

        if (schedules.length > 0) {
            console.log(`Populating dropdown with ${schedules.length} schedules`);
            scenarioSelect.innerHTML = '';

            // Add most recent schedule first (default)
            schedules.forEach((schedule, index) => {
                const option = document.createElement('option');
                option.value = index === 0 ? '3stage' : `schedule_${index}`;
                option.textContent = index === 0
                    ? `${schedule.display_name} (Current)`
                    : schedule.display_name;
                scenarioSelect.appendChild(option);
                console.log(`Added schedule option: ${option.textContent}`);
            });

            // Set default to most recent
            scenarioSelect.value = '3stage';
            console.log(`✓ Loaded ${schedules.length} available schedules`);
        } else {
            // Fallback if no schedules found
            console.warn('No schedules found, using fallback');
            scenarioSelect.innerHTML = '<option value="3stage">3-Stage Rolling Window (Default)</option>';
        }
    } catch (error) {
        console.error('Error loading available schedules:', error);
        // Fallback to default
        const scenarioSelect = document.getElementById('scenarioSelect');
        if (scenarioSelect) {
            scenarioSelect.innerHTML = '<option value="3stage">3-Stage Rolling Window (Default)</option>';
        }
    }
}

// Helper function to fetch with timeout
async function fetchWithTimeout(url, options = {}, timeout = 30000) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeout);

    try {
        const response = await fetch(url, {
            ...options,
            signal: controller.signal
        });
        clearTimeout(timeoutId);
        return response;
    } catch (error) {
        clearTimeout(timeoutId);
        if (error.name === 'AbortError') {
            throw new Error(`Request timeout after ${timeout/1000}s`);
        }
        throw error;
    }
}

// Load all scenarios at startup for quick switching
async function loadAllScenarios() {
    try {
        // Clear filter cache when loading new schedule
        filterCache.clear();
        console.log('🗑️  Filter cache cleared (loading new schedule)');

        // PERFORMANCE OPTIMIZATION: Load 3-stage first (production scenario) with reduced initial batch
        showLoading('Loading 3-stage scheduler data...');

        // Reset pagination state
        pagination3stage.offset = 0;
        pagination3stage.limit = 200;  // Reduced from 500 for faster initial load

        // Load 3-stage scheduler scenario FIRST (it's the production one)
        try {
            const response3stage = await fetchWithTimeout(
                `/api/scenario/3stage?offset=0&limit=200`,
                {},
                15000  // Reduced timeout from 30s to 15s for faster failure detection
            );

            if (response3stage.ok) {
                const data3stage = await response3stage.json();

                // Update pagination state from response
                if (data3stage.pagination) {
                    pagination3stage.total = data3stage.pagination.total;
                    pagination3stage.filtered_total = data3stage.pagination.filtered_total || data3stage.pagination.total;
                    pagination3stage.has_more = data3stage.pagination.has_more;
                    pagination3stage.offset = data3stage.pagination.offset + data3stage.pagination.returned;
                }

                allScenarios['3stage'] = data3stage;
                console.log(`✓ Loaded 3stage: ${data3stage.tasks ? data3stage.tasks.length : 0} tasks of ${pagination3stage.total} total (99.9% success)`);

                if (pagination3stage.has_more) {
                    console.log(`  → ${pagination3stage.total - pagination3stage.offset} more tasks available (load on demand)`);
                }
            } else {
                console.warn(`✗ 3-stage scenario not available: ${response3stage.status}`);
            }
        } catch (e) {
            console.warn('✗ 3-stage scenario error:', e.message);
        }

        // Load other scenarios in the background (lazy load)
        // This prevents blocking the initial page render
        setTimeout(async () => {
            try {
                const scenariosResponse = await fetchWithTimeout('/api/scenarios', {}, 10000);
                if (!scenariosResponse.ok) {
                    console.warn('Failed to load scenario list');
                    return;
                }
                const scenariosInfo = await scenariosResponse.json();

                console.log('Background loading scenarios:', scenariosInfo.scenarios.map(s => s.id));

                // Load each scenario in background
                for (const scenario of scenariosInfo.scenarios) {
                    try {
                        const response = await fetchWithTimeout(`/api/scenario/${scenario.id}`, {}, 15000);
                        if (response.ok) {
                            const data = await response.json();
                            allScenarios[scenario.id] = data;
                            console.log(`✓ Background loaded ${scenario.id}: ${data.tasks ? data.tasks.length : 0} tasks`);
                        }
                    } catch (error) {
                        console.error(`✗ Error background loading ${scenario.id}:`, error.message);
                    }
                }
            } catch (error) {
                console.error('Error loading scenarios in background:', error);
            }
        }, 1000);  // Start background load after 1 second


        // Set the initial scenario data - MAKE SURE THIS IS CORRECT
        if (allScenarios[currentScenario] && allScenarios[currentScenario].tasks) {
            scenarioData = allScenarios[currentScenario];
            console.log('Set scenarioData to', currentScenario, 'with', scenarioData.tasks.length, 'tasks');
        } else if (allScenarios['baseline'] && allScenarios['baseline'].tasks) {
            currentScenario = 'baseline';
            scenarioData = allScenarios['baseline'];
            console.log('Fallback to baseline with', scenarioData.tasks.length, 'tasks');
        } else {
            console.error('No valid scenarios loaded!');
            console.log('allScenarios:', allScenarios);
        }

        hideLoading();

        // Verify the data structure
        console.log('Final scenarioData keys:', Object.keys(scenarioData));
        console.log('Has tasks?', !!scenarioData.tasks);
        console.log('Task count:', scenarioData.tasks?.length || 0);

        if (scenarioData && scenarioData.tasks && scenarioData.tasks.length > 0) {
            populateTeamDropdowns();

            // Load any previously saved assignments from localStorage
            loadAssignmentsFromStorage(true);

            // Auto-populate assignments from optimizer mechanic_ids if none were loaded
            populateAssignmentsFromOptimizer();

            updateProductFilter();
            updateCustomerFilter();
            updateView();
        } else {
            console.error('ScenarioData is missing tasks!');
            showError('No task data available. Please check the server.');
        }
    } catch (error) {
        console.error('Error loading scenarios:', error);
        hideLoading();
        showError('Failed to load scenario data. Please refresh the page.');
    }
}

// Setup all event listeners
function setupEventListeners() {
    console.log('Setting up event listeners...');

    // Event delegation for dynamically created chain buttons
    document.addEventListener('click', function(e) {
        if (e.target.classList.contains('chain-btn')) {
            const taskId = e.target.dataset.taskId;
            if (taskId) {
                showTaskChain(taskId);
            }
        }
    });

    // View tab switching
    document.querySelectorAll('.view-tab').forEach(tab => {
        tab.addEventListener('click', function() {
            switchView(this.dataset.view);
        });
    });

    // Scenario selection
    const scenarioSelect = document.getElementById('scenarioSelect');
    if (scenarioSelect) {
        scenarioSelect.addEventListener('change', function() {
            switchScenario(this.value);
        });
    }

    // Superintendent selection - NEW FILTER
    const superintendentSelect = document.getElementById('superintendentSelect');
    if (superintendentSelect) {
        superintendentSelect.addEventListener('change', async function() {
            selectedSuperintendent = this.value;

            // CASCADE: Update team dropdown to show only teams under this superintendent
            updateTeamDropdownForSuperintendent();

            // For 3-stage scenario, reload tasks filtered by superintendent
            if (currentScenario === '3stage') {
                await reload3StageDataWithFilters();
            } else {
                updateTeamLeadView();
            }
        });
    }

    // Team selection - UPDATED FOR MULTI-SELECT
    const teamSelect = document.getElementById('teamSelect');
    if (teamSelect) {
        teamSelect.addEventListener('change', async function() {
            // Get array of selected values from multi-select
            selectedTeams = Array.from(this.selectedOptions).map(opt => opt.value);

            // If nothing selected, default to 'all'
            if (selectedTeams.length === 0) {
                selectedTeams = ['all'];
                this.options[0].selected = true;  // Select "All Teams" option
            }

            updateSkillDropdown();
            updateShiftDropdown();

            // For 3-stage scenario, reload tasks filtered by team (top 500 for this team)
            if (currentScenario === '3stage') {
                await reload3StageDataWithFilters();
            } else {
                updateTeamLeadView();
            }
        });
    }

    // Skill selection
    const skillSelect = document.getElementById('skillSelect');
    if (skillSelect) {
        skillSelect.addEventListener('change', async function() {
            selectedSkill = this.value;

            // For 3-stage scenario, reload tasks filtered by skill
            if (currentScenario === '3stage') {
                await reload3StageDataWithFilters();
            } else {
                updateTeamLeadView();
            }
        });
    }

    // Shift selection
    const shiftSelect = document.getElementById('shiftSelect');
    if (shiftSelect) {
        shiftSelect.addEventListener('change', async function() {
            selectedShift = this.value;

            // For 3-stage scenario, reload tasks filtered by shift
            if (currentScenario === '3stage') {
                await reload3StageDataWithFilters();
            } else {
                updateTeamLeadView();
            }
        });
    }

    // Work Group selection (mechanic, quality, customer)
    const workGroupSelect = document.getElementById('workGroupSelect');
    if (workGroupSelect) {
        workGroupSelect.addEventListener('change', async function() {
            selectedWorkGroup = this.value;

            // For 3-stage scenario, reload tasks filtered by work group
            if (currentScenario === '3stage') {
                await reload3StageDataWithFilters();
            } else {
                updateTeamLeadView();
            }
        });
    }

    // Product selection
    const productSelect = document.getElementById('productSelect');
    if (productSelect) {
        productSelect.addEventListener('change', async function() {
            selectedProduct = this.value;

            // For 3-stage scenario, reload tasks filtered by product
            if (currentScenario === '3stage') {
                await reload3StageDataWithFilters();
            } else {
                updateTeamLeadView();
            }
        });
    }

    // Customer (airline code) selection
    const customerSelect = document.getElementById('customerSelect');
    if (customerSelect) {
        customerSelect.addEventListener('change', async function() {
            selectedCustomer = this.value;
            if (currentScenario === '3stage') {
                await reload3StageDataWithFilters();
            } else {
                updateTeamLeadView();
            }
        });
    }

    // Job search input with debouncing and autocomplete
    const jobSearchInput = document.getElementById('jobSearchInput');
    const clearSearchBtn = document.getElementById('clearSearchBtn');
    const searchSuggestions = document.getElementById('searchSuggestions');

    if (jobSearchInput) {
        jobSearchInput.addEventListener('input', function() {
            const inputValue = this.value.trim();

            // Show/hide clear button
            if (clearSearchBtn) {
                clearSearchBtn.style.display = inputValue ? 'block' : 'none';
            }

            // Show autocomplete suggestions (immediate, no debounce)
            if (inputValue.length >= 2) {
                showSearchSuggestions(inputValue);
            } else {
                hideSearchSuggestions();
            }

            // Clear previous debounce timer
            if (searchDebounceTimer) {
                clearTimeout(searchDebounceTimer);
            }

            // Debounce: wait 300ms after user stops typing for actual search
            searchDebounceTimer = setTimeout(async () => {
                jobSearchTerm = inputValue;
                console.log(`🔍 Job search triggered: "${jobSearchTerm}"`);

                // For 3-stage scenario, reload tasks filtered by search term
                if (currentScenario === '3stage') {
                    await reload3StageDataWithFilters();
                } else {
                    updateTeamLeadView();
                }
            }, 300);  // 300ms delay after last keystroke
        });

        // Handle keyboard navigation in suggestions
        jobSearchInput.addEventListener('keydown', function(e) {
            if (searchSuggestions && searchSuggestions.style.display !== 'none') {
                const suggestions = searchSuggestions.querySelectorAll('.suggestion-item');

                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    selectedSuggestionIndex = Math.min(selectedSuggestionIndex + 1, suggestions.length - 1);
                    updateSuggestionSelection(suggestions);
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    selectedSuggestionIndex = Math.max(selectedSuggestionIndex - 1, -1);
                    updateSuggestionSelection(suggestions);
                } else if (e.key === 'Enter') {
                    if (selectedSuggestionIndex >= 0 && suggestions[selectedSuggestionIndex]) {
                        e.preventDefault();
                        suggestions[selectedSuggestionIndex].click();
                        return;
                    }
                    // Normal Enter behavior - trigger immediate search
                    if (searchDebounceTimer) {
                        clearTimeout(searchDebounceTimer);
                    }
                    jobSearchTerm = this.value.trim();
                    hideSearchSuggestions();
                    console.log(`🔍 Job search (Enter key): "${jobSearchTerm}"`);

                    if (currentScenario === '3stage') {
                        reload3StageDataWithFilters();
                    } else {
                        updateTeamLeadView();
                    }
                } else if (e.key === 'Escape') {
                    hideSearchSuggestions();
                }
            } else if (e.key === 'Enter') {
                // Normal Enter behavior when suggestions not shown
                if (searchDebounceTimer) {
                    clearTimeout(searchDebounceTimer);
                }
                jobSearchTerm = this.value.trim();
                console.log(`🔍 Job search (Enter key): "${jobSearchTerm}"`);

                if (currentScenario === '3stage') {
                    reload3StageDataWithFilters();
                } else {
                    updateTeamLeadView();
                }
            }
        });

        // Hide suggestions when clicking outside
        document.addEventListener('click', function(e) {
            if (searchSuggestions && !jobSearchInput.contains(e.target) && !searchSuggestions.contains(e.target)) {
                hideSearchSuggestions();
            }
        });
    }

    // Mechanic selection for individual view
    const mechanicSelect = document.getElementById('mechanicSelect');
    if (mechanicSelect && !mechanicSelect.hasAttribute('data-listener-added')) {
        mechanicSelect.setAttribute('data-listener-added', 'true');
        mechanicSelect.addEventListener('change', handleMechanicSelection);
    }

    // Auto-assign button
    const autoAssignBtn = document.querySelector('button[onclick="autoAssign()"]');
    if (autoAssignBtn && !autoAssignBtn.hasAttribute('data-listener-added')) {
        autoAssignBtn.setAttribute('data-listener-added', 'true');
        autoAssignBtn.removeAttribute('onclick');
        autoAssignBtn.addEventListener('click', function() {
            autoAssign();
        });
    }

    // Save button
    const saveBtn = document.querySelector('button[onclick="saveAssignmentsToStorage()"]');
    if (saveBtn && !saveBtn.hasAttribute('data-listener-added')) {
        saveBtn.setAttribute('data-listener-added', 'true');
        saveBtn.removeAttribute('onclick');
        saveBtn.addEventListener('click', function() {
            saveAssignmentsToStorage();
        });
    }

    // Load button
    const loadBtn = document.querySelector('button[onclick="loadAssignmentsFromStorage()"]');
    if (loadBtn && !loadBtn.hasAttribute('data-listener-added')) {
        loadBtn.setAttribute('data-listener-added', 'true');
        loadBtn.removeAttribute('onclick');
        loadBtn.addEventListener('click', function() {
            loadAssignmentsFromStorage();
        });
    }

    // Clear saved button
    const clearSavedBtn = document.querySelector('button[onclick="clearSavedAssignments()"]');
    if (clearSavedBtn && !clearSavedBtn.hasAttribute('data-listener-added')) {
        clearSavedBtn.setAttribute('data-listener-added', 'true');
        clearSavedBtn.removeAttribute('onclick');
        clearSavedBtn.addEventListener('click', function() {
            clearSavedAssignments();
        });
    }

    // Clear view button
    const clearViewBtn = document.querySelector('button[onclick="clearAllAssignments()"]');
    if (clearViewBtn && !clearViewBtn.hasAttribute('data-listener-added')) {
        clearViewBtn.setAttribute('data-listener-added', 'true');
        clearViewBtn.removeAttribute('onclick');
        clearViewBtn.addEventListener('click', function() {
            clearAllAssignments();
        });
    }

    // Export button
    const exportBtn = document.querySelector('button[onclick="exportTasks()"]');
    if (exportBtn && !exportBtn.hasAttribute('data-listener-added')) {
        exportBtn.setAttribute('data-listener-added', 'true');
        exportBtn.removeAttribute('onclick');
        exportBtn.addEventListener('click', function() {
            exportTasks();
        });
    }

    // Gantt view controls (if in project view)
    const ganttProductSelect = document.getElementById('ganttProductSelect');
    if (ganttProductSelect) {
        ganttProductSelect.addEventListener('change', function() {
            if (typeof renderGanttChart === 'function') {
                renderGanttChart();
            }
        });
    }

    const ganttTeamSelect = document.getElementById('ganttTeamSelect');
    if (ganttTeamSelect) {
        ganttTeamSelect.addEventListener('change', function() {
            if (typeof renderGanttChart === 'function') {
                renderGanttChart();
            }
        });
    }

    const ganttSortSelect = document.getElementById('ganttSortSelect');
    if (ganttSortSelect) {
        ganttSortSelect.addEventListener('change', function() {
            if (typeof handleGanttSortChange === 'function') {
                handleGanttSortChange();
            }
        });
    }

    // Timeline controls
    const timelineProductSelect = document.getElementById('timelineProductSelect');
    if (timelineProductSelect) {
        timelineProductSelect.addEventListener('change', renderTimeline);
    }

    const timelineTeamSelect = document.getElementById('timelineTeamSelect');
    if (timelineTeamSelect) {
        timelineTeamSelect.addEventListener('change', renderTimeline);
    }

    const timelineScale = document.getElementById('timelineScale');
    if (timelineScale) {
        timelineScale.addEventListener('change', function() {
            const currentWindow = timeline ? timeline.getWindow() : null;
            initializeTimeline();
            setTimeout(() => {
                if (timeline && currentWindow) {
                    timeline.setWindow(currentWindow.start, currentWindow.end);
                }
            }, 100);
        });
    }

    const timelineGroupBy = document.getElementById('timelineGroupBy');
    if (timelineGroupBy) {
        timelineGroupBy.addEventListener('change', renderTimeline);
    }

    const focusDateInput = document.getElementById('timelineFocusDate');
    if (focusDateInput) {
        focusDateInput.addEventListener('change', function() {
            if (this.value) {
                goToDate(new Date(this.value));
            }
        });
    }

    // Supply chain controls
    const supplyChainProductFilter = document.getElementById('supplyChainProductFilter');
    if (supplyChainProductFilter && !supplyChainProductFilter.hasAttribute('data-initialized')) {
        supplyChainProductFilter.setAttribute('data-initialized', 'true');
        supplyChainProductFilter.addEventListener('change', () => {
            updateLatePartsTimeline();
            updateLatePartsImpactTable();
        });
    }

    document.querySelectorAll('.scenario-compare').forEach(checkbox => {
        if (!checkbox.hasAttribute('data-listener-added')) {
            checkbox.setAttribute('data-listener-added', 'true');
            checkbox.addEventListener('change', () => {
                updateLatePartsTimeline();
                updateProductImpactGrid();
            });
        }
    });

    // Task assignment selects (dynamic)
    document.addEventListener('change', function(e) {
        if (e.target.classList.contains('assign-select')) {
            const taskId = e.target.dataset.taskId;
            const position = e.target.dataset.position || '0';
            const mechanicId = e.target.value;

            if (!savedAssignments[currentScenario]) {
                savedAssignments[currentScenario] = {};
            }

            if (!savedAssignments[currentScenario][taskId]) {
                const task = scenarioData.tasks.find(t => t.taskId === taskId);
                if (task) {
                    savedAssignments[currentScenario][taskId] = {
                        mechanics: [],
                        team: task.team,
                        mechanicsNeeded: task.mechanics || 1
                    };
                }
            }

            if (savedAssignments[currentScenario][taskId]) {
                const assignment = savedAssignments[currentScenario][taskId];
                if (!assignment.mechanics) assignment.mechanics = [];

                while (assignment.mechanics.length <= parseInt(position)) {
                    assignment.mechanics.push('');
                }

                assignment.mechanics[parseInt(position)] = mechanicId;

                const filledCount = assignment.mechanics.filter(m => m).length;
                assignment.partial = filledCount < assignment.mechanicsNeeded;
            }

            if (mechanicId) {
                e.target.style.backgroundColor = '#d4edda';
                setTimeout(() => {
                    e.target.style.backgroundColor = '';
                    e.target.classList.add('has-saved-assignment');
                }, 1000);
            } else {
                e.target.classList.remove('has-saved-assignment');
            }

            // updateMechanicSchedulesFromAssignments(); // This is no longer needed as schedules are generated on-demand.

            if (mechanicId) {
                fetch('/api/assign_task', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        taskId: taskId,
                        mechanicId: mechanicId,
                        position: position,
                        scenario: currentScenario
                    })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        console.log(`Task ${taskId} position ${position} assigned to ${mechanicId}`);
                    }
                })
                .catch(error => {
                    console.error('Error saving assignment:', error);
                });
            }

            if (typeof updateAssignmentSummary === 'function') {
                updateAssignmentSummary();
            }
        }
    });

    // Window resize handler for responsive adjustments
    let resizeTimeout;
    window.addEventListener('resize', function() {
        clearTimeout(resizeTimeout);
        resizeTimeout = setTimeout(function() {
            if (currentView === 'project' && typeof renderGanttChart === 'function') {
                renderGanttChart();
            }
        }, 250);
    });

    // Handle browser back/forward buttons
    window.addEventListener('popstate', function(e) {
        if (e.state && e.state.view) {
            switchView(e.state.view);
        }
        if (e.state && e.state.scenario) {
            switchScenario(e.state.scenario);
        }
    });

    // Infinite scroll for 3-stage task loading
    let scrollDebounceTimer = null;
    window.addEventListener('scroll', function() {
        // Only active for team-lead view with 3stage scenario
        if (currentView !== 'team-lead' || currentScenario !== '3stage') {
            return;
        }

        // Debounce scroll events (check every 200ms max)
        if (scrollDebounceTimer) {
            return;
        }

        scrollDebounceTimer = setTimeout(() => {
            scrollDebounceTimer = null;

            // Check if user scrolled near bottom (within 300px of bottom)
            const scrollPosition = window.innerHeight + window.scrollY;
            const documentHeight = document.documentElement.scrollHeight;
            const distanceFromBottom = documentHeight - scrollPosition;

            if (distanceFromBottom < 300 && pagination3stage.has_more && !pagination3stage.loading) {
                console.log('📜 Near bottom - loading more tasks...');
                loadMoreTasks();
            }
        }, 200);
    });

    console.log('Event listeners setup complete');
}

// Handle Gantt sort functionality
function handleGanttSortChange() {
    const sortBy = document.getElementById('ganttSortSelect').value;
    const productFilter = document.getElementById('ganttProductSelect').value || 'all';
    const teamFilter = document.getElementById('ganttTeamSelect').value || 'all';

    let tasks = getGanttTasks(productFilter, teamFilter);

    // Sort tasks based on selection
    switch(sortBy) {
        case 'start':
            tasks.sort((a, b) => new Date(a.start) - new Date(b.start));
            break;
        case 'product':
            tasks.sort((a, b) => {
                if (a.product !== b.product) {
                    return a.product.localeCompare(b.product);
                }
                return new Date(a.start) - new Date(b.start);
            });
            break;
        case 'priority':
            tasks.sort((a, b) => {
                if (a.priority !== b.priority) {
                    return a.priority - b.priority;
                }
                return new Date(a.start) - new Date(b.start);
            });
            break;
        case 'team':
            tasks.sort((a, b) => {
                if (a.team !== b.team) {
                    return a.team.localeCompare(b.team);
                }
                return new Date(a.start) - new Date(b.start);
            });
            break;
        case 'duration':
            tasks.sort((a, b) => {
                if (a.duration !== b.duration) {
                    return b.duration - a.duration; // Longest first
                }
                return new Date(a.start) - new Date(b.start);
            });
            break;
        default:
            tasks.sort((a, b) => new Date(a.start) - new Date(b.start));
    }

    renderGanttChartWithTasks(tasks);
}


// Setup product filter (new feature)
function setupProductFilter() {
    const teamFilters = document.querySelector('.team-filters');
    if (teamFilters && !document.getElementById('productSelect')) {
        const productFilter = document.createElement('div');
        productFilter.className = 'filter-group';
        productFilter.innerHTML = `
            <label>Product:</label>
            <select id="productSelect">
                <option value="all">All Products</option>
            </select>
        `;
        teamFilters.appendChild(productFilter);

        document.getElementById('productSelect').addEventListener('change', function() {
            selectedProduct = this.value;
            updateTeamLeadView();
        });
    }
}

// Switch scenario with enhanced handling
// Switch scenario with enhanced handling
function switchScenario(scenario) {
    if (allScenarios[scenario]) {
        // Clear filter cache when switching schedules
        filterCache.clear();
        console.log('🗑️  Filter cache cleared (switching schedule)');

        currentScenario = scenario;
        scenarioData = allScenarios[scenario];

        console.log(`Switched to ${scenario}, attempting to load saved assignments...`);

        // Automatically load assignments from storage for the new scenario
        loadAssignmentsFromStorage(true); // Pass true for silent loading

        // Auto-populate from optimizer mechanic_ids if no saved assignments were loaded
        populateAssignmentsFromOptimizer();

        // CRITICAL: Re-populate team dropdowns with new scenario's capacities
        populateTeamDropdowns();

        updateProductFilter();
        updateCustomerFilter();
        showScenarioInfo();
        updateView();

        // The view update will handle applying the just-loaded assignments.
    }
}

// Update product filter dropdown
function updateProductFilter() {
    const productSelect = document.getElementById('productSelect');
    if (productSelect && scenarioData.products) {
        // Use global state as source of truth
        const savedProduct = selectedProduct || productSelect.value || 'all';

        // Build new HTML atomically
        let newHTML = '<option value="all">All Aircraft</option>';
        scenarioData.products.forEach(product => {
            const escaped = product.name.replace(/"/g, '&quot;').replace(/</g, '&lt;');
            newHTML += `<option value="${escaped}">${escaped} (${product.totalTasks} tasks)</option>`;
        });
        productSelect.innerHTML = newHTML;

        // Restore selection from global state
        if ([...productSelect.options].some(opt => opt.value === savedProduct)) {
            productSelect.value = savedProduct;
        } else {
            productSelect.value = 'all';
            selectedProduct = 'all';
        }
    }
}

// Update customer (airline code) filter dropdown
function updateCustomerFilter() {
    const customerSelect = document.getElementById('customerSelect');
    if (!customerSelect) return;

    const savedCustomer = selectedCustomer || 'all';

    // Collect unique customer codes from products and tasks
    const codes = new Set();
    if (scenarioData && scenarioData.products) {
        scenarioData.products.forEach(p => {
            if (p.customer_code) codes.add(p.customer_code);
        });
    }
    if (scenarioData && scenarioData.tasks) {
        scenarioData.tasks.forEach(t => {
            if (t.customer_code) codes.add(t.customer_code);
        });
    }

    let html = '<option value="all">All Customers</option>';
    [...codes].sort().forEach(code => {
        // Count tasks for this customer
        const count = (scenarioData.products || []).filter(p => p.customer_code === code).length;
        html += `<option value="${code}">${code}${count > 0 ? ` (${count} aircraft)` : ''}</option>`;
    });
    customerSelect.innerHTML = html;

    if ([...customerSelect.options].some(opt => opt.value === savedCustomer)) {
        customerSelect.value = savedCustomer;
    } else {
        customerSelect.value = 'all';
        selectedCustomer = 'all';
    }
}

// Show scenario-specific information
function showScenarioInfo() {
    let infoBanner = document.getElementById('scenarioInfo');
    if (!infoBanner) {
        const mainContent = document.querySelector('.main-content');
        infoBanner = document.createElement('div');
        infoBanner.id = 'scenarioInfo';
        infoBanner.style.cssText = 'background: #f0f9ff; border: 1px solid #3b82f6; border-radius: 8px; padding: 12px; margin-bottom: 20px;';
        mainContent.insertBefore(infoBanner, mainContent.firstChild);
    }

    let infoHTML = `<strong>${currentScenario.toUpperCase()}</strong>: `;
    if (currentScenario === 'scenario3' && scenarioData.achievedMaxLateness !== undefined) {
        if (scenarioData.achievedMaxLateness === 0) {
            infoHTML += `✓ Achieved zero lateness with ${scenarioData.totalWorkforce} workers`;
        } else {
            infoHTML += `Minimum achievable lateness: ${scenarioData.achievedMaxLateness} days (${scenarioData.totalWorkforce} workers)`;
        }
    } else if (currentScenario === 'scenario2') {
        infoHTML += `Optimal uniform capacity: ${scenarioData.optimalMechanics || 'N/A'} mechanics, ${scenarioData.optimalQuality || 'N/A'} quality per team`;
    } else {
        infoHTML += `Workforce: ${scenarioData.totalWorkforce}, Makespan: ${scenarioData.makespan} days`;
    }
    infoBanner.innerHTML = infoHTML;
}

// Switch between views
function switchView(view) {
    currentView = view;
    console.log(`Switching to view: ${view}`);

    // Update active tab
    document.querySelectorAll('.view-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.view === view);
    });

    // Update active content
    document.querySelectorAll('.view-content').forEach(content => {
        // The new scenario view has a different content attribute name
        const contentName = content.dataset.viewContent || content.id.replace('-view', '');
        content.style.display = contentName === view ? 'block' : 'none';
        if (contentName === view) {
            content.classList.add('active');
        } else {
            content.classList.remove('active');
        }
    });

    // Call the main update function
    updateView();

    // The updateView() -> initScenarioView() path seems unreliable for the scenario view.
    // A direct call is added here to ensure the view initializes correctly.
    if (view === 'scenario') {
        initScenarioView();
    }

    // Initialize Schedule Budget view when activated
    if (view === 'schedule-budget') {
        if (typeof onScheduleBudgetViewActivated === 'function') {
            onScheduleBudgetViewActivated();
        }
    }

    // Auto-load Shift Performance data when navigating to that view
    if (view === 'shift-performance') {
        if (typeof autoLoadLatestPerformance === 'function' && !spPerformanceData) {
            autoLoadLatestPerformance();
        }
    }
}

// ========= WORKER GANTT CHART IMPLEMENTATION =========
let workerGantt = null;
let lastKnownScrollStart = null; // Variable to track scroll direction

const SHIFT_HOURS = {
    '3rd': { start: 23, end: 6, duration: 7 }, // Crosses midnight
    '1st': { start: 6, end: 14.5, duration: 8.5 }, // 2:30 PM
    '2nd': { start: 14.5, end: 23, duration: 8.5 }
};

// This function is the core of the custom time axis.
// It maps a real date to a "display" date on a linear timeline.
function mapRealTimeToDisplayTime(realDate, shift) {
    if (!(realDate instanceof Date)) {
        realDate = new Date(realDate);
    }

    const realHours = realDate.getHours() + realDate.getMinutes() / 60;
    const dayStart = new Date(realDate);
    dayStart.setHours(0, 0, 0, 0);

    let displayDate = new Date(dayStart);
    let hoursIntoDisplayDay = 0;

    const shiftInfo = SHIFT_HOURS[shift];
    if (!shiftInfo) {
        // Default for unknown shifts: map linearly
        return realDate;
    }

    // Calculate hours into the specific shift
    let hoursIntoShift = 0;
    if (shift === '3rd') {
        if (realHours >= shiftInfo.start) { // e.g., 23:30 on Day 1
            hoursIntoShift = realHours - shiftInfo.start;
        } else { // e.g., 01:00 on Day 2
            hoursIntoShift = (24 - shiftInfo.start) + realHours;
        }
    } else {
        hoursIntoShift = realHours - shiftInfo.start;
    }
    hoursIntoShift = Math.max(0, Math.min(hoursIntoShift, shiftInfo.duration));


    // Map to the 24-hour display block for the day
    if (shift === '3rd') {
        hoursIntoDisplayDay = (hoursIntoShift / shiftInfo.duration) * 8; // 0-8 hours
    } else if (shift === '1st') {
        hoursIntoDisplayDay = 8 + (hoursIntoShift / shiftInfo.duration) * 8; // 8-16 hours
    } else if (shift === '2nd') {
        hoursIntoDisplayDay = 16 + (hoursIntoShift / shiftInfo.duration) * 8; // 16-24 hours
    }

    // For 3rd shift tasks starting late at night, they belong to the *next* day's schedule block.
    if (shift === '3rd' && realHours >= shiftInfo.start) {
        displayDate.setDate(displayDate.getDate() + 1);
    }

    displayDate.setHours(hoursIntoDisplayDay, (hoursIntoDisplayDay % 1) * 60, 0, 0);

    return displayDate;
}

function initializeWorkerGantt() {
    console.log("Initializing Advanced Worker Gantt...");

    const container = document.getElementById('worker-gantt-container-advanced');
    if (!container) {
        console.error("Advanced Worker Gantt container not found!");
        return;
    }

    if (workerGantt) {
        workerGantt.destroy();
    }

    const items = new vis.DataSet([]);
    const groups = new vis.DataSet([]);

    const today = new Date();
    const minDate = new Date(today.getFullYear() - 1, today.getMonth(), today.getDate());
    const maxDate = new Date(today.getFullYear() + 1, today.getMonth(), today.getDate());

    const options = {
        stack: false,
        editable: false,
        zoomable: false, // Zoom is now controlled by timescale dropdown
        moveable: false, // Allow pan for the main timeline
        orientation: 'top',
        height: '100%',
        min: minDate,
        max: maxDate,
        showMajorLabels: false, // Hide all default labels
        showMinorLabels: false,
        groupOrder: 'order', // Use a simple order property
        tooltip: {
            followMouse: true,
            overflowMethod: 'cap'
        }
    };

    workerGantt = new vis.Timeline(container, items, groups, options);

    // Set default view date to today
    const viewDateInput = document.getElementById('wg-view-date');
    if (viewDateInput && !viewDateInput.value) {
        viewDateInput.valueAsDate = new Date();
    }

    setupWorkerGanttEventListeners();
    populateWorkerGanttFilters();
    renderWorkerGantt(); // Renders the data
    updateWorkerGanttWindow(); // Sets the initial view window
}

function setupWorkerGanttEventListeners() {
    if (!workerGantt) return;

    // Add event listeners for filters
    document.getElementById('wg-team-filter').addEventListener('change', renderWorkerGantt);
    document.getElementById('wg-shift-filter').addEventListener('change', renderWorkerGantt);
    document.getElementById('wg-skillset-filter').addEventListener('change', renderWorkerGantt);
    document.getElementById('wg-worker-filter').addEventListener('change', renderWorkerGantt);
    document.getElementById('wg-refresh-btn').addEventListener('click', renderWorkerGantt);
    document.getElementById('wg-view-date').addEventListener('change', updateWorkerGanttWindow);
    document.getElementById('wg-timescale-filter').addEventListener('change', updateWorkerGanttWindow);
    document.getElementById('wg-back-btn').addEventListener('click', () => moveTimeline('back'));
    document.getElementById('wg-forward-btn').addEventListener('click', () => moveTimeline('forward'));

    // This event updates the custom header whenever the window changes.
    workerGantt.on('rangechanged', renderAdvancedGanttHeader);

    workerGantt.on('select', function(properties) {
        const selectedIds = properties.items;

        // First, clear all existing highlights from all items
        const itemsToClear = [];
        workerGantt.itemsData.forEach(item => {
            if (item.className && item.className.includes('wg-highlight')) {
                itemsToClear.push({ id: item.id, className: item.className.replace(' wg-highlight', '').trim() });
            }
        });
        if (itemsToClear.length > 0) {
            workerGantt.itemsData.update(itemsToClear);
        }

        if (selectedIds.length === 0) {
            return;
        }

        // --- New Highlighting Logic ---
        const allTasks = scenarioData.tasks;
        const selectedInstanceId = selectedIds[0].split('_').pop();
        const selectedTask = allTasks.find(t => t.taskId === selectedInstanceId);

        if (!selectedTask || !selectedTask.originalTaskId) {
            console.warn("Selected task or its originalTaskId not found, cannot highlight dependencies.");
            return;
        }

        const selectedOriginalId = selectedTask.originalTaskId;
        const predecessorsMap = scenarioData.predecessors_map || {};
        const successorsMap = scenarioData.successors_map || {};

        const allUpstream = new Set();
        const allDownstream = new Set();
        const predQueue = [selectedOriginalId];
        const succQueue = [selectedOriginalId];
        const visitedPred = new Set();
        const visitedSucc = new Set();

        // Find all downstream original IDs
        while (succQueue.length > 0) {
            const currentId = succQueue.shift();
            if (visitedSucc.has(currentId)) continue;
            visitedSucc.add(currentId);
            allDownstream.add(currentId);
            (successorsMap[currentId] || []).forEach(succId => succQueue.push(succId));
        }

        // Find all upstream original IDs
        while (predQueue.length > 0) {
            const currentId = predQueue.shift();
            if (visitedPred.has(currentId)) continue;
            visitedPred.add(currentId);
            allUpstream.add(currentId);
            (predecessorsMap[currentId] || []).forEach(predId => predQueue.push(predId));
        }

        const originalIdsToHighlight = new Set([...allUpstream, ...allDownstream]);

        // Find all task INSTANCES that correspond to these original IDs and highlight them.
        const itemsToUpdate = [];
        workerGantt.itemsData.forEach(item => {
            const itemInstanceId = item.id.split('_').pop();
            const task = allTasks.find(t => t.taskId === itemInstanceId);
            if (task && originalIdsToHighlight.has(task.originalTaskId)) {
                if (!item.className || !item.className.includes('wg-highlight')) {
                    itemsToUpdate.push({ id: item.id, className: `${item.className || ''} wg-highlight`.trim() });
                }
            }
        });

        if (itemsToUpdate.length > 0) {
            workerGantt.itemsData.update(itemsToUpdate);
        }
    });
}

function populateWorkerGanttFilters() {
    console.log("Populating Worker Gantt filters...");
    if (!scenarioData || !scenarioData.teamCapacities) return;

    const teamSelect = document.getElementById('wg-team-filter');
    const skillsetSelect = document.getElementById('wg-skillset-filter');
    const workerSelect = document.getElementById('wg-worker-filter');

    if (!teamSelect || !skillsetSelect || !workerSelect) return;

    const teams = new Set();
    const skills = new Set();
    const allWorkers = [];

    Object.entries(scenarioData.teamCapacities).forEach(([teamSkill, capacity]) => {
        const parsed = parseTeamSkill(teamSkill);
        const baseTeam = parsed.baseTeam;
        const skill = parsed.skill;
        const shift = parsed.shift;

        teams.add(baseTeam);
        if(skill) skills.add(skill);

        for (let i = 1; i <= capacity; i++) {
            const workerId = `${teamSkill}_${i}`;
            const workerLabel = buildResourceLabel(baseTeam, i, shift, skill);
            allWorkers.push({ id: workerId, name: workerLabel });
        }
    });

    // Populate Teams
    const currentTeam = teamSelect.value;
    teamSelect.innerHTML = '<option value="all">All Teams</option>';
    [...teams].sort().forEach(team => {
        const option = document.createElement('option');
        option.value = team;
        option.textContent = team;
        teamSelect.appendChild(option);
    });
    teamSelect.value = currentTeam;


    // Populate Skillsets
    const currentSkill = skillsetSelect.value;
    skillsetSelect.innerHTML = '<option value="all">All Skillsets</option>';
    [...skills].sort().forEach(skill => {
        const option = document.createElement('option');
        option.value = skill;
        option.textContent = skill;
        skillsetSelect.appendChild(option);
    });
    skillsetSelect.value = currentSkill;

    // Populate Workers
    const currentWorker = workerSelect.value;
    workerSelect.innerHTML = '<option value="all">All Workers</option>';
    allWorkers.sort((a, b) => a.name.localeCompare(b.name)).forEach(worker => {
        const option = document.createElement('option');
        option.value = worker.id;
        option.textContent = worker.name;
        workerSelect.appendChild(option);
    });
    workerSelect.value = currentWorker;
}

// Helper to get a set of non-working days (weekends and common holidays)
function getNonWorkingDaysSet() {
    const nonWorkingDays = new Set();
    if (!scenarioData.holidays) return nonWorkingDays;

    const allProductLines = Object.keys(scenarioData.holidays);
    if (allProductLines.length === 0) return nonWorkingDays;

    // Find intersection of all holiday dates
    let commonHolidays = new Set(scenarioData.holidays[allProductLines[0]]);
    for (let i = 1; i < allProductLines.length; i++) {
        const productHolidays = new Set(scenarioData.holidays[allProductLines[i]]);
        commonHolidays = new Set([...commonHolidays].filter(date => productHolidays.has(date)));
    }

    commonHolidays.forEach(dateStr => nonWorkingDays.add(dateStr));

    // Add weekends for a reasonable range (e.g., 2 years)
    const today = new Date();
    for (let i = -365; i < 365; i++) {
        const date = new Date(today);
        date.setDate(today.getDate() + i);
        if (date.getDay() === 0 || date.getDay() === 6) { // Sunday or Saturday
            nonWorkingDays.add(date.toISOString().split('T')[0]);
        }
    }
    return nonWorkingDays;
}

// New function to render the custom header for the advanced Gantt
function renderAdvancedGanttHeader() {
    const headerContainer = document.querySelector('.gantt-header-advanced');
    if (!headerContainer || !workerGantt) return;

    const nonWorkingDays = getNonWorkingDaysSet();
    const NON_WORKING_DAY_COLOR = '#e5e7eb'; // A darker grey

    headerContainer.innerHTML = '';
    const window = workerGantt.getWindow();
    let current = new Date(window.start);
    current.setHours(0, 0, 0, 0);

    const totalWidth = workerGantt.body.dom.center.clientWidth;
    const timeToPixels = totalWidth / (window.end - window.start);

    // Create containers for each row of the header
    const dateRow = document.createElement('div');
    const shiftRow = document.createElement('div');
    dateRow.style.whiteSpace = 'nowrap';
    shiftRow.style.whiteSpace = 'nowrap';

    dateRow.style.display = 'flex';
    shiftRow.style.display = 'flex';


    // Get timescale value
    const timescale = document.getElementById('wg-timescale-filter').value;

    while (current < window.end) {
        const dayStart = new Date(current);

        const displayShift3Start = new Date(dayStart); displayShift3Start.setHours(0);
        const displayShift1Start = new Date(dayStart); displayShift1Start.setHours(8);
        const displayShift2Start = new Date(dayStart); displayShift2Start.setHours(16);
        const displayShift2End = new Date(dayStart); displayShift2End.setHours(24);

        const dateWidth = (displayShift2End - displayShift3Start) * timeToPixels;
        const shiftWidth = dateWidth / 3;

        // Date Header - create three cells to position date over 1st shift
        const isNonWorking = nonWorkingDays.has(dayStart.toISOString().split('T')[0]);

        const dateCell1 = document.createElement('div');
        dateCell1.style.width = `${shiftWidth}px`;
        if (isNonWorking) dateCell1.style.backgroundColor = NON_WORKING_DAY_COLOR;
        dateRow.appendChild(dateCell1);

        const dateCell2 = document.createElement('div');
        dateCell2.className = 'gantt-header-item header-date';
        dateCell2.textContent = dayStart.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        dateCell2.style.width = `${shiftWidth}px`;
        dateCell2.style.textAlign = 'center';
        if (isNonWorking) dateCell2.style.backgroundColor = NON_WORKING_DAY_COLOR;
        dateRow.appendChild(dateCell2);

        const dateCell3 = document.createElement('div');
        dateCell3.style.width = `${shiftWidth}px`;
        if (isNonWorking) dateCell3.style.backgroundColor = NON_WORKING_DAY_COLOR;
        dateRow.appendChild(dateCell3);


        // Combined Shift and Time Headers
        const shifts = ['3rd', '1st', '2nd'];
        shifts.forEach(shiftText => {
            const shiftContainer = document.createElement('div');
            shiftContainer.className = 'gantt-header-item';
            if (isNonWorking) shiftContainer.style.backgroundColor = NON_WORKING_DAY_COLOR;
            shiftContainer.style.width = `${shiftWidth}px`;
            shiftContainer.style.display = 'flex';
            shiftContainer.style.flexDirection = 'column';
            shiftContainer.style.alignItems = 'center';
            shiftContainer.style.height = '40px'; // Combined height

            const shiftNameDiv = document.createElement('div');
            shiftNameDiv.className = 'header-shift';
            shiftNameDiv.textContent = shiftText;
            shiftContainer.appendChild(shiftNameDiv);

            const timeContainer = document.createElement('div');
            timeContainer.className = 'header-time';
            timeContainer.style.width = '100%';
            timeContainer.style.display = 'flex';
            timeContainer.style.alignItems = 'center';
            timeContainer.style.padding = '0 5px';
            timeContainer.style.fontSize = '12px';
            timeContainer.style.boxSizing = 'border-box';


            const shiftInfo = SHIFT_HOURS[shiftText];
            const formatTime = (hours) => {
                const h = Math.floor(hours);
                const m = Math.round((hours - h) * 60);
                const d = new Date();
                d.setHours(h, m);
                return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true }).replace(' AM', 'a').replace(' PM', 'p');
            };

            const startTime = formatTime(shiftInfo.start);
            const endTime = formatTime(shiftInfo.end);

            if (timescale === '1' || timescale === '2') {
                timeContainer.style.justifyContent = 'space-between';
                const midTimeValue = shiftInfo.start + shiftInfo.duration / 2;
                const midTime = formatTime(midTimeValue >= 24 ? midTimeValue - 24 : midTimeValue);
                timeContainer.innerHTML = `
                    <span style="text-align: left;">${startTime}</span>
                    <span style="text-align: center;">${midTime}</span>
                    <span style="text-align: right;">${endTime}</span>
                `;
            } else { // 1 week and 2 week views
                timeContainer.style.justifyContent = 'flex-start';
                timeContainer.innerHTML = `<span style="text-align: left;">${startTime}</span>`;
            }
            shiftContainer.appendChild(timeContainer);
            shiftRow.appendChild(shiftContainer);
        });

        current.setDate(current.getDate() + 1);
    }
     headerContainer.appendChild(dateRow);
     headerContainer.appendChild(shiftRow);
}


// New function to render the custom sidebar for the advanced Gantt
function renderAdvancedGanttSidebar(teams) {
    const sidebarContainer = document.querySelector('.gantt-sidebar-advanced');
    if (!sidebarContainer) return;

    let sidebarHTML = '<div style="display: flex; flex-direction: column;">';
    const workerRowHeight = 36; // This MUST match the height of rows in vis.js timeline items

    Object.keys(teams).sort().forEach(teamName => {
        const workersInTeam = teams[teamName];
        if (workersInTeam.length > 0) {
            const teamLabelHeight = workersInTeam.length * workerRowHeight;

            sidebarHTML += `<div style="display: flex; height: ${teamLabelHeight}px; border-bottom: 2px solid #ccc;">`;
            sidebarHTML += `<div class="team-group-label" style="height: 100%; width: 40px;">${teamName}</div>`;
            sidebarHTML += `<div style="display: flex; flex-direction: column; flex-grow: 1;">`;

            workersInTeam.forEach(worker => {
                const workerName = `${worker.name}${worker.skill ? ` (${worker.skill})` : ''}`;
                sidebarHTML += `<div class="worker-group-label" style="height: ${workerRowHeight}px; line-height: ${workerRowHeight}px; border-top: 1px solid #eee;">${workerName}</div>`;
            });

            sidebarHTML += `</div></div>`;
        }
    });

    sidebarContainer.innerHTML = sidebarHTML + '</div>';
}

function renderWorkerGantt() {
    console.log("Rendering Advanced Worker Gantt...");
    if (!workerGantt) {
        initializeWorkerGantt();
        return;
    }

    const assignments = savedAssignments[currentScenario] || {};
    const mechanicSchedules = generateMechanicSchedulesFromAssignments(assignments);

    if (Object.keys(mechanicSchedules).length === 0) {
        document.getElementById('worker-gantt-container-advanced').innerHTML = `<div style="padding: 40px; text-align: center; color: #6b7280;"><h3>No Task Assignments Available</h3></div>`;
        document.querySelector('.gantt-sidebar-advanced').innerHTML = '';
        document.querySelector('.gantt-header-advanced').innerHTML = '';
        return;
    }

    const selectedTeamFilter = document.getElementById('wg-team-filter').value;
    const selectedShift = document.getElementById('wg-shift-filter').value;
    const selectedSkill = document.getElementById('wg-skillset-filter').value;
    const selectedWorker = document.getElementById('wg-worker-filter').value;

    let allWorkers = [];
    Object.entries(scenarioData.teamCapacities).forEach(([teamSkill, capacity]) => {
        const parsed = parseTeamSkill(teamSkill);
        const baseTeam = parsed.baseTeam;
        const skill = parsed.skill;
        const shift = parsed.shift;
        const shiftLabel = shift ? `${shift}${shift === 1 ? 'st' : shift === 2 ? 'nd' : 'rd'}` : null;
        for (let i = 1; i <= capacity; i++) {
            const workerId = `${teamSkill}_${i}`;
            const workerName = buildResourceLabel(baseTeam, i, shift, skill);
            allWorkers.push({ id: workerId, name: workerName, team: baseTeam, skill: skill, shift: shiftLabel });
        }
    });

    let filteredWorkers = allWorkers.filter(w => (selectedTeamFilter === 'all' || w.team === selectedTeamFilter) && (selectedShift === 'all' || w.shift === selectedShift) && (selectedSkill === 'all' || w.skill === selectedSkill) && (selectedWorker === 'all' || w.id === selectedWorker));

    if (filteredWorkers.length === 0) {
        document.getElementById('worker-gantt-container-advanced').innerHTML = `<div style="padding: 40px; text-align: center; color: #6b7280;"><h3>No Workers Match Filters</h3></div>`;
        document.querySelector('.gantt-sidebar-advanced').innerHTML = '';
        document.querySelector('.gantt-header-advanced').innerHTML = '';
        return;
    }

    const teams = {};
    filteredWorkers.forEach(w => {
        if (!teams[w.team]) teams[w.team] = [];
        teams[w.team].push(w);
    });

    const visGroups = new vis.DataSet();
    const visItems = new vis.DataSet();
    let groupOrder = 0;

    const sortedTeamNames = Object.keys(teams).sort();
    const orderedWorkers = [];

    sortedTeamNames.forEach(teamName => {
        const workersInTeam = teams[teamName];
        const shiftOrder = { '1st': 1, '2nd': 2, '3rd': 3 };
        workersInTeam.sort((a, b) => {
            const shiftCompare = (shiftOrder[a.shift] || 99) - (shiftOrder[b.shift] || 99);
            if (shiftCompare !== 0) return shiftCompare;
            const numA = parseInt(a.name.match(/\d+$/)?.[0] || 0);
            const numB = parseInt(b.name.match(/\d+$/)?.[0] || 0);
            return numA - numB;
        });

        workersInTeam.forEach(worker => {
            orderedWorkers.push(worker);
            visGroups.add({
                id: worker.id,
                content: '', // Content is now in the custom sidebar
                order: groupOrder++
            });
        });
    });

    const productColors = {};
    const lightColors = ["#E0BBE4", "#957DAD", "#D291BC", "#FEC8D8", "#FFDFD3"];
    let colorIndex = 0;

    orderedWorkers.forEach(worker => {
        const schedule = mechanicSchedules[worker.id] || null;
        if (schedule && schedule.tasks) {
            schedule.tasks.forEach(task => {
                if (!productColors[task.product]) {
                    productColors[task.product] = lightColors[colorIndex % lightColors.length];
                    colorIndex++;
                }
                const realStart = new Date(task.startTime);
                const realEnd = new Date(task.endTime);
                const displayStart = mapRealTimeToDisplayTime(realStart, worker.shift);
                const displayEnd = mapRealTimeToDisplayTime(realEnd, worker.shift);
                const uniqueItemId = `${worker.id}_${task.taskId}`;

                visItems.add({
                    id: uniqueItemId,
                    group: worker.id,
                    content: task.taskId,
                    start: displayStart,
                    end: displayEnd,
                    title: `Task: ${task.taskId}<br>Team: ${task.team || 'N/A'}<br>Product: ${task.product || 'N/A'}<br>Worker: ${worker.name || 'N/A'}<br>Shift: ${worker.shift || 'N/A'}<br>Duration: ${task.duration || 'N/A'} min<br>Real Start: ${realStart.toLocaleString()}<br>Real End: ${realEnd.toLocaleString()}`,
                    style: `background-color: ${productColors[task.product]}; border-color: ${productColors[task.product]};`,
                    className: `wg-task ${task.isCritical ? 'wg-critical' : ''}`
                });
            });
        }
    });

    workerGantt.setGroups(visGroups);
    workerGantt.setItems(visItems);

    renderAdvancedGanttSidebar(teams);
    renderAdvancedGanttHeader();

    const legendContainer = document.getElementById('wg-product-legend');
    if (legendContainer) {
        legendContainer.innerHTML = '';
        Object.keys(productColors).forEach(product => {
            const color = productColors[product];
            const legendItem = document.createElement('div');
            legendItem.style.cssText = 'display: flex; align-items: center; gap: 6px; font-size: 12px;';
            legendItem.innerHTML = `<div style="width: 16px; height: 16px; background-color: ${color}; border-radius: 3px;"></div> <span>${product}</span>`;
            legendContainer.appendChild(legendItem);
        });
    }
}

function updateWorkerGanttWindow() {
    if (!workerGantt) return;

    const viewDateInput = document.getElementById('wg-view-date');
    const timescaleSelect = document.getElementById('wg-timescale-filter');

    if (!viewDateInput || !timescaleSelect) return;

    // Get the start date from the picker and set it to the beginning of that day (midnight)
    const startDate = new Date(viewDateInput.value || new Date());
    startDate.setHours(0, 0, 0, 0);

    const durationDays = parseInt(timescaleSelect.value, 10);

    // Calculate the end date by adding the duration to the start date
    const endDate = new Date(startDate.getTime());
    endDate.setDate(endDate.getDate() + durationDays);

    // Set the timeline window. The key is to ensure the start and end are aligned with midnight.
    workerGantt.setWindow(startDate, endDate, { animation: true });

    console.log(`Gantt window updated to: ${startDate.toLocaleString()} - ${endDate.toLocaleString()}`);
}

// Moves the timeline window back or forward based on the selected timescale.
function moveTimeline(direction) {
    if (!workerGantt) return;

    // Get the number of days to jump from the timescale filter
    const timescaleSelect = document.getElementById('wg-timescale-filter');
    const daysToJump = parseInt(timescaleSelect.value, 10);

    // If the value is not a valid number, default to 1 day
    if (isNaN(daysToJump)) {
        console.error("Invalid timescale value:", timescaleSelect.value);
        return;
    }

    // Get the current date from the date picker, which is our source of truth
    const viewDateInput = document.getElementById('wg-view-date');
    const currentDate = new Date(viewDateInput.value || new Date());

    // Calculate the new date
    let newDate = new Date(currentDate);
    if (direction === 'back') {
        newDate.setDate(newDate.getDate() - daysToJump);
    } else if (direction === 'forward') {
        newDate.setDate(newDate.getDate() + daysToJump);
    } else {
        return; // Invalid direction
    }

    // Set the new date in the date picker
    // This will trigger the 'change' event on the date input,
    // which in turn calls updateWorkerGanttWindow() to redraw the timeline.
    // This ensures we are using the existing logic and keeping the UI consistent.
    viewDateInput.valueAsDate = newDate;
    viewDateInput.dispatchEvent(new Event('change'));
}



// Main view update function
function updateView() {
    console.log(`Updating view: ${currentView} for scenario: ${currentScenario}`);

    const mainContent = document.querySelector('.main-content') || document.querySelector('.ios-main-content');
    if (!mainContent) {
        console.error('Main content area not found!');
        return;
    }

    // Hide all views first
    document.querySelectorAll('.view-content').forEach(view => {
        view.style.display = 'none';
    });

    // Show the active view
    const activeView = document.getElementById(`${currentView}-view`);
    if (activeView) {
        activeView.style.display = 'block';
    } else {
        console.error(`View content for '${currentView}' not found.`);
    }

    // Call view-specific update functions
    switch (currentView) {
        case 'team-lead':
            console.log('Calling updateTeamLeadView');
            updateTeamLeadView();
            break;
        case 'management':
            console.log('Calling updateManagementView');
            updateManagementView();
            break;
        case 'mechanic':
            console.log('Calling updateMechanicView');
            updateMechanicView();
            break;
        case 'project':
            console.log('Calling initializeCustomGantt');
            initializeCustomGantt();
            break;
        case 'supply-chain':
            console.log('Calling updateSupplyChainView');
            updateSupplyChainView();
            break;
        case 'worker-gantt':
            console.log('Calling initializeWorkerGantt');
            initializeWorkerGantt();
            break;
        case 'scenario':
            console.log('Calling initScenarioView');
            initScenarioView();
            break;
        case 'industrial-engineering':
            console.log('Calling updateIEView');
            updateIEView();
            break;
        case 'development':
            console.log('Calling updateDevelopmentView');
            updateDevelopmentView();
            break;
        case 'shift-performance':
            console.log('Calling autoLoadLatestPerformance');
            autoLoadLatestPerformance();
            break;
        case 'staffing':
            console.log('Calling staffing initialization');
            // Primary: use the staffing-dashboard.js module (handles both templates)
            if (typeof initializeStaffingDashboard === 'function') {
                initializeStaffingDashboard();
            }
            // Fallback: try loading the simple 3-stage staffing table
            if (currentScenario === '3stage') {
                load3StageStaffingData();
            }
            break;
        default:
            console.log(`No update function for view: ${currentView}`);
    }
}

// Helper functions to identify team resource types.
// Uses teamMetadata from optimizer export when available (definitive classification),
// falls back to name-based heuristics for backward compatibility.
function _teamResourceType(teamName) {
    if (!teamName) return 'mechanic';
    if (scenarioData && scenarioData.teamMetadata && scenarioData.teamMetadata[teamName]) {
        return scenarioData.teamMetadata[teamName].resource_type || 'mechanic';
    }
    // Fallback heuristics for schedules without teamMetadata
    if (teamName.toUpperCase().startsWith('QA-') || teamName.toLowerCase().includes('quality')) return 'quality';
    if (teamName.toUpperCase().includes('VENDOR')) return 'vendor';
    return 'mechanic';
}

function isQualityTeam(teamName) {
    return _teamResourceType(teamName) === 'quality';
}

function isCustomerTeam(teamName) {
    return _teamResourceType(teamName) === 'customer';
}

function isVendorTeam(teamName) {
    return _teamResourceType(teamName) === 'vendor';
}

function populateTeamDropdowns() {
    console.log(`Populating team dropdowns for scenario: ${currentScenario}`);

    if (!scenarioData || !scenarioData.teamCapacities) {
        console.warn('No team capacity data available in current scenario');
        return;
    }

    const teamCapacities = scenarioData.teamCapacities;

    // Extract base teams and aggregate capacities
    const baseTeams = new Map();
    const teamSkills = new Map();

    Object.entries(teamCapacities).forEach(([teamSkill, capacity]) => {
        const parsed = parseTeamSkill(teamSkill);
        const baseTeam = parsed.baseTeam;
        const skill = parsed.skill;

        if (!baseTeams.has(baseTeam)) {
            baseTeams.set(baseTeam, 0);
            teamSkills.set(baseTeam, new Set());
        }
        baseTeams.set(baseTeam, baseTeams.get(baseTeam) + capacity);

        if (skill) {
            teamSkills.get(baseTeam).add(skill);
        }
    });

    // Parse teams into hierarchical structure
    // Build superintendent-to-team mapping from actual task data
    const mechanicsBySuper = new Map();
    const qualityBySuper = new Map();
    const customerCodes = new Map();

    // Build team-to-superintendent mapping from tasks (actual superintendent values)
    const teamToSuperintendent = new Map();
    if (scenarioData.tasks && scenarioData.tasks.length > 0) {
        scenarioData.tasks.forEach(task => {
            const taskTeam = task.team || '';
            const taskSuper = task.superintendent || '';
            if (taskTeam && taskSuper && taskSuper !== 'UNKNOWN') {
                teamToSuperintendent.set(taskTeam, taskSuper);
            }
        });
    }

    // Also track vendor teams separately
    const vendorTeams = new Map();

    baseTeams.forEach((capacity, team) => {
        const resType = _teamResourceType(team);

        if (resType === 'customer') {
            customerCodes.set(team, capacity);
        } else if (resType === 'vendor') {
            vendorTeams.set(team, capacity);
        } else {
            // Get superintendent from task data mapping, fallback to extraction from team name
            let superintendent = teamToSuperintendent.get(team);

            if (!superintendent) {
                // Fallback: try to extract from team name prefix and map to superintendent format
                const superMatch = team.match(/^(P\d{2}|POSITION\s*\d+)/i);
                if (superMatch) {
                    const prefix = superMatch[1].toUpperCase();
                    if (prefix.startsWith('P') && prefix.length >= 2) {
                        const posNum = parseInt(prefix.substring(1, 2));
                        superintendent = `S-CF-POSITION ${posNum}`;
                    } else if (prefix.startsWith('POSITION')) {
                        superintendent = prefix.replace(/POSITION\s*/i, 'S-CF-POSITION ');
                    }
                }
            }

            if (!superintendent) {
                superintendent = 'Other';
            }

            const targetMap = (resType === 'quality') ? qualityBySuper : mechanicsBySuper;
            if (!targetMap.has(superintendent)) {
                targetMap.set(superintendent, []);
            }
            targetMap.get(superintendent).push({ name: team, capacity: capacity });
        }
    });

    // Sort superintendents and teams
    const sortSupers = (map) => {
        const sorted = new Map([...map.entries()].sort((a, b) => a[0].localeCompare(b[0])));
        sorted.forEach((teams) => teams.sort((a, b) => a.name.localeCompare(b.name)));
        return sorted;
    };

    const mechanicHierarchy = sortSupers(mechanicsBySuper);
    const qualityHierarchy = sortSupers(qualityBySuper);
    const sortedCustomers = new Map([...customerCodes.entries()].sort((a, b) => a[0].localeCompare(b[0])));

    // Update team dropdown with hierarchical structure
    const teamSelect = document.getElementById('teamSelect');
    if (teamSelect) {
        // Save current multi-select state from global (more reliable than DOM .value for multi-select)
        const savedTeams = selectedTeams && selectedTeams.length > 0 ? selectedTeams.slice() : ['all'];

        teamSelect.innerHTML = `
            <option value="all">All Teams</option>
        `;

        // Add Manufacturing Teams (hierarchical by superintendent)
        if (mechanicHierarchy.size > 0) {
            const mfgOptgroup = document.createElement('optgroup');
            mfgOptgroup.label = '━━━ MANUFACTURING TEAMS ━━━';
            mfgOptgroup.disabled = true;
            teamSelect.appendChild(mfgOptgroup);

            const option = document.createElement('option');
            option.value = 'all-mechanics';
            option.textContent = 'All Manufacturing Teams';
            teamSelect.appendChild(option);

            mechanicHierarchy.forEach((teams, superintendent) => {
                // Add superintendent-level option
                const superOption = document.createElement('option');
                superOption.value = `super-mech-${superintendent}`;
                const totalCap = teams.reduce((sum, t) => sum + t.capacity, 0);
                superOption.textContent = `  All ${superintendent} (${teams.length} teams, ${totalCap} capacity)`;
                teamSelect.appendChild(superOption);

                // Add individual teams under this superintendent
                teams.forEach(team => {
                    const teamOption = document.createElement('option');
                    teamOption.value = team.name;
                    teamOption.textContent = `    ${team.name} (${team.capacity})`;
                    teamSelect.appendChild(teamOption);
                });
            });
        }

        // Add Quality Teams (hierarchical by superintendent)
        if (qualityHierarchy.size > 0) {
            const qualOptgroup = document.createElement('optgroup');
            qualOptgroup.label = '━━━ QUALITY TEAMS ━━━';
            qualOptgroup.disabled = true;
            teamSelect.appendChild(qualOptgroup);

            const option = document.createElement('option');
            option.value = 'all-quality';
            option.textContent = 'All Quality Teams';
            teamSelect.appendChild(option);

            qualityHierarchy.forEach((teams, superintendent) => {
                // Add superintendent-level option
                const superOption = document.createElement('option');
                superOption.value = `super-qual-${superintendent}`;
                const totalCap = teams.reduce((sum, t) => sum + t.capacity, 0);
                superOption.textContent = `  All ${superintendent} (${teams.length} teams, ${totalCap} capacity)`;
                teamSelect.appendChild(superOption);

                // Add individual teams under this superintendent
                teams.forEach(team => {
                    const teamOption = document.createElement('option');
                    teamOption.value = team.name;
                    teamOption.textContent = `    ${team.name} (${team.capacity})`;
                    teamSelect.appendChild(teamOption);
                });
            });
        }

        // Add Customer Codes (flat list)
        if (sortedCustomers.size > 0) {
            const custOptgroup = document.createElement('optgroup');
            custOptgroup.label = '━━━ CUSTOMERS ━━━';
            custOptgroup.disabled = true;
            teamSelect.appendChild(custOptgroup);

            const option = document.createElement('option');
            option.value = 'all-customer';
            option.textContent = 'All Customers';
            teamSelect.appendChild(option);

            sortedCustomers.forEach((capacity, customerCode) => {
                const custOption = document.createElement('option');
                custOption.value = customerCode;
                custOption.textContent = `  ${customerCode} (${capacity})`;
                teamSelect.appendChild(custOption);
            });
        }

        // Add Vendor Teams (flat list)
        const sortedVendors = new Map([...vendorTeams.entries()].sort((a, b) => a[0].localeCompare(b[0])));
        if (sortedVendors.size > 0) {
            const vendOptgroup = document.createElement('optgroup');
            vendOptgroup.label = '━━━ VENDORS ━━━';
            vendOptgroup.disabled = true;
            teamSelect.appendChild(vendOptgroup);

            const option = document.createElement('option');
            option.value = 'all-vendor';
            option.textContent = 'All Vendors';
            teamSelect.appendChild(option);

            sortedVendors.forEach((capacity, vendorName) => {
                const vendOption = document.createElement('option');
                vendOption.value = vendorName;
                vendOption.textContent = `  ${vendorName} (${capacity})`;
                teamSelect.appendChild(vendOption);
            });
        }

        // Restore multi-select selection from saved state
        // First deselect all, then re-select the saved values
        Array.from(teamSelect.options).forEach(opt => opt.selected = false);
        const matchingOptions = Array.from(teamSelect.options).filter(opt =>
            savedTeams.includes(opt.value)
        );
        if (matchingOptions.length > 0) {
            matchingOptions.forEach(opt => opt.selected = true);
        } else {
            teamSelect.options[0].selected = true;  // Select "All Teams"
            selectedTeams = ['all'];
        }
    }

    // Populate superintendent dropdown - NEW
    // Populate superintendent dropdown from ALL SOI data (not just current schedule)
    const superintendentSelect = document.getElementById('superintendentSelect');
    if (superintendentSelect) {
        const currentSuperSelection = selectedSuperintendent || superintendentSelect.value || 'all';

        // Fetch ALL superintendents from ALL SOI data
        fetch('/api/staffing-dashboard/all-soi-metadata')
            .then(response => response.json())
            .then(data => {
                const allSuperintendents = data.superintendents || [];

                // Rebuild superintendent dropdown
                superintendentSelect.innerHTML = '<option value="all">All Superintendents</option>';

                // Add superintendent options in sorted order
                allSuperintendents.forEach(superintendent => {
                    const option = document.createElement('option');
                    option.value = superintendent;

                    // Count teams under this superintendent from current schedule
                    const mechTeams = mechanicHierarchy.get(superintendent) || [];
                    const qualTeams = qualityHierarchy.get(superintendent) || [];
                    const totalTeams = mechTeams.length + qualTeams.length;

                    if (totalTeams > 0) {
                        option.textContent = `${superintendent} (${totalTeams} teams in schedule)`;
                    } else {
                        option.textContent = superintendent;
                    }
                    superintendentSelect.appendChild(option);
                });

                // Restore selection if still valid
                if (Array.from(superintendentSelect.options).some(opt => opt.value === currentSuperSelection)) {
                    superintendentSelect.value = currentSuperSelection;
                } else {
                    superintendentSelect.value = 'all';
                    selectedSuperintendent = 'all';
                }
            })
            .catch(error => {
                console.error('Error fetching ALL SOI metadata:', error);
                // Fallback to old behavior
                const allSuperintendents = new Set();
                mechanicHierarchy.forEach((teams, superintendent) => {
                    allSuperintendents.add(superintendent);
                });
                qualityHierarchy.forEach((teams, superintendent) => {
                    allSuperintendents.add(superintendent);
                });

                superintendentSelect.innerHTML = '<option value="all">All Superintendents</option>';
                Array.from(allSuperintendents).sort().forEach(superintendent => {
                    const option = document.createElement('option');
                    option.value = superintendent;
                    const mechTeams = mechanicHierarchy.get(superintendent) || [];
                    const qualTeams = qualityHierarchy.get(superintendent) || [];
                    const totalTeams = mechTeams.length + qualTeams.length;
                    option.textContent = `${superintendent} (${totalTeams} teams)`;
                    superintendentSelect.appendChild(option);
                });

                if (Array.from(superintendentSelect.options).some(opt => opt.value === currentSuperSelection)) {
                    superintendentSelect.value = currentSuperSelection;
                } else {
                    superintendentSelect.value = 'all';
                    selectedSuperintendent = 'all';
                }
            });
    }

    window.teamSkillsMap = teamSkills;
    window.mechanicHierarchy = mechanicHierarchy;
    window.qualityHierarchy = qualityHierarchy;
    updateSkillDropdown();
    updateShiftDropdown();
}

function updateSkillDropdown() {
    const skillSelect = document.getElementById('skillSelect');
    if (!skillSelect) return;

    // Save current selection from the global state (more reliable than DOM)
    const savedSkill = selectedSkill || skillSelect.value || 'all';

    // Helper to rebuild the dropdown atomically (don't clear until new options are ready)
    function rebuildSkillOptions(skillsSet) {
        const sortedSkills = Array.from(skillsSet).sort();

        // Build new HTML atomically - don't clear until replacement is ready
        let newHTML = '<option value="all">All Skills</option>';
        sortedSkills.forEach(skill => {
            const escaped = skill.replace(/"/g, '&quot;').replace(/</g, '&lt;');
            newHTML += `<option value="${escaped}">${escaped}</option>`;
        });
        skillSelect.innerHTML = newHTML;

        // Restore selection: prefer the global state, fall back to previous DOM value
        if (Array.from(skillSelect.options).some(opt => opt.value === savedSkill)) {
            skillSelect.value = savedSkill;
        } else {
            skillSelect.value = 'all';
            selectedSkill = 'all';
        }
    }

    // Gather skills from current schedule data (synchronous - always available)
    function getSkillsFromSchedule() {
        const skills = new Set();
        if (selectedTeams.includes('all') || selectedTeams.includes('all-mechanics') || selectedTeams.includes('all-quality')) {
            const teamFilter = selectedTeams.includes('all-mechanics') ? 'Mechanic' :
                             selectedTeams.includes('all-quality') ? 'Quality' : '';
            Object.keys(scenarioData.teamCapacities || {}).forEach(teamSkill => {
                if (teamFilter && !teamSkill.includes(teamFilter)) return;
                const skillMatch = teamSkill.match(/\((.+?)\)/);
                if (skillMatch) skills.add(skillMatch[1]);
            });
        } else if (selectedTeams.length > 0 && window.teamSkillsMap) {
            selectedTeams.forEach(team => {
                const teamSkills = window.teamSkillsMap.get(team);
                if (teamSkills) teamSkills.forEach(s => skills.add(s));
            });
        }
        return skills;
    }

    // Immediately rebuild from schedule data (synchronous, no flash)
    const scheduleSkills = getSkillsFromSchedule();
    rebuildSkillOptions(scheduleSkills);

    // Then try to supplement with ALL SOI metadata (async, enhances but doesn't reset)
    fetch('/api/staffing-dashboard/all-soi-metadata')
        .then(response => response.json())
        .then(data => {
            const allSkills = new Set(data.skills || []);
            // Merge with schedule skills
            scheduleSkills.forEach(s => allSkills.add(s));
            rebuildSkillOptions(allSkills);
        })
        .catch(error => {
            // Async fetch failed - keep the synchronous rebuild (already done above)
            console.warn('Skill metadata fetch failed, using schedule data only:', error.message);
        });
}

function updateShiftDropdown() {
    const shiftSelect = document.getElementById('shiftSelect');
    if (!shiftSelect || !scenarioData) return;

    // Use the global state as the source of truth for current selection
    const savedShift = selectedShift || shiftSelect.value || 'all';

    // Get available shifts based on selected team(s)
    let availableShifts = new Set();

    if (!scenarioData.teamShifts) {
        // Derive available shifts from teamCapacities keys (format: "TEAM S{N} (SKILL)")
        const tcKeys = Object.keys(scenarioData.teamCapacities || {});
        if (tcKeys.length > 0) {
            tcKeys.forEach(k => {
                const p = parseTeamSkill(k);
                if (p.shift) {
                    const label = p.shift === 1 ? '1st' : p.shift === 2 ? '2nd' : `${p.shift}rd`;
                    availableShifts.add(label);
                }
            });
            if (availableShifts.size === 0) {
                availableShifts.add('1st');
                availableShifts.add('2nd');
                availableShifts.add('3rd');
            }
        } else {
            availableShifts.add('1st');
            availableShifts.add('2nd');
            availableShifts.add('3rd');
        }
    } else {
        if (selectedTeams.includes('all')) {
            Object.values(scenarioData.teamShifts).forEach(shifts => {
                if (Array.isArray(shifts)) shifts.forEach(s => availableShifts.add(s));
            });
        } else if (selectedTeams.includes('all-mechanics')) {
            Object.entries(scenarioData.teamShifts).forEach(([team, shifts]) => {
                if (team.toLowerCase().includes('mechanic') || team.toLowerCase().includes('mech')) {
                    if (Array.isArray(shifts)) shifts.forEach(s => availableShifts.add(s));
                }
            });
        } else if (selectedTeams.includes('all-quality')) {
            Object.entries(scenarioData.teamShifts).forEach(([team, shifts]) => {
                if (team.toLowerCase().includes('quality') || team.toLowerCase().includes('qual')) {
                    if (Array.isArray(shifts)) shifts.forEach(s => availableShifts.add(s));
                }
            });
        } else {
            selectedTeams.forEach(selectedTeam => {
                const teamShifts = scenarioData.teamShifts[selectedTeam];
                if (Array.isArray(teamShifts)) {
                    teamShifts.forEach(s => availableShifts.add(s));
                } else {
                    availableShifts.add('1st');
                    availableShifts.add('2nd');
                    availableShifts.add('3rd');
                }
            });
        }
    }

    if (availableShifts.size === 0) {
        availableShifts.add('1st');
        availableShifts.add('2nd');
        availableShifts.add('3rd');
    }

    const shiftOrder = ['1st', '2nd', '3rd'];
    const sortedShifts = Array.from(availableShifts).sort((a, b) =>
        shiftOrder.indexOf(a) - shiftOrder.indexOf(b)
    );

    // Build new HTML atomically - don't clear until replacement is ready
    let newHTML = '<option value="all">All Shifts</option>';
    sortedShifts.forEach(shift => {
        let shiftLabel = shift + ' Shift';
        if (shift === '1st') shiftLabel += ' (6:00 AM - 2:30 PM)';
        else if (shift === '2nd') shiftLabel += ' (2:30 PM - 11:00 PM)';
        else if (shift === '3rd') shiftLabel += ' (11:00 PM - 6:00 AM)';
        newHTML += `<option value="${shift}">${shiftLabel}</option>`;
    });
    shiftSelect.innerHTML = newHTML;

    // Restore selection from global state
    if (Array.from(shiftSelect.options).some(opt => opt.value === savedShift)) {
        shiftSelect.value = savedShift;
    } else {
        shiftSelect.value = 'all';
        selectedShift = 'all';
    }
}

// CASCADE FILTER: Update team dropdown based on selected superintendent
function updateTeamDropdownForSuperintendent() {
    const teamSelect = document.getElementById('teamSelect');
    if (!teamSelect) return;

    // If "All Superintendents" selected, show all teams
    if (selectedSuperintendent === 'all') {
        // Repopulate full team dropdown
        populateTeamDropdowns();
        updateSkillDropdown();
        return;
    }

    // Filter teams to only those under the selected superintendent
    const teamsUnderSuper = new Set();

    // Method 1: Use the hierarchy maps (built from actual task data)
    if (window.mechanicHierarchy && window.mechanicHierarchy.get(selectedSuperintendent)) {
        const mechTeams = window.mechanicHierarchy.get(selectedSuperintendent) || [];
        mechTeams.forEach(t => teamsUnderSuper.add(t.name));
    }
    if (window.qualityHierarchy && window.qualityHierarchy.get(selectedSuperintendent)) {
        const qualTeams = window.qualityHierarchy.get(selectedSuperintendent) || [];
        qualTeams.forEach(t => teamsUnderSuper.add(t.name));
    }

    // Method 2: Also scan tasks directly (more reliable for superintendent matching)
    if (scenarioData && scenarioData.tasks) {
        scenarioData.tasks.forEach(task => {
            if (task.superintendent === selectedSuperintendent && task.team) {
                teamsUnderSuper.add(task.team);
            }
        });
    }

    // Clear and rebuild team dropdown
    const currentSelection = Array.from(teamSelect.selectedOptions).map(opt => opt.value);
    teamSelect.innerHTML = '';

    // Add "All Teams (for this superintendent)" option
    const allOption = document.createElement('option');
    allOption.value = 'all';
    allOption.textContent = `All Teams (${selectedSuperintendent})`;
    teamSelect.appendChild(allOption);

    // Add filtered teams (sorted)
    Array.from(teamsUnderSuper).sort().forEach(teamName => {
        const option = document.createElement('option');
        option.value = teamName;
        option.textContent = teamName;
        // Restore selection if it was previously selected
        if (currentSelection.includes(teamName)) {
            option.selected = true;
        }
        teamSelect.appendChild(option);
    });

    // If no previous selections are valid, select "all"
    if (Array.from(teamSelect.selectedOptions).length === 0) {
        allOption.selected = true;
        selectedTeams = ['all'];
    } else {
        selectedTeams = Array.from(teamSelect.selectedOptions).map(opt => opt.value);
    }

    // CASCADE: Update skill dropdown for the new team selection
    updateSkillDropdown();

    // Update the view with the new filter
    updateTeamLeadView();

    console.log(`Filtered teams for superintendent ${selectedSuperintendent}:`, Array.from(teamsUnderSuper));
}

// Fallback fetch for superintendent team mapping (kept for API data enrichment)
function fetchTeamDataForSuperintendent() {
    fetch('/api/staffing-dashboard/all-soi-metadata')
        .then(response => response.json())
        .then(data => {
            console.log('ALL SOI metadata loaded for superintendent cascade');
        })
        .catch(error => {
            console.error('Error fetching team data for superintendent filter:', error);
            // Fallback: just update skill dropdown
            updateSkillDropdown();
        });
}

// Helper function to check if a team matches the selected teams filter
// @param taskBaseTeam - The base team name (e.g., "P11-CF-WBJ")
// @param isCustomerTask - Whether this is a customer task
// @param isQualityTask - Whether this is a quality task
// @param taskSuperintendent - The superintendent for this task (e.g., "S-CF-POSITION 1")
function teamMatchesSelection(taskBaseTeam, isCustomerTask, isQualityTask, taskSuperintendent = null) {
    // Check superintendent filter first
    if (selectedSuperintendent !== 'all') {
        // If taskSuperintendent is provided, use it directly for matching
        if (taskSuperintendent) {
            if (taskSuperintendent !== selectedSuperintendent) {
                return false;
            }
        } else {
            // Fallback: try to match team name prefix (legacy behavior for team capacities)
            // Extract superintendent prefix from team name (e.g., "P11" from "P11-CF-WBJ")
            const teamPrefix = taskBaseTeam ? taskBaseTeam.match(/^([A-Z]+\d+)/i)?.[1] : null;
            // Also check if selectedSuperintendent contains a position number we can match
            const superMatch = selectedSuperintendent.match(/POSITION\s*(\d+)/i);
            if (superMatch && teamPrefix) {
                const positionNum = superMatch[1];
                // Map superintendent position to team prefix (P11 = POSITION 1, P22 = POSITION 2, etc.)
                const expectedPrefix = `P${positionNum}${positionNum}`;
                if (!teamPrefix.toUpperCase().startsWith(`P${positionNum}`)) {
                    return false;
                }
            } else {
                // If we can't determine, skip this check to avoid false negatives
                // This allows team capacities to still be shown
            }
        }
    }

    // Then check team selection (array)
    for (const selectedTeam of selectedTeams) {
        if (selectedTeam === 'all') {
            return true;  // Include ALL tasks
        } else if (selectedTeam === 'all-mechanics') {
            if (!isCustomerTask && !isQualityTask && taskBaseTeam && !isQualityTeam(taskBaseTeam) && !isVendorTeam(taskBaseTeam)) {
                return true;
            }
        } else if (selectedTeam === 'all-quality') {
            if (!isCustomerTask && taskBaseTeam && isQualityTeam(taskBaseTeam)) {
                return true;
            }
        } else if (selectedTeam === 'all-customer') {
            if (isCustomerTask) {
                return true;
            }
        } else if (selectedTeam === 'all-vendor') {
            if (taskBaseTeam && isVendorTeam(taskBaseTeam)) {
                return true;
            }
        } else if (selectedTeam.startsWith('super-mech-')) {
            const superCode = selectedTeam.replace('super-mech-', '');
            // If we have taskSuperintendent, use it for matching
            if (taskSuperintendent) {
                // Match if superintendent contains the superCode (e.g., "S-CF-POSITION 1" contains "POSITION 1")
                if (!isCustomerTask && !isQualityTask &&
                    taskSuperintendent.toUpperCase().includes(superCode.toUpperCase()) &&
                    !isQualityTeam(taskBaseTeam)) {
                    return true;
                }
            } else {
                // Fallback to team name prefix matching (for team capacities)
                if (!isCustomerTask && !isQualityTask &&
                    taskBaseTeam && taskBaseTeam.match(new RegExp(`^${superCode}`, 'i')) &&
                    !isQualityTeam(taskBaseTeam)) {
                    return true;
                }
            }
        } else if (selectedTeam.startsWith('super-qual-')) {
            const superCode = selectedTeam.replace('super-qual-', '');
            // If we have taskSuperintendent, use it for matching
            if (taskSuperintendent) {
                // Match if superintendent contains the superCode
                if (!isCustomerTask &&
                    taskSuperintendent.toUpperCase().includes(superCode.toUpperCase()) &&
                    isQualityTeam(taskBaseTeam)) {
                    return true;
                }
            } else {
                // Fallback to team name prefix matching (for team capacities)
                if (!isCustomerTask &&
                    taskBaseTeam && taskBaseTeam.match(new RegExp(`^${superCode}`, 'i')) &&
                    isQualityTeam(taskBaseTeam)) {
                    return true;
                }
            }
        } else {
            // Individual team selection
            if (selectedTeam.toLowerCase().includes('customer')) {
                if (isCustomerTask) {
                    return true;
                }
            } else {
                if (taskBaseTeam === selectedTeam) {
                    return true;
                }
            }
        }
    }
    return false;
}

// Enhanced Team Lead View with separate team and skill filtering
/**
 * Highlight search term in text
 * @param {string} text - Text to search in
 * @param {string} searchTerm - Term to highlight
 * @returns {string} HTML with highlighted text
 */
function highlightSearchTerm(text, searchTerm) {
    if (!text || !searchTerm || searchTerm.length === 0) {
        return text;
    }

    // Escape special regex characters in search term
    const escapedTerm = searchTerm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

    // Case-insensitive global search
    const regex = new RegExp(`(${escapedTerm})`, 'gi');

    // Wrap matches in <mark> tag with styling
    return text.replace(regex, '<mark style="background-color: #fef08a; padding: 1px 3px; border-radius: 2px; font-weight: 600;">$1</mark>');
}

async function updateTeamLeadView() {
    console.log(`[updateTeamLeadView] Fired. Current saved assignments for ${currentScenario}:`, JSON.parse(JSON.stringify(savedAssignments[currentScenario] || {})));

    if (!scenarioData) {
        console.error('[updateTeamLeadView] No scenario data available');
        showError('No scenario data loaded. Please refresh the page.');
        return;
    }

    if (!scenarioData.tasks || scenarioData.tasks.length === 0) {
        console.error('[updateTeamLeadView] No tasks in scenario data');
        showError('No tasks available in current scenario.');
        return;
    }

    // Show loading indicator for large datasets
    const taskCount = scenarioData.tasks?.length || 0;
    if (taskCount > 5000) {
        showLoading(`Processing ${taskCount.toLocaleString()} tasks...`);
    }

    // ========== SECTION 1: Calculate Team Capacity ==========
    let teamCap = 0;
    Object.entries(scenarioData.teamCapacities || {}).forEach(([teamSkill, capacity]) => {
        const parsed = parseTeamSkill(teamSkill);
        let baseTeam = parsed.baseTeam;
        let skill = parsed.skill;

        // Check if team matches using definitive resource type classification
        const isCustTeam = isCustomerTeam(baseTeam);
        const isQualTeam = isQualityTeam(baseTeam);
        const teamMatches = teamMatchesSelection(baseTeam, isCustTeam, isQualTeam);

        let skillMatches = selectedSkill === 'all' || skill === selectedSkill;
        if (teamMatches && skillMatches) {
            teamCap += capacity;
        }
    });
    document.getElementById('teamCapacity').textContent = teamCap;

    // ========== SECTION 2: Filter and Sort Tasks ==========
    let tasks = (scenarioData.tasks || []).filter(task => {
        const taskTeamSkill = task.teamSkill || task.team || '';
        let taskBaseTeam = task.team;
        let taskSkill = task.skill;

        // Identify customer tasks by task flags and team metadata
        const isCustomerTask = (task.taskId && task.taskId.includes('CC_')) ||
                              task.type === 'Customer' ||
                              task.type === 'Customer Inspection' ||
                              task.isCustomerTask === true ||
                              task.workGroup === 'customer' ||
                              isCustomerTeam(taskBaseTeam);

        if (taskTeamSkill.includes('(')) {
            const parsedTS = parseTeamSkill(taskTeamSkill);
            if (parsedTS.skill) {
                taskBaseTeam = parsedTS.baseTeam;
                taskSkill = parsedTS.skill;
            }
        }

        // Use helper function to check team match (handles multi-select + superintendent)
        // Pass task.superintendent for direct matching instead of regex-based team name matching
        const teamMatch = teamMatchesSelection(taskBaseTeam, isCustomerTask, task.isQualityTask || false, task.superintendent);

        let skillMatch = selectedSkill === 'all' || taskSkill === selectedSkill;
        // Convert display shift ('1st','2nd','3rd') to numeric for comparison with task.shift (int)
        const selectedShiftNum = selectedShift === '1st' ? 1 : selectedShift === '2nd' ? 2 : selectedShift === '3rd' ? 3 : selectedShift;
        const shiftMatch = selectedShift === 'all' || task.shift == selectedShiftNum;
        const productMatch = selectedProduct === 'all' || task.product === selectedProduct;

        // Customer (airline code) filter
        let customerMatch = selectedCustomer === 'all';
        if (!customerMatch) {
            let cc = task.customer_code || '';
            if (!cc && scenarioData.products) {
                const ln = task.line_number || 0;
                const prod = scenarioData.products.find(p => p.line_number === ln);
                if (prod) cc = prod.customer_code || '';
            }
            customerMatch = cc === selectedCustomer;
        }

        // Work group filter (mechanic, quality, customer, vendor)
        let workGroupMatch = selectedWorkGroup === 'all';
        if (!workGroupMatch) {
            // Determine task's work group
            const taskWorkGroup = task.workGroup ||
                (task.isCustomerTask ? 'customer' : (task.isQualityTask ? 'quality' : (task.isVendorTask ? 'vendor' : 'mechanic')));
            workGroupMatch = taskWorkGroup === selectedWorkGroup;
        }

        return teamMatch && skillMatch && shiftMatch && productMatch && customerMatch && workGroupMatch;
    });

    // CRITICAL FIX: Sort tasks by priority BEFORE slicing (priority 1 = highest)
    // Secondary sort by start time ensures segments display in execution order
    tasks.sort((a, b) => {
        const aPriority = a.priority || 9999999;  // Tasks without priority go to end
        const bPriority = b.priority || 9999999;

        // Primary sort: Priority (ascending - lower number = higher priority)
        if (aPriority !== bPriority) {
            return aPriority - bPriority;  // Ascending order (1, 2, 3, ...)
        }

        // Secondary sort: Start time (ascending - earlier time first)
        // This ensures segments with same priority display in chronological order
        const aStartTime = new Date(a.startTime || 0);
        const bStartTime = new Date(b.startTime || 0);
        return aStartTime - bStartTime;
    });

    // ========== SECTION 3: Filter to Top 200 per Team ==========
    // Group tasks by base team (not team-shift-skill)
    const tasksByTeam = {};
    tasks.forEach(task => {
        // Use task.team (ALL SOI name) for display grouping; teamSkill has staffing team for auto-assign
        const baseTeam = task.team || (task.teamSkill ? parseTeamSkill(task.teamSkill).baseTeam : null) || 'UNKNOWN';

        if (!tasksByTeam[baseTeam]) {
            tasksByTeam[baseTeam] = [];
        }
        tasksByTeam[baseTeam].push(task);
    });

    // Display all tasks (no per-team cap)
    const displayTasks = [];
    Object.values(tasksByTeam).forEach(teamTasks => {
        displayTasks.push(...teamTasks);
    });

    // Re-sort combined list by priority with secondary sort by start time
    displayTasks.sort((a, b) => {
        const aPriority = a.priority || 9999999;
        const bPriority = b.priority || 9999999;

        // Primary sort: Priority
        if (aPriority !== bPriority) {
            return aPriority - bPriority;
        }

        // Secondary sort: Start time (ensures segments display in execution order)
        const aStartTime = new Date(a.startTime || 0);
        const bStartTime = new Date(b.startTime || 0);
        return aStartTime - bStartTime;
    });

    const totalTasks = tasks.length;
    const uniqueTeams = Object.keys(tasksByTeam).length;

    // Tasks for this shift (count displayed, not just today)
    document.getElementById('tasksToday').textContent = displayTasks.length;
    const taskDetailEl = document.getElementById('tasksTodayDetail');
    if (taskDetailEl) {
        taskDetailEl.textContent = `${totalTasks} total, ${displayTasks.length} shown`;
    }

    // Critical tasks
    const critical = displayTasks.filter(t =>
        t.priority <= 10 || t.isLatePartTask || t.isReworkTask ||
        t.isCritical || (t.slackHours !== undefined && t.slackHours < 24)
    ).length;
    document.getElementById('criticalTasks').textContent = critical;

    // Aircraft count and detail
    const activeAircraft = new Set(displayTasks.map(t => t.line_number || 0).filter(ln => ln > 0));
    const aircraftEl = document.getElementById('aircraftCount');
    if (aircraftEl) aircraftEl.textContent = activeAircraft.size;
    const aircraftDetailEl = document.getElementById('aircraftDetail');
    if (aircraftDetailEl) {
        const lines = [...activeAircraft].sort((a, b) => a - b).slice(0, 5);
        aircraftDetailEl.textContent = lines.length <= 5 ? lines.join(', ') : lines.join(', ') + '...';
    }

    // Task type breakdown
    const typeCounts = {};
    displayTasks.forEach(t => {
        const tt = t.type || 'Other';
        typeCounts[tt] = (typeCounts[tt] || 0) + 1;
    });
    const breakdownEl = document.getElementById('taskTypeBreakdown');
    if (breakdownEl) {
        const entries = Object.entries(typeCounts).sort((a, b) => b[1] - a[1]);
        breakdownEl.innerHTML = entries.map(([type, count]) =>
            `<div style="display:flex;justify-content:space-between;"><span>${type}</span><strong>${count}</strong></div>`
        ).join('');
    }

    // Utilization — capacity-weighted average (exclude non-matchingteams)
    let totalWork = 0;
    let totalCapacity = 0;
    Object.entries(scenarioData.teamCapacities || {}).forEach(([teamSkill, capacity]) => {
        const parsed = parseTeamSkill(teamSkill);
        let baseTeam = parsed.baseTeam;
        let skill = parsed.skill;

        // Skip non-mechanic teams for utilization calc (unlimited capacity teams skew it)
        const resType = _teamResourceType(baseTeam);
        if (resType === 'customer' || resType === 'vendor') return;

        const teamMatches = teamMatchesSelection(baseTeam, false, resType === 'quality');
        let skillMatches = selectedSkill === 'all' || skill === selectedSkill;

        if (teamMatches && skillMatches && scenarioData.utilization && scenarioData.utilization[teamSkill]) {
            const u = scenarioData.utilization[teamSkill];
            if (typeof u === 'object' && u.work !== undefined) {
                totalWork += u.work;
                totalCapacity += u.capacity;
            } else {
                totalWork += u;
                totalCapacity += 100;
            }
        }
    });
    const avgUtil = totalCapacity > 0 ? Math.round(totalWork / totalCapacity * 100) : 0;
    document.getElementById('teamUtilization').textContent = avgUtil + '%';

    // ========== SECTION 4: Show Info about Filtering ==========
    if (totalTasks > displayTasks.length) {
        let warningDiv = document.getElementById('taskLimitWarning');
        if (!warningDiv) {
            warningDiv = document.createElement('div');
            warningDiv.id = 'taskLimitWarning';
            warningDiv.className = 'task-limit-warning';
            warningDiv.style.cssText = 'background: #DBEAFE; border: 1px solid #3B82F6; padding: 12px; margin-bottom: 15px; border-radius: 6px;';
            const tableContainer = document.querySelector('.task-table-container');
            if (tableContainer) {
                tableContainer.parentNode.insertBefore(warningDiv, tableContainer);
            }
        }
        warningDiv.innerHTML = `ℹ️ Showing top 200 priorities per team (${displayTasks.length.toLocaleString()} of ${totalTasks.toLocaleString()} total tasks across ${uniqueTeams} teams)`;
    } else {
        const warningDiv = document.getElementById('taskLimitWarning');
        if (warningDiv) warningDiv.remove();
    }

    // ========== SECTION 5: Generate Mechanic Options ONCE ==========
    const filterKey = `${currentScenario}_${selectedTeams.join('|')}_${selectedSuperintendent}_${selectedSkill}`;
    let mechanicOptions = '';

    if (mechanicOptionsCache[filterKey]) {
        mechanicOptions = mechanicOptionsCache[filterKey];
    } else {
        let optionsHtml = '<option value="">Unassigned</option>';

        Object.entries(scenarioData.teamCapacities || {}).forEach(([teamSkill, capacity]) => {
            const parsed = parseTeamSkill(teamSkill);
            let baseTeam = parsed.baseTeam;
            let skill = parsed.skill;
            let shift = parsed.shift;

            // Use definitive resource type classification
            const isCust = isCustomerTeam(baseTeam);
            const isQualTeam = isQualityTeam(baseTeam);
            let includeThis = teamMatchesSelection(baseTeam, isCust, isQualTeam);

            if (includeThis && selectedSkill !== 'all' && skill !== selectedSkill) {
                includeThis = false;
            }

            if (includeThis && capacity > 0) {
                for (let i = 1; i <= capacity; i++) {
                    const mechId = `${teamSkill}_${i}`;
                    const label = buildResourceLabel(baseTeam, i, shift, skill);
                    optionsHtml += `<option value="${mechId}">${label}</option>`;
                }
            }
        });

        mechanicOptions = optionsHtml;
        mechanicOptionsCache[filterKey] = mechanicOptions;
    }

    // ========== SECTION 6: Build Table HTML Efficiently ==========
    const tbody = document.getElementById('taskTableBody');
    const rows = [];

    displayTasks.forEach(task => {
        const startTime = new Date(task.startTime);
        const mechanicsNeeded = task.mechanics || 1;

        // Check if this is a customer task
        const isCustomerTask = (task.taskId && task.taskId.includes('CC_')) ||
                              task.type === 'Customer' ||
                              task.type === 'Customer Inspection' ||
                              task.isCustomerTask === true;

        let typeIndicator = '';
        if (isCustomerTask) typeIndicator = ' 👤';
        else if (task.isLatePartTask) typeIndicator = ' 📦';
        else if (task.isReworkTask) typeIndicator = ' 🔧';
        else if (task.isCritical) typeIndicator = ' ⚡';

        // Add segmentation badge for 3-stage scheduler
        let segmentBadge = '';
        if (task.is_duration_segment && task.segment_id !== undefined && task.total_segments) {
            segmentBadge = ` <span class="segment-badge" title="Duration segment ${task.segment_id + 1} of ${task.total_segments}">Seg ${task.segment_id + 1}/${task.total_segments}</span>`;
        }

        // Add inspection indicator for QA tasks
        let inspectionIndicator = '';
        if (task.is_inspection) {
            inspectionIndicator = ' <span class="inspection-badge" title="Quality inspection task">🔍 QA</span>';
        }

        let dependencyInfo = '';

        if (task.dependencies && task.dependencies.length > 0) {
            const deps = task.dependencies.slice(0, 3).map(d =>
                typeof d === 'object' ? (d.taskId || d.id || d.task) : d
            ).join(', ');
            const more = task.dependencies.length > 3 ? ` +${task.dependencies.length - 3} more` : '';
            dependencyInfo = `<span style="color: #6b7280; font-size: 11px;">Deps: ${deps}${more}</span>`;
        }

        let assignmentCells = '';
        if (mechanicsNeeded === 1) {
            assignmentCells = `
                <select class="assign-select" data-task-id="${task.taskId}" data-position="0">
                    ${mechanicOptions}
                </select>`;
        } else {
            assignmentCells = `<div style="display: flex; flex-direction: column; gap: 5px;">`;
            for (let i = 0; i < mechanicsNeeded; i++) {
                assignmentCells += `
                    <select class="assign-select" data-task-id="${task.taskId}" data-position="${i}" style="width: 100%; font-size: 12px;">
                        <option value="">Worker ${i + 1}</option>
                        ${mechanicOptions}
                    </select>`;
            }
            assignmentCells += `</div>`;
        }

        let rowStyle = '';
        if (isCustomerTask) rowStyle = 'background-color: #f3e8ff;';  // Light purple for customer
        else if (task.isLatePartTask) rowStyle = 'background-color: #fef3c7;';
        else if (task.isReworkTask) rowStyle = 'background-color: #fee2e2;';
        else if (task.isCritical) rowStyle = 'background-color: #dbeafe;';

        // Determine task type for display
        let taskType = task.type;
        if (isCustomerTask && !taskType.includes('Customer')) {
            taskType = 'Customer';
        }

        // Use task.team (ALL SOI name) for table display; teamSkill has staffing team for auto-assign
        const baseTeam = task.team || (task.teamSkill ? parseTeamSkill(task.teamSkill).baseTeam : null) || 'UNKNOWN';
        const superintendent = task.superintendent || 'UNKNOWN';

        // Resolve customer code (airline): from task directly, or look up from products
        let customerCode = task.customer_code || '';
        if (!customerCode && scenarioData.products) {
            const ln = task.line_number || 0;
            const product = scenarioData.products.find(p => p.line_number === ln);
            if (product) customerCode = product.customer_code || '';
        }

        // Apply search highlighting if search is active
        const highlightedTaskId = highlightSearchTerm(task.taskId, jobSearchTerm);
        const highlightedType = highlightSearchTerm(taskType, jobSearchTerm);
        const highlightedTeam = highlightSearchTerm(baseTeam, jobSearchTerm);
        const highlightedSOI = task.soi ? highlightSearchTerm(task.soi, jobSearchTerm) : '';
        const highlightedCustomer = highlightSearchTerm(customerCode, jobSearchTerm);

        rows.push(`
            <tr style="${rowStyle}" data-task-id="${task.taskId}">
                <td class="priority">${task.priority || '-'}</td>
                <td>${task.line_number || task.product?.replace('Line ', '') || '-'}</td>
                <td>${highlightedCustomer || '-'}</td>
                <td class="task-id">
                    ${highlightedTaskId}${typeIndicator}${segmentBadge}${inspectionIndicator}${devBadge}
                    ${highlightedSOI ? `<br><small style="color: #6b7280;">SOI: ${highlightedSOI}</small>` : ''}
                    <button class="chain-btn" data-task-id="${task.taskId}" title="View Dependency Chain">⛓️</button>
                </td>
                <td><span class="task-type ${getTaskTypeClass(taskType)}">${highlightedType}</span></td>
                <td>${highlightedTeam}</td>
                <td>${superintendent}</td>
                <td>${formatDateTime(startTime)}</td>
                <td>${task.duration} min</td>
                <td style="text-align: center;">${mechanicsNeeded}</td>
                <td>${assignmentCells}</td>
            </tr>
        `);
    });

    // Single DOM update
    tbody.innerHTML = rows.join('');

    // ========== SECTION 7: Update Summary Stats ==========
    updateTaskTypeSummary(displayTasks);
    updateSelectionStatus();

    // ========== SECTION 8: Load Saved Assignments ==========
    if (savedAssignments[currentScenario]) {
        setTimeout(() => loadSavedAssignments(), 10);
    }

    // Hide loading indicator
    hideLoading();

    console.log(`[updateTeamLeadView] Complete. Displayed ${displayTasks.length} of ${tasks.length} tasks.`);

    // Update pagination controls if we're in 3-stage scenario
    if (currentScenario === '3stage') {
        updatePaginationControls();
    }
}

/**
 * Update pagination controls visibility and info
 */
function updatePaginationControls() {
    const controls = document.getElementById('paginationControls');
    const info = document.getElementById('paginationInfo');
    const loadMoreBtn = document.getElementById('loadMoreBtn');

    if (!controls || !info || !loadMoreBtn) return;

    const currentTaskCount = scenarioData.tasks ? scenarioData.tasks.length : 0;
    const totalTasks = pagination3stage.filtered_total || pagination3stage.total || 0;

    if (totalTasks > 0) {
        controls.style.display = 'block';

        // Build descriptive text based on current filters
        let filterDesc = '';
        if (selectedTeams && selectedTeams.length > 0 && !selectedTeams.includes('all')) {
            if (selectedTeams.includes('all-mechanics')) {
                filterDesc = ' mechanic';
            } else if (selectedTeams.includes('all-quality')) {
                filterDesc = ' QA';
            } else if (selectedTeams.includes('all-customer')) {
                filterDesc = ' customer';
            } else if (selectedTeams.length === 1) {
                filterDesc = ` ${selectedTeams[0]}`;
            } else {
                filterDesc = ` ${selectedTeams.length} teams`;
            }
        }
        if (selectedSkill && selectedSkill !== 'all') {
            filterDesc += ` (${selectedSkill})`;
        }
        if (selectedShift && selectedShift !== 'all') {
            filterDesc += ` shift ${selectedShift}`;
        }
        if (selectedWorkGroup && selectedWorkGroup !== 'all') {
            filterDesc += ` ${selectedWorkGroup}`;
        }
        if (selectedProduct && selectedProduct !== 'all') {
            filterDesc += ` ${selectedProduct}`;
        }

        info.textContent = `Showing ${currentTaskCount.toLocaleString()} of ${totalTasks.toLocaleString()}${filterDesc} tasks`;

        // Show/hide Load More button based on whether there are more tasks
        if (pagination3stage.has_more && !pagination3stage.loading) {
            loadMoreBtn.style.display = 'inline-block';
            loadMoreBtn.disabled = false;
            loadMoreBtn.textContent = 'Load Next 500 Tasks';
        } else if (pagination3stage.loading) {
            loadMoreBtn.disabled = true;
            loadMoreBtn.textContent = 'Loading...';
        } else {
            loadMoreBtn.style.display = 'none';
        }
    } else {
        controls.style.display = 'none';
    }
}

/**
 * Load next 500 tasks and append to current view
 */
async function loadMoreTasks() {
    if (pagination3stage.loading || !pagination3stage.has_more) {
        return;
    }

    try {
        pagination3stage.loading = true;
        updatePaginationControls();

        showLoading(`Loading next 500 tasks...`);

        // Build URL with current filters and pagination offset
        const url = build3StageAPIUrl(pagination3stage.offset, 500);

        // Fetch next page
        const response = await fetchWithTimeout(url, {}, 30000);

        if (!response.ok) {
            throw new Error(`Failed to load more tasks: ${response.status}`);
        }

        const data = await response.json();

        // Append new tasks to existing tasks
        if (data.tasks && data.tasks.length > 0) {
            scenarioData.tasks = scenarioData.tasks.concat(data.tasks);
            allScenarios['3stage'].tasks = scenarioData.tasks;

            // Update pagination state
            if (data.pagination) {
                pagination3stage.has_more = data.pagination.has_more;
                pagination3stage.offset = data.pagination.offset + data.pagination.returned;
            }

            console.log(`✓ Loaded ${data.tasks.length} more tasks. Total: ${scenarioData.tasks.length}`);

            // Refresh the view to show new tasks
            updateTeamLeadView();
        }

        hideLoading();
    } catch (error) {
        console.error('Error loading more tasks:', error);
        hideLoading();
        showError(`Failed to load more tasks: ${error.message}`);
    } finally {
        pagination3stage.loading = false;
        updatePaginationControls();
    }
}

/**
 * Load ALL remaining tasks at once
 */
async function loadAllTasks() {
    if (pagination3stage.loading) {
        return;
    }

    const totalFiltered = pagination3stage.filtered_total || pagination3stage.total;
    const confirmed = confirm(
        `This will load all ${totalFiltered.toLocaleString()} tasks matching current filters. ` +
        `This may take several seconds and could slow down your browser. Continue?`
    );

    if (!confirmed) {
        return;
    }

    try {
        pagination3stage.loading = true;
        updatePaginationControls();

        showLoading(`Loading all ${totalFiltered.toLocaleString()} tasks...`);

        // Build URL with filters
        const url = build3StageAPIUrl(0, 0);  // offset=0, limit=0 (all tasks)

        // Fetch ALL tasks (limit=0 means no limit)
        const response = await fetchWithTimeout(url, {}, 120000);  // 2 minute timeout for full dataset

        if (!response.ok) {
            throw new Error(`Failed to load all tasks: ${response.status}`);
        }

        const data = await response.json();

        // Replace tasks with full dataset
        if (data.tasks) {
            scenarioData.tasks = data.tasks;
            allScenarios['3stage'].tasks = data.tasks;

            // Update pagination state
            pagination3stage.offset = data.tasks.length;
            pagination3stage.has_more = false;

            console.log(`✓ Loaded all ${data.tasks.length} tasks`);

            // Refresh the view
            updateTeamLeadView();
        }

        hideLoading();
    } catch (error) {
        console.error('Error loading all tasks:', error);
        hideLoading();
        showError(`Failed to load all tasks: ${error.message}`);
    } finally {
        pagination3stage.loading = false;
        updatePaginationControls();
    }
}

/**
 * Build API URL with current filter parameters
 */
function build3StageAPIUrl(offset, limit) {
    let url = `/api/scenario/3stage?offset=${offset}&limit=${limit}`;

    // Add filter parameters
    // Handle multi-select teams - send all specific teams to backend
    if (selectedTeams && !selectedTeams.includes('all')) {
        // Find all specific teams (not aggregate options)
        const specificTeams = selectedTeams.filter(t =>
            !t.startsWith('all-') &&
            !t.startsWith('super-'));

        if (specificTeams.length > 0) {
            // Send all selected teams to backend (comma-separated)
            url += `&team=${encodeURIComponent(specificTeams.join(','))}`;
        }
        // If only aggregate/super options selected, don't send to backend, filter client-side
    }

    // Add superintendent filter if set
    if (selectedSuperintendent && selectedSuperintendent !== 'all') {
        url += `&superintendent=${encodeURIComponent(selectedSuperintendent)}`;
    }

    if (selectedSkill && selectedSkill !== 'all') {
        url += `&skill=${encodeURIComponent(selectedSkill)}`;
    }

    if (selectedShift && selectedShift !== 'all') {
        // Convert display values ('1st','2nd','3rd') to numeric (1,2,3) for backend
        const shiftNum = selectedShift === '1st' ? 1 : selectedShift === '2nd' ? 2 : selectedShift === '3rd' ? 3 : selectedShift;
        url += `&shift=${shiftNum}`;
    }

    if (selectedWorkGroup && selectedWorkGroup !== 'all') {
        url += `&workgroup=${encodeURIComponent(selectedWorkGroup)}`;
    }

    if (selectedProduct && selectedProduct !== 'all') {
        url += `&product=${encodeURIComponent(selectedProduct)}`;
    }

    // Add job search term if provided
    if (jobSearchTerm && jobSearchTerm.length > 0) {
        url += `&search=${encodeURIComponent(jobSearchTerm)}`;
    }

    return url;
}

/**
 * Reload 3-stage data with current filters
 * Optimized with client-side caching for instant filtering
 */
async function reload3StageDataWithFilters() {
    // Check cache first - instant response if filters haven't changed
    if (filterCache.isValid()) {
        console.log('✓ Using cached filtered data (instant)');
        scenarioData.tasks = filterCache.data;
        allScenarios['3stage'].tasks = filterCache.data;

        // Update pagination metadata
        pagination3stage.filtered_total = filterCache.data.length;
        pagination3stage.has_more = false;

        // Update view immediately
        updateTeamLeadView();
        return;
    }

    // If request already in flight for same filters, wait for it (deduplication)
    if (filterCache.pendingRequest) {
        console.log('⏳ Waiting for in-flight request...');
        return await filterCache.pendingRequest;
    }

    // Create new request
    filterCache.pendingRequest = (async () => {
        try {
            pagination3stage.loading = true;

            // HYBRID STRATEGY:
            // - If search term provided → load ALL results (usually <100 tasks, instant)
            // - If only filters → load first 500 tasks (lazy load rest on demand)
            const isSearchMode = jobSearchTerm && jobSearchTerm.length > 0;
            const initialLimit = isSearchMode ? 0 : 500;  // 0 = all results, 500 = first page

            if (isSearchMode) {
                showLoading(`Searching for "${jobSearchTerm}"...`);
            } else {
                showLoading(`Loading tasks...`);
            }

            // Build URL with current filters
            const url = build3StageAPIUrl(0, initialLimit);

            console.log(`🔄 Fetching ${isSearchMode ? 'search results' : 'filtered tasks'} from: ${url}`);

            // Fetch tasks with appropriate timeout
            const timeout = isSearchMode ? 10000 : 30000;  // Search should be fast
            const response = await fetchWithTimeout(url, {}, timeout);

            if (!response.ok) {
                throw new Error(`Failed to load filtered tasks: ${response.status}`);
            }

            const data = await response.json();

            // Cache the full filtered dataset
            if (data.tasks) {
                filterCache.set(data.tasks);

                scenarioData.tasks = data.tasks;
                allScenarios['3stage'].tasks = data.tasks;

                // Update pagination state from response
                if (data.pagination) {
                    pagination3stage.total = data.pagination.total;
                    pagination3stage.filtered_total = data.pagination.filtered_total || data.pagination.total;
                    pagination3stage.offset = data.pagination.returned;
                    pagination3stage.has_more = data.pagination.has_more || false;
                }

                // Merge other scenario data, but NEVER overwrite keys that
                // drive filter dropdowns — those were populated on initial load
                // and must remain stable across filtered reloads.
                const protectedKeys = new Set([
                    'tasks', 'teamCapacities', 'teamShifts', 'products',
                    'utilization', 'mechanic_timelines'
                ]);
                Object.keys(data).forEach(key => {
                    if (!protectedKeys.has(key)) {
                        scenarioData[key] = data[key];
                        allScenarios['3stage'][key] = data[key];
                    }
                });

                // Log appropriate message based on mode
                if (isSearchMode) {
                    console.log(`✓ Search complete: Found ${data.tasks.length} tasks matching "${jobSearchTerm}"`);
                    if (data.pagination.filtered_total !== data.pagination.total) {
                        console.log(`  (Filtered from ${pagination3stage.total} total tasks)`);
                    }
                } else {
                    console.log(`✓ Loaded & cached: ${data.tasks.length} tasks`);
                    console.log(`  Filtered: ${pagination3stage.filtered_total} of ${pagination3stage.total} total tasks`);
                    if (pagination3stage.has_more) {
                        console.log(`  📄 More tasks available - scroll to load`);
                    }
                }

                // Refresh the view
                updateTeamLeadView();
            }

            hideLoading();
        } catch (error) {
            console.error('Error reloading filtered tasks:', error);
            hideLoading();
            showError(`Failed to reload tasks: ${error.message}`);
            throw error;  // Re-throw so caller knows it failed
        } finally {
            pagination3stage.loading = false;
            filterCache.pendingRequest = null;  // Clear pending request
        }
    })();

    return await filterCache.pendingRequest;
}

// Helper function for task type summary with customer support
function updateTaskTypeSummary(tasks) {
    const taskTypeCounts = {};
    let latePartCount = 0;
    let reworkCount = 0;
    let customerCount = 0;

    tasks.forEach(task => {
        // Ensure we're getting the type as a string
        const taskType = task.type || 'Unknown';
        taskTypeCounts[taskType] = (taskTypeCounts[taskType] || 0) + 1;

        if (task.isLatePartTask) latePartCount++;
        if (task.isReworkTask) reworkCount++;
        if (task.isCustomerTask) customerCount++;
    });

    let summaryDiv = document.getElementById('taskTypeSummary');
    if (!summaryDiv) {
        const statsContainer = document.querySelector('.team-stats');
        if (statsContainer) {
            summaryDiv = document.createElement('div');
            summaryDiv.id = 'taskTypeSummary';
            summaryDiv.className = 'stat-card';
            summaryDiv.style.gridColumn = 'span 2';
            statsContainer.appendChild(summaryDiv);
        }
    }

    if (summaryDiv) {
        let summaryHTML = '<h3>Task Type Breakdown</h3><div style="display: flex; gap: 15px; margin-top: 10px; flex-wrap: wrap;">';

        // Make sure we're iterating over the counts correctly
        Object.entries(taskTypeCounts).forEach(([type, count]) => {
            // Ensure type is a string
            const typeStr = String(type);
            const countNum = Number(count) || 0;

            summaryHTML += `
                <div style="flex: 1; min-width: 100px;">
                    <div style="font-size: 18px; font-weight: bold; color: ${getTaskTypeColor(typeStr)};">${countNum}</div>
                    <div style="font-size: 11px; color: #6b7280;">${typeStr}</div>
                </div>`;
        });

        summaryHTML += '</div>';

        if (latePartCount > 0 || reworkCount > 0 || customerCount > 0) {
            summaryHTML += `<div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid #e5e7eb;">`;
            if (latePartCount > 0) summaryHTML += `<span style="margin-right: 15px;">📦 Late Parts: ${latePartCount}</span>`;
            if (reworkCount > 0) summaryHTML += `<span style="margin-right: 15px;">🔧 Rework: ${reworkCount}</span>`;
            if (customerCount > 0) summaryHTML += `<span>👤 Customer: ${customerCount}</span>`;
            summaryHTML += '</div>';
        }

        summaryDiv.innerHTML = summaryHTML;
    }
}

// Helper function to generate mechanic options based on current filters
function generateMechanicOptionsForFilters() {
    let options = '';

    // Get filtered team capacities based on current team and skill selection
    Object.entries(scenarioData.teamCapacities || {}).forEach(([teamSkill, capacity]) => {
        const parsed = parseTeamSkill(teamSkill);
        let baseTeam = parsed.baseTeam;
        let skill = parsed.skill;
        let shift = parsed.shift;

        // Check if this team/skill matches current filters using helper
        const isCustomerTeamFlag = baseTeam.toLowerCase().includes('customer');
        const isQualTeam = baseTeam.toLowerCase().includes('quality');
        let includeThis = teamMatchesSelection(baseTeam, isCustomerTeamFlag, isQualTeam);

        // Skill filter
        if (includeThis && selectedSkill !== 'all' && skill !== selectedSkill) {
            includeThis = false;
        }

        if (includeThis && capacity > 0) {
            for (let i = 1; i <= capacity; i++) {
                const mechId = `${teamSkill}_${i}`;
                const label = buildResourceLabel(baseTeam, i, shift, skill);
                options += `<option value="${mechId}">${label}</option>`;
            }
        }
    });

    return options;
}

// Helper function to update selection status
function updateSelectionStatus() {
    let statusText = '';

    // Handle multi-select teams
    if (selectedTeams.includes('all')) {
        statusText = `Teams: All teams`;
    } else if (selectedTeams.length === 1) {
        const team = selectedTeams[0];
        if (team === 'all-mechanics') {
            statusText = `Teams: All mechanic teams`;
        } else if (team === 'all-quality') {
            statusText = `Teams: All quality teams`;
        } else {
            statusText = `Team: ${team}`;
        }
    } else {
        statusText = `Teams: ${selectedTeams.length} selected`;
    }

    // Add superintendent if filtered
    if (selectedSuperintendent !== 'all') {
        statusText = `Superintendent: ${selectedSuperintendent} | ${statusText}`;
    }

    if (selectedSkill !== 'all') {
        statusText += ` | Skill: ${selectedSkill}`;
    }

    statusText += ` | Shift: ${selectedShift === 'all' ? 'All shifts' : selectedShift}`;
    if (selectedWorkGroup !== 'all') {
        statusText += ` | Work Group: ${selectedWorkGroup.charAt(0).toUpperCase() + selectedWorkGroup.slice(1)}`;
    }
    statusText += ` | Product: ${selectedProduct === 'all' ? 'All products' : selectedProduct}`;

    // Create or update status div
    let statusDiv = document.getElementById('teamSelectionStatus');
    if (!statusDiv) {
        statusDiv = document.createElement('div');
        statusDiv.id = 'teamSelectionStatus';
        statusDiv.style.cssText = `
            background: #E0F2FE;
            border: 1px solid #0284C7;
            border-radius: 6px;
            padding: 8px 12px;
            margin-bottom: 15px;
            font-size: 13px;
            color: #075985;
        `;
        const filtersDiv = document.querySelector('.team-filters');
        if (filtersDiv) {
            filtersDiv.parentNode.insertBefore(statusDiv, filtersDiv.nextSibling);
        }
    }

    statusDiv.innerHTML = `<strong>Active Filters:</strong> ${statusText}`;
}

// Removed unused helper functions - functionality is now integrated into autoAssign()

// Helper to check if task matches selected team
function taskMatchesTeamFilter(task, selectedTeam, teamsToInclude) {
    if (selectedTeam === 'all' || selectedTeam === 'all-mechanics' || selectedTeam === 'all-quality') {
        // For group selections, check base team
        const baseTeam = task.team || task.teamSkill;
        return teamsToInclude.some(team => baseTeam.includes(team));
    } else {
        // For specific team selection, match base team
        return task.team === selectedTeam;
    }
}

// Add this helper function
function ensureSavedAssignments() {
    if (typeof savedAssignments === 'undefined' || !savedAssignments) {
        console.warn('savedAssignments not initialized, reinitializing...');
        initializeSavedAssignments();
    }
}



// Compute task breakdown counts from scenarioData.tasks for a given line number
function computeTaskBreakdown(lineNumber) {
    const breakdown = { Production: 0, Rework: 0, 'Quality Inspection': 0, Customer: 0, Vendor: 0 };
    if (!scenarioData || !scenarioData.tasks) return breakdown;

    const acTasks = scenarioData.tasks.filter(t => t.line_number === lineNumber);
    acTasks.forEach(t => {
        if (t.isVendorTask || t.workGroup === 'vendor') {
            breakdown.Vendor++;
        } else if (t.isCustomerTask || t.workGroup === 'customer' || t.type === 'Customer') {
            breakdown.Customer++;
        } else if (t.isQualityTask || t.workGroup === 'quality' || t.type === 'Quality Inspection' || t.is_inspection) {
            breakdown['Quality Inspection']++;
        } else if (t.isReworkTask || t.type === 'Rework') {
            breakdown.Rework++;
        } else {
            breakdown.Production++;
        }
    });
    return breakdown;
}

// Build a mini timeline bar chart from scheduled tasks per day for an aircraft
function buildMiniTimeline(lineNumber) {
    if (!scenarioData || !scenarioData.tasks) return '';
    const acTasks = scenarioData.tasks.filter(t => t.line_number === lineNumber);
    if (acTasks.length === 0) return '';

    const dayBuckets = {};
    acTasks.forEach(t => {
        const d = t.day || 0;
        dayBuckets[d] = (dayBuckets[d] || 0) + 1;
    });
    const days = Object.keys(dayBuckets).map(Number).sort((a, b) => a - b);
    if (days.length === 0) return '';

    const maxCount = Math.max(...Object.values(dayBuckets));
    const barWidth = Math.max(2, Math.min(6, Math.floor(280 / days.length)));
    return `
        <div style="margin-top: 8px;">
            <div style="font-size: 10px; font-weight: 600; color: #6B7280; margin-bottom: 4px;">Tasks/Day</div>
            <div style="display: flex; align-items: flex-end; gap: 1px; height: 36px;">
                ${days.map(d => {
                    const h = Math.max(2, Math.round(32 * dayBuckets[d] / maxCount));
                    return `<div style="width: ${barWidth}px; height: ${h}px; background: #3B82F6; border-radius: 1px;" title="Day ${d}: ${dayBuckets[d]} tasks"></div>`;
                }).join('')}
            </div>
            <div style="display: flex; justify-content: space-between; font-size: 9px; color: #9CA3AF; margin-top: 2px;">
                <span>Day ${days[0]}</span>
                <span>Day ${days[days.length - 1]}</span>
            </div>
        </div>`;
}

// Enhanced Management View with sorting, filtering and aggregation
function updateManagementView() {
    if (!scenarioData) return;
    document.getElementById('totalWorkforce').textContent = scenarioData.totalWorkforce;
    document.getElementById('makespan').textContent = scenarioData.makespan;
    document.getElementById('onTimeRate').textContent = scenarioData.onTimeRate + '%';
    document.getElementById('avgUtilization').textContent = scenarioData.avgUtilization + '%';

    // Hide the aircraft lateness panel (redundant with risk status + product cards)
    const aircraftPanel = document.getElementById('aircraftLatenessPanel');
    if (aircraftPanel) aircraftPanel.style.display = 'none';

    // Hide team utilization analysis
    const utilizationCardMgmt = document.getElementById('utilizationChartMgmt');
    if (utilizationCardMgmt) {
        const utilCard = utilizationCardMgmt.closest('.ios-card');
        if (utilCard) utilCard.style.display = 'none';
    }

const productGrid = document.getElementById('productGrid');
productGrid.innerHTML = '';
scenarioData.products.forEach(product => {
    const status = product.onTime ? 'on-time' :
        product.latenessDays <= 5 ? 'at-risk' : 'late';
    const statusLabel = status.replace('-', ' ').replace(/\b\w/g, c => c.toUpperCase());
    const statusColors = {
        'on-time': { bg: '#D1FAE5', border: '#34D399', text: '#065F46' },
        'at-risk': { bg: '#FEF3C7', border: '#FBBF24', text: '#92400E' },
        'late':    { bg: '#FEE2E2', border: '#F87171', text: '#991B1B' },
    };
    const sc = statusColors[status] || statusColors['late'];

    // Format dates for display
    const projectedDate = product.projectedCompletion ?
        new Date(product.projectedCompletion).toLocaleDateString('en-US', {
            month: 'short',
            day: 'numeric',
            year: 'numeric'
        }) : 'TBD';

    const requiredDate = product.deliveryDate ?
        new Date(product.deliveryDate).toLocaleDateString('en-US', {
            month: 'short',
            day: 'numeric',
            year: 'numeric'
        }) : 'TBD';

    // Compute real task breakdown from scheduled tasks
    const lineNumber = product.line_number;
    const breakdown = computeTaskBreakdown(lineNumber);
    const totalFromBreakdown = breakdown.Production + breakdown.Rework + breakdown['Quality Inspection'] + breakdown.Customer + breakdown.Vendor;

    // Build mini timeline
    const timelineHTML = buildMiniTimeline(lineNumber);

    const daysLate = product.latenessDays || 0;
    const daysRemaining = product.daysRemaining;
    const criticalTasks = product.criticalPath || 0;
    const progress = product.progress || 0;

    const card = document.createElement('div');
    card.className = 'product-card';
    card.style.cssText = 'cursor: default;';
    card.innerHTML = `
        <div class="product-header" style="margin-bottom: 8px;">
            <div class="product-name">${product.name}</div>
            <span style="display: inline-block; padding: 3px 10px; border-radius: 6px;
                         font-size: 11px; font-weight: 600; background: ${sc.bg}; color: ${sc.text};
                         border: 1px solid ${sc.border};">
                ${statusLabel}
            </span>
        </div>

        <!-- Delivery Dates -->
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px; padding: 10px; background: #f9fafb; border-radius: 8px;">
            <div style="text-align: center;">
                <div style="font-size: 10px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px;">Required (CS 744)</div>
                <div style="font-size: 13px; font-weight: 700; color: #374151; margin-top: 2px;">${requiredDate}</div>
            </div>
            <div style="text-align: center;">
                <div style="font-size: 10px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px;">Projected</div>
                <div style="font-size: 13px; font-weight: 700; color: ${daysLate > 0 ? '#EF4444' : '#059669'}; margin-top: 2px;">${projectedDate}</div>
            </div>
        </div>

        <!-- Key Metrics Row -->
        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-bottom: 10px;">
            <div style="background: ${daysLate > 0 ? '#FEE2E2' : '#D1FAE5'}; border-radius: 8px; padding: 8px; text-align: center;">
                <div style="font-size: 18px; font-weight: 700; color: ${daysLate > 0 ? '#991B1B' : '#065F46'};">
                    ${daysLate > 0 ? '+' + daysLate : daysLate}
                </div>
                <div style="font-size: 10px; color: ${daysLate > 0 ? '#B91C1C' : '#047857'};">days ${daysLate > 0 ? 'late' : 'margin'}</div>
            </div>
            <div style="background: #EFF6FF; border-radius: 8px; padding: 8px; text-align: center;">
                <div style="font-size: 18px; font-weight: 700; color: #1E40AF;">${daysRemaining}</div>
                <div style="font-size: 10px; color: #3B82F6;">days left</div>
            </div>
            <div style="background: #FFF7ED; border-radius: 8px; padding: 8px; text-align: center;">
                <div style="font-size: 18px; font-weight: 700; color: #9A3412;">${criticalTasks}</div>
                <div style="font-size: 10px; color: #EA580C;">critical</div>
            </div>
        </div>

        <!-- Progress Bar -->
        <div style="margin-bottom: 10px;">
            <div style="display: flex; justify-content: space-between; font-size: 10px; color: #6B7280; margin-bottom: 3px;">
                <span>Progress</span>
                <span>${progress}%</span>
            </div>
            <div style="background: #E5E7EB; border-radius: 6px; height: 8px; overflow: hidden;">
                <div style="background: ${progress >= 80 ? '#059669' : progress >= 50 ? '#D97706' : '#DC2626'};
                            height: 100%; width: ${progress}%; border-radius: 6px;"></div>
            </div>
        </div>

        <!-- Task Breakdown -->
        <div style="background: #f9fafb; border-radius: 8px; padding: 10px; margin-bottom: 8px;">
            <div style="font-size: 11px; font-weight: 600; color: #374151; margin-bottom: 6px;">
                Task Breakdown (${totalFromBreakdown || product.totalTasks} total)
            </div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 4px;">
                <div style="display: flex; justify-content: space-between; font-size: 11px;">
                    <span style="color: #6B7280;">Production</span>
                    <span style="font-weight: 600; color: #374151;">${breakdown.Production}</span>
                </div>
                <div style="display: flex; justify-content: space-between; font-size: 11px;">
                    <span style="color: #6B7280;">QA Inspection</span>
                    <span style="font-weight: 600; color: #374151;">${breakdown['Quality Inspection']}</span>
                </div>
                <div style="display: flex; justify-content: space-between; font-size: 11px;">
                    <span style="color: #6B7280;">Rework</span>
                    <span style="font-weight: 600; color: ${breakdown.Rework > 0 ? '#DC2626' : '#374151'};">${breakdown.Rework}</span>
                </div>
                <div style="display: flex; justify-content: space-between; font-size: 11px;">
                    <span style="color: #6B7280;">Customer</span>
                    <span style="font-weight: 600; color: #374151;">${breakdown.Customer}</span>
                </div>
                ${breakdown.Vendor > 0 ? `
                <div style="display: flex; justify-content: space-between; font-size: 11px;">
                    <span style="color: #6B7280;">Vendor</span>
                    <span style="font-weight: 600; color: #D97706;">${breakdown.Vendor}</span>
                </div>` : ''}
                ${product.latePartsCount > 0 ? `
                <div style="display: flex; justify-content: space-between; font-size: 11px;">
                    <span style="color: #6B7280;">Late Parts</span>
                    <span style="font-weight: 600; color: #DC2626;">${product.latePartsCount}</span>
                </div>` : ''}
            </div>
        </div>

        <!-- Mini Timeline -->
        ${timelineHTML}
    `;
    productGrid.appendChild(card);
});

}

// New function to handle utilization display with controls
function buildUtilizationDisplay() {
    // Try management-specific ID first (iOS template), then standard ID
    const utilizationChart = document.getElementById('utilizationChartMgmt') || document.getElementById('utilizationChart');

    // Add control panel if it doesn't exist
    let controlPanel = document.getElementById('utilizationControls');
    if (!controlPanel) {
        const chartContainer = utilizationChart.parentElement;
        controlPanel = document.createElement('div');
        controlPanel.id = 'utilizationControls';
        controlPanel.style.cssText = `
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
            padding: 15px;
            background: #f9fafb;
            border-radius: 8px;
            align-items: center;
            flex-wrap: wrap;
        `;
        controlPanel.innerHTML = `
            <div style="display: flex; gap: 10px; align-items: center;">
                <label style="font-weight: 500; color: #6b7280; font-size: 14px;">View:</label>
                <select id="utilizationView" style="padding: 6px 10px; border: 1px solid #e5e7eb; border-radius: 6px; font-size: 14px;">
                    <option value="all">All Teams</option>
                    <option value="role">Group by Role</option>
                    <option value="skill">Group by Skill</option>
                    <option value="mechanic-skill">Mechanic Skills Only</option>
                </select>
            </div>
            <div style="display: flex; gap: 10px; align-items: center;">
                <label style="font-weight: 500; color: #6b7280; font-size: 14px;">Sort:</label>
                <select id="utilizationSort" style="padding: 6px 10px; border: 1px solid #e5e7eb; border-radius: 6px; font-size: 14px;">
                    <option value="name">Name</option>
                    <option value="util-high">Utilization (High to Low)</option>
                    <option value="util-low">Utilization (Low to High)</option>
                </select>
            </div>
            <div style="display: flex; gap: 10px; align-items: center;">
                <label style="font-weight: 500; color: #6b7280; font-size: 14px;">Filter:</label>
                <input type="number" id="utilizationThreshold" placeholder="Min %" style="width: 60px; padding: 6px; border: 1px solid #e5e7eb; border-radius: 6px; font-size: 14px;">
                <button onclick="applyUtilizationFilter()" style="padding: 6px 12px; background: #3b82f6; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px;">Apply</button>
            </div>
        `;
        chartContainer.insertBefore(controlPanel, utilizationChart);

        // Add event listeners
        document.getElementById('utilizationView').addEventListener('change', updateUtilizationDisplay);
        document.getElementById('utilizationSort').addEventListener('change', updateUtilizationDisplay);
    }

    // Initial display
    updateUtilizationDisplay();
}

// Function to update utilization display based on controls
function updateUtilizationDisplay() {
    const utilizationChart = document.getElementById('utilizationChartMgmt') || document.getElementById('utilizationChart');
    const viewMode = document.getElementById('utilizationView').value;
    const sortMode = document.getElementById('utilizationSort').value;
    const threshold = parseFloat(document.getElementById('utilizationThreshold').value) || 0;

    utilizationChart.innerHTML = '';

    // Process data based on view mode
    let utilizationData = [];

    if (viewMode === 'all') {
        // Show all teams individually
        Object.entries(scenarioData.utilization).forEach(([team, utilization]) => {
            if (utilization >= threshold) {
                const capacity = scenarioData.teamCapacities?.[team] || 0;
                const label = `${team} (${capacity} worker${capacity !== 1 ? 's' : ''})`;
                utilizationData.push({ name: label, utilization: utilization, type: getTeamType(team) });
            }
        });
    } else if (viewMode === 'role') {
        // Aggregate by role (Mechanic, Quality, Customer, Vendor)
        const roleAggregation = { Mechanic: [], Quality: [], Customer: [], Vendor: [] };

        Object.entries(scenarioData.utilization).forEach(([team, utilization]) => {
            const type = getTeamType(team);
            if (roleAggregation[type]) {
                roleAggregation[type].push(utilization);
            }
        });

        Object.entries(roleAggregation).forEach(([role, utils]) => {
            if (utils.length > 0) {
                const avgUtil = utils.reduce((a, b) => a + b, 0) / utils.length;
                if (avgUtil >= threshold) {
                    const stats = scenarioData.aggStats?.by_role?.[role] || { teams: utils.length, workers: 'N/A' };
                    utilizationData.push({
                        name: `${role} Teams (${stats.teams} teams, ${stats.workers} workers)`,
                        utilization: Math.round(avgUtil * 10) / 10,
                        type: role,
                        count: utils.length,
                        min: Math.min(...utils),
                        max: Math.max(...utils)
                    });
                }
            }
        });
    } else if (viewMode === 'skill') {
        // Aggregate by skill across all teams
        const skillAggregation = {};

        Object.entries(scenarioData.utilization).forEach(([team, utilization]) => {
            const skillMatch = team.match(/\(([^)]+)\)/);
            const skill = skillMatch ? skillMatch[1] : 'No Skill';

            if (!skillAggregation[skill]) {
                skillAggregation[skill] = [];
            }
            skillAggregation[skill].push(utilization);
        });

        Object.entries(skillAggregation).forEach(([skill, utils]) => {
            const avgUtil = utils.reduce((a, b) => a + b, 0) / utils.length;
            if (avgUtil >= threshold) {
                const stats = scenarioData.aggStats?.by_skill?.[skill] || { teams: utils.length, workers: 'N/A' };
                utilizationData.push({
                    name: `${skill} (${stats.teams} teams, ${stats.workers} workers)`,
                    utilization: Math.round(avgUtil * 10) / 10,
                    type: 'skill',
                    count: utils.length
                });
            }
        });
    } else if (viewMode === 'mechanic-skill') {
        // Show only mechanic teams grouped by skill
        const mechanicSkills = {};

        Object.entries(scenarioData.utilization).forEach(([team, utilization]) => {
            // Extract base team from "TEAM S{N} (SKILL)" format for type checking
            const parsedTeam = parseTeamSkill(team);
            const baseTeam = parsedTeam.baseTeam;
            if (getTeamType(baseTeam) === 'Mechanic') {
                const skillMatch = team.match(/\(([^)]+)\)/);
                const skill = skillMatch ? skillMatch[1] : 'General';

                if (!mechanicSkills[skill]) {
                    mechanicSkills[skill] = { teams: [], total: 0 };
                }
                mechanicSkills[skill].teams.push({ team, utilization });
                mechanicSkills[skill].total += utilization;
            }
        });

        Object.entries(mechanicSkills).forEach(([skill, data]) => {
            const avgUtil = data.total / data.teams.length;
            if (avgUtil >= threshold) {
                // Add skill group header
                utilizationData.push({
                    name: `Production Skill: ${skill}`,
                    utilization: Math.round(avgUtil * 10) / 10,
                    type: 'skill-header',
                    isGroup: true
                });
                // Add individual teams under this skill
                data.teams.forEach(({ team, utilization }) => {
                    if (utilization >= threshold) {
                        utilizationData.push({
                            name: `  ${team}`,
                            utilization: utilization,
                            type: 'Mechanic',
                            indent: true
                        });
                    }
                });
            }
        });
    }

    // Sort data
    if (sortMode === 'util-high') {
        utilizationData.sort((a, b) => b.utilization - a.utilization);
    } else if (sortMode === 'util-low') {
        utilizationData.sort((a, b) => a.utilization - b.utilization);
    } else {
        utilizationData.sort((a, b) => a.name.localeCompare(b.name));
    }

    // Display data
    utilizationData.forEach(data => {
        const item = document.createElement('div');
        item.className = 'utilization-item';
        item.style.display = 'grid';
        item.style.gridTemplateColumns = '300px 1fr';
        item.style.gap = '10px';
        item.style.alignItems = 'center';

        if (data.indent) {
            item.style.marginLeft = '20px';
        }
        if (data.isGroup) {
            item.style.background = '#f3f4f6';
            item.style.padding = '8px';
            item.style.borderRadius = '6px';
            item.style.marginTop = '10px';
            item.style.marginBottom = '5px';
        }

        let fillColor = 'linear-gradient(90deg, #10b981, #10b981)';
        if (data.utilization > 90) {
            fillColor = 'linear-gradient(90deg, #ef4444, #ef4444)';
        } else if (data.utilization > 75) {
            fillColor = 'linear-gradient(90deg, #f59e0b, #f59e0b)';
        }

        let labelHTML = data.name;
        if (data.count !== undefined && data.min !== undefined) {
            labelHTML += `<br><span style="font-size: 11px; color: #9ca3af;">Range: ${Math.round(data.min)}% - ${Math.round(data.max)}%</span>`;
        }

        item.innerHTML = `
            <div class="team-label" style="${data.isGroup ? 'font-weight: 600;' : ''}">${labelHTML}</div>
            <div class="utilization-bar">
                <div class="utilization-fill" style="width: ${data.utilization}%; background: ${fillColor};">
                    <span class="utilization-percent">${data.utilization}%</span>
                </div>
            </div>
        `;
        utilizationChart.appendChild(item);
    });

    // Add summary at bottom
    const summary = document.createElement('div');
    summary.style.cssText = `
        margin-top: 20px;
        padding: 12px;
        background: #f9fafb;
        border-radius: 8px;
        font-size: 13px;
        color: #6b7280;
    `;
    summary.innerHTML = `
        <strong>Summary:</strong> Showing ${utilizationData.filter(d => !d.isGroup).length} items
        ${threshold > 0 ? ` (filtered >= ${threshold}%)` : ''}
    `;
    utilizationChart.appendChild(summary);
}

// Helper function to determine team type
function getTeamType(teamName) {
    if (isCustomerTeam(teamName)) return 'Customer';
    if (isQualityTeam(teamName)) return 'Quality';
    if (isVendorTeam(teamName)) return 'Vendor';
    // If not customer, quality, or vendor, it's a mechanic/production team
    return 'Mechanic';
}

// Apply utilization filter
window.applyUtilizationFilter = function() {
    updateUtilizationDisplay();
}

// Show product details modal (used for burndown chart access)
async function showProductDetails(productName) {
    // Find the product data from loaded scenario
    const product = scenarioData && scenarioData.products
        ? scenarioData.products.find(p => p.name === productName)
        : null;

    // Build modal content
    const status = product
        ? (product.onTime ? 'on-time' : (product.latenessDays <= 5 ? 'at-risk' : 'late'))
        : 'unknown';
    const statusLabel = status.replace('-', ' ').replace(/\b\w/g, c => c.toUpperCase());
    const statusColors = {
        'on-time': { bg: '#D1FAE5', border: '#34D399', text: '#065F46' },
        'at-risk': { bg: '#FEF3C7', border: '#FBBF24', text: '#92400E' },
        'late':    { bg: '#FEE2E2', border: '#F87171', text: '#991B1B' },
        'unknown': { bg: '#F3F4F6', border: '#D1D5DB', text: '#374151' },
    };
    const sc = statusColors[status];

    const fmtDate = (d) => {
        if (!d) return 'N/A';
        return new Date(d).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    };

    const deliveryDate = product ? fmtDate(product.deliveryDate) : 'N/A';
    const projectedDate = product ? fmtDate(product.projectedCompletion) : 'N/A';
    const daysLate = product ? product.latenessDays : 0;
    const daysRemaining = product ? product.daysRemaining : 'N/A';
    const progress = product ? product.progress : 0;
    const criticalTasks = product ? product.criticalPath : 0;

    // Compute task breakdown from actual scheduled tasks
    const lineNumber = product ? product.line_number : null;
    const breakdown = lineNumber ? computeTaskBreakdown(lineNumber) : { Production: 0, Rework: 0, 'Quality Inspection': 0, Customer: 0, Vendor: 0 };
    const prodCount = breakdown.Production;
    const qualCount = breakdown['Quality Inspection'];
    const reworkCount = breakdown.Rework;
    const custCount = breakdown.Customer;
    const vendorCount = breakdown.Vendor;
    const latePartCount = product ? (product.latePartsCount || 0) : 0;
    const totalTasks = prodCount + qualCount + reworkCount + custCount + vendorCount;

    // Build per-day histogram from schedule tasks for a mini timeline
    const timelineHTML = lineNumber ? buildMiniTimeline(lineNumber) : '';

    // Create/show modal overlay
    let overlay = document.getElementById('productDetailOverlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'productDetailOverlay';
        document.body.appendChild(overlay);
    }

    overlay.style.cssText = `
        position: fixed; inset: 0; z-index: 10000;
        background: rgba(0,0,0,0.5); display: flex;
        align-items: center; justify-content: center;
        animation: fadeIn 0.2s ease;
    `;

    overlay.innerHTML = `
        <div style="background: white; border-radius: 16px; max-width: 520px; width: 92%; max-height: 90vh;
                    overflow-y: auto; box-shadow: 0 20px 60px rgba(0,0,0,0.3); padding: 24px;">
            <!-- Header -->
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                <div>
                    <h2 style="margin: 0; font-size: 20px; color: #111827;">${productName}</h2>
                    <span style="display: inline-block; margin-top: 4px; padding: 3px 10px; border-radius: 6px;
                                 font-size: 12px; font-weight: 600; background: ${sc.bg}; color: ${sc.text};
                                 border: 1px solid ${sc.border};">
                        ${statusLabel}
                    </span>
                </div>
                <button onclick="document.getElementById('productDetailOverlay').style.display='none'"
                        style="background: #F3F4F6; border: none; border-radius: 50%; width: 32px; height: 32px;
                               font-size: 18px; cursor: pointer; color: #6B7280; display: flex; align-items: center; justify-content: center;">
                    &times;
                </button>
            </div>

            <!-- Delivery Dates -->
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px;">
                <div style="background: #F9FAFB; border-radius: 10px; padding: 14px; text-align: center;">
                    <div style="font-size: 11px; color: #6B7280; text-transform: uppercase; letter-spacing: 0.5px;">Required (CS 744)</div>
                    <div style="font-size: 15px; font-weight: 700; color: #374151; margin-top: 4px;">${deliveryDate}</div>
                </div>
                <div style="background: #F9FAFB; border-radius: 10px; padding: 14px; text-align: center;">
                    <div style="font-size: 11px; color: #6B7280; text-transform: uppercase; letter-spacing: 0.5px;">Projected Completion</div>
                    <div style="font-size: 15px; font-weight: 700; color: ${daysLate > 0 ? '#EF4444' : '#059669'}; margin-top: 4px;">${projectedDate}</div>
                </div>
            </div>

            <!-- Key Metrics Row -->
            <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 16px;">
                <div style="background: ${daysLate > 0 ? '#FEE2E2' : '#D1FAE5'}; border-radius: 10px; padding: 12px; text-align: center;">
                    <div style="font-size: 22px; font-weight: 700; color: ${daysLate > 0 ? '#991B1B' : '#065F46'};">
                        ${daysLate > 0 ? '+' + daysLate : daysLate}
                    </div>
                    <div style="font-size: 11px; color: ${daysLate > 0 ? '#B91C1C' : '#047857'};">days ${daysLate > 0 ? 'late' : 'margin'}</div>
                </div>
                <div style="background: #EFF6FF; border-radius: 10px; padding: 12px; text-align: center;">
                    <div style="font-size: 22px; font-weight: 700; color: #1E40AF;">${daysRemaining}</div>
                    <div style="font-size: 11px; color: #3B82F6;">days remaining</div>
                </div>
                <div style="background: #FFF7ED; border-radius: 10px; padding: 12px; text-align: center;">
                    <div style="font-size: 22px; font-weight: 700; color: #9A3412;">${criticalTasks}</div>
                    <div style="font-size: 11px; color: #EA580C;">critical tasks</div>
                </div>
            </div>

            <!-- Progress Bar -->
            <div style="margin-bottom: 16px;">
                <div style="display: flex; justify-content: space-between; font-size: 12px; color: #6B7280; margin-bottom: 4px;">
                    <span>Schedule Progress</span>
                    <span>${progress}%</span>
                </div>
                <div style="background: #E5E7EB; border-radius: 8px; height: 10px; overflow: hidden;">
                    <div style="background: ${progress >= 80 ? '#059669' : progress >= 50 ? '#D97706' : '#DC2626'};
                                height: 100%; width: ${progress}%; transition: width 0.5s ease; border-radius: 8px;"></div>
                </div>
            </div>

            <!-- Task Breakdown -->
            <div style="background: #F9FAFB; border-radius: 10px; padding: 14px; margin-bottom: 12px;">
                <div style="font-size: 13px; font-weight: 600; color: #374151; margin-bottom: 10px;">
                    Task Breakdown (${totalTasks} total)
                </div>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                    <div style="display: flex; justify-content: space-between; font-size: 13px;">
                        <span style="color: #6B7280;">Production</span>
                        <span style="font-weight: 600; color: #374151;">${prodCount}</span>
                    </div>
                    <div style="display: flex; justify-content: space-between; font-size: 13px;">
                        <span style="color: #6B7280;">QA Inspection</span>
                        <span style="font-weight: 600; color: #374151;">${qualCount}</span>
                    </div>
                    <div style="display: flex; justify-content: space-between; font-size: 13px;">
                        <span style="color: #6B7280;">Rework</span>
                        <span style="font-weight: 600; color: ${reworkCount > 0 ? '#DC2626' : '#374151'};">${reworkCount}</span>
                    </div>
                    <div style="display: flex; justify-content: space-between; font-size: 13px;">
                        <span style="color: #6B7280;">Customer</span>
                        <span style="font-weight: 600; color: #374151;">${custCount}</span>
                    </div>
                    ${vendorCount > 0 ? `
                    <div style="display: flex; justify-content: space-between; font-size: 13px;">
                        <span style="color: #6B7280;">Vendor</span>
                        <span style="font-weight: 600; color: #D97706;">${vendorCount}</span>
                    </div>` : ''}
                    ${latePartCount > 0 ? `
                    <div style="display: flex; justify-content: space-between; font-size: 13px;">
                        <span style="color: #6B7280;">Late Parts</span>
                        <span style="font-weight: 600; color: #DC2626;">${latePartCount}</span>
                    </div>` : ''}
                </div>
            </div>

            <!-- Mini Timeline -->
            ${timelineHTML}

            <!-- Actions -->
            <div style="margin-top: 16px; display: flex; gap: 10px;">
                <button onclick="switchMgmtTab('burndown')" style="flex: 1; padding: 10px; background: #0033A0; color: white;
                    border: none; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer;">
                    View Burndown Chart
                </button>
                <button onclick="document.getElementById('productDetailOverlay').style.display='none'"
                    style="flex: 1; padding: 10px; background: #F3F4F6; color: #374151;
                    border: none; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer;">
                    Close
                </button>
            </div>
        </div>
    `;

    // Close on overlay background click
    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) overlay.style.display = 'none';
    });
}

function displayAggregatedView(viewData, viewType, selection) {
    const { tasks, mechanics, totalMechanics, teamName } = viewData;

    // Update header
    let headerText = '';
    if (selection === 'all') {
        headerText = 'All Workers Schedule';
    } else if (selection === 'all-mechanics') {
        headerText = 'All Mechanics Schedule';
    } else if (selection === 'all-quality') {
        headerText = 'All Quality Inspectors Schedule';
    } else if (selection === 'all-customer') {
        headerText = 'All Customer Inspectors Schedule';
    } else if (selection === 'all-vendor') {
        headerText = 'All Vendors Schedule';
    } else if (viewType === 'team') {
        headerText = `${teamName} Team Schedule`;
    }

    const mechanicNameElement = document.getElementById('mechanicName');
    if (mechanicNameElement) {
        mechanicNameElement.textContent = headerText;
    }

    // Build timeline with worker assignments
    const timeline = document.getElementById('mechanicTimeline');
    if (!timeline) return;

    timeline.innerHTML = '';

    if (tasks.length === 0) {
        timeline.innerHTML = `
            <div style="padding: 20px; text-align: center; color: #6b7280;">
                <div style="font-size: 48px; margin-bottom: 10px;">📋</div>
                <div style="font-size: 16px; font-weight: 500;">No Tasks Assigned</div>
                <div style="font-size: 14px; margin-top: 5px;">Use the Team Lead view to assign tasks first</div>
            </div>
        `;
        return;
    }

    // Add summary header
    const summaryHeader = document.createElement('div');
    summaryHeader.style.cssText = `
        background: #e0f2fe;
        padding: 12px;
        border-radius: 8px;
        margin-bottom: 15px;
    `;
    summaryHeader.innerHTML = `
        <strong>Coverage Summary</strong><br>
        Total Workers: ${totalMechanics}<br>
        Total Tasks: ${tasks.length}<br>
        ${Object.values(mechanics).map(m => `${m.name}: ${m.taskCount} tasks`).slice(0, 3).join('<br>')}
        ${totalMechanics > 3 ? `<br>...and ${totalMechanics - 3} more workers` : ''}
    `;
    timeline.appendChild(summaryHeader);

    // Group tasks by date first, then by time slots
    const tasksByDate = {};
    tasks.forEach(task => {
        const startTime = new Date(task.startTime);
        const dateKey = startTime.toDateString();

        if (!tasksByDate[dateKey]) {
            tasksByDate[dateKey] = {};
        }

        const timeKey = startTime.toISOString();
        if (!tasksByDate[dateKey][timeKey]) {
            tasksByDate[dateKey][timeKey] = [];
        }
        tasksByDate[dateKey][timeKey].push(task);
    });

    // Sort dates and limit total displayed items
    const sortedDates = Object.keys(tasksByDate).sort((a, b) =>
        new Date(a) - new Date(b)
    );

    let totalSlotsDisplayed = 0;
    const maxSlots = 50;

    sortedDates.forEach(dateStr => {
        if (totalSlotsDisplayed >= maxSlots) return;

        // Add date header
        const dateHeader = document.createElement('div');
        dateHeader.style.cssText = `
            background: #f3f4f6;
            padding: 8px 12px;
            font-weight: 600;
            color: #374151;
            margin: 15px 0 5px 0;
            border-radius: 6px;
            border-left: 3px solid #3b82f6;
        `;
        dateHeader.textContent = new Date(dateStr).toLocaleDateString('en-US', {
            weekday: 'long',
            month: 'short',
            day: 'numeric',
            year: 'numeric'
        });
        timeline.appendChild(dateHeader);

        // Sort time slots within this date
        const timeSlots = tasksByDate[dateStr];
        const sortedTimes = Object.keys(timeSlots).sort();

        sortedTimes.forEach(time => {
            if (totalSlotsDisplayed >= maxSlots) return;

            const slotTasks = timeSlots[time];
            const startTime = new Date(time);

            const slotDiv = document.createElement('div');
            slotDiv.className = 'timeline-item';
            slotDiv.style.borderLeftColor = '#3b82f6';

            const concurrentCount = slotTasks.length;
            const taskList = slotTasks.slice(0, 3).map(t =>
                `${t.taskId} (${t.assignedToName ? t.assignedToName.split(' - ')[0] : 'Unassigned'})`
            ).join(', ');

            slotDiv.innerHTML = `
                <div class="timeline-time">${formatTime(startTime)}</div>
                <div class="timeline-content">
                    <div class="timeline-task">
                        ${concurrentCount} Concurrent Task${concurrentCount > 1 ? 's' : ''}
                    </div>
                    <div class="timeline-details">
                        <span>${taskList}${concurrentCount > 3 ? ` +${concurrentCount - 3} more` : ''}</span>
                    </div>
                </div>
            `;

            timeline.appendChild(slotDiv);
            totalSlotsDisplayed++;
        });
    });

    // Add workload distribution
    const workloadDiv = document.createElement('div');
    workloadDiv.style.cssText = `
        background: #f9fafb;
        border: 1px solid #e5e7eb;
        border-radius: 8px;
        padding: 12px;
        margin-top: 20px;
    `;

    const totalMinutes = tasks.reduce((sum, t) => sum + (t.duration || 60), 0);
    const avgMinutesPerWorker = totalMechanics > 0 ? Math.round(totalMinutes / totalMechanics) : 0;

    workloadDiv.innerHTML = `
        <strong>Workload Analysis</strong><br>
        Total Work: ${Math.round(totalMinutes / 60)} hours<br>
        Average per Worker: ${Math.round(avgMinutesPerWorker / 60 * 10) / 10} hours<br>
        Utilization: ${Math.round(avgMinutesPerWorker / 480 * 100)}% (based on 8-hour shift)
    `;

    timeline.appendChild(workloadDiv);
}

function displayIndividualView(mechanicSchedule, mechanicId) {
    const mechanicNameElement = document.getElementById('mechanicName');
    const timeline = document.getElementById('mechanicTimeline');

    if (!timeline) return;

    if (!mechanicSchedule) {
        if (mechanicNameElement) {
            mechanicNameElement.textContent = 'Task Schedule';
        }
        timeline.innerHTML = `
            <div style="padding: 20px; text-align: center; color: #6b7280;">
                <div style="font-size: 48px; margin-bottom: 10px;">📋</div>
                <div style="font-size: 16px; font-weight: 500;">No Tasks Assigned</div>
                <div style="font-size: 14px; margin-top: 5px;">Use the Team Lead view to assign tasks</div>
            </div>
        `;
        return;
    }

    const mechanicTasks = mechanicSchedule.tasks || [];

    // Update header
    if (mechanicNameElement) {
        mechanicNameElement.textContent =
            `Task Schedule for ${mechanicSchedule.displayName || mechanicId}`;
    }

    // Build timeline
    timeline.innerHTML = '';

    if (mechanicTasks.length === 0) {
        timeline.innerHTML = `
            <div style="padding: 20px; text-align: center; color: #6b7280;">
                <div style="font-size: 48px; margin-bottom: 10px;">📋</div>
                <div style="font-size: 16px; font-weight: 500;">No Tasks Assigned</div>
                <div style="font-size: 14px; margin-top: 5px;">Use the Team Lead view to assign tasks</div>
            </div>
        `;
        return;
    }

    // Group tasks by date
    const tasksByDate = {};
    mechanicTasks.forEach(task => {
        const date = new Date(task.startTime).toDateString();
        if (!tasksByDate[date]) {
            tasksByDate[date] = [];
        }
        tasksByDate[date].push(task);
    });

    // Display tasks with changeover indicators
    Object.entries(tasksByDate).forEach(([date, tasks]) => {
        const dateHeader = document.createElement('div');
        dateHeader.style.cssText = `
            background: #f3f4f6;
            padding: 8px 12px;
            font-weight: 600;
            color: #374151;
            margin: 10px 0 5px 0;
            border-radius: 6px;
        `;
        dateHeader.textContent = date;
        timeline.appendChild(dateHeader);

        // Sort tasks by start time within date to detect changeovers
        tasks.sort((a, b) => new Date(a.startTime) - new Date(b.startTime));

        tasks.forEach((task, index) => {
            // Insert changeover indicator when mechanic switches aircraft
            if (index > 0) {
                const prevTask = tasks[index - 1];
                const prevProduct = prevTask.product || prevTask.lineNumber || '';
                const currProduct = task.product || task.lineNumber || '';
                if (prevProduct && currProduct && prevProduct !== currProduct) {
                    const changeover = document.createElement('div');
                    changeover.className = 'changeover-indicator';
                    changeover.innerHTML = `
                        <div class="changeover-line"></div>
                        <div class="changeover-badge">
                            <span class="changeover-icon">&#x1F6B6;</span>
                            <span>15 min changeover &mdash; ${prevProduct} &#x2192; ${currProduct}</span>
                        </div>
                        <div class="changeover-line"></div>
                    `;
                    timeline.appendChild(changeover);
                }
            }

            const startTime = new Date(task.startTime);
            const item = document.createElement('div');
            item.className = 'timeline-item';

            let borderColor = '#3b82f6';
            let typeIcon = '🔧';

            if (task.type === 'Quality Inspection') {
                borderColor = '#10b981';
                typeIcon = '✔';
            } else if (task.type === 'Late Part') {
                borderColor = '#f59e0b';
                typeIcon = '📦';
            } else if (task.type === 'Rework') {
                borderColor = '#ef4444';
                typeIcon = '🔄';
            }

            item.style.borderLeftColor = borderColor;
            item.innerHTML = `
                <div class="timeline-time">${formatTime(startTime)}</div>
                <div class="timeline-content">
                    <div class="timeline-task">
                        ${typeIcon} Task ${task.taskId} - ${task.type}
                    </div>
                    <div class="timeline-details">
                        <span>📦 ${task.product}</span>
                        <span>⏱️ ${task.duration} minutes</span>
                    </div>
                </div>
            `;
            timeline.appendChild(item);
        });
    });
}

function displayNoSelection() {
    const mechanicNameElement = document.getElementById('mechanicName');
    const timeline = document.getElementById('mechanicTimeline');

    if (mechanicNameElement) {
        mechanicNameElement.textContent = 'Task Schedule';
    }

    if (timeline) {
        timeline.innerHTML =
            '<div style="padding: 20px; color: #6b7280;">Select a worker or team to view schedule</div>';
    }
}

// Also add these helper functions if they don't exist:
function getAggregatedTasks(selection, skillFilter) {
    const allTasks = [];
    const mechanicsSummary = {};

    const assignments = savedAssignments[currentScenario] || {};
    const schedules = generateMechanicSchedulesFromAssignments(assignments);

    if (Object.keys(schedules).length === 0) {
        return { tasks: [], mechanics: {}, totalMechanics: 0 };
    }

    Object.entries(schedules).forEach(([mechanicId, schedule]) => {
        // Determine if this worker should be included based on selection
        let include = false;
        const isQuality = schedule.isQuality || isQualityTeam(schedule.team);
        const isCustomer = schedule.isCustomer || isCustomerTeam(schedule.team);
        const isVendor = schedule.isVendor || isVendorTeam(schedule.team);

        if (selection === 'all') {
            include = true;
        } else if (selection === 'all-mechanics' && !isQuality && !isCustomer && !isVendor) {
            include = true;
        } else if (selection === 'all-quality' && isQuality) {
            include = true;
        } else if (selection === 'all-customer' && isCustomer) {
            include = true;
        } else if (selection === 'all-vendor' && isVendor) {
            include = true;
        }

        if (include) {
            // Add mechanic to summary
            mechanicsSummary[mechanicId] = {
                name: schedule.displayName || mechanicId,
                taskCount: schedule.tasks ? schedule.tasks.length : 0,
                team: schedule.team,
                skill: schedule.skill
            };

            // Add tasks with mechanic info
            if (schedule.tasks) {
                schedule.tasks.forEach(task => {
                    allTasks.push({
                        ...task,
                        assignedTo: mechanicId,
                        assignedToName: schedule.displayName || mechanicId
                    });
                });
            }
        }
    });

    // Sort tasks by start time
    allTasks.sort((a, b) => new Date(a.startTime) - new Date(b.startTime));

    return {
        tasks: allTasks,
        mechanics: mechanicsSummary,
        totalMechanics: Object.keys(mechanicsSummary).length
    };
}

function getTeamTasks(teamName, skillFilter) {
    const teamTasks = [];
    const mechanicsSummary = {};

    const teamAssignments = savedAssignments[currentScenario] || {};
    const schedules = generateMechanicSchedulesFromAssignments(teamAssignments);
    if (Object.keys(schedules).length === 0) {
        return { tasks: [], mechanics: {}, totalMechanics: 0, teamName: teamName };
    }

    Object.entries(schedules).forEach(([mechanicId, schedule]) => {
        // Check if this mechanic belongs to the selected team
        if (schedule.team === teamName || mechanicId.includes(teamName)) {
            // Parse skill
            const teamSkillMatch = mechanicId.match(/^(.+?)_\d+$/);
            const teamSkill = teamSkillMatch ? teamSkillMatch[1] : mechanicId;
            const skillMatch = teamSkill.match(/\((.+?)\)/);
            const skill = skillMatch ? skillMatch[1] : null;

            // Apply skill filter
            if (skillFilter === 'all' || skill === skillFilter) {
                mechanicsSummary[mechanicId] = {
                    name: schedule.displayName || mechanicId,
                    taskCount: schedule.tasks ? schedule.tasks.length : 0,
                    skill: skill
                };

                if (schedule.tasks) {
                    schedule.tasks.forEach(task => {
                        teamTasks.push({
                            ...task,
                            assignedTo: mechanicId,
                            assignedToName: schedule.displayName || mechanicId
                        });
                    });
                }
            }
        }
    });

    // Sort tasks by start time
    teamTasks.sort((a, b) => new Date(a.startTime) - new Date(b.startTime));

    return {
        tasks: teamTasks,
        mechanics: mechanicsSummary,
        totalMechanics: Object.keys(mechanicsSummary).length,
        teamName: teamName
    };
}

function getIndividualMechanicTasks(mechanicId) {
    const assignments = savedAssignments[currentScenario] || {};
    const schedules = generateMechanicSchedulesFromAssignments(assignments);
    return schedules[mechanicId] || null;
}

function formatTime(date) {
    return date.toLocaleTimeString('en-US', {
        hour: 'numeric',
        minute: '2-digit',
        hour12: true
    });
}

// Helper functions
function getTaskTypeClass(type) {
    const typeMap = {
        'Production': 'production',
        'Quality Inspection': 'quality',
        'Customer Inspection': 'customer',
        'Late Part': 'late-part',
        'Rework': 'rework',
        'Vendor': 'vendor'
    };
    return typeMap[type] || 'production';
}

function getTaskTypeColor(type) {
    const colorMap = {
        'Production': '#10b981',
        'Quality Inspection': '#3b82f6',
        'Customer Inspection': '#8b5cf6',  // Purple for customer
        'Late Part': '#f59e0b',
        'Rework': '#ef4444',
        'Vendor': '#d97706'  // Amber for vendor
    };
    return colorMap[type] || '#6b7280';
}

function formatDate(date) {
    return date.toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        year: 'numeric'
    });
}

// Gantt chart helpers
function getGanttColor(product, isCritical) {
    const productColors = {
        'Product A': 'gantt-prod-a',
        'Product B': 'gantt-prod-b',
        'Product C': 'gantt-prod-c',
        'Product D': 'gantt-prod-d',
        'Product E': 'gantt-prod-e'
    };
    let classes = '';
    if (productColors[product]) {
        classes += productColors[product];
    }
    if (isCritical) {
        classes += ' gantt-critical';
    }
    return classes.trim();
}

// Replace all your Gantt chart functions in dashboard-js.js with these vis.js Timeline functions

let timeline = null;
let timelineContainer = null;
let currentTimelineData = [];
let currentTimelineGroups = [];

// (First initializeTimeline removed - kept the enhanced version below with better error handling)

// Get timeline options based on selected time scale
function getTimelineOptions(timeScale) {
    const baseOptions = {
        stack: true,
        showCurrentTime: true,
        zoomable: true,
        moveable: true,
        selectable: true,
        multiselect: false,
        editable: false,
        orientation: 'top',
        height: '600px',
        margin: {
            item: 2,
            axis: 20
        },
        tooltip: {
            followMouse: true,
            overflowMethod: 'cap'
        }
    };

    // Custom format and zoom settings based on time scale
    const scaleConfigs = {
        '15min': {
            format: {
                minorLabels: {
                    minute: 'mm',
                    hour: 'HH:mm'
                },
                majorLabels: {
                    minute: 'HH:mm',
                    hour: 'ddd D MMMM HH:mm',
                    day: 'ddd D MMMM YYYY'
                }
            },
            zoomMin: 1000 * 60 * 15, // 15 minutes
            zoomMax: 1000 * 60 * 60 * 8, // 8 hours
            timeAxis: { scale: 'minute', step: 15 }
        },
        '30min': {
            format: {
                minorLabels: {
                    minute: 'HH:mm',
                    hour: 'HH:mm'
                },
                majorLabels: {
                    minute: 'HH:mm',
                    hour: 'ddd D MMMM HH:mm',
                    day: 'ddd D MMMM YYYY'
                }
            },
            zoomMin: 1000 * 60 * 30, // 30 minutes
            zoomMax: 1000 * 60 * 60 * 12, // 12 hours
            timeAxis: { scale: 'minute', step: 30 }
        },
        '1hour': {
            format: {
                minorLabels: {
                    hour: 'HH:mm',
                    day: 'D'
                },
                majorLabels: {
                    hour: 'ddd D MMMM',
                    day: 'MMMM YYYY'
                }
            },
            zoomMin: 1000 * 60 * 60, // 1 hour
            zoomMax: 1000 * 60 * 60 * 24, // 1 day
            timeAxis: { scale: 'hour', step: 1 }
        },
        '4hour': {
            format: {
                minorLabels: {
                    hour: 'HH:mm',
                    day: 'D'
                },
                majorLabels: {
                    hour: 'ddd D MMMM',
                    day: 'MMMM YYYY'
                }
            },
            zoomMin: 1000 * 60 * 60 * 4, // 4 hours
            zoomMax: 1000 * 60 * 60 * 24 * 3, // 3 days
            timeAxis: { scale: 'hour', step: 4 }
        },
        '8hour': {
            format: {
                minorLabels: {
                    hour: 'HH:mm',
                    day: 'D'
                },
                majorLabels: {
                    hour: 'ddd D MMMM',
                    day: 'MMMM YYYY'
                }
            },
            zoomMin: 1000 * 60 * 60 * 8, // 8 hours
            zoomMax: 1000 * 60 * 60 * 24 * 7, // 1 week
            timeAxis: { scale: 'hour', step: 8 }
        },
        '1day': {
            format: {
                minorLabels: {
                    day: 'D',
                    week: 'w'
                },
                majorLabels: {
                    day: 'MMMM YYYY',
                    week: 'MMMM YYYY',
                    month: 'YYYY'
                }
            },
            zoomMin: 1000 * 60 * 60 * 24, // 1 day
            zoomMax: 1000 * 60 * 60 * 24 * 31, // 1 month
            timeAxis: { scale: 'day', step: 1 }
        },
        '1week': {
            format: {
                minorLabels: {
                    week: 'w',
                    month: 'MMM'
                },
                majorLabels: {
                    week: 'MMMM YYYY',
                    month: 'YYYY'
                }
            },
            zoomMin: 1000 * 60 * 60 * 24 * 7, // 1 week
            zoomMax: 1000 * 60 * 60 * 24 * 365, // 1 year
            timeAxis: { scale: 'week', step: 1 }
        },
        '2weeks': {
            format: {
                minorLabels: {
                    week: 'w',
                    month: 'MMM'
                },
                majorLabels: {
                    week: 'MMMM YYYY',
                    month: 'YYYY'
                }
            },
            zoomMin: 1000 * 60 * 60 * 24 * 14, // 2 weeks
            zoomMax: 1000 * 60 * 60 * 24 * 365, // 1 year
            timeAxis: { scale: 'week', step: 2 }
        },
        '1month': {
            format: {
                minorLabels: {
                    month: 'MMM',
                    year: 'YYYY'
                },
                majorLabels: {
                    month: 'YYYY',
                    year: ''
                }
            },
            zoomMin: 1000 * 60 * 60 * 24 * 30, // 1 month
            zoomMax: 1000 * 60 * 60 * 24 * 365 * 5, // 5 years
            timeAxis: { scale: 'month', step: 1 }
        }
    };

    const config = scaleConfigs[timeScale] || scaleConfigs['1day'];

    return {
        ...baseOptions,
        ...config
    };
}

// Enhanced event listener setup with time scale handling
function setupTimelineEventListeners() {
    if (!timeline) return;

    // Product filter
    const productSelect = document.getElementById('timelineProductSelect');
    if (productSelect) {
        productSelect.addEventListener('change', renderTimeline);
    }

    // Team filter
    const teamSelect = document.getElementById('timelineTeamSelect');
    if (teamSelect) {
        teamSelect.addEventListener('change', renderTimeline);
    }

    // Time scale selector
    const scaleSelect = document.getElementById('timelineScale');
    if (scaleSelect) {
        scaleSelect.addEventListener('change', function() {
            // Reinitialize timeline with new scale
            const currentWindow = timeline.getWindow();
            initializeTimeline();
            // Try to maintain current view if possible
            setTimeout(() => {
                if (timeline && currentWindow) {
                    timeline.setWindow(currentWindow.start, currentWindow.end);
                }
            }, 100);
        });
    }

    // Group by selector
    const groupBySelect = document.getElementById('timelineGroupBy');
    if (groupBySelect) {
        groupBySelect.addEventListener('change', renderTimeline);
    }

    // Focus date input
    const focusDateInput = document.getElementById('timelineFocusDate');
    if (focusDateInput) {
        focusDateInput.addEventListener('change', function() {
            if (this.value) {
                goToDate(new Date(this.value));
            }
        });
    }

    // Timeline event handlers
    timeline.on('select', function (properties) {
        if (properties.items.length > 0) {
            const itemId = properties.items[0];
            const item = currentTimelineData.find(d => d.id === itemId);
            if (item) {
                showTaskDetails(item);
            }
        }
    });

    timeline.on('doubleClick', function (properties) {
        if (properties.time) {
            focusOnTime(new Date(properties.time));
        }
    });

    // Update focus date input when timeline window changes
    timeline.on('rangechange', function (properties) {
        const focusDateInput = document.getElementById('timelineFocusDate');
        if (focusDateInput && properties.start && properties.end) {
            const midTime = new Date((properties.start.getTime() + properties.end.getTime()) / 2);
            focusDateInput.value = midTime.toISOString().slice(0, 16);
        }
    });
}


// Go to current time
function goToNow() {
    if (!timeline) return;

    const now = new Date();
    focusOnTime(now);

    // Update focus date input
    const focusDateInput = document.getElementById('timelineFocusDate');
    if (focusDateInput) {
        focusDateInput.value = now.toISOString().slice(0, 16);
    }
}

// Focus on specific date/time
function goToDate(date) {
    if (!timeline || !date) return;

    focusOnTime(date);
}

// Validate task data before positioning
function validateTaskData(tasks) {
    console.log('\n=== VALIDATING TASK DATA ===');
    tasks.slice(0, 5).forEach(task => {
        const startDate = new Date(task.startDate);
        const endDate = new Date(task.endDate);

        console.log(`Task ${task.id}:
            Raw start: ${task.startDate}
            Raw end: ${task.endDate}
            Parsed start: ${startDate.toLocaleString()} (${startDate.getTime()})
            Parsed end: ${endDate.toLocaleString()} (${endDate.getTime()})
            Valid: ${!isNaN(startDate.getTime()) && !isNaN(endDate.getTime())}`);
    });
}

// Focus on specific time with appropriate window based on scale
function focusOnTime(centerTime) {
    if (!timeline) return;

    const timeScale = document.getElementById('timelineScale')?.value || '1day';
    let windowSize;

    // Set window size based on time scale
    const windowSizes = {
        '15min': 1000 * 60 * 60 * 2,      // 2 hours
        '30min': 1000 * 60 * 60 * 4,      // 4 hours
        '1hour': 1000 * 60 * 60 * 8,      // 8 hours
        '4hour': 1000 * 60 * 60 * 24,     // 1 day
        '8hour': 1000 * 60 * 60 * 24 * 2, // 2 days
        '1day': 1000 * 60 * 60 * 24 * 7,  // 1 week
        '1week': 1000 * 60 * 60 * 24 * 30, // 1 month
        '2weeks': 1000 * 60 * 60 * 24 * 60, // 2 months
        '1month': 1000 * 60 * 60 * 24 * 365 // 1 year
    };

    windowSize = windowSizes[timeScale] || windowSizes['1day'];

    const start = new Date(centerTime.getTime() - windowSize / 2);
    const end = new Date(centerTime.getTime() + windowSize / 2);

    timeline.setWindow(start, end, { animation: true });
}

// Enhanced render function with scale-aware time windows
function renderTimeline() {
    if (!timeline) {
        initializeTimeline();
        return;
    }

    const productFilter = document.getElementById('timelineProductSelect')?.value || 'all';
    const teamFilter = document.getElementById('timelineTeamSelect')?.value || 'all';
    const groupBy = document.getElementById('timelineGroupBy')?.value || 'team';

    const filteredTasks = getTimelineTasks(productFilter, teamFilter);

    if (filteredTasks.length === 0) {
        timeline.setData([]);
        timeline.setGroups([]);
        updateTimelineStats([], []);
        return;
    }

    const timelineItems = convertTasksToTimelineItems(filteredTasks);
    const timelineGroups = createTimelineGroups(filteredTasks, groupBy);

    timeline.setItems(timelineItems);
    timeline.setGroups(timelineGroups);

    currentTimelineData = timelineItems;
    currentTimelineGroups = timelineGroups;

    updateTimelineStats(timelineItems, timelineGroups);
    updateTimelineProductFilter();
}

// Enhanced fit to tasks with scale awareness
function fitTimelineToTasks() {
    if (!timeline || currentTimelineData.length === 0) return;

    const startTimes = currentTimelineData.map(item => new Date(item.start));
    const endTimes = currentTimelineData.map(item => new Date(item.end));
    const minStart = new Date(Math.min(...startTimes));
    const maxEnd = new Date(Math.max(...endTimes));

    // Add padding based on current time scale
    const timeScale = document.getElementById('timelineScale')?.value || '1day';
    const paddings = {
        '15min': 1000 * 60 * 30,           // 30 minutes
        '30min': 1000 * 60 * 60,           // 1 hour
        '1hour': 1000 * 60 * 60 * 2,       // 2 hours
        '4hour': 1000 * 60 * 60 * 4,       // 4 hours
        '8hour': 1000 * 60 * 60 * 8,       // 8 hours
        '1day': 1000 * 60 * 60 * 24,       // 1 day
        '1week': 1000 * 60 * 60 * 24 * 2,  // 2 days
        '2weeks': 1000 * 60 * 60 * 24 * 7, // 1 week
        '1month': 1000 * 60 * 60 * 24 * 14 // 2 weeks
    };

    const padding = paddings[timeScale] || paddings['1day'];

    minStart.setTime(minStart.getTime() - padding);
    maxEnd.setTime(maxEnd.getTime() + padding);

    timeline.setWindow(minStart, maxEnd, { animation: true });
}

// Update timeline statistics with scale info
function updateTimelineStats(items, groups) {
    document.getElementById('timelineTotalTasks').textContent = items.length;
    document.getElementById('timelineGroupsCount').textContent = groups.length;

    if (items.length === 0) {
        document.getElementById('timelineSpan').textContent = '-';
        document.getElementById('timelinePeakConcurrency').textContent = '0';
        return;
    }

    // Calculate time span
    const startTimes = items.map(item => new Date(item.start));
    const endTimes = items.map(item => new Date(item.end));
    const minStart = new Date(Math.min(...startTimes));
    const maxEnd = new Date(Math.max(...endTimes));
    const spanMinutes = Math.round((maxEnd - minStart) / (1000 * 60));

    let spanText;
    if (spanMinutes < 60) {
        spanText = `${spanMinutes}min`;
    } else if (spanMinutes < 60 * 24) {
        const hours = Math.floor(spanMinutes / 60);
        const minutes = spanMinutes % 60;
        spanText = minutes > 0 ? `${hours}h ${minutes}min` : `${hours}h`;
    } else {
        const days = Math.floor(spanMinutes / (60 * 24));
        const hours = Math.floor((spanMinutes % (60 * 24)) / 60);
        spanText = hours > 0 ? `${days}d ${hours}h` : `${days}d`;
    }

    document.getElementById('timelineSpan').textContent = spanText;

    // Calculate peak concurrency
    const concurrency = calculatePeakConcurrency(items);
    document.getElementById('timelinePeakConcurrency').textContent = concurrency;
}

// Make new functions globally available
window.goToNow = goToNow;
window.goToDate = goToDate;


// Get tasks filtered by product and team
function getTimelineTasks(productFilter, teamFilter) {
    if (!scenarioData || !scenarioData.tasks) return [];

    return scenarioData.tasks.filter(task => {
        // Product filter
        if (productFilter !== 'all' && task.product !== productFilter) {
            return false;
        }

        // Team filter with role aggregation
        if (teamFilter === 'all') {
            return true;
        } else if (teamFilter === 'all-mechanics') {
            return task.team && task.team.toLowerCase().includes('mechanic');
        } else if (teamFilter === 'all-quality') {
            return task.team && task.team.toLowerCase().includes('quality');
        } else if (teamFilter === 'all-customer') {
            return task.team && (task.team.toLowerCase().includes('customer') || task.isCustomerTask);
        } else {
            return task.team === teamFilter;
        }
    });
}

// Convert tasks to vis.js timeline items
function convertTasksToTimelineItems(tasks) {
    return tasks.map(task => {
        const startTime = new Date(task.startTime);
        const endTime = new Date(task.endTime);

        // Determine task type and color - FIX: Remove invalid types for vis.js
        let className = 'task-production';
        let taskType = 'Production';

        if (task.isCustomerTask || task.type === 'Customer' || task.type === 'Customer Inspection') {
            className = 'task-customer';
            taskType = 'Customer';
        } else if (task.type === 'Quality Inspection' || task.team?.toLowerCase().includes('quality')) {
            className = 'task-quality';
            taskType = 'Quality';
        } else if (task.isLatePartTask || task.type === 'Late Part') {
            className = 'task-late-part';
            taskType = 'Late Part';
        } else if (task.isReworkTask || task.type === 'Rework') {
            className = 'task-rework';
            taskType = 'Rework';
        }

        // Add priority indicators
        if (task.isCritical || task.priority <= 10) {
            className += ' task-critical';
        } else if (task.priority <= 20) {
            className += ' task-high-priority';
        }

        // Create tooltip content
        const duration = task.duration || Math.round((endTime - startTime) / (1000 * 60));
        const tooltip = `
            <b>${task.taskId} - ${taskType}</b><br/>
            Product: ${task.product}<br/>
            Team: ${task.team}<br/>
            Duration: ${duration} minutes<br/>
            Start: ${startTime.toLocaleString()}<br/>
            End: ${endTime.toLocaleString()}<br/>
            ${task.priority ? `Priority: ${task.priority}<br/>` : ''}
            ${task.dependencies?.length ? `Dependencies: ${task.dependencies.length}<br/>` : ''}
        `;

        return {
            id: task.taskId,
            content: `${task.taskId}<br/><small>${duration}min</small>`,
            start: startTime,
            end: endTime,
            group: getTaskGroup(task, document.getElementById('timelineGroupBy')?.value || 'team'),
            className: className,
            title: tooltip,
            // FIX: Use standard vis.js item type instead of custom types
            type: 'range',  // vis.js recognizes: 'box', 'point', 'range', 'background'
            // Store original task data
            taskData: task,
            taskType: taskType,  // Keep our custom type separate
            duration: duration,
            priority: task.priority || 999
        };
    });
}


// Create groups for timeline
function createTimelineGroups(tasks, groupBy) {
    const groupMap = new Map();

    tasks.forEach(task => {
        const groupId = getTaskGroup(task, groupBy);
        if (!groupMap.has(groupId)) {
            groupMap.set(groupId, {
                id: groupId,
                content: groupId,
                tasks: []
            });
        }
        groupMap.get(groupId).tasks.push(task);
    });

    // Convert to array and add task counts
    return Array.from(groupMap.values()).map(group => ({
        id: group.id,
        content: `${group.content} (${group.tasks.length})`,
        style: getGroupStyle(group.id, groupBy)
    }));
}

// Get group ID for a task
function getTaskGroup(task, groupBy) {
    switch (groupBy) {
        case 'product':
            return task.product || 'Unknown Product';
        case 'type':
            if (task.isCustomerTask || task.type === 'Customer') return 'Customer Tasks';
            if (task.type === 'Quality Inspection') return 'Quality Inspection';
            if (task.isLatePartTask || task.type === 'Late Part') return 'Late Parts';
            if (task.isReworkTask || task.type === 'Rework') return 'Rework';
            return 'Production';
        case 'team':
        default:
            return task.team || 'Unknown Team';
    }
}

// Get group styling
function getGroupStyle(groupId, groupBy) {
    if (groupBy === 'type') {
        if (groupId.includes('Customer')) return 'background-color: #f3e8ff; border-left: 4px solid #8b5cf6;';
        if (groupId.includes('Quality')) return 'background-color: #eff6ff; border-left: 4px solid #3b82f6;';
        if (groupId.includes('Late Part')) return 'background-color: #fefbf3; border-left: 4px solid #f59e0b;';
        if (groupId.includes('Rework')) return 'background-color: #fef2f2; border-left: 4px solid #ef4444;';
        return 'background-color: #f0fdf4; border-left: 4px solid #10b981;';
    }
    return '';
}

// Set timeline window based on range
function setTimelineWindow(range, tasks) {
    if (!timeline || tasks.length === 0) return;

    const now = new Date();
    const taskTimes = tasks.map(t => new Date(t.startTime));
    const minTime = new Date(Math.min(...taskTimes));
    const maxTime = new Date(Math.max(...tasks.map(t => new Date(t.endTime))));

    let start, end;

    switch (range) {
        case 'day':
            start = new Date(minTime);
            start.setHours(0, 0, 0, 0);
            end = new Date(start);
            end.setDate(end.getDate() + 1);
            break;
        case 'week':
            start = new Date(minTime);
            start.setDate(start.getDate() - start.getDay());
            start.setHours(0, 0, 0, 0);
            end = new Date(start);
            end.setDate(end.getDate() + 7);
            break;
        case 'month':
        default:
            start = new Date(minTime);
            start.setDate(1);
            start.setHours(0, 0, 0, 0);
            end = new Date(maxTime);
            end.setMonth(end.getMonth() + 1);
            end.setDate(1);
            break;
    }

    timeline.setWindow(start, end);
}

// Calculate maximum concurrent tasks
function calculatePeakConcurrency(items) {
    const events = [];

    // Create start/end events
    items.forEach(item => {
        events.push({ time: new Date(item.start), type: 'start' });
        events.push({ time: new Date(item.end), type: 'end' });
    });

    // Sort by time
    events.sort((a, b) => a.time - b.time);

    let current = 0;
    let max = 0;

    events.forEach(event => {
        if (event.type === 'start') {
            current++;
            max = Math.max(max, current);
        } else {
            current--;
        }
    });

    return max;
}

// Update product filter dropdown
function updateTimelineProductFilter() {
    const productSelect = document.getElementById('timelineProductSelect');
    if (!productSelect || !scenarioData?.products) return;

    const currentValue = productSelect.value;
    productSelect.innerHTML = '<option value="all">All Products</option>';

    scenarioData.products.forEach(product => {
        const option = document.createElement('option');
        option.value = product.name;
        option.textContent = product.name;
        productSelect.appendChild(option);
    });

    // Restore selection
    if ([...productSelect.options].some(opt => opt.value === currentValue)) {
        productSelect.value = currentValue;
    }
}

// Show task details popup
function showTaskDetails(item) {
    const task = item.taskData;
    if (!task) return;

    const details = `
Task: ${task.taskId}
Type: ${item.type}
Product: ${task.product}
Team: ${task.team}
Duration: ${item.duration} minutes
Start: ${new Date(item.start).toLocaleString()}
End: ${new Date(item.end).toLocaleString()}
Priority: ${task.priority || 'N/A'}
Dependencies: ${task.dependencies?.length || 0}
${task.isCritical ? 'CRITICAL TASK' : ''}
    `;

    alert(details);
}

// Refresh timeline
function refreshTimeline() {
    renderTimeline();
    showNotification('Timeline refreshed', 'success');
}

// Export timeline data
function exportTimelineData() {
    if (currentTimelineData.length === 0) {
        alert('No timeline data to export');
        return;
    }

    const productFilter = document.getElementById('timelineProductSelect')?.value || 'all';
    const teamFilter = document.getElementById('timelineTeamSelect')?.value || 'all';
    const groupBy = document.getElementById('timelineGroupBy')?.value || 'team';

    let csvContent = "High-Granularity Production Timeline Export\n";
    csvContent += `Generated: ${new Date().toLocaleString()}\n`;
    csvContent += `Scenario: ${currentScenario}\n`;
    csvContent += `Filters: Product=${productFilter}, Team=${teamFilter}, GroupBy=${groupBy}\n\n`;

    csvContent += "Task ID,Type,Product,Team,Group,Priority,Start Time,End Time,Duration (min),Critical,Dependencies\n";

    currentTimelineData
        .sort((a, b) => new Date(a.start) - new Date(b.start))
        .forEach(item => {
            const task = item.taskData;
            const startTime = new Date(item.start).toLocaleString();
            const endTime = new Date(item.end).toLocaleString();
            const isCritical = task.isCritical ? 'Yes' : 'No';
            const dependencies = task.dependencies?.length || 0;

            csvContent += `"${item.id}","${item.type}","${task.product}","${task.team}","${item.group}","${item.priority}","${startTime}","${endTime}","${item.duration}","${isCritical}","${dependencies}"\n`;
        });

    // Add statistics
    csvContent += "\nTimeline Statistics:\n";
    csvContent += `Total Tasks: ${currentTimelineData.length}\n`;
    csvContent += `Groups: ${currentTimelineGroups.length}\n`;
    csvContent += `Peak Concurrency: ${document.getElementById('timelinePeakConcurrency').textContent}\n`;
    csvContent += `Time Span: ${document.getElementById('timelineSpan').textContent}\n`;

    // Download
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    const url = URL.createObjectURL(blob);
    link.setAttribute('href', url);
    link.setAttribute('download', `timeline_${currentScenario}_${new Date().toISOString().slice(0, 10)}.csv`);
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    showNotification('Timeline data exported successfully!', 'success');
}

// Replace the old Gantt functions - these are for backward compatibility
function setupGanttProductFilter() {
    updateTimelineProductFilter();
}

// Enhanced getGanttTasks function with role aggregation support
function getGanttTasks(productFilter = 'all', teamFilter = 'all') {
    if (!scenarioData || !scenarioData.tasks) return [];

    return scenarioData.tasks
        .filter(task => {
            // Product filter
            if (productFilter !== 'all' && task.product !== productFilter) {
                return false;
            }

            // Team filter with role aggregation
            if (teamFilter === 'all') {
                return true;
            } else if (teamFilter === 'all-mechanics') {
                return task.team && task.team.toLowerCase().includes('mechanic');
            } else if (teamFilter === 'all-quality') {
                return task.team && task.team.toLowerCase().includes('quality');
            } else if (teamFilter === 'all-customer') {
                return task.team && task.team.toLowerCase().includes('customer');
            } else {
                return task.team === teamFilter;
            }
        })
        .map(task => ({
            id: task.taskId,
            name: `${task.team} - Task ${task.taskId} - ${task.type}`,
            start: task.startTime,
            end: task.endTime,
            progress: 100,
            custom_class: getGanttColor(task.product, task.isCriticalPath),
            dependencies: (task.dependencies || []).map(d =>
                typeof d === 'object' ? (d.taskId || d.id || d.task) : d
            ).join(','),
            // Additional properties for sorting
            product: task.product,
            team: task.team,
            type: task.type,
            priority: task.priority || 999,
            duration: task.duration || 0
        }));
}

let gantt;
// Enhanced render function with view mode support
function renderGanttChart() {
    // Add null checks for DOM elements
    const ganttProductSelect = document.getElementById('ganttProductSelect');
    const ganttTeamSelect = document.getElementById('ganttTeamSelect');
    const ganttSortSelect = document.getElementById('ganttSortSelect');

    const productFilter = ganttProductSelect ? ganttProductSelect.value || 'all' : 'all';
    const teamFilter = ganttTeamSelect ? ganttTeamSelect.value || 'all' : 'all';
    const sortBy = ganttSortSelect ? ganttSortSelect.value || 'start' : 'start';

    let tasks = getGanttTasks(productFilter, teamFilter);

    if (tasks.length === 0) {
        const ganttDiv = document.getElementById('ganttChart');
        if (ganttDiv) {
            ganttDiv.innerHTML = '<div style="color: #ef4444; padding: 40px; text-align: center;">No tasks to display for the selected filters.</div>';
        }
        return;
    }

    // Apply sorting with null checks
    switch(sortBy) {
        case 'start':
            tasks.sort((a, b) => new Date(a.start) - new Date(b.start));
            break;
        case 'product':
            tasks.sort((a, b) => {
                const productA = a.product || '';
                const productB = b.product || '';
                if (productA !== productB) {
                    return productA.localeCompare(productB);
                }
                return new Date(a.start) - new Date(b.start);
            });
            break;
        case 'priority':
            tasks.sort((a, b) => {
                const priorityA = a.priority || 999;
                const priorityB = b.priority || 999;
                if (priorityA !== priorityB) {
                    return priorityA - priorityB;
                }
                return new Date(a.start) - new Date(b.start);
            });
            break;
        case 'team':
            tasks.sort((a, b) => {
                const teamA = a.team || '';
                const teamB = b.team || '';
                if (teamA !== teamB) {
                    return teamA.localeCompare(teamB);
                }
                return new Date(a.start) - new Date(b.start);
            });
            break;
        case 'duration':
            tasks.sort((a, b) => {
                const durA = a.duration || 0;
                const durB = b.duration || 0;
                if (durA !== durB) {
                    return durB - durA;
                }
                return new Date(a.start) - new Date(b.start);
            });
            break;
    }

    renderGanttChartWithTasks(tasks);
}

// Fix 3: Enhanced renderGanttChartWithTasks with better error handling

function renderGanttChartWithTasks(tasks) {
    const ganttDiv = document.getElementById('ganttChart');
    if (!ganttDiv) {
        console.error('Gantt chart container not found');
        return;
    }

    const ganttViewModeSelect = document.getElementById('ganttViewMode');
    const ganttSortSelect = document.getElementById('ganttSortSelect');

    const viewMode = ganttViewModeSelect ? ganttViewModeSelect.value || 'Day' : 'Day';
    const sortBy = ganttSortSelect ? ganttSortSelect.value || 'start' : 'start';

    // Clear the gantt chart
    ganttDiv.innerHTML = '';

    // Remove any existing info divs
    const existingInfoDivs = ganttDiv.parentNode ? ganttDiv.parentNode.querySelectorAll('.gantt-info-div') : [];
    existingInfoDivs.forEach(div => div.remove());

    if (tasks.length === 0) {
        ganttDiv.innerHTML = '<div style="color: #ef4444; padding: 40px; text-align: center;">No tasks to display.</div>';
        return;
    }

    try {
        // Validate tasks data before creating Gantt
        const validatedTasks = tasks.map(task => ({
            id: task.id || `task_${Math.random().toString(36).substr(2, 9)}`,
            name: task.name || `Task ${task.id}`,
            start: task.start,
            end: task.end,
            progress: task.progress || 0,
            custom_class: task.custom_class || '',
            dependencies: task.dependencies || '',
            product: task.product || 'Unknown',
            team: task.team || 'Unknown',
            type: task.type || 'Production',
            priority: task.priority || 999,
            duration: task.duration || 0
        }));

        // Create new Gantt chart with error handling
        if (typeof Gantt !== 'undefined') {
            window.gantt = new Gantt(ganttDiv, validatedTasks, {
                header_height: 50,
                column_width: viewMode === 'Hour' ? 60 : viewMode === 'Day' ? 30 : viewMode === 'Week' ? 140 : 300,
                step: viewMode === 'Hour' ? 24 : undefined,
                view_modes: ['Quarter Day', 'Half Day', 'Day', 'Week', 'Month'],
                bar_height: 20,
                bar_corner_radius: 3,
                arrow_curve: 5,
                padding: 18,
                view_mode: viewMode,
                date_format: 'YYYY-MM-DD',
                custom_popup_html: function(task) {
                    const start_date = new Date(task.start);
                    const end_date = new Date(task.end);
                    const duration = Math.ceil((end_date - start_date) / (1000 * 60 * 60 * 24));

                    return `
                        <div class="details-container">
                            <h5>${task.name || 'Unnamed Task'}</h5>
                            <p><strong>Product:</strong> ${task.product || 'N/A'}</p>
                            <p><strong>Team:</strong> ${task.team || 'N/A'}</p>
                            <p><strong>Type:</strong> ${task.type || 'N/A'}</p>
                            <p><strong>Duration:</strong> ${duration} day${duration !== 1 ? 's' : ''}</p>
                            <p><strong>Start:</strong> ${start_date.toLocaleDateString()}</p>
                            <p><strong>End:</strong> ${end_date.toLocaleDateString()}</p>
                            ${task.priority ? `<p><strong>Priority:</strong> ${task.priority}</p>` : ''}
                        </div>
                    `;
                }
            });

            // Add task count info
            if (ganttDiv.parentNode) {
                const infoDiv = document.createElement('div');
                infoDiv.className = 'gantt-info-div';
                infoDiv.style.cssText = 'padding: 10px; background: #f3f4f6; border-radius: 6px; margin-bottom: 10px; font-size: 14px; color: #374151;';
                infoDiv.innerHTML = `Showing ${validatedTasks.length} tasks - View: ${viewMode} - Sorted by: ${sortBy}`;
                ganttDiv.parentNode.insertBefore(infoDiv, ganttDiv);
            }

        } else {
            ganttDiv.innerHTML = `
                <div style="color: #ef4444; padding: 40px; text-align: center;">
                    <h3>Gantt Library Not Loaded</h3>
                    <p>The Frappe Gantt library is not available. Please check if it's properly included in your HTML.</p>
                </div>
            `;
        }

    } catch (error) {
        console.error('Error creating Gantt chart:', error);
        ganttDiv.innerHTML = `
            <div style="color: #ef4444; padding: 40px; text-align: center;">
                <h3>Error Loading Gantt Chart</h3>
                <p>${error.message}</p>
                <button class="btn btn-primary" onclick="renderGanttChart()">Retry</button>
            </div>
        `;
    }
}

// Fix 4: Add initialization check for timeline

function initializeTimeline() {
    console.log('Starting timeline initialization...');

    timelineContainer = document.getElementById('timelineVisualization');
    if (!timelineContainer) {
        console.error('Timeline container not found');
        return;
    }

    if (typeof vis === 'undefined') {
        console.error('vis.js library not loaded');
        timelineContainer.innerHTML = `
            <div style="padding: 40px; text-align: center; color: #ef4444;">
                <h3>Timeline Library Missing</h3>
                <p>The vis.js library is not loaded. Please check your HTML includes.</p>
                <p>Add this to your HTML head: <code>&lt;script src="/static/js/vendor/vis-timeline-7.4.8.min.js"&gt;&lt;/script&gt;</code></p>
            </div>
        `;
        return;
    }

    // Clear any existing timeline
    if (timeline) {
        try {
            timeline.destroy();
        } catch (e) {
            console.warn('Error destroying existing timeline:', e);
        }
        timeline = null;
    }

    // Get current time scale setting
    const timeScaleSelect = document.getElementById('timelineScale');
    const timeScale = timeScaleSelect ? timeScaleSelect.value || '1day' : '1day';
    const timelineOptions = getTimelineOptions(timeScale);

    try {
        timeline = new vis.Timeline(timelineContainer, [], [], timelineOptions);
        console.log(`Timeline created successfully with ${timeScale} scale`);

        setupTimelineEventListeners();

        // Set initial focus date to now
        const focusDateInput = document.getElementById('timelineFocusDate');
        if (focusDateInput && !focusDateInput.value) {
            focusDateInput.value = new Date().toISOString().slice(0, 16);
        }

        renderTimeline();

    } catch (error) {
        console.error('Error creating timeline:', error);
        timelineContainer.innerHTML = `
            <div style="padding: 40px; text-align: center; color: #ef4444;">
                <h3>Timeline Creation Error</h3>
                <p>${error.message}</p>
                <button class="btn btn-primary" onclick="location.reload()">Refresh Page</button>
            </div>
        `;
    }
}

// Setup enhanced team filter with role aggregation
function setupGanttTeamFilter() {
    const select = document.getElementById('ganttTeamSelect');
    if (!select) return;

    select.innerHTML = '';

    // Add aggregation options first
    select.innerHTML = `
        <option value="all">All Teams</option>
        <option value="all-mechanics">All Mechanic Teams</option>
        <option value="all-quality">All Quality Teams</option>
        <option value="all-customer">All Customer Teams</option>
        <option value="all-vendor">All Vendor Teams</option>
    `;

    if (scenarioData.tasks) {
        // Get unique teams and organize by type
        const mechanicTeams = new Set();
        const qualityTeams = new Set();
        const customerTeams = new Set();
        const vendorTeamsSet = new Set();
        const otherTeams = new Set();

        scenarioData.tasks.forEach(task => {
            const team = task.team;
            if (team) {
                if (team.toLowerCase().includes('customer')) {
                    customerTeams.add(team);
                } else if (team.toLowerCase().includes('quality')) {
                    qualityTeams.add(team);
                } else if (team.toUpperCase().includes('VENDOR')) {
                    vendorTeamsSet.add(team);
                } else if (team.toLowerCase().includes('mechanic')) {
                    mechanicTeams.add(team);
                } else {
                    otherTeams.add(team);
                }
            }
        });

        // Add team groups
        if (mechanicTeams.size > 0) {
            const optgroup = document.createElement('optgroup');
            optgroup.label = 'Mechanic Teams';
            Array.from(mechanicTeams).sort().forEach(team => {
                const option = document.createElement('option');
                option.value = team;
                option.textContent = team;
                optgroup.appendChild(option);
            });
            select.appendChild(optgroup);
        }

        if (qualityTeams.size > 0) {
            const optgroup = document.createElement('optgroup');
            optgroup.label = 'Quality Teams';
            Array.from(qualityTeams).sort().forEach(team => {
                const option = document.createElement('option');
                option.value = team;
                option.textContent = team;
                optgroup.appendChild(option);
            });
            select.appendChild(optgroup);
        }

        if (customerTeams.size > 0) {
            const optgroup = document.createElement('optgroup');
            optgroup.label = 'Customer Teams';
            Array.from(customerTeams).sort().forEach(team => {
                const option = document.createElement('option');
                option.value = team;
                option.textContent = team;
                optgroup.appendChild(option);
            });
            select.appendChild(optgroup);
        }

        if (vendorTeamsSet.size > 0) {
            const optgroup = document.createElement('optgroup');
            optgroup.label = 'Vendor Teams';
            Array.from(vendorTeamsSet).sort().forEach(team => {
                const option = document.createElement('option');
                option.value = team;
                option.textContent = team;
                optgroup.appendChild(option);
            });
            select.appendChild(optgroup);
        }

        if (otherTeams.size > 0) {
            const optgroup = document.createElement('optgroup');
            optgroup.label = 'Other Teams';
            Array.from(otherTeams).sort().forEach(team => {
                const option = document.createElement('option');
                option.value = team;
                option.textContent = team;
                optgroup.appendChild(option);
            });
            select.appendChild(optgroup);
        }
    }

    select.onchange = renderGanttChart;
}

// Loading and error states
function showLoading(message = 'Loading...') {
    const content = document.querySelector('.main-content');
    if (content) {
        const loadingDiv = document.createElement('div');
        loadingDiv.id = 'loadingIndicator';
        loadingDiv.className = 'loading';
        loadingDiv.innerHTML = `
            <div style="text-align: center;">
                <div class="spinner"></div>
                <div style="margin-top: 20px;">${message}</div>
            </div>
        `;
        content.appendChild(loadingDiv);
    }
}

function hideLoading() {
    const loadingDiv = document.getElementById('loadingIndicator');
    if (loadingDiv) {
        loadingDiv.remove();
    }
}

function showError(message) {
    const content = document.querySelector('.main-content');
    if (content) {
        content.innerHTML = `
            <div style="text-align: center; padding: 40px; color: #ef4444;">
                <h2>Error</h2>
                <p>${message}</p>
                <button onclick="location.reload()" class="btn btn-primary" style="margin-top: 20px;">
                    Reload Page
                </button>
            </div>
        `;
    }
}

function formatDateTime(date) {
    return date.toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        hour12: true
    });
}

// --- Core Task Assignment Logic ---

/**
 * Generates task assignments for a given set of tasks and workers.
 * This is the core logic, free of UI interactions.
 * @param {Array} tasksToAssign - The tasks to be assigned.
 * @param {Object} workerPool - An object of all available workers for the scenario.
 * @returns {Object} An object containing the new mechanicSchedules and assignment stats.
 */
function generateAssignments(tasksToAssign, workerPool) {
    // The optimizer already solved resource contention and minimised global
    // lateness.  Each task carries a mechanic_id that identifies which worker
    // it was assigned to.  We simply map those IDs to dashboard worker IDs
    // instead of re-solving the assignment problem.

    const newSchedules = {};
    let successCount = 0;

    // Build a mapping: for each teamSkill, collect distinct mechanic_ids
    // and map them to sequential worker numbers (1-indexed).
    const teamMechanicIds = {};   // teamSkill -> sorted array of unique mechanic_ids
    tasksToAssign.forEach(task => {
        const ts = task.teamSkill || task.team || '';
        const mid = task.mechanic_id;
        if (mid === undefined || mid === null) return;
        if (!teamMechanicIds[ts]) teamMechanicIds[ts] = new Set();
        teamMechanicIds[ts].add(mid);
    });
    // Convert sets to sorted arrays for stable numbering
    Object.keys(teamMechanicIds).forEach(ts => {
        teamMechanicIds[ts] = Array.from(teamMechanicIds[ts]).sort((a, b) => a - b);
    });

    // Assign each task to the worker the optimizer chose
    tasksToAssign.forEach(task => {
        const ts = task.teamSkill || task.team || '';
        const mid = task.mechanic_id;
        if (mid === undefined || mid === null) return;

        const ids = teamMechanicIds[ts] || [];
        const workerNum = ids.indexOf(mid) + 1;          // 1-indexed
        const workerId = `${ts}_${workerNum}`;

        // Look up or create the worker entry
        const worker = workerPool[workerId];
        if (!worker) {
            // Worker not in the filtered pool — shouldn't happen with correct
            // teamCapacities, but handle gracefully.
            return;
        }

        successCount++;

        if (!newSchedules[workerId]) {
            newSchedules[workerId] = {
                mechanicId: workerId,
                displayName: worker.displayName,
                team: worker.baseTeam,
                skill: worker.skill,
                tasks: []
            };
        }
        newSchedules[workerId].tasks.push(task);
    });

    // Sort each worker's tasks by start time
    Object.values(newSchedules).forEach(schedule => {
        schedule.tasks.sort((a, b) => new Date(a.startTime) - new Date(b.startTime));
    });

    return {
        schedules: newSchedules,
        successCount: successCount,
        delayedCount: 0,
        conflictCount: 0
    };
}


// Auto-assign function with capacity limits and persistent storage
// Auto-assign function with proper skill-based nomenclature
// Clear job search and reload tasks
async function clearJobSearch() {
    const jobSearchInput = document.getElementById('jobSearchInput');
    const clearSearchBtn = document.getElementById('clearSearchBtn');

    if (jobSearchInput) {
        jobSearchInput.value = '';
        jobSearchTerm = '';

        // Hide clear button and suggestions
        if (clearSearchBtn) {
            clearSearchBtn.style.display = 'none';
        }
        hideSearchSuggestions();

        console.log('🔍 Search cleared - reloading all tasks');

        // Reload tasks without search filter
        if (currentScenario === '3stage') {
            await reload3StageDataWithFilters();
        } else {
            updateTeamLeadView();
        }
    }
}

/**
 * Show search suggestions dropdown
 */
function showSearchSuggestions(searchTerm) {
    const searchSuggestions = document.getElementById('searchSuggestions');
    if (!searchSuggestions || !scenarioData.tasks) return;

    // Reset selection index for new search
    selectedSuggestionIndex = -1;

    const lowerSearch = searchTerm.toLowerCase();

    // Find matching tasks (limit to 10 suggestions for performance)
    const matches = [];
    const seenIds = new Set();

    for (const task of scenarioData.tasks) {
        if (matches.length >= 10) break;

        const taskId = task.taskId || '';
        const soi = task.soi || '';
        const type = task.type || '';

        // Skip duplicates
        if (seenIds.has(taskId)) continue;

        // Check if task matches search
        if (taskId.toLowerCase().includes(lowerSearch) ||
            soi.toLowerCase().includes(lowerSearch) ||
            type.toLowerCase().includes(lowerSearch)) {

            matches.push({
                taskId: taskId,
                soi: soi,
                type: type,
                team: task.team || '',
                lineNumber: task.line_number || task.product || ''
            });
            seenIds.add(taskId);
        }
    }

    if (matches.length === 0) {
        hideSearchSuggestions();
        return;
    }

    // Build suggestions HTML
    const suggestionsHTML = matches.map((match, index) => {
        const highlightedId = highlightSearchTerm(match.taskId, searchTerm);
        const highlightedSOI = match.soi ? highlightSearchTerm(match.soi, searchTerm) : '';
        const highlightedType = highlightSearchTerm(match.type, searchTerm);

        return `
            <div class="suggestion-item" data-index="${index}" data-task-id="${match.taskId}"
                 style="padding: 8px 12px; cursor: pointer; border-bottom: 1px solid #e5e7eb;">
                <div style="font-weight: 600; font-size: 13px;">${highlightedId}</div>
                <div style="font-size: 11px; color: #6b7280; margin-top: 2px;">
                    ${highlightedType} • ${match.team} • Line ${match.lineNumber}
                    ${highlightedSOI ? `<br>SOI: ${highlightedSOI}` : ''}
                </div>
            </div>
        `;
    }).join('');

    searchSuggestions.innerHTML = suggestionsHTML;
    searchSuggestions.style.display = 'block';

    // Add click handlers to suggestions
    searchSuggestions.querySelectorAll('.suggestion-item').forEach(item => {
        item.addEventListener('mouseenter', function() {
            // Highlight on hover
            searchSuggestions.querySelectorAll('.suggestion-item').forEach(s => {
                s.style.backgroundColor = '';
            });
            this.style.backgroundColor = '#f3f4f6';
        });

        item.addEventListener('mouseleave', function() {
            this.style.backgroundColor = '';
        });

        item.addEventListener('click', function() {
            const taskId = this.getAttribute('data-task-id');
            const jobSearchInput = document.getElementById('jobSearchInput');

            if (jobSearchInput) {
                jobSearchInput.value = taskId;
                jobSearchTerm = taskId;
                hideSearchSuggestions();

                // Trigger search
                console.log(`🔍 Suggestion selected: "${taskId}"`);
                if (currentScenario === '3stage') {
                    reload3StageDataWithFilters();
                } else {
                    updateTeamLeadView();
                }
            }
        });
    });
}

/**
 * Hide search suggestions dropdown
 */
function hideSearchSuggestions() {
    const searchSuggestions = document.getElementById('searchSuggestions');
    if (searchSuggestions) {
        searchSuggestions.style.display = 'none';
        searchSuggestions.innerHTML = '';
    }
}

/**
 * Update selected suggestion highlight (for keyboard navigation)
 */
function updateSuggestionSelection(suggestions) {
    suggestions.forEach((item, index) => {
        if (index === selectedSuggestionIndex) {
            item.style.backgroundColor = '#3b82f6';
            item.style.color = 'white';
            item.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        } else {
            item.style.backgroundColor = '';
            item.style.color = '';
        }
    });
}

async function autoAssign() {
    // 1. Ensure ALL tasks are loaded before assigning (not just the visible page)
    if (pagination3stage.has_more) {
        const loadAll = confirm(
            `Only ${scenarioData.tasks.length} of ${pagination3stage.filtered_total || pagination3stage.total} tasks are loaded. ` +
            `Auto-assign needs all tasks. Load remaining tasks now?`
        );
        if (loadAll) {
            await loadAllTasks();
        } else {
            return;
        }
    }
    const tasksToAssign = scenarioData.tasks;

    // 2. Build the pool of available workers based on current UI filters
    const workerPool = {};
    const teamsToInclude = getFilteredTeams(); // Using a new helper for clarity

    teamsToInclude.forEach(teamSkill => {
        const capacity = (scenarioData.teamCapacities && scenarioData.teamCapacities[teamSkill]) || 0;
        const parsed = parseTeamSkill(teamSkill);
        const baseTeam = parsed.baseTeam;
        const skill = parsed.skill;
        const shift = parsed.shift;

        for (let i = 1; i <= capacity; i++) {
            const workerId = `${teamSkill}_${i}`;
            const isCustomer = isCustomerTeam(baseTeam);
            const isQuality = isQualityTeam(baseTeam);
            const isVendor = isVendorTeam(baseTeam);
            const displayName = buildResourceLabel(baseTeam, i, shift, skill);
            workerPool[workerId] = { id: workerId, baseTeam, skill, shift, displayName, isQuality, isCustomer, isVendor };
        }
    });

    // 3. Call the core assignment logic
    const assignmentResult = generateAssignments(tasksToAssign, workerPool);

    // 4. Update UI and global state based on results
    // This part remains in `autoAssign` because it's UI-specific
    if (!savedAssignments[currentScenario]) savedAssignments[currentScenario] = {};
    // DEPRECATED: savedAssignments[currentScenario].mechanicSchedules = assignmentResult.schedules;

    // Convert the mechanic-keyed schedule back into a task-keyed assignment object
    const newAssignmentsByTask = {};
    Object.entries(assignmentResult.schedules).forEach(([mechanicId, schedule]) => {
        schedule.tasks.forEach(task => {
            if (!newAssignmentsByTask[task.taskId]) {
                const originalTask = scenarioData.tasks.find(t => t.taskId === task.taskId);
                if (!originalTask) {
                    console.warn(`autoAssign: Task ${task.taskId} not found in scenarioData, skipping`);
                    return;
                }
                newAssignmentsByTask[task.taskId] = {
                    mechanics: [],
                    team: originalTask.team,
                    mechanicsNeeded: originalTask.mechanics || 1
                };
            }
            newAssignmentsByTask[task.taskId].mechanics.push(mechanicId);
        });
    });

    // Merge these new assignments into the main savedAssignments object, which is the source of truth
    Object.assign(savedAssignments[currentScenario], newAssignmentsByTask);


    // Update dropdowns and provide visual feedback
    updateAssignmentsInUI(assignmentResult.schedules);

    // Update summary stats
    if (typeof updateAssignmentSummary === 'function') {
        updateAssignmentSummary();
    }

    // 5. Show alert with results
    const totalWorkers = Object.keys(workerPool).length;
    const unassigned = tasksToAssign.length - assignmentResult.successCount;
    alert(`Auto-Assignment Complete!\n\n` +
          `Assigned: ${assignmentResult.successCount} / ${tasksToAssign.length}\n` +
          (unassigned > 0 ? `Unassigned: ${unassigned}\n` : '') +
          `\nAvailable Workforce: ${totalWorkers}\n\n` +
          `Assignments have been saved.`);

    // console.log('Auto-assigned schedules:', savedAssignments[currentScenario].mechanicSchedules);
}

// Helper to get filtered teams based on UI dropdowns
function getFilteredTeams() {
    let teamsToInclude = new Set();

    // Handle multi-select teams
    selectedTeams.forEach(selectedTeam => {
        if (selectedTeam === 'all') {
            Object.keys(scenarioData.teamCapacities || {}).forEach(t => teamsToInclude.add(t));
        } else if (selectedTeam === 'all-mechanics') {
            Object.keys(scenarioData.teamCapacities || {})
                .filter(t => { const bt = parseTeamSkill(t).baseTeam; return !isCustomerTeam(bt) && !isQualityTeam(bt) && !isVendorTeam(bt); })
                .forEach(t => teamsToInclude.add(t));
        } else if (selectedTeam === 'all-quality') {
            Object.keys(scenarioData.teamCapacities || {})
                .filter(t => isQualityTeam(parseTeamSkill(t).baseTeam))
                .forEach(t => teamsToInclude.add(t));
        } else {
            Object.keys(scenarioData.teamCapacities || {})
                .filter(t => t.startsWith(selectedTeam))
                .forEach(t => teamsToInclude.add(t));
        }
    });

    // Convert Set back to Array
    teamsToInclude = Array.from(teamsToInclude);

    if (selectedSkill !== 'all') {
        teamsToInclude = teamsToInclude.filter(teamSkill => {
            const skillMatch = teamSkill.match(/\((.+?)\)/);
            return skillMatch && skillMatch[1] === selectedSkill;
        });
    }
    return teamsToInclude;
}

// Helper to update the UI after auto-assignment
function updateAssignmentsInUI(schedules) {
    // First, clear all existing assignments from the UI
    document.querySelectorAll('.assign-select').forEach(select => {
        select.value = '';
        select.classList.remove('has-saved-assignment');
        select.style.backgroundColor = '';
    });

    // Create a map of task assignments to handle multi-worker tasks correctly
    const taskToWorkerMap = {};
    Object.entries(schedules).forEach(([workerId, schedule]) => {
        schedule.tasks.forEach(task => {
            if (!taskToWorkerMap[task.taskId]) {
                taskToWorkerMap[task.taskId] = [];
            }
            taskToWorkerMap[task.taskId].push(workerId);
        });
    });

    // Now, populate the UI with the new assignments
    Object.entries(taskToWorkerMap).forEach(([taskId, workerIds]) => {
        const taskRow = document.querySelector(`#taskTableBody tr[data-task-id="${taskId}"]`);
        if (taskRow) {
            const selectElements = taskRow.querySelectorAll('.assign-select');
            workerIds.forEach((workerId, index) => {
                if (selectElements[index]) {
                    selectElements[index].value = workerId;
                    selectElements[index].style.backgroundColor = '#d4edda';
                    setTimeout(() => {
                        selectElements[index].style.backgroundColor = '';
                        selectElements[index].classList.add('has-saved-assignment');
                    }, 2000);
                }
            });
        }
    });
}

// Load saved assignments into the table
function loadSavedAssignments() {
    console.log(`[loadSavedAssignments] Fired. Attempting to load from:`, JSON.parse(JSON.stringify(savedAssignments[currentScenario] || {})));
    if (!savedAssignments[currentScenario]) return;

    const assignments = savedAssignments[currentScenario];
    const taskRows = document.querySelectorAll('#taskTableBody tr');
    let loadedCount = 0;

    taskRows.forEach(row => {
        const taskId = row.dataset.taskId;
        if (!taskId || !assignments[taskId]) return;

        const taskAssignment = assignments[taskId];
        const selectElements = row.querySelectorAll('.assign-select');

        // Restore assignments to dropdowns
        taskAssignment.mechanics.forEach((mechId, index) => {
            if (selectElements[index]) {
                // Check if this mechanic option exists in the dropdown
                const optionExists = Array.from(selectElements[index].options)
                    .some(opt => opt.value === mechId);

                if (optionExists) {
                    selectElements[index].value = mechId;
                    selectElements[index].classList.add('has-saved-assignment');
                    loadedCount++;
                }
            }
        });
    });

    // Update summary
    if (typeof updateAssignmentSummary === 'function') {
        updateAssignmentSummary();
    }

    if (loadedCount > 0) {
        console.log(`Loaded ${loadedCount} saved assignments for ${currentScenario}`);
    }
    // updateMechanicSchedulesFromAssignments(); // No longer needed
}

// Save assignments to localStorage for persistence across sessions
function saveAssignmentsToStorage() {
    try {
        localStorage.setItem(`assignments_${currentScenario}`, JSON.stringify(savedAssignments[currentScenario]));
        alert('Assignments saved successfully!');
    } catch (e) {
        console.error('Failed to save assignments:', e);
        alert('Failed to save assignments to browser storage.');
    }
}

// Load assignments from localStorage
function loadAssignmentsFromStorage(silent = false) {
    try {
        const stored = localStorage.getItem(`assignments_${currentScenario}`);
        if (stored) {
            savedAssignments[currentScenario] = JSON.parse(stored);
            if (!silent) {
                alert('Previous assignments loaded successfully!');
            }
            console.log(`Successfully loaded ${Object.keys(savedAssignments[currentScenario]).length} assignment entries from localStorage for ${currentScenario}.`);
            // Refresh the view to apply loaded assignments to the UI
            updateView();
        } else {
            if (!silent) {
                alert('No saved assignments found for this scenario.');
            }
        }
    } catch (e) {
        console.error('Failed to load assignments:', e);
        if (!silent) {
            alert('Failed to load assignments from browser storage.');
        }
    }
}

// Clear all saved assignments
function clearSavedAssignments() {
    if (confirm('This will clear all saved assignments for this scenario. Continue?')) {
        savedAssignments[currentScenario] = {};
        localStorage.removeItem(`assignments_${currentScenario}`);

        // Clear all dropdowns
        document.querySelectorAll('.assign-select').forEach(select => {
            select.value = '';
            select.classList.remove('has-saved-assignment');
        });

        alert('Saved assignments cleared.');

        if (typeof updateAssignmentSummary === 'function') {
            updateAssignmentSummary();
        }
    }
}

// Export tasks function
async function exportTasks() {
    try {
        // Export to CSV including assignments
        const tasks = scenarioData.tasks || [];
        const assignments = savedAssignments[currentScenario] || {};

        // Build CSV data
        let csvContent = "Task ID,Type,Product,Team,Start Time,End Time,Duration,Mechanics Needed,Assigned Mechanics\n";

        tasks.forEach(task => {
            const assignment = assignments[task.taskId];
            const assignedMechanics = assignment ? assignment.mechanics.join('; ') : 'Unassigned';

            csvContent += `"${task.taskId}","${task.type}","${task.product}","${task.team}",`;
            csvContent += `"${task.startTime}","${task.endTime}","${task.duration}","${task.mechanics || 1}",`;
            csvContent += `"${assignedMechanics}"\n`;
        });

        // Download the CSV
        const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
        const link = document.createElement('a');
        const url = URL.createObjectURL(blob);
        link.setAttribute('href', url);
        link.setAttribute('download', `assignments_${currentScenario}_${new Date().toISOString().slice(0, 10)}.csv`);
        link.style.visibility = 'hidden';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);

        if (typeof showNotification === 'function') {
            showNotification('Assignments exported successfully!', 'success');
        } else {
            alert('Assignments exported successfully!');
        }
    } catch (error) {
        console.error('Export failed:', error);
        // Fallback to server export
        window.location.href = `/api/export/${currentScenario}`;
    }
}

// (Old updateSupplyChainView removed - replaced by API-based version below)

// Collect late parts data from all scenarios
function collectLatePartsData() {
    latePartsData = {
        baseline: [],
        scenario1: [],
        scenario2: [],
        scenario3: []
    };

    // Process each scenario
    Object.keys(allScenarios).forEach(scenarioId => {
        const scenario = allScenarios[scenarioId];
        if (scenario && scenario.tasks) {
            console.log(`Processing ${scenarioId}: ${scenario.tasks.length} tasks`);

            const lateParts = scenario.tasks.filter(task => {
                // More comprehensive late part detection
                return task.isLatePartTask === true ||
                       task.type === 'Late Part' ||
                       (task.taskId && (
                           task.taskId.includes('LP_') ||
                           task.taskId.includes('Late') ||
                           task.taskId.startsWith('LP') ||
                           task.taskId.includes('_LP_')
                       )) ||
                       // Also check task description/name if available
                       (task.name && task.name.toLowerCase().includes('late part'));
            });

            console.log(`Found ${lateParts.length} late parts in ${scenarioId}`);

            // Log first few for debugging
            if (lateParts.length > 0) {
                console.log('Sample late parts:', lateParts.slice(0, 3).map(lp => ({
                    taskId: lp.taskId,
                    type: lp.type,
                    product: lp.product
                })));
            }

            latePartsData[scenarioId] = lateParts.map(task => ({
                ...task,
                scenario: scenarioId,
                startDate: new Date(task.startTime),
                endDate: new Date(task.endTime),
                dayOfSchedule: Math.floor((new Date(task.startTime) - getScheduleStartDate(scenario)) / (1000 * 60 * 60 * 24))
            }));
        }
    });

    console.log('Late parts collected:', Object.keys(latePartsData).map(s => `${s}: ${latePartsData[s].length}`));
}

// Calculate supply chain metrics vis scenario comparisons
function calculateSupplyChainMetrics() {
    supplyChainMetrics = {
        totalLateParts: 0,
        affectedProducts: new Set(),
        criticalLateParts: 0,
        avgDelayImpact: 0,
        relativeDelayImpact: 0,
        referenceScenario: currentScenario,  // Use main dropdown scenario as reference
        comparisonScenario: getSelectedComparisonScenario(),
        byProduct: {},
        byScenario: {}
    };

    // Process each scenario's late parts
    Object.entries(latePartsData).forEach(([scenarioId, lateParts]) => {
        supplyChainMetrics.byScenario[scenarioId] = {
            count: lateParts.length,
            products: new Set(),
            critical: 0,
            earliestDay: 999,
            latestDay: 0
        };

        lateParts.forEach(part => {
            // Update global metrics
            supplyChainMetrics.totalLateParts++;
            supplyChainMetrics.affectedProducts.add(part.product);

            if (part.isCritical || part.slackHours < 24) {
                supplyChainMetrics.criticalLateParts++;
                supplyChainMetrics.byScenario[scenarioId].critical++;
            }

            // Update product metrics
            if (!supplyChainMetrics.byProduct[part.product]) {
                supplyChainMetrics.byProduct[part.product] = {
                    totalParts: 0,
                    criticalParts: 0,
                    scenarios: {}
                };
            }

            supplyChainMetrics.byProduct[part.product].totalParts++;
            if (part.isCritical) {
                supplyChainMetrics.byProduct[part.product].criticalParts++;
            }

            // Track per-scenario product data
            if (!supplyChainMetrics.byProduct[part.product].scenarios[scenarioId]) {
                supplyChainMetrics.byProduct[part.product].scenarios[scenarioId] = {
                    count: 0,
                    scheduleDays: []
                };
            }

            supplyChainMetrics.byProduct[part.product].scenarios[scenarioId].count++;
            supplyChainMetrics.byProduct[part.product].scenarios[scenarioId].scheduleDays.push(part.dayOfSchedule);

            // Update scenario metrics
            supplyChainMetrics.byScenario[scenarioId].products.add(part.product);
            supplyChainMetrics.byScenario[scenarioId].earliestDay = Math.min(
                supplyChainMetrics.byScenario[scenarioId].earliestDay,
                part.dayOfSchedule
            );
            supplyChainMetrics.byScenario[scenarioId].latestDay = Math.max(
                supplyChainMetrics.byScenario[scenarioId].latestDay,
                part.dayOfSchedule
            );
        });
    });

    // Calculate relative delay impact: comparison_scenario - reference_scenario
    const referenceScenario = allScenarios[supplyChainMetrics.referenceScenario];
    const comparisonScenario = allScenarios[supplyChainMetrics.comparisonScenario];
    const relativeImpacts = [];

    if (referenceScenario && comparisonScenario &&
        referenceScenario.products && comparisonScenario.products &&
        supplyChainMetrics.referenceScenario !== supplyChainMetrics.comparisonScenario) {

        // Create reference product map
        const referenceProducts = {};
        referenceScenario.products.forEach(product => {
            referenceProducts[product.name] = product.latenessDays;
        });

        // Calculate relative impacts for each product
        comparisonScenario.products.forEach(product => {
            const referenceLateness = referenceProducts[product.name] || 0;
            const comparisonLateness = product.latenessDays;
            const relativeDifference = comparisonLateness - referenceLateness;
            relativeImpacts.push(relativeDifference);
        });
    }

    supplyChainMetrics.relativeDelayImpact = relativeImpacts.length > 0 ?
        (relativeImpacts.reduce((a, b) => a + b, 0) / relativeImpacts.length).toFixed(1) : 0;

    // Also keep absolute calculation for fallback
    const absoluteImpacts = [];
    Object.values(allScenarios).forEach(scenario => {
        if (scenario.products) {
            scenario.products.forEach(product => {
                if (product.latenessDays > 0) {
                    absoluteImpacts.push(product.latenessDays);
                }
            });
        }
    });

    supplyChainMetrics.avgDelayImpact = absoluteImpacts.length > 0 ?
        (absoluteImpacts.reduce((a, b) => a + b, 0) / absoluteImpacts.length).toFixed(1) : 0;
}

// Initialize feedback system when scenario changes
function onScenarioChange(newScenario) {
    currentScenario = newScenario;
    initializeFeedbackSystem();
    loadSavedFeedback();
}

// Update metric cards
function updateSupplyChainMetrics() {
    // Get the current selected scenario's late parts count
    const currentScenarioLateParts = latePartsData[currentScenario] || [];

    // Count unique late parts across all scenarios (for comparison)
    const allLateParts = new Set();
    const allProducts = new Set();
    let totalCritical = 0;

    Object.values(latePartsData).forEach(parts => {
        parts.forEach(part => {
            // Use full task ID for uniqueness, not split
            allLateParts.add(part.taskId);
            allProducts.add(part.product);

            if (part.isCritical || part.slackHours < 24) {
                totalCritical++;
            }
        });
    });

    // Update metrics to show current scenario's data
    document.getElementById('totalLateParts').textContent = currentScenarioLateParts.length;
    document.getElementById('affectedProducts').textContent =
        new Set(currentScenarioLateParts.map(p => p.product)).size;
    document.getElementById('criticalLateParts').textContent =
        currentScenarioLateParts.filter(p => p.isCritical || p.slackHours < 24).length;

    // Calculate average delay impact for current scenario
    // FIX: Don't redeclare currentScenario - use currentScenarioData instead
    const currentScenarioData = allScenarios[currentScenario];
    if (currentScenarioData && currentScenarioData.products) {
        const delays = currentScenarioData.products
            .filter(p => p.latenessDays > 0)
            .map(p => p.latenessDays);

        const avgDelay = delays.length > 0 ?
            (delays.reduce((a, b) => a + b, 0) / delays.length).toFixed(1) : 0;

        document.getElementById('avgDelayImpact').textContent = avgDelay;
    } else {
        document.getElementById('avgDelayImpact').textContent = '0';
    }

    console.log(`Metrics updated - Late parts: ${currentScenarioLateParts.length}, Products affected: ${new Set(currentScenarioLateParts.map(p => p.product)).size}`);
}

// Update late parts timeline
function updateLatePartsTimeline() {
    const timeline = document.getElementById('latePartsTimeline');
    const selectedScenarios = getSelectedScenarios();
    const selectedProduct = document.getElementById('supplyChainProductFilter')?.value || 'all';

    let html = '';

    selectedScenarios.forEach(scenarioId => {
        let parts = latePartsData[scenarioId] || [];

        // Filter by product if selected
        if (selectedProduct !== 'all') {
            parts = parts.filter(p => p.product === selectedProduct);
        }

        // Sort by start time
        parts.sort((a, b) => a.startDate - b.startDate);

        const scenarioColor = getScenarioColor(scenarioId);

        html += `
            <div class="timeline-scenario ${scenarioId}">
                <h4>${scenarioId.toUpperCase()}: ${parts.length} Late Parts</h4>
                <div class="timeline-items">
        `;

        // Show first 10 late parts
        parts.slice(0, 10).forEach(part => {
            const critical = part.isCritical ? 'critical' : '';
            html += `
                <div class="timeline-late-part ${critical}">
                    <div style="min-width: 80px; font-weight: 600; color: ${scenarioColor};">
                        Day ${part.dayOfSchedule}
                    </div>
                    <div style="flex: 1;">
                        <strong>${part.taskId}</strong> - ${part.product}
                        ${part.team ? `(${part.team})` : ''}
                    </div>
                    <div style="min-width: 100px; text-align: right; font-size: 12px; color: #6b7280;">
                        ${formatDateTime(part.startDate)}
                    </div>
                    ${critical ? '<span style="color: #ef4444; font-size: 11px;">CRITICAL</span>' : ''}
                </div>
            `;
        });

        if (parts.length > 10) {
            html += `<div style="padding: 8px; color: #6b7280; font-size: 12px;">... and ${parts.length - 10} more</div>`;
        }

        html += `
                </div>
            </div>
        `;
    });

    timeline.innerHTML = html || '<div class="supply-chain-loading">No late parts data available</div>';
}

// Update impact table
function updateLatePartsImpactTable() {
    const tbody = document.getElementById('latePartsTableBody');
    const selectedProduct = document.getElementById('supplyChainProductFilter')?.value || 'all';

    // Group late parts by base task ID
    const latePartGroups = {};

    Object.entries(latePartsData).forEach(([scenarioId, parts]) => {
        parts.forEach(part => {
            const baseTaskId = part.taskId.split('_')[0]; // Remove instance suffix

            if (!latePartGroups[baseTaskId]) {
                latePartGroups[baseTaskId] = {
                    taskId: baseTaskId,
                    product: part.product,
                    type: part.type,
                    team: part.team,
                    scenarios: {}
                };
            }

            latePartGroups[baseTaskId].scenarios[scenarioId] = {
                startTime: part.startTime,
                endTime: part.endTime,
                dayOfSchedule: part.dayOfSchedule,
                isCritical: part.isCritical,
                slackHours: part.slackHours
            };
        });
    });

    // Filter by product
    let filteredGroups = Object.values(latePartGroups);
    if (selectedProduct !== 'all') {
        filteredGroups = filteredGroups.filter(g => g.product === selectedProduct);
    }

    // Build table rows
    let html = '';
    filteredGroups.slice(0, 50).forEach(group => {
        const criticalInAny = Object.values(group.scenarios).some(s => s.isCritical);
        const rowClass = criticalInAny ? 'highlight-late-part' : '';

        html += `<tr class="${rowClass}">`;
        html += `<td><strong>${group.taskId}</strong></td>`;
        html += `<td>${group.product}</td>`;
        html += `<td><span class="task-type late-part">${group.type || 'Late Part'}</span></td>`;
        html += `<td>${group.team || '-'}</td>`;

        // Add schedule cells for each scenario
        ['baseline', 'scenario1', 'scenario2', 'scenario3'].forEach(scenarioId => {
            if (group.scenarios[scenarioId]) {
                const sched = group.scenarios[scenarioId];
                const schedClass = sched.dayOfSchedule <= 5 ? 'schedule-early' :
                                  sched.dayOfSchedule <= 10 ? 'schedule-ontime' : 'schedule-late';
                html += `
                    <td class="schedule-cell ${schedClass}">
                        Day ${sched.dayOfSchedule}<br>
                        <small>${new Date(sched.startTime).toLocaleDateString()}</small>
                    </td>
                `;
            } else {
                html += `<td class="schedule-cell">-</td>`;
            }
        });

        // Critical path indicator
        html += `<td style="text-align: center;">`;
        if (criticalInAny) {
            html += `<span style="color: #ef4444;">✓</span>`;
        } else {
            html += `-`;
        }
        html += `</td>`;

        // Downstream impact
        const impactedTasks = calculateDownstreamImpact(group.taskId);
        html += `<td>${impactedTasks} tasks</td>`;

        html += `</tr>`;
    });

    tbody.innerHTML = html || '<tr><td colspan="10" style="text-align: center; color: #6b7280;">No late parts to display</td></tr>';
}

// Update product impact grid
function updateProductImpactGrid() {
    const grid = document.getElementById('productImpactGrid');
    const selectedScenarios = getSelectedScenarios();

    let html = '';

    Object.entries(supplyChainMetrics.byProduct).forEach(([product, metrics]) => {
        const latePartCount = Math.floor(metrics.totalParts / 4); // Average across scenarios

        html += `
            <div class="product-impact-card">
                <div class="product-impact-header">
                    <div class="product-impact-name">${product}</div>
                    <div class="late-part-count">${latePartCount} late parts</div>
                </div>

                <div style="font-size: 12px; color: #6b7280; margin: 5px 0;">
                    Critical parts: ${metrics.criticalParts > 0 ? Math.floor(metrics.criticalParts / 4) : 0}
                </div>

                <div class="impact-scenarios">
        `;

        selectedScenarios.forEach(scenarioId => {
            const scenario = allScenarios[scenarioId];
            if (scenario && scenario.products) {
                const productData = scenario.products.find(p => p.name === product);
                if (productData) {
                    const impactClass = productData.latenessDays > 0 ? 'late' :
                                       productData.latenessDays < 0 ? 'early' : 'ontime';
                    const impactText = productData.latenessDays > 0 ? `+${productData.latenessDays}d late` :
                                      productData.latenessDays < 0 ? `${Math.abs(productData.latenessDays)}d early` :
                                      'On time';

                    html += `
                        <div class="impact-scenario-row">
                            <span class="scenario-label">${scenarioId}:</span>
                            <span class="impact-days ${impactClass}">${impactText}</span>
                        </div>
                    `;
                }
            }
        });

        html += `
                </div>
            </div>
        `;
    });

    grid.innerHTML = html || '<div style="color: #6b7280;">No product impact data available</div>';
}

// Update risk matrix
function updateRiskMatrix() {
    const matrix = document.getElementById('riskMatrix');

    // Categorize late parts by risk level
    const riskCategories = {
        high: [],
        medium: [],
        low: []
    };

    Object.values(latePartsData).forEach(parts => {
        parts.forEach(part => {
            if (part.isCritical && part.dayOfSchedule > 10) {
                riskCategories.high.push(part);
            } else if (part.isCritical || part.dayOfSchedule > 15) {
                riskCategories.medium.push(part);
            } else {
                riskCategories.low.push(part);
            }
        });
    });

    let html = `
        <div class="risk-label">Risk Level</div>
        <div class="risk-cell risk-low">
            <strong>LOW</strong>
            <div class="risk-items">${Math.floor(riskCategories.low.length / 4)} items</div>
            <div style="font-size: 10px; margin-top: 5px;">Non-critical, early schedule</div>
        </div>
        <div class="risk-cell risk-medium">
            <strong>MEDIUM</strong>
            <div class="risk-items">${Math.floor(riskCategories.medium.length / 4)} items</div>
            <div style="font-size: 10px; margin-top: 5px;">Critical OR late schedule</div>
        </div>
        <div class="risk-cell risk-high">
            <strong>HIGH</strong>
            <div class="risk-items">${Math.floor(riskCategories.high.length / 4)} items</div>
            <div style="font-size: 10px; margin-top: 5px;">Critical AND late schedule</div>
        </div>
    `;

    matrix.innerHTML = html;
}

function getSelectedComparisonScenario() {
    const checkedRadio = document.querySelector('input[name="scenario-compare"]:checked');
    return checkedRadio ? checkedRadio.value : 'baseline';
}

// Helper functions
function getSelectedScenarios() {
    const checkboxes = document.querySelectorAll('.scenario-compare:checked');
    return Array.from(checkboxes).map(cb => cb.value);
}

// Add this function to debug what tasks exist
function debugTaskData() {
    console.log('=== DEBUGGING TASK DATA ===');

    Object.entries(allScenarios).forEach(([scenarioId, scenario]) => {
        if (scenario && scenario.tasks) {
            console.log(`\n${scenarioId.toUpperCase()}: ${scenario.tasks.length} total tasks`);

            // Count different task types
            const taskTypes = {};
            const latePartCandidates = [];

            scenario.tasks.forEach(task => {
                // Count by type
                const type = task.type || 'Unknown';
                taskTypes[type] = (taskTypes[type] || 0) + 1;

                // Check for late part indicators
                const isLatePartCandidate =
                    task.isLatePartTask ||
                    task.type === 'Late Part' ||
                    task.taskId.includes('LP_') ||
                    task.taskId.includes('Late') ||
                    task.product === 'Product D'; // Debug Product D specifically

                if (isLatePartCandidate) {
                    latePartCandidates.push({
                        taskId: task.taskId,
                        type: task.type,
                        product: task.product,
                        isLatePartTask: task.isLatePartTask,
                        team: task.team
                    });
                }
            });

            console.log('Task types:', taskTypes);
            console.log(`Late part candidates: ${latePartCandidates.length}`);
            if (latePartCandidates.length > 0) {
                console.log('Sample late parts:', latePartCandidates.slice(0, 5));
            }
        }
    });
}

// Call this in browser console or add it to updateSupplyChainView
debugTaskData();

function getScenarioColor(scenarioId) {
    const colors = {
        baseline: '#10b981',
        scenario1: '#f59e0b',
        scenario2: '#8b5cf6',
        scenario3: '#ef4444'
    };
    return colors[scenarioId] || '#6b7280';
}

function getScheduleStartDate(scenario) {
    if (scenario.tasks && scenario.tasks.length > 0) {
        const dates = scenario.tasks.map(t => new Date(t.startTime));
        return new Date(Math.min(...dates));
    }
    return new Date();
}

function calculateDownstreamImpact(taskId) {
    // This would need to analyze task dependencies
    // For now, return a placeholder
    return Math.floor(Math.random() * 10) + 5;
}

// Setup supply chain filters
function setupSupplyChainFilters() {
    // Setup product filter
    const productFilter = document.getElementById('supplyChainProductFilter');
    if (productFilter && !productFilter.hasAttribute('data-initialized')) {
        productFilter.setAttribute('data-initialized', 'true');

        // Populate products
        const products = new Set();
        Object.values(latePartsData).forEach(parts => {
            parts.forEach(part => products.add(part.product));
        });

        productFilter.innerHTML = '<option value="all">All Products</option>';
        Array.from(products).sort().forEach(product => {
            const option = document.createElement('option');
            option.value = product;
            option.textContent = product;
            productFilter.appendChild(option);
        });

        productFilter.addEventListener('change', () => {
            updateLatePartsTimeline();
            updateLatePartsImpactTable();
        });
    }

    // Setup scenario checkboxes
    document.querySelectorAll('.scenario-compare').forEach(checkbox => {
        if (!checkbox.hasAttribute('data-listener-added')) {
            checkbox.setAttribute('data-listener-added', 'true');
            checkbox.addEventListener('change', () => {
                updateLatePartsTimeline();
                updateProductImpactGrid();
            });
        }
    });
}

// Export supply chain report
window.exportSupplyChainReport = function() {
    let csvContent = "Supply Chain Late Parts Report\n";
    csvContent += `Generated: ${new Date().toLocaleString()}\n\n`;

    // Summary section
    csvContent += "SUMMARY METRICS\n";
    csvContent += `Total Unique Late Parts,${document.getElementById('totalLateParts').textContent}\n`;
    csvContent += `Affected Products,${document.getElementById('affectedProducts').textContent}\n`;
    csvContent += `Critical Late Parts,${document.getElementById('criticalLateParts').textContent}\n`;
    csvContent += `Average Delay Impact,${document.getElementById('avgDelayImpact').textContent} days\n\n`;

    // Late parts details
    csvContent += "LATE PARTS SCHEDULE COMPARISON\n";
    csvContent += "Task ID,Product,Type,Team,Baseline Day,Scenario1 Day,Scenario2 Day,Scenario3 Day,Critical Path\n";

    const latePartGroups = {};
    Object.entries(latePartsData).forEach(([scenarioId, parts]) => {
        parts.forEach(part => {
            const baseTaskId = part.taskId.split('_')[0];
            if (!latePartGroups[baseTaskId]) {
                latePartGroups[baseTaskId] = {
                    taskId: baseTaskId,
                    product: part.product,
                    type: part.type || 'Late Part',
                    team: part.team || '-',
                    scenarios: {}
                };
            }
            latePartGroups[baseTaskId].scenarios[scenarioId] = {
                dayOfSchedule: part.dayOfSchedule,
                isCritical: part.isCritical
            };
        });
    });

    Object.values(latePartGroups).forEach(group => {
        const baselineDay = group.scenarios.baseline?.dayOfSchedule || '-';
        const scenario1Day = group.scenarios.scenario1?.dayOfSchedule || '-';
        const scenario2Day = group.scenarios.scenario2?.dayOfSchedule || '-';
        const scenario3Day = group.scenarios.scenario3?.dayOfSchedule || '-';
        const isCritical = Object.values(group.scenarios).some(s => s.isCritical) ? 'Yes' : 'No';

        csvContent += `"${group.taskId}","${group.product}","${group.type}","${group.team}",`;
        csvContent += `${baselineDay},${scenario1Day},${scenario2Day},${scenario3Day},${isCritical}\n`;
    });

    // Product impact section
    csvContent += "\nPRODUCT DELIVERY IMPACT\n";
    csvContent += "Product,Late Parts Count,Baseline Impact,Scenario1 Impact,Scenario2 Impact,Scenario3 Impact\n";

    Object.entries(supplyChainMetrics.byProduct).forEach(([product, metrics]) => {
        const latePartCount = Math.floor(metrics.totalParts / 4);
        let impacts = [];

        ['baseline', 'scenario1', 'scenario2', 'scenario3'].forEach(scenarioId => {
            const scenario = allScenarios[scenarioId];
            if (scenario && scenario.products) {
                const productData = scenario.products.find(p => p.name === product);
                if (productData) {
                    const impact = productData.latenessDays > 0 ? `+${productData.latenessDays}d` :
                                  productData.latenessDays < 0 ? `${productData.latenessDays}d` : 'On time';
                    impacts.push(impact);
                } else {
                    impacts.push('-');
                }
            } else {
                impacts.push('-');
            }
        });

        csvContent += `"${product}",${latePartCount},${impacts.join(',')}\n`;
    });

    // Download the CSV
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    const url = URL.createObjectURL(blob);
    link.setAttribute('href', url);
    link.setAttribute('download', `supply_chain_report_${new Date().toISOString().slice(0, 10)}.csv`);
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    showNotification('Supply chain report exported successfully!', 'success');
};


// Refresh Gantt chart function
function refreshGanttChart() {
    renderGanttChart();
    showNotification('Gantt chart refreshed', 'success');
}

// Export Gantt chart functionality
function exportGanttChart() {
    const productFilter = document.getElementById('ganttProductSelect').value || 'all';
    const teamFilter = document.getElementById('ganttTeamSelect').value || 'all';
    const sortBy = document.getElementById('ganttSortSelect').value || 'start';
    const viewMode = document.getElementById('ganttViewMode').value || 'Day';

    let tasks = getGanttTasks(productFilter, teamFilter);

    if (tasks.length === 0) {
        alert('No tasks to export for the selected filters.');
        return;
    }

    // Sort tasks (same logic as render)
    switch(sortBy) {
        case 'start':
            tasks.sort((a, b) => new Date(a.start) - new Date(b.start));
            break;
        case 'product':
            tasks.sort((a, b) => {
                if (a.product !== b.product) {
                    return a.product.localeCompare(b.product);
                }
                return new Date(a.start) - new Date(b.start);
            });
            break;
        case 'priority':
            tasks.sort((a, b) => {
                if (a.priority !== b.priority) {
                    return a.priority - b.priority;
                }
                return new Date(a.start) - new Date(b.start);
            });
            break;
        case 'team':
            tasks.sort((a, b) => {
                if (a.team !== b.team) {
                    return a.team.localeCompare(b.team);
                }
                return new Date(a.start) - new Date(b.start);
            });
            break;
        case 'duration':
            tasks.sort((a, b) => b.duration - a.duration);
            break;
    }

    // Create CSV content
    let csvContent = "Gantt Chart Export\n";
    csvContent += `Generated: ${new Date().toLocaleString()}\n`;
    csvContent += `Scenario: ${currentScenario}\n`;
    csvContent += `Filters: Product=${productFilter}, Team=${teamFilter}\n`;
    csvContent += `Sort: ${sortBy}, View: ${viewMode}\n\n`;

    csvContent += "Task ID,Task Name,Product,Team,Type,Priority,Start Date,End Date,Duration (Days),Dependencies\n";

    tasks.forEach(task => {
        const startDate = new Date(task.start).toLocaleDateString();
        const endDate = new Date(task.end).toLocaleDateString();
        const duration = Math.ceil((new Date(task.end) - new Date(task.start)) / (1000 * 60 * 60 * 24));
        const dependencies = task.dependencies || '';

        csvContent += `"${task.id}","${task.name}","${task.product}","${task.team}","${task.type}","${task.priority}","${startDate}","${endDate}","${duration}","${dependencies}"\n`;
    });

    // Download the CSV
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    const url = URL.createObjectURL(blob);
    link.setAttribute('href', url);
    link.setAttribute('download', `gantt_chart_${currentScenario}_${new Date().toISOString().slice(0, 10)}.csv`);
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    showNotification('Gantt chart exported successfully!', 'success');
}

// Refresh data
async function refreshData() {
    if (confirm('This will recalculate all scenarios. It may take a few minutes. Continue?')) {
        showLoading('Refreshing all scenarios...');
        try {
            const response = await fetch('/api/refresh', { method: 'POST' });
            const result = await response.json();

            if (result.success) {
                await loadAllScenarios();
                alert('All scenarios refreshed successfully!');
            } else {
                alert('Failed to refresh: ' + result.error);
            }
        } catch (error) {
            alert('Error refreshing data: ' + error.message);
        } finally {
            hideLoading();
        }
    }
}

// Clear all assignments (for current view only, doesn't clear saved)
function clearAllAssignments() {
    if (!confirm('This will clear all current assignments in the view. Continue?')) return;

    // Reset all dropdowns
    document.querySelectorAll('.assign-select').forEach(select => {
        select.value = '';
        select.classList.remove('assigned', 'conflict', 'partial', 'has-saved-assignment');
    });

    // Update summary
    if (typeof updateAssignmentSummary === 'function') {
        updateAssignmentSummary();
    }

    // Visual feedback
    if (typeof showNotification === 'function') {
        showNotification('Current view assignments cleared', 'info');
    } else {
        alert('Current view assignments cleared');
    }
}

// Add refresh button to header if not exists
function setupRefreshButton() {
    const controls = document.querySelector('.controls');
    if (controls && !document.getElementById('refreshBtn')) {
        const refreshBtn = document.createElement('button');
        refreshBtn.id = 'refreshBtn';
        refreshBtn.className = 'btn btn-secondary';
        refreshBtn.innerHTML = '🔄 Refresh Data';
        refreshBtn.onclick = refreshData;
        refreshBtn.style.marginLeft = '10px';
        controls.appendChild(refreshBtn);
    }
}

// View assignment report
function viewAssignmentReport() {
    if (typeof currentScenario !== 'undefined') {
        window.open(`/api/assignment_report/${currentScenario}`, '_blank');
    }
}

// Update assignment summary panel
function updateAssignmentSummary() {
    const rows = document.querySelectorAll('#taskTableBody tr');
    let total = 0, complete = 0, partial = 0, unassigned = 0;

    rows.forEach(row => {
        total++;
        const selects = row.querySelectorAll('.assign-select');
        const assigned = Array.from(selects).filter(s => s.value).length;
        const needed = selects.length;

        if (assigned === 0) {
            unassigned++;
            row.classList.remove('fully-assigned', 'partially-assigned');
        } else if (assigned < needed) {
            partial++;
            row.classList.remove('fully-assigned');
            row.classList.add('partially-assigned');
        } else {
            complete++;
            row.classList.add('fully-assigned');
            row.classList.remove('partially-assigned');
        }
    });

    // Update summary panel
    document.getElementById('summaryTotal').textContent = total;
    document.getElementById('summaryComplete').textContent = complete;
    document.getElementById('summaryPartial').textContent = partial;
    document.getElementById('summaryUnassigned').textContent = unassigned;

    // Update progress bar
    const progress = total > 0 ? (complete / total) * 100 : 0;
    document.getElementById('summaryProgress').style.width = progress + '%';

    // Show/hide panel
    const panel = document.getElementById('assignmentSummary');
    if (panel) {
        if (total > 0) {
            panel.classList.add('visible');
        } else {
            panel.classList.remove('visible');
        }
    }
}

// Show notification
function showNotification(message, type = 'success') {
    const notification = document.createElement('div');
    notification.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        padding: 12px 20px;
        background: ${type === 'success' ? '#10b981' : type === 'error' ? '#ef4444' : '#3b82f6'};
        color: white;
        border-radius: 8px;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
        z-index: 10000;
        animation: slideInRight 0.3s ease-out;
    `;
    notification.textContent = message;

    document.body.appendChild(notification);

    setTimeout(() => {
        notification.style.animation = 'slideOutRight 0.3s ease-out';
        setTimeout(() => notification.remove(), 300);
    }, 3000);
}

// ========= CUSTOM GANTT CHART IMPLEMENTATION =========
// Add this to your dashboard-js.js file

let customGanttTasks = [];
let customGanttViewMode = 'days';


// Initialize custom Gantt chart
// Initialize custom Gantt chart with enhanced time scale support
function initializeCustomGantt() {
    console.log('Initializing custom Gantt chart...');

    if (!scenarioData || !scenarioData.tasks) {
        console.error('No task data available for Gantt chart');

        // Show message in the Gantt area
        const container = document.querySelector('.gantt-container-new');
        if (container) {
            container.innerHTML = `
                <div style="padding: 40px; text-align: center; color: #6b7280;">
                    <h3>No Task Data Available</h3>
                    <p>Please ensure scenario data is loaded properly.</p>
                </div>
            `;
        }
        return;
    }

    console.log(`Converting ${scenarioData.tasks.length} tasks to Gantt format...`);

    // Convert task data to Gantt format
    customGanttTasks = convertTasksToGanttFormat(scenarioData.tasks);

    // Set default view mode
    customGanttViewMode = '1day';

    // Set the dropdown to default value
    const viewModeSelect = document.getElementById('ganttViewMode');
    if (viewModeSelect) {
        viewModeSelect.value = customGanttViewMode;
    }

    console.log(`Converted to ${customGanttTasks.length} Gantt tasks`);

    // Populate filter dropdowns
    populateGanttFilters();

    // Render the Gantt chart
    renderCustomGanttChart();

    console.log('Custom Gantt chart initialization complete');
}

// Convert dashboard tasks to Gantt format
function convertTasksToGanttFormat(tasks) {
    return tasks.map(task => {
        const startDate = new Date(task.startTime);
        const endDate = new Date(task.endTime);

        // Calculate actual duration in days (not minutes)
        const durationDays = Math.max(1, Math.ceil((endDate - startDate) / (1000 * 60 * 60 * 24)));

        // Determine task type for styling
        let taskType = 'production';
        if (task.isCustomerTask || task.type === 'Customer' || task.type === 'Customer Inspection') {
            taskType = 'customer';
        } else if (task.type === 'Quality Inspection') {
            taskType = 'quality';
        } else if (task.isLatePartTask || task.type === 'Late Part') {
            taskType = 'late-part';
        } else if (task.isReworkTask || task.type === 'Rework') {
            taskType = 'rework';
        }

        return {
            id: task.taskId,
            name: `${task.taskId} - ${task.type || 'Task'}`,
            type: taskType,
            originalType: task.type,
            product: task.product,
            team: task.team,
            startDate: startDate,
            endDate: endDate,
            duration: durationDays, // Now correctly in days
            progress: task.progress || 0,
            critical: task.isCritical || task.priority <= 10,
            priority: task.priority || 999,
            dependencies: task.dependencies || []
        };
    });
}

// (Duplicate initializeCustomGantt removed - kept the version above with view mode init)

// Generate date range for Gantt chart with granular time scales
// Fixed date range generation with proper alignment
// Fixed date range generation with proper time alignment
// Dynamic date range generation - always shows exactly 35 days
function generateGanttDateRange(tasks, mode = '1day') {
    if (tasks.length === 0) {
        return [new Date()];
    }

    console.log(`\n=== GENERATING DYNAMIC 35-DAY RANGE FOR ${mode} ===`);

    // Find the earliest task date as our starting point
    const allTaskDates = [];
    tasks.forEach(task => {
        if (task.startDate && task.endDate) {
            const start = new Date(task.startDate);
            const end = new Date(task.endDate);
            if (!isNaN(start.getTime()) && !isNaN(end.getTime())) {
                allTaskDates.push(start, end);
            }
        }
    });

    if (allTaskDates.length === 0) {
        console.error('No valid task dates found!');
        return [new Date()];
    }

    const earliestTaskDate = new Date(Math.min(...allTaskDates));
    console.log(`Earliest task date: ${earliestTaskDate.toLocaleDateString()}`);

    // Start 1 day before the earliest task for context
    let rangeStart = new Date(earliestTaskDate);
    rangeStart.setDate(rangeStart.getDate() - 1);

    // End exactly 35 days after the start (36 total days for good measure)
    let rangeEnd = new Date(rangeStart);
    rangeEnd.setDate(rangeEnd.getDate() + 36);

    console.log(`35-day range: ${rangeStart.toLocaleDateString()} to ${rangeEnd.toLocaleDateString()}`);

    // Align to period boundaries
    switch (mode) {
        case '15min':
            rangeStart.setMinutes(Math.floor(rangeStart.getMinutes() / 15) * 15, 0, 0);
            rangeEnd.setMinutes(Math.ceil(rangeEnd.getMinutes() / 15) * 15, 0, 0);
            break;
        case '30min':
            rangeStart.setMinutes(Math.floor(rangeStart.getMinutes() / 30) * 30, 0, 0);
            rangeEnd.setMinutes(Math.ceil(rangeEnd.getMinutes() / 30) * 30, 0, 0);
            break;
        case '1hour':
        case '4hour':
        case '8hour':
            rangeStart.setMinutes(0, 0, 0);
            rangeEnd.setMinutes(0, 0, 0);
            break;
        default:
            rangeStart.setHours(0, 0, 0, 0);
            rangeEnd.setHours(0, 0, 0, 0);
    }

    // Calculate exactly how many periods we need for 36 days
    const totalDays = 36;
    let expectedPeriods;

    switch (mode) {
        case '15min': expectedPeriods = totalDays * 24 ; break;      // 3,456 periods
        case '30min': expectedPeriods = totalDays * 24 ; break;      // 1,728 periods
        case '1hour': expectedPeriods = totalDays * 24; break;          // 864 periods
        case '4hour': expectedPeriods = totalDays * 6; break;           // 216 periods
        case '8hour': expectedPeriods = totalDays * 3; break;           // 108 periods
        case '1day': expectedPeriods = totalDays; break;                // 36 periods
        case '1week': expectedPeriods = Math.ceil(totalDays / 7); break; // ~5 periods
        case '2weeks': expectedPeriods = Math.ceil(totalDays / 14); break; // ~3 periods
        case '1month': expectedPeriods = Math.ceil(totalDays / 30); break; // ~2 periods
        default: expectedPeriods = totalDays;
    }

    console.log(`Expected periods for ${totalDays} days in ${mode} mode: ${expectedPeriods}`);

    // Generate exactly the right number of periods
    const periods = [];
    const current = new Date(rangeStart);
    const endTime = rangeEnd.getTime();

    let iterations = 0;
    while (current.getTime() < endTime && periods.length < expectedPeriods + 100) { // +100 buffer for safety
        periods.push(new Date(current));

        // Increment based on mode
        switch (mode) {
            case '15min': current.setMinutes(current.getMinutes() + 15); break;
            case '30min': current.setMinutes(current.getMinutes() + 30); break;
            case '1hour': current.setHours(current.getHours() + 1); break;
            case '4hour': current.setHours(current.getHours() + 4); break;
            case '8hour': current.setHours(current.getHours() + 8); break;
            case '1day': current.setDate(current.getDate() + 1); break;
            case '1week': current.setDate(current.getDate() + 7); break;
            case '2weeks': current.setDate(current.getDate() + 14); break;
            case '1month': current.setMonth(current.getMonth() + 1); break;
            default: current.setDate(current.getDate() + 1);
        }

        iterations++;

        // Safety break to prevent infinite loops
        if (iterations > expectedPeriods + 500) {
            console.warn('Breaking loop - too many iterations');
            break;
        }
    }

    console.log(`Generated ${periods.length} periods (expected ~${expectedPeriods})`);
    console.log(`Range: ${periods[0]?.toLocaleDateString()} to ${periods[periods.length-1]?.toLocaleDateString()}`);

    // Performance warning for large column counts
    if (periods.length > 1500) {
        console.warn(`⚠️ Generated ${periods.length} columns. This may impact browser performance.`);
    }

    return periods;
}

// Check if Product C_25 is in the filtered task list
function checkTaskFiltering() {
    console.log('\n=== CHECKING TASK FILTERING ===');

    // Check original tasks
    if (window.scenarioData && window.scenarioData.tasks) {
        const originalC25 = window.scenarioData.tasks.filter(t =>
            t.taskId?.includes('C_25') || t.product?.includes('C')
        );
        console.log(`Original scenarioData.tasks with C: ${originalC25.length}`);
    }

    // Check converted tasks
    if (window.customGanttTasks) {
        const convertedC25 = window.customGanttTasks.filter(t =>
            t.id?.includes('C_25') || t.product?.includes('C')
        );
        console.log(`Converted customGanttTasks with C: ${convertedC25.length}`);
    }

    // Check filtered tasks
    const filteredTasks = getFilteredGanttTasks();
    const filteredC25 = filteredTasks.filter(t =>
        t.id?.includes('C_25') || t.product?.includes('C')
    );
    console.log(`Filtered tasks with C: ${filteredC25.length}`);

    // Show the specific C_25 task
    const c25Task = filteredTasks.find(t => t.id === 'Product C_25');
    if (c25Task) {
        console.log(`Found Product C_25 in filtered tasks:`, c25Task);
    } else {
        console.log(`Product C_25 NOT found in filtered tasks!`);
    }
}



// Debug function to verify 15-minute periods contain the expected times
function debug15MinutePeriods(task, dates) {
    if (task.id === 'Product_B_QI_116') {
        console.log(`\n=== DEBUG: Task ${task.id} 15-minute alignment ===`);
        console.log(`Task: ${task.startDate.toLocaleString()} - ${task.endDate.toLocaleString()}`);

        // Find periods that should contain this task
        const relevantPeriods = dates.filter((date, index) => {
            const nextPeriod = new Date(date);
            nextPeriod.setMinutes(nextPeriod.getMinutes() + 15);
            const overlaps = task.startDate < nextPeriod && task.endDate > date;

            if (overlaps) {
                console.log(`✓ Period ${index}: ${date.toLocaleTimeString()} - ${nextPeriod.toLocaleTimeString()} SHOULD contain task`);
            }

            return overlaps;
        });

        console.log(`Found ${relevantPeriods.length} relevant periods for task`);
    }
}


// Get filtered tasks based on current selections
// Get filtered tasks based on current selections including critical path filter
function getFilteredGanttTasks() {
    const productFilter = document.getElementById('ganttProductFilter')?.value || 'all';
    const teamFilter = document.getElementById('ganttTeamFilter')?.value || 'all';
    const criticalFilter = document.getElementById('ganttCriticalFilter')?.value || 'all';
    const sortBy = document.getElementById('ganttSortBy')?.value || 'startDate';

    let filtered = [...customGanttTasks];

    // Apply product filter
    if (productFilter !== 'all') {
        filtered = filtered.filter(task => task.product === productFilter);
    }

    // Apply team filter
    if (teamFilter !== 'all') {
        filtered = filtered.filter(task => task.team === teamFilter);
    }

    // Apply critical path filter
    if (criticalFilter === 'critical') {
        filtered = filtered.filter(task =>
            task.critical ||
            task.isCritical ||
            task.isCriticalPath ||
            task.priority <= 10 ||
            (task.slackHours !== undefined && task.slackHours < 24)
        );
    } else if (criticalFilter === 'non-critical') {
        filtered = filtered.filter(task =>
            !task.critical &&
            !task.isCritical &&
            !task.isCriticalPath &&
            (task.priority === undefined || task.priority > 10) &&
            (task.slackHours === undefined || task.slackHours >= 24)
        );
    }

    // Apply sorting
    filtered.sort((a, b) => {
        switch (sortBy) {
            case 'startDate':
                return a.startDate - b.startDate;
            case 'product':
                return a.product.localeCompare(b.product) || a.startDate - b.startDate;
            case 'team':
                return a.team.localeCompare(b.team) || a.startDate - b.startDate;
            case 'priority':
                return a.priority - b.priority || a.startDate - b.startDate;
            default:
                return a.startDate - b.startDate;
        }
    });

    return filtered;
}

// Render the complete Gantt chart with enhanced time scale support
// Render with validation
// Enhanced renderCustomGanttChart with specific debugging
function renderCustomGanttChart() {
    const tasks = getFilteredGanttTasks();

    if (tasks.length === 0) {
        console.log('No tasks to render');
        return;
    }

    customGanttViewMode = document.getElementById('ganttViewMode')?.value || '1day';
    const dates = generateGanttDateRange(tasks, customGanttViewMode);

    // Debug specific problematic task
    debugSpecificTask('Product C_25', tasks, dates, customGanttViewMode);

    renderGanttHeader(dates);
    renderGanttTasks(tasks, dates);
    updateGanttStats(tasks);

    console.log(`Rendered Gantt: ${tasks.length} tasks, ${dates.length} periods, ${customGanttViewMode} scale`);
}

// Find and inspect task data
function findTaskInData() {
    console.log('\n🔍 SEARCHING FOR PRODUCT C_25 TASK:');

    if (window.customGanttTasks) {
        const found = window.customGanttTasks.filter(t =>
            t.id?.includes('C_25') ||
            t.name?.includes('C_25') ||
            t.taskId?.includes('C_25')
        );
        console.log('Found in customGanttTasks:', found);
    }

    if (window.scenarioData && window.scenarioData.tasks) {
        const found = window.scenarioData.tasks.filter(t =>
            t.taskId?.includes('C_25') ||
            t.name?.includes('C_25') ||
            t.product?.includes('C')
        );
        console.log('Found in scenarioData.tasks:', found.slice(0, 3));
    }
}

// Debug Product C_25 step by step
function debugProductC25() {
    console.log('\n=== DEBUGGING PRODUCT C_25 STEP BY STEP ===');

    // Find the task
    const task = customGanttTasks.find(t => t.id === 'Product C_25' || t.name?.includes('Product C_25'));
    if (!task) {
        console.log('Task not found in customGanttTasks');
        return;
    }

    console.log('1. TASK DATA:');
    console.log(`   ID: ${task.id}`);
    console.log(`   Start: ${task.startDate} (${task.startDate.toLocaleString()})`);
    console.log(`   End: ${task.endDate} (${task.endDate.toLocaleString()})`);
    console.log(`   Start timestamp: ${task.startDate.getTime()}`);
    console.log(`   End timestamp: ${task.endDate.getTime()}`);

    // Generate date range
    const timeScale = '15min';
    const allTasks = customGanttTasks;

    console.log('\n2. GENERATING DATE RANGE:');
    const dates = generateGanttDateRange(allTasks, timeScale);
    console.log(`   Generated ${dates.length} periods`);
    console.log(`   First period: ${dates[0].toLocaleString()}`);
    console.log(`   Last period: ${dates[dates.length-1].toLocaleString()}`);

    // Find periods that include August 28th
    console.log('\n3. SEARCHING FOR AUGUST 28TH PERIODS:');
    const aug28Periods = [];
    dates.forEach((date, index) => {
        if (date.getDate() === 28 && date.getMonth() === 7 && date.getFullYear() === 2025) { // August = month 7
            aug28Periods.push({index, date: date.toLocaleString()});
        }
    });

    console.log(`   Found ${aug28Periods.length} periods on August 28th:`);
    aug28Periods.forEach(p => console.log(`   - Period ${p.index}: ${p.date}`));

    // Check overlap with each August 28th period
    console.log('\n4. CHECKING OVERLAPS WITH AUGUST 28TH PERIODS:');
    aug28Periods.forEach(period => {
        const periodStart = dates[period.index];
        const periodEnd = new Date(periodStart);
        periodEnd.setMinutes(periodEnd.getMinutes() + 15);

        const taskStartMs = task.startDate.getTime();
        const taskEndMs = task.endDate.getTime();
        const periodStartMs = periodStart.getTime();
        const periodEndMs = periodEnd.getTime();

        const overlaps = taskStartMs < periodEndMs && taskEndMs > periodStartMs;

        console.log(`   Period ${period.index} (${periodStart.toLocaleString()} - ${periodEnd.toLocaleString()}): ${overlaps ? 'OVERLAPS' : 'no overlap'}`);

        if (overlaps) {
            console.log(`     Task: ${taskStartMs} - ${taskEndMs}`);
            console.log(`     Period: ${periodStartMs} - ${periodEndMs}`);
            console.log(`     Task starts before period ends? ${taskStartMs < periodEndMs}`);
            console.log(`     Task ends after period starts? ${taskEndMs > periodStartMs}`);
        }
    });

    // Run the actual positioning function
    console.log('\n5. ACTUAL POSITIONING FUNCTION RESULT:');
    const position = calculateGanttTaskPosition(task, dates, timeScale);
    console.log(`   Start index: ${position.startIndex}`);
    console.log(`   End index: ${position.endIndex}`);
    console.log(`   Width: ${position.width}`);

    if (position.startIndex >= 0) {
        const actualStartPeriod = dates[position.startIndex];
        console.log(`   Actual start period: ${actualStartPeriod.toLocaleString()}`);
        console.log(`   Expected date: August 28th`);
        console.log(`   Actual date: ${actualStartPeriod.toLocaleDateString()}`);
        console.log(`   Days difference: ${Math.round((actualStartPeriod.getTime() - task.startDate.getTime()) / (1000*60*60*24))}`);
    }

    return {task, dates, aug28Periods, position};
}

// Debug specific task positioning
function debugSpecificTask(taskId, tasks, dates, timeScale) {
    const task = tasks.find(t => t.id === taskId || t.name?.includes(taskId) || t.taskId === taskId);

    if (!task) {
        console.log(`❌ Task ${taskId} not found in task list`);
        return;
    }

    console.log(`\n🔍 DEBUGGING TASK: ${taskId}`);
    console.log(`Task Object:`, task);
    console.log(`Task Start: ${task.startDate} (${typeof task.startDate})`);
    console.log(`Task End: ${task.endDate} (${typeof task.endDate})`);

    // Parse dates
    const startDate = new Date(task.startDate);
    const endDate = new Date(task.endDate);

    console.log(`Parsed Start: ${startDate.toLocaleString()} (${startDate.getTime()})`);
    console.log(`Parsed End: ${endDate.toLocaleString()} (${endDate.getTime()})`);
    console.log(`Is Valid: Start=${!isNaN(startDate.getTime())}, End=${!isNaN(endDate.getTime())}`);

    // Show date range
    console.log(`\n📅 DATE RANGE (${timeScale}):`);
    console.log(`First period: ${dates[0]?.toLocaleString()}`);
    console.log(`Last period: ${dates[dates.length-1]?.toLocaleString()}`);
    console.log(`Total periods: ${dates.length}`);

    // Check each period for overlap
    console.log(`\n🔄 CHECKING OVERLAPS:`);
    let foundOverlaps = [];

    for (let i = 0; i < Math.min(dates.length, 20); i++) { // Check first 20 periods
        const period = dates[i];
        const periodEnd = new Date(period);

        // Calculate period end
        switch (timeScale) {
            case '15min': periodEnd.setMinutes(periodEnd.getMinutes() + 15); break;
            case '30min': periodEnd.setMinutes(periodEnd.getMinutes() + 30); break;
            case '1hour': periodEnd.setHours(periodEnd.getHours() + 1); break;
            default: periodEnd.setDate(periodEnd.getDate() + 1);
        }

        const taskStartMs = startDate.getTime();
        const taskEndMs = endDate.getTime();
        const periodStartMs = period.getTime();
        const periodEndMs = periodEnd.getTime();

        const overlaps = taskStartMs < periodEndMs && taskEndMs > periodStartMs;

        console.log(`Period ${i}: ${period.toLocaleString()} - ${periodEnd.toLocaleString()} = ${overlaps ? '✅ OVERLAP' : '❌ no overlap'}`);

        if (overlaps) {
            foundOverlaps.push(i);
        }
    }

    console.log(`\n📍 EXPECTED POSITION:`);
    if (foundOverlaps.length > 0) {
        console.log(`Should start at column ${foundOverlaps[0]} and end at column ${foundOverlaps[foundOverlaps.length-1]}`);
        console.log(`Width should be: ${foundOverlaps.length} columns`);
    } else {
        console.log(`❌ NO OVERLAPPING PERIODS FOUND!`);
    }

    // Check what the actual positioning function returns
    const actualPosition = calculateGanttTaskPosition(task, dates, timeScale);
    console.log(`\n🎯 ACTUAL POSITION:`);
    console.log(`Calculated: start=${actualPosition.startIndex}, end=${actualPosition.endIndex}, width=${actualPosition.width}`);

    return { task, expectedOverlaps: foundOverlaps, actualPosition };
}

// Render Gantt chart header with date and shift rows
function renderGanttHeader(dates) {
    const header = document.getElementById('ganttHeaderNew');
    if (!header) return;

    header.innerHTML = '';

    const timeScale = customGanttViewMode || '1day';
    const columnConfig = getGanttColumnConfig(timeScale);

    // Create first header row (dates)
    const dateRow = document.createElement('tr');

    // Task column header (spans both rows)
    const taskHeader = document.createElement('th');
    taskHeader.style.cssText = `
        min-width: 250px;
        max-width: 250px;
        text-align: left;
        padding: 12px;
        background: #f1f5f9;
        position: sticky;
        left: 0;
        z-index: 11;
        border-bottom: 2px solid #e5e7eb;
        border-right: 2px solid #d1d5db;
        font-weight: 600;
        color: #374151;
    `;
    taskHeader.textContent = 'Tasks';
    taskHeader.rowSpan = 2; // Span both header rows
    dateRow.appendChild(taskHeader);

    // Date column headers
    dates.forEach(date => {
        const dateHeader = document.createElement('th');
        const isWeekend = date.getDay() === 0 || date.getDay() === 6;
        const isToday = isTimeToday(date, timeScale);

        dateHeader.style.cssText = `
            min-width: ${columnConfig.width}px;
            width: ${columnConfig.width}px;
            text-align: center;
            padding: 6px 4px;
            border-bottom: 1px solid #e5e7eb;
            border-right: 1px solid #e5e7eb;
            font-weight: 600;
            font-size: ${Math.max(10, columnConfig.fontSize - 1)}px;
            color: ${isToday ? '#ef4444' : '#374151'};
            background: ${isWeekend && columnConfig.showWeekends ? '#f9fafb' : isToday ? '#fef2f2' : '#f8fafc'};
            ${columnConfig.vertical && columnConfig.width < 50 ? 'writing-mode: vertical-rl; text-orientation: mixed;' : ''}
        `;

        // Set date header text
        dateHeader.textContent = formatGanttDateLabel(date, timeScale);
        dateHeader.title = formatGanttHeaderTooltip(date, timeScale);

        dateRow.appendChild(dateHeader);
    });

    // Create second header row (shifts) - only for time scales that show hours
    const shiftRow = document.createElement('tr');

    dates.forEach(date => {
        const shiftHeader = document.createElement('th');
        const isWeekend = date.getDay() === 0 || date.getDay() === 6;
        const isToday = isTimeToday(date, timeScale);

        shiftHeader.style.cssText = `
            min-width: ${columnConfig.width}px;
            width: ${columnConfig.width}px;
            text-align: center;
            padding: 4px 2px;
            border-bottom: 2px solid #e5e7eb;
            border-right: 1px solid #e5e7eb;
            font-weight: 500;
            font-size: ${Math.max(9, columnConfig.fontSize - 2)}px;
            color: ${isToday ? '#ef4444' : '#6b7280'};
            background: ${isWeekend && columnConfig.showWeekends ? '#f9fafb' : isToday ? '#fef2f2' : '#f8fafc'};
        `;

        // Set shift header text based on time scale
        const shiftText = getShiftForTime(date, timeScale);
        shiftHeader.textContent = shiftText;

        if (shiftText && shiftText !== '-') {
            const shiftInfo = getShiftInfo(shiftText);
            shiftHeader.title = `${shiftText} Shift: ${shiftInfo.start} - ${shiftInfo.end} (${shiftInfo.duration})`;
        }

        shiftRow.appendChild(shiftHeader);
    });

    header.appendChild(dateRow);
    header.appendChild(shiftRow);
}

// Render Gantt chart task rows with time-scale aware positioning
function renderGanttTasks(tasks, dates) {
    const tbody = document.getElementById('ganttBodyNew');
    if (!tbody) {
        console.error('Gantt tbody not found');
        return;
    }

    tbody.innerHTML = '';

    const timeScale = customGanttViewMode || '1day';
    const columnConfig = getGanttColumnConfig(timeScale);

    tasks.forEach((task, taskIndex) => {
        const row = document.createElement('tr');
        row.style.cssText = `
            height: 36px;
            background: ${taskIndex % 2 === 0 ? 'white' : '#fafbfc'};
        `;
        row.addEventListener('mouseenter', () => row.style.background = '#f0f9ff');
        row.addEventListener('mouseleave', () => row.style.background = taskIndex % 2 === 0 ? 'white' : '#fafbfc');

        // Task name cell
        const taskCell = document.createElement('td');
        taskCell.style.cssText = `
            min-width: 250px;
            max-width: 250px;
            padding: 8px 12px;
            background: #f8fafc;
            position: sticky;
            left: 0;
            z-index: 9;
            border-right: 2px solid #d1d5db;
            border-bottom: 1px solid #f3f4f6;
            vertical-align: middle;
        `;

        taskCell.innerHTML = `
            <div style="font-weight: 600; color: #1f2937; font-size: 13px; margin-bottom: 2px;">
                ${task.name || task.id}
            </div>
            <div style="color: #6b7280; font-size: 11px;">
                ${task.team || 'Unknown Team'} • ${task.product || 'Unknown Product'}
                ${task.critical ? ' • <span style="color: #ef4444;">CRITICAL</span>' : ''}
            </div>
        `;
        row.appendChild(taskCell);

        // Date cells with task bars
        let taskBarRendered = false;
        const taskPosition = calculateGanttTaskPosition(task, dates, timeScale);

        dates.forEach((date, dateIndex) => {
            const dateCell = document.createElement('td');
            const isWeekend = date.getDay() === 0 || date.getDay() === 6;
            const isToday = isTimeToday(date, timeScale);

            dateCell.style.cssText = `
                min-width: ${columnConfig.width}px;
                width: ${columnConfig.width}px;
                padding: 0;
                margin: 0;
                position: relative;
                height: 36px;
                vertical-align: middle;
                border-right: 1px solid #e5e7eb;
                border-bottom: 1px solid #f3f4f6;
                background: ${isWeekend && columnConfig.showWeekends ? '#f9fafb' : isToday ? '#fef2f2' : 'transparent'};
            `;

            // Render task bar on the start position
            if (dateIndex === taskPosition.startIndex && !taskBarRendered) {
                const bar = document.createElement('div');
                const barWidth = taskPosition.width * columnConfig.width - 2;

                bar.style.cssText = `
                    position: absolute;
                    top: 4px;
                    left: 1px;
                    height: 28px;
                    width: ${Math.max(barWidth, columnConfig.width - 2)}px;
                    border-radius: 4px;
                    display: flex;
                    align-items: center;
                    padding: 0 ${Math.max(8, columnConfig.width / 5)}px;
                    color: white;
                    font-weight: 500;
                    font-size: ${Math.max(9, columnConfig.fontSize - 1)}px;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
                    cursor: pointer;
                    transition: all 0.2s ease;
                    ${getTaskBarStyle(task.type)};
                    ${task.critical ? 'border: 2px solid #fbbf24; box-shadow: 0 0 0 2px rgba(251, 191, 36, 0.2);' : ''}
                    overflow: hidden;
                `;

                // Adjust text content based on bar width
                if (barWidth > 60) {
                    bar.textContent = task.id;
                } else if (barWidth > 30) {
                    bar.textContent = task.id.substring(0, 8) + (task.id.length > 8 ? '...' : '');
                } else {
                    bar.innerHTML = '<div style="width: 100%; height: 100%;"></div>'; // Just color bar
                }

                // Enhanced tooltip with time scale specific info
                bar.title = getTaskBarTooltip(task, timeScale);

                bar.addEventListener('mouseenter', () => {
                    bar.style.boxShadow = '0 2px 8px rgba(0, 0, 0, 0.3)';
                    bar.style.transform = 'translateY(-1px)';
                });

                bar.addEventListener('mouseleave', () => {
                    bar.style.boxShadow = '0 1px 3px rgba(0, 0, 0, 0.2)';
                    bar.style.transform = 'translateY(0)';
                });

                dateCell.appendChild(bar);
                taskBarRendered = true;
            }

            row.appendChild(dateCell);
        });

        tbody.appendChild(row);
    });

    console.log(`Rendered ${tasks.length} task rows with ${timeScale} time scale`);
}

// Calculate task position in the Gantt chart
// Enhanced task positioning with debugging
// Enhanced task positioning with detailed debugging
// Enhanced task positioning with 15-minute debugging
// (First calculateGanttTaskPosition removed - kept cleaner version below)

// Get task bar CSS styles based on task type
function getTaskBarStyle(type) {
    const styles = {
        'production': 'background: linear-gradient(135deg, #10b981, #059669);',
        'quality': 'background: linear-gradient(135deg, #3b82f6, #2563eb);',
        'rework': 'background: linear-gradient(135deg, #ef4444, #dc2626);',
        'late-part': 'background: linear-gradient(135deg, #f59e0b, #d97706);',
        'customer': 'background: linear-gradient(135deg, #8b5cf6, #7c3aed);'
    };

    return styles[type] || styles['production'];
}

// Get week number helper function
function getWeekNumber(date) {
    const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
    const dayNum = d.getUTCDay() || 7;
    d.setUTCDate(d.getUTCDate() + 4 - dayNum);
    const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
    return Math.ceil((((d - yearStart) / 86400000) + 1) / 7);
}

// Update Gantt chart statistics
// 1. Fix the duration calculation
// Enhanced updateGanttStats to include critical path information
function updateGanttStats(tasks) {
    const totalTasksEl = document.getElementById('ganttTotalTasks');
    const totalDurationEl = document.getElementById('ganttTotalDuration');
    const criticalTasksEl = document.getElementById('ganttCriticalTasks');
    const completionRateEl = document.getElementById('ganttCompletionRate');

    if (!totalTasksEl || !totalDurationEl || !criticalTasksEl || !completionRateEl) {
        console.error('Gantt stats elements not found in DOM');
        return;
    }

    totalTasksEl.textContent = tasks.length;

    // Calculate project makespan
    if (tasks.length > 0) {
        const startDates = tasks.map(t => t.startDate);
        const endDates = tasks.map(t => t.endDate);
        const projectStart = new Date(Math.min(...startDates));
        const projectEnd = new Date(Math.max(...endDates));
        const makespanDays = Math.ceil((projectEnd - projectStart) / (1000 * 60 * 60 * 24));
        totalDurationEl.textContent = makespanDays + ' days';
    } else {
        totalDurationEl.textContent = '0 days';
    }

    // Count critical tasks with comprehensive criteria
    const criticalCount = tasks.filter(t =>
        t.critical ||
        t.isCritical ||
        t.isCriticalPath ||
        t.priority <= 10 ||
        (t.slackHours !== undefined && t.slackHours < 24)
    ).length;

    criticalTasksEl.textContent = criticalCount;

    // Add percentage indicator if useful
    if (tasks.length > 0) {
        const criticalPercentage = Math.round((criticalCount / tasks.length) * 100);
        criticalTasksEl.title = `${criticalPercentage}% of visible tasks are critical`;
    }

    // Calculate completion rate
    let completionRate = 0;
    if (scenarioData && scenarioData.onTimeRate !== undefined) {
        completionRate = scenarioData.onTimeRate;
    } else {
        const onScheduleCount = tasks.filter(t => t.progress >= 75).length;
        completionRate = tasks.length > 0 ? Math.round((onScheduleCount / tasks.length) * 100) : 0;
    }
    completionRateEl.textContent = completionRate + '%';

    console.log(`Stats: ${tasks.length} tasks, ${criticalCount} critical (${Math.round((criticalCount/tasks.length)*100)}%)`);
}


// Populate Gantt filter dropdowns
// Populate Gantt filter dropdowns with enhanced event handling
// Populate Gantt filter dropdowns with enhanced event handling including critical filter
function populateGanttFilters() {
    if (customGanttTasks.length === 0) return;

    // Populate product filter (preserve selection)
    const products = [...new Set(customGanttTasks.map(t => t.product))].sort();
    const productSelect = document.getElementById('ganttProductFilter');
    if (productSelect) {
        const savedProduct = productSelect.value;
        productSelect.innerHTML = '<option value="all">All Products</option>';
        products.forEach(product => {
            const option = document.createElement('option');
            option.value = product;
            option.textContent = product;
            productSelect.appendChild(option);
        });
        if (savedProduct && Array.from(productSelect.options).some(o => o.value === savedProduct)) {
            productSelect.value = savedProduct;
        }
    }

    // Populate team filter (preserve selection)
    const teams = [...new Set(customGanttTasks.map(t => t.team))].sort();
    const teamSelect = document.getElementById('ganttTeamFilter');
    if (teamSelect) {
        const savedTeam = teamSelect.value;
        teamSelect.innerHTML = '<option value="all">All Teams</option>';
        teams.forEach(team => {
            const option = document.createElement('option');
            option.value = team;
            option.textContent = team;
            teamSelect.appendChild(option);
        });
        if (savedTeam && Array.from(teamSelect.options).some(o => o.value === savedTeam)) {
            teamSelect.value = savedTeam;
        }
    }

    // Add event listeners for all filters including critical filter
    const filterElements = ['ganttProductFilter', 'ganttTeamFilter', 'ganttCriticalFilter', 'ganttSortBy'];
    filterElements.forEach(id => {
        const element = document.getElementById(id);
        if (element && !element.hasAttribute('data-listener-added')) {
            element.setAttribute('data-listener-added', 'true');
            element.addEventListener('change', renderCustomGanttChart);
        }
    });

    // Add view mode change listener with time scale support
    const viewModeSelect = document.getElementById('ganttViewMode');
    if (viewModeSelect && !viewModeSelect.hasAttribute('data-listener-added')) {
        viewModeSelect.setAttribute('data-listener-added', 'true');
        viewModeSelect.addEventListener('change', (e) => {
            customGanttViewMode = e.target.value;
            console.log(`Switching to time scale: ${customGanttViewMode}`);
            renderCustomGanttChart();
        });
    }
}

function updateGanttStatsDetailed(tasks) {
    document.getElementById('ganttTotalTasks').textContent = tasks.length;

    if (tasks.length > 0) {
        const startDates = tasks.map(t => t.startDate);
        const endDates = tasks.map(t => t.endDate);
        const projectStart = new Date(Math.min(...startDates));
        const projectEnd = new Date(Math.max(...endDates));

        // Calculate makespan
        const makespanDays = Math.ceil((projectEnd - projectStart) / (1000 * 60 * 60 * 24));
        document.getElementById('ganttTotalDuration').textContent = makespanDays + ' days';

        // Add additional stats if you want them
        console.log(`Project Statistics:
        - Project Start: ${projectStart.toLocaleDateString()}
        - Project End: ${projectEnd.toLocaleDateString()}
        - Makespan: ${makespanDays} days
        - Total Tasks: ${tasks.length}
        - Average Task Duration: ${(tasks.reduce((sum, t) => sum + t.duration, 0) / tasks.length).toFixed(1)} days
        - Parallel Efficiency: ${((tasks.reduce((sum, t) => sum + t.duration, 0)) / makespanDays).toFixed(1)}x`);
    } else {
        document.getElementById('ganttTotalDuration').textContent = '0 days';
    }

    const criticalCount = tasks.filter(t => t.critical).length;
    document.getElementById('ganttCriticalTasks').textContent = criticalCount;

    const onScheduleCount = tasks.filter(t => t.progress >= 75).length;
    const completionRate = tasks.length > 0 ? Math.round((onScheduleCount / tasks.length) * 100) : 0;
    document.getElementById('ganttCompletionRate').textContent = completionRate + '%';
}

// Expose functions globally for HTML onclick handlers
window.autoAssign = autoAssign;
window.saveAssignmentsToStorage = saveAssignmentsToStorage;
window.loadAssignmentsFromStorage = loadAssignmentsFromStorage;
window.clearSavedAssignments = clearSavedAssignments;
window.clearAllAssignments = clearAllAssignments;
window.exportTasks = exportTasks;
window.viewAssignmentReport = viewAssignmentReport;
window.updateAssignmentSummary = updateAssignmentSummary;
window.showNotification = showNotification;
window.handleGanttSortChange = handleGanttSortChange;
window.refreshGanttChart = refreshGanttChart;
window.exportGanttChart = exportGanttChart;
window.debugProductC25 = debugProductC25;
window.checkTaskFiltering = checkTaskFiltering;
window.refreshTimeline = refreshTimeline;
window.exportTimelineData = exportTimelineData;
window.fitTimelineToTasks = fitTimelineToTasks;
window.refreshCustomGantt = refreshCustomGantt;
window.exportCustomGantt = exportCustomGantt;
window.fitGanttToTasks = fitGanttToTasks;

// Get column configuration for different time scales
// Update getGanttColumnConfig to account for dual headers
function getGanttColumnConfig(timeScale) {
    const configs = {
        '15min': { width: 35, fontSize: 10, vertical: true, showWeekends: false },
        '30min': { width: 40, fontSize: 10, vertical: true, showWeekends: false },
        '1hour': { width: 45, fontSize: 11, vertical: true, showWeekends: false },
        '4hour': { width: 55, fontSize: 11, vertical: false, showWeekends: false },
        '8hour': { width: 65, fontSize: 11, vertical: false, showWeekends: false },
        '1day': { width: 45, fontSize: 11, vertical: true, showWeekends: true },
        '1week': { width: 70, fontSize: 11, vertical: false, showWeekends: false },
        '2weeks': { width: 80, fontSize: 11, vertical: false, showWeekends: false },
        '1month': { width: 90, fontSize: 11, vertical: false, showWeekends: false }
    };
    return configs[timeScale] || configs['1day'];
}

// Format header labels based on time scale
function formatGanttHeaderLabel(date, timeScale) {
    switch (timeScale) {
        case '15min':
        case '30min':
            return date.toLocaleTimeString('en-US', {
                hour: '2-digit',
                minute: '2-digit',
                hour12: false
            });
        case '1hour':
        case '4hour':
        case '8hour':
            return date.toLocaleTimeString('en-US', {
                hour: '2-digit',
                hour12: false
            }) + 'h';
        case '1day':
            return date.getDate().toString().padStart(2, '0');
        case '1week':
            return `W${getWeekNumber(date)}`;
        case '2weeks':
            return `W${getWeekNumber(date)}-${getWeekNumber(new Date(date.getTime() + 7 * 24 * 60 * 60 * 1000))}`;
        case '1month':
            return date.toLocaleDateString('en-US', { month: 'short' });
        default:
            return date.getDate().toString();
    }
}

// Format header tooltips
function formatGanttHeaderTooltip(date, timeScale) {
    switch (timeScale) {
        case '15min':
        case '30min':
        case '1hour':
        case '4hour':
        case '8hour':
            return date.toLocaleString('en-US', {
                weekday: 'short',
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            });
        case '1day':
            return date.toLocaleDateString('en-US', {
                weekday: 'long',
                month: 'long',
                day: 'numeric',
                year: 'numeric'
            });
        case '1week':
        case '2weeks':
            return `Week of ${date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}`;
        case '1month':
            return date.toLocaleDateString('en-US', {
                month: 'long',
                year: 'numeric'
            });
        default:
            return date.toLocaleDateString();
    }
}

// Check if a time period includes today
function isTimeToday(date, timeScale) {
    const today = new Date();

    switch (timeScale) {
        case '15min':
        case '30min':
        case '1hour':
        case '4hour':
        case '8hour':
            // Check if the hour period includes current time
            const endTime = new Date(date);
            const interval = parseInt(timeScale.replace(/\D/g, '')) || 1;
            const unit = timeScale.includes('min') ? 'minutes' : 'hours';

            if (unit === 'minutes') {
                endTime.setMinutes(endTime.getMinutes() + interval);
            } else {
                endTime.setHours(endTime.getHours() + interval);
            }

            return today >= date && today < endTime;
        case '1day':
            return date.toDateString() === today.toDateString();
        case '1week':
            const weekStart = new Date(date);
            weekStart.setDate(weekStart.getDate() - weekStart.getDay());
            const weekEnd = new Date(weekStart);
            weekEnd.setDate(weekEnd.getDate() + 6);
            return today >= weekStart && today <= weekEnd;
        case '2weeks':
            const twoWeekEnd = new Date(date);
            twoWeekEnd.setDate(twoWeekEnd.getDate() + 13);
            return today >= date && today <= twoWeekEnd;
        case '1month':
            return date.getMonth() === today.getMonth() && date.getFullYear() === today.getFullYear();
        default:
            return false;
    }
}

// Calculate task position with time-scale awareness
function calculateGanttTaskPosition(task, dates, timeScale) {
    let startIndex = -1;
    let endIndex = -1;

    for (let i = 0; i < dates.length; i++) {
        const date = dates[i];

        // Check if task overlaps with this time period
        if (taskOverlapsTimePeriod(task, date, timeScale)) {
            if (startIndex === -1) {
                startIndex = i;
            }
            endIndex = i;
        }
    }

    // Handle tasks that start before or end after visible range
    if (startIndex === -1) {
        // Task is completely outside the visible range
        return { startIndex: 0, endIndex: 0, width: 0 };
    }

    return {
        startIndex: startIndex,
        endIndex: endIndex,
        width: endIndex - startIndex + 1
    };
}

// Check if task overlaps with a time period
// Fixed task overlap detection with better date comparison
// Simplified and more reliable overlap detection
function taskOverlapsTimePeriod(task, periodStart, timeScale) {
    // Create period end time
    const periodEnd = new Date(periodStart);

    switch (timeScale) {
        case '15min':
            periodEnd.setMinutes(periodEnd.getMinutes() + 15);
            break;
        case '30min':
            periodEnd.setMinutes(periodEnd.getMinutes() + 30);
            break;
        case '1hour':
            periodEnd.setHours(periodEnd.getHours() + 1);
            break;
        case '4hour':
            periodEnd.setHours(periodEnd.getHours() + 4);
            break;
        case '8hour':
            periodEnd.setHours(periodEnd.getHours() + 8);
            break;
        case '1day':
            periodEnd.setDate(periodEnd.getDate() + 1);
            break;
        case '1week':
            periodEnd.setDate(periodEnd.getDate() + 7);
            break;
        case '2weeks':
            periodEnd.setDate(periodEnd.getDate() + 14);
            break;
        case '1month':
            periodEnd.setMonth(periodEnd.getMonth() + 1);
            break;
        default:
            periodEnd.setDate(periodEnd.getDate() + 1);
    }

    // Convert all dates to UTC milliseconds to avoid timezone issues
    const taskStartMs = new Date(task.startDate).getTime();
    const taskEndMs = new Date(task.endDate).getTime();
    const periodStartMs = periodStart.getTime();
    const periodEndMs = periodEnd.getTime();

    // Simple overlap check: task overlaps period if task_start < period_end AND task_end > period_start
    const overlaps = taskStartMs < periodEndMs && taskEndMs > periodStartMs;

    // Debug specific problematic task
    if (task.id && task.id.includes('E_QI_101')) {
        console.log(`TASK ${task.id}:
            Task: ${new Date(taskStartMs).toLocaleString()} - ${new Date(taskEndMs).toLocaleString()}
            Period: ${new Date(periodStartMs).toLocaleString()} - ${new Date(periodEndMs).toLocaleString()}
            Overlaps: ${overlaps}`);
    }

    return overlaps;
}

// Generate tooltip content based on time scale
function getTaskBarTooltip(task, timeScale) {
    let startStr, endStr, durationStr;

    switch (timeScale) {
        case '15min':
        case '30min':
        case '1hour':
        case '4hour':
        case '8hour':
            startStr = task.startDate.toLocaleString('en-US', {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            });
            endStr = task.endDate.toLocaleString('en-US', {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            });

            const durationHours = (task.endDate - task.startDate) / (1000 * 60 * 60);
            if (durationHours < 1) {
                durationStr = `${Math.round(durationHours * 60)} minutes`;
            } else {
                durationStr = `${durationHours.toFixed(1)} hours`;
            }
            break;
        default:
            startStr = task.startDate.toLocaleDateString();
            endStr = task.endDate.toLocaleDateString();
            const durationDays = Math.ceil((task.endDate - task.startDate) / (1000 * 60 * 60 * 24));
            durationStr = `${durationDays} day${durationDays !== 1 ? 's' : ''}`;
    }

    return `${task.name || task.id}\nStart: ${startStr}\nEnd: ${endStr}\nDuration: ${durationStr}\nTeam: ${task.team || 'Unknown'}\nProduct: ${task.product || 'Unknown'}${task.critical ? '\n⚠️ CRITICAL TASK' : ''}`;
}

// Refresh the custom Gantt chart
function refreshCustomGantt() {
    renderCustomGanttChart();
    showNotification('Gantt chart refreshed', 'success');
}

// Export custom Gantt chart data to CSV
function exportCustomGantt() {
    const tasks = getFilteredGanttTasks();

    if (tasks.length === 0) {
        alert('No tasks to export');
        return;
    }

    const productFilter = document.getElementById('ganttProductFilter')?.value || 'all';
    const teamFilter = document.getElementById('ganttTeamFilter')?.value || 'all';
    const sortBy = document.getElementById('ganttSortBy')?.value || 'startDate';
    const timeScale = customGanttViewMode || '1day';

    let csvContent = "Custom Gantt Chart Export\n";
    csvContent += `Generated: ${new Date().toLocaleString()}\n`;
    csvContent += `Scenario: ${currentScenario}\n`;
    csvContent += `Time Scale: ${timeScale}\n`;
    csvContent += `Filters: Product=${productFilter}, Team=${teamFilter}, Sort=${sortBy}\n\n`;

    csvContent += "Task ID,Task Name,Type,Product,Team,Start Date,End Date,Duration,Progress,Critical,Priority\n";

    tasks.forEach(task => {
        const startDate = task.startDate.toISOString();
        const endDate = task.endDate.toISOString();
        const durationText = timeScale.includes('min') || timeScale.includes('hour') ?
            `${((task.endDate - task.startDate) / (1000 * 60 * 60)).toFixed(1)} hours` :
            `${task.duration} days`;

        csvContent += `"${task.id}","${task.name}","${task.originalType}","${task.product}","${task.team}","${startDate}","${endDate}","${durationText}","${task.progress}%","${task.critical ? 'Yes' : 'No'}","${task.priority}"\n`;
    });

    // Add summary statistics
    csvContent += "\nSummary Statistics:\n";
    csvContent += `Total Tasks: ${tasks.length}\n`;
    csvContent += `Critical Tasks: ${tasks.filter(t => t.critical).length}\n`;
    csvContent += `Time Scale: ${timeScale}\n`;

    // Task type breakdown
    const typeBreakdown = {};
    tasks.forEach(task => {
        typeBreakdown[task.type] = (typeBreakdown[task.type] || 0) + 1;
    });

    csvContent += "\nTask Type Breakdown:\n";
    Object.entries(typeBreakdown).forEach(([type, count]) => {
        csvContent += `${type}: ${count}\n`;
    });

    // Download CSV
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    const url = URL.createObjectURL(blob);
    link.setAttribute('href', url);
    link.setAttribute('download', `gantt_chart_${currentScenario}_${timeScale}_${new Date().toISOString().slice(0, 10)}.csv`);
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    showNotification('Gantt chart exported successfully!', 'success');
}

// Fit Gantt chart to show all tasks optimally
function fitGanttToTasks() {
    const tasks = getFilteredGanttTasks();

    if (tasks.length === 0) {
        showNotification('No tasks to fit', 'info');
        return;
    }

    const container = document.querySelector('.gantt-container-new');
    if (!container) {
        console.error('Gantt container not found');
        return;
    }

    // Reset horizontal scroll to beginning
    container.scrollLeft = 0;

    // Calculate optimal view based on task time span
    const startDates = tasks.map(t => t.startDate);
    const endDates = tasks.map(t => t.endDate);
    const minStart = new Date(Math.min(...startDates));
    const maxEnd = new Date(Math.max(...endDates));

    const totalTimeSpan = maxEnd - minStart;
    const spanDays = totalTimeSpan / (1000 * 60 * 60 * 24);
    const spanHours = totalTimeSpan / (1000 * 60 * 60);

    // Suggest optimal time scale based on span
    let suggestedScale = customGanttViewMode;
    const viewModeSelect = document.getElementById('ganttViewMode');

    if (spanHours <= 4) {
        suggestedScale = '15min';
    } else if (spanHours <= 12) {
        suggestedScale = '30min';
    } else if (spanHours <= 48) {
        suggestedScale = '1hour';
    } else if (spanDays <= 3) {
        suggestedScale = '4hour';
    } else if (spanDays <= 7) {
        suggestedScale = '8hour';
    } else if (spanDays <= 31) {
        suggestedScale = '1day';
    } else if (spanDays <= 90) {
        suggestedScale = '1week';
    } else if (spanDays <= 180) {
        suggestedScale = '2weeks';
    } else {
        suggestedScale = '1month';
    }

    // Update view mode if it would improve visibility
    if (suggestedScale !== customGanttViewMode) {
        const shouldSwitch = confirm(
            `Current time span is ${spanDays.toFixed(1)} days. ` +
            `Switch from ${customGanttViewMode} to ${suggestedScale} view for better fit?`
        );

        if (shouldSwitch && viewModeSelect) {
            viewModeSelect.value = suggestedScale;
            customGanttViewMode = suggestedScale;
            renderCustomGanttChart();
        }
    }

    // Smooth scroll to show first task
    setTimeout(() => {
        const firstTaskBar = container.querySelector('[style*="position: absolute"]');
        if (firstTaskBar) {
            const rect = firstTaskBar.getBoundingClientRect();
            const containerRect = container.getBoundingClientRect();

            if (rect.left < containerRect.left || rect.right > containerRect.right) {
                firstTaskBar.scrollIntoView({
                    behavior: 'smooth',
                    block: 'nearest',
                    inline: 'start'
                });
            }
        }
    }, 100);

    showNotification(`Fitted to ${tasks.length} tasks (${spanDays.toFixed(1)} day span)`, 'success');
}

// Format date labels (separate from time labels)
// Fixed date label formatting with better debugging
function formatGanttDateLabel(date, timeScale) {
    let label;
    switch (timeScale) {
        case '15min':
        case '30min':
        case '1hour':
        case '4hour':
        case '8hour':
            // Show date for time-based scales
            label = date.toLocaleDateString('en-US', {
                month: 'short',
                day: 'numeric'
            });
            break;
        case '1day':
            label = date.toLocaleDateString('en-US', {
                month: 'short',
                day: 'numeric'
            });
            break;
        case '1week':
            label = `Week ${getWeekNumber(date)}`;
            break;
        case '2weeks':
            label = `W${getWeekNumber(date)}-${getWeekNumber(new Date(date.getTime() + 7 * 24 * 60 * 60 * 1000))}`;
            break;
        case '1month':
            label = date.toLocaleDateString('en-US', { month: 'short', year: '2-digit' });
            break;
        default:
            label = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    }

    console.log(`Date ${date.toISOString()} -> Label: ${label}`);
    return label;
}

// Determine which shift a time period belongs to
function getShiftForTime(date, timeScale) {
    // Only show shifts for time-based scales
    if (!['15min', '30min', '1hour', '4hour', '8hour'].includes(timeScale)) {
        return '-';
    }

    const hour = date.getHours();
    const minute = date.getMinutes();
    const timeDecimal = hour + (minute / 60);

    // Shift definitions based on your requirements:
    // 1st: 6:00 - 14:30 (6.0 - 14.5)
    // 2nd: 14:30 - 23:00 (14.5 - 23.0)
    // 3rd: 23:00 - 6:00 (23.0 - 24.0 and 0.0 - 6.0)

    if (timeDecimal >= 6.0 && timeDecimal < 14.5) {
        return '1st';
    } else if (timeDecimal >= 14.5 && timeDecimal < 23.0) {
        return '2nd';
    } else {
        return '3rd';
    }
}

// Get shift information
function getShiftInfo(shiftName) {
    const shifts = {
        '1st': { start: '6:00 AM', end: '2:30 PM', duration: '8.5 hours' },
        '2nd': { start: '2:30 PM', end: '11:00 PM', duration: '8.5 hours' },
        '3rd': { start: '11:00 PM', end: '6:00 AM', duration: '7 hours' }
    };
    return shifts[shiftName] || { start: '-', end: '-', duration: '-' };
}

// Task Feedback System - Add to dashboard-js.js

// Global feedback data storage
let taskFeedback = {};
let aircraftTasks = {}; // Cache for smart autocomplete

// Initialize feedback system
function initializeFeedbackSystem() {
    // Initialize feedback storage for current scenario if not exists
    if (!window.taskFeedback) {
        window.taskFeedback = {};
    }
    if (!window.taskFeedback[currentScenario]) {
        window.taskFeedback[currentScenario] = {};
    }

    // Initialize aircraft task cache for autocomplete
    if (!window.aircraftTasks) {
        window.aircraftTasks = {};
    }

    // Build aircraft task cache for smart autocomplete
    buildAircraftTaskCache();
}

// Build cache of tasks by aircraft for smart autocomplete
// Build cache of tasks by aircraft for smart autocomplete
function buildAircraftTaskCache() {
    window.aircraftTasks = {};

    if (scenarioData && scenarioData.tasks) {
        scenarioData.tasks.forEach(task => {
            const product = task.product;
            if (!window.aircraftTasks[product]) {
                window.aircraftTasks[product] = [];
            }
            window.aircraftTasks[product].push({
                taskId: task.taskId,
                type: task.type,
                team: task.team,
                startTime: task.startTime,
                dependencies: task.dependencies || []
            });
        });
    }

    console.log('Built aircraft task cache:', Object.keys(window.aircraftTasks).map(k => `${k}: ${window.aircraftTasks[k].length} tasks`));
}


// Enhanced Individual Mechanic View with feedback forms
function displayIndividualViewWithFeedback(mechanicSchedule, mechanicId) {
    const mechanicNameElement = document.getElementById('mechanicName');
    const timeline = document.getElementById('mechanicTimeline');

    if (!timeline) return;

    if (!mechanicSchedule) {
        if (mechanicNameElement) {
            mechanicNameElement.textContent = 'Task Schedule';
        }
        timeline.innerHTML = `
            <div style="padding: 20px; text-align: center; color: #6b7280;">
                <div style="font-size: 48px; margin-bottom: 10px;">📋</div>
                <div style="font-size: 16px; font-weight: 500;">No Tasks Assigned</div>
                <div style="font-size: 14px; margin-top: 5px;">Use the Team Lead view to assign tasks</div>
            </div>
        `;
        return;
    }

    const mechanicTasks = mechanicSchedule.tasks || [];

    // Update header
    if (mechanicNameElement) {
        mechanicNameElement.textContent =
            `Task Schedule for ${mechanicSchedule.displayName || mechanicId}`;
    }

    // Build enhanced timeline with feedback forms
    timeline.innerHTML = '';

    if (mechanicTasks.length === 0) {
        timeline.innerHTML = `
            <div style="padding: 20px; text-align: center; color: #6b7280;">
                <div style="font-size: 48px; margin-bottom: 10px;">📋</div>
                <div style="font-size: 16px; font-weight: 500;">No Tasks Assigned</div>
                <div style="font-size: 14px; margin-top: 5px;">Use the Team Lead view to assign tasks</div>
            </div>
        `;
        return;
    }

    // Add feedback summary header
    const feedbackSummary = document.createElement('div');
    feedbackSummary.style.cssText = `
        background: #f0f9ff;
        border: 1px solid #3b82f6;
        border-radius: 8px;
        padding: 12px;
        margin-bottom: 20px;
    `;

    const completedFeedback = mechanicTasks.filter(task =>
        taskFeedback[currentScenario] &&
        taskFeedback[currentScenario][`${mechanicId}_${task.taskId}`]
    ).length;

    feedbackSummary.innerHTML = `
        <strong>Feedback Status:</strong> ${completedFeedback}/${mechanicTasks.length} tasks have feedback
        <button onclick="exportMechanicFeedback('${mechanicId}')"
                style="float: right; padding: 4px 8px; background: #3b82f6; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px;">
            Export My Feedback
        </button>
    `;
    timeline.appendChild(feedbackSummary);

    // Group tasks by date
    const tasksByDate = {};
    mechanicTasks.forEach(task => {
        const date = new Date(task.startTime).toDateString();
        if (!tasksByDate[date]) {
            tasksByDate[date] = [];
        }
        tasksByDate[date].push(task);
    });

    // Display tasks with feedback forms, including changeover indicators
    Object.entries(tasksByDate).forEach(([date, tasks]) => {
        const dateHeader = document.createElement('div');
        dateHeader.style.cssText = `
            background: #f3f4f6;
            padding: 8px 12px;
            font-weight: 600;
            color: #374151;
            margin: 10px 0 5px 0;
            border-radius: 6px;
        `;
        dateHeader.textContent = date;
        timeline.appendChild(dateHeader);

        // Sort tasks by start time within date to detect changeovers
        tasks.sort((a, b) => new Date(a.startTime) - new Date(b.startTime));

        tasks.forEach((task, index) => {
            // Insert changeover indicator when mechanic switches aircraft
            if (index > 0) {
                const prevTask = tasks[index - 1];
                const prevProduct = prevTask.product || prevTask.lineNumber || '';
                const currProduct = task.product || task.lineNumber || '';
                if (prevProduct && currProduct && prevProduct !== currProduct) {
                    const changeover = document.createElement('div');
                    changeover.className = 'changeover-indicator';
                    changeover.innerHTML = `
                        <div class="changeover-line"></div>
                        <div class="changeover-badge">
                            <span class="changeover-icon">&#x1F6B6;</span>
                            <span>15 min changeover &mdash; ${prevProduct} &#x2192; ${currProduct}</span>
                        </div>
                        <div class="changeover-line"></div>
                    `;
                    timeline.appendChild(changeover);
                }
            }
            const taskContainer = createTaskFeedbackItem(task, mechanicId);
            timeline.appendChild(taskContainer);
        });
    });
}

// Create individual task item with feedback form
// (First versions of autocomplete/selection functions removed - kept enhanced versions below)


function createPredecessorEntryHTML(feedbackKey, index, currentProduct, predecessorTask = '', notes = '') {
    const entryId = `${feedbackKey}-entry-${index}`;
    const inputId = `predecessor-input-${entryId}`;

    return `
        <div id="${entryId}" class="predecessor-entry" style="display: flex; gap: 10px; align-items: start; background: #f0f8ff; padding: 10px; border: 1px solid #d1e9ff; border-radius: 6px; margin-bottom: 8px;">
            <div class="predecessor-input-container" style="flex: 1; position: relative;">
                <label for="${inputId}" style="font-size: 11px; display: block; margin-bottom: 4px; font-weight: 500;">Predecessor Task ID (Optional)</label>
                <input type="text" id="${inputId}" class="predecessor-task-id predecessor-input"
                       oninput="handlePredecessorAutocomplete(this, '${currentProduct}')"
                       placeholder="e.g., A_12..." value="${predecessorTask}" style="width: 100%; padding: 6px; border: 1px solid #d1d5db; border-radius: 4px;">
                <div class="autocomplete-suggestions"></div>
            </div>
            <div style="flex: 2;">
                <label style="font-size: 11px; display: block; margin-bottom: 4px; font-weight: 500;">Notes (Required)</label>
                <textarea class="predecessor-notes" placeholder="Describe the issue or what you're waiting for..." style="width: 100%; height: 40px; padding: 6px; border: 1px solid #d1d5db; border-radius: 4px; resize: vertical;">${notes}</textarea>
            </div>
            <button type="button" onclick="this.parentElement.remove()" style="background: #ef4444; color: white; border: none; border-radius: 50%; width: 24px; height: 24px; cursor: pointer; font-size: 14px; line-height: 24px; margin-top: 22px; flex-shrink: 0;">
                &times;
            </button>
        </div>
    `;
}

function addPredecessorEntry(feedbackKey, currentProduct) {
    const listContainer = document.getElementById(`predecessor-list-${feedbackKey}`);
    if (listContainer) {
        const newIndex = listContainer.children.length;
        const newEntryHTML = createPredecessorEntryHTML(feedbackKey, newIndex, currentProduct);
        listContainer.insertAdjacentHTML('beforeend', newEntryHTML);
    }
}

// Create individual task item with feedback form
// Create individual task item with feedback form
function createTaskFeedbackItem(task, mechanicId) {
    const container = document.createElement('div');
    container.className = 'task-feedback-item';
    container.style.cssText = `
        border: 1px solid #e5e7eb;
        border-radius: 8px;
        margin-bottom: 15px;
        background: white;
        overflow: hidden;
    `;

    const startTime = new Date(task.startTime);
    const feedbackKey = `${mechanicId}_${task.taskId}`;
    const sanitizedKey = sanitizeForQuerySelector(feedbackKey);
    const existingFeedback = taskFeedback[currentScenario] && taskFeedback[currentScenario][feedbackKey];

    let borderColor = '#3b82f6';
    let typeIcon = '🔧';

    if (task.type === 'Quality Inspection') {
        borderColor = '#10b981';
        typeIcon = '✓';
    } else if (task.type === 'Late Part') {
        borderColor = '#f59e0b';
        typeIcon = '📦';
    } else if (task.type === 'Rework') {
        borderColor = '#ef4444';
        typeIcon = '🔄';
    } else if (task.isCustomerTask) {
        borderColor = '#8b5cf6';
        typeIcon = '👤';
    }

    container.innerHTML = `
        <div style="border-left: 4px solid ${borderColor}; padding: 15px;">
            <!-- Task Header -->
            <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px;">
                <div style="flex: 1;">
                    <div style="font-weight: 600; font-size: 14px; color: #1f2937; margin-bottom: 4px; display: flex; align-items: center; gap: 8px;">
                        <span>${typeIcon} Task ${task.taskId} - ${task.type}</span>
                        <button class="chain-btn" data-task-id="${task.taskId}" title="View Dependency Chain">⛓️</button>
                    </div>
                    <div style="color: #6b7280; font-size: 12px;">
                        📦 ${task.product} • ⏰ ${formatTime(startTime)} • ⌛ ${task.duration} minutes
                    </div>
                </div>
                <div style="text-align: right;">
                    ${existingFeedback ?
                        `<span style="background: #10b981; color: white; padding: 2px 6px; border-radius: 4px; font-size: 11px;">
                            Feedback Submitted
                        </span>` :
                        `<span style="background: #f59e0b; color: white; padding: 2px 6px; border-radius: 4px; font-size: 11px;">
                            Feedback Needed
                        </span>`
                    }
                </div>
            </div>

            <!-- Feedback Form -->
            <div id="feedback-form-${sanitizedKey}" style="background: #f9fafb; padding: 12px; border-radius: 6px; border: 1px solid #e5e7eb;">
                <div style="font-weight: 500; margin-bottom: 10px; color: #374151;">
                    Task Status & Feedback:
                </div>

                <!-- Status Selection -->
                <div style="margin-bottom: 12px;">
                    <label style="display: flex; align-items: center; margin-bottom: 6px; cursor: pointer;">
                        <input type="radio" name="status-${sanitizedKey}" value="completed"
                               ${!existingFeedback || existingFeedback.status === 'completed' ? 'checked' : ''}
                               onchange="toggleFeedbackFields('${sanitizedKey}')"
                               style="margin-right: 6px;">
                        <span style="color: #10b981; font-weight: 500;">✓ Completed On Time</span>
                    </label>
                    <label style="display: flex; align-items: center; cursor: pointer;">
                        <input type="radio" name="status-${sanitizedKey}" value="delayed"
                               ${existingFeedback && existingFeedback.status === 'delayed' ? 'checked' : ''}
                               onchange="toggleFeedbackFields('${sanitizedKey}')"
                               style="margin-right: 6px;">
                        <span style="color: #ef4444; font-weight: 500;">⚠️ Delayed or Had Issues</span>
                    </label>
                </div>

                <!-- Delay Reason Fields (shown only when delayed) -->
                <div id="delay-fields-${sanitizedKey}" style="display: ${existingFeedback && existingFeedback.status === 'delayed' ? 'block' : 'none'};">
                    <!-- Delay Reason -->
                    <div style="margin-bottom: 10px;">
                        <label style="display: block; font-weight: 500; margin-bottom: 4px; color: #374151;">
                            Reason for Delay:
                        </label>
                        <select id="reason-${sanitizedKey}" style="width: 100%; padding: 6px; border: 1px solid #d1d5db; border-radius: 4px;">
                            <option value="">Select Reason</option>
                            <option value="predecessor" ${existingFeedback?.reason === 'predecessor' ? 'selected' : ''}>
                                Held by Predecessor Task(s)
                            </option>
                            <option value="awaiting-quality" ${existingFeedback?.reason === 'awaiting-quality' ? 'selected' : ''}>
                                Awaiting Quality Inspection
                            </option>
                            <option value="awaiting-customer" ${existingFeedback?.reason === 'awaiting-customer' ? 'selected' : ''}>
                                Awaiting Customer Inspection
                            </option>
                            <option value="found-parts" ${existingFeedback?.reason === 'found-parts' ? 'selected' : ''}>
                                Searched for Parts but Found Them
                            </option>
                            <option value="missing-parts" ${existingFeedback?.reason === 'missing-parts' ? 'selected' : ''}>
                                Missing Parts/Had to Order Parts
                            </option>
                            <option value="caused-damage" ${existingFeedback?.reason === 'caused-damage' ? 'selected' : ''}>
                                Caused Damage/Need Rework Tag
                            </option>
                            <option value="missing-tooling" ${existingFeedback?.reason === 'missing-tooling' ? 'selected' : ''}>
                                Tooling Missing
                            </option>
                            <option value="other" ${existingFeedback?.reason === 'other' ? 'selected' : ''}>
                                Other (specify below)
                            </option>
                        </select>
                    </div>

                    <!-- Container for reason-specific fields -->
                    <div id="reason-details-container-${sanitizedKey}"></div>

                    <!-- General Notes and Delay Duration -->
                    <div style="margin-top: 15px; padding-top: 15px; border-top: 1px solid #e5e7eb;">
                        <label style="display: block; font-weight: 500; margin-bottom: 4px; color: #374151;">
                            General Notes for this Delay:
                        </label>
                        <textarea id="notes-${sanitizedKey}"
                                  placeholder="Additional details about the delay or issue..."
                                  style="width: 100%; padding: 6px; border: 1px solid #d1d5db; border-radius: 4px; resize: vertical; min-height: 60px;">${existingFeedback?.notes || ''}</textarea>
                    </div>
                    <div style="margin-top: 10px;">
                        <label style="display: block; font-weight: 500; margin-bottom: 4px; color: #374151;">
                            Estimated Total Delay (minutes):
                        </label>
                        <input type="number"
                               id="delay-${sanitizedKey}"
                               placeholder="e.g., 30"
                               value="${existingFeedback?.delayMinutes || ''}"
                               style="width: 100px; padding: 6px; border: 1px solid #d1d5db; border-radius: 4px;">
                    </div>
                </div>

                <!-- Action Buttons -->
                <div style="margin-top: 12px; display: flex; gap: 8px;">
                    <button onclick="saveFeedback('${feedbackKey}', '${task.taskId}', '${mechanicId}')"
                            style="background: #10b981; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 12px;">
                        Save Feedback
                    </button>
                    ${existingFeedback ?
                        `<button type="button" onclick="clearFeedback('${feedbackKey}')"
                                style="background: #6b7280; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 12px;">
                            Clear Feedback
                        </button>` : ''
                    }
                </div>
            </div>
        </div>
    `;

    setTimeout(() => setupReasonDropdownHandler(sanitizedKey, task.product), 100);

    return container;
}


function setupReasonDropdownHandler(sanitizedKey, currentProduct) {
    const reasonSelect = document.getElementById(`reason-${sanitizedKey}`);
    const detailsContainer = document.getElementById(`reason-details-container-${sanitizedKey}`);

    const handleReasonChange = () => {
        if (!reasonSelect || !detailsContainer) return;

        if (reasonSelect.value === 'predecessor') {
            // Inject the HTML for the predecessor list
            detailsContainer.innerHTML = `
                <div id="predecessor-container-${sanitizedKey}" style="margin-top: 10px;">
                    <div id="predecessor-list-${sanitizedKey}">
                        ${createPredecessorEntryHTML(sanitizedKey, 0, currentProduct)}
                    </div>
                    <button type="button" onclick="addPredecessorEntry('${sanitizedKey}', '${currentProduct}')" style="margin-top: 8px; background: #3b82f6; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 12px;">
                        + Add Another Predecessor
                    </button>
                </div>
            `;
        } else {
            detailsContainer.innerHTML = ''; // Clear it for other reasons
        }
    };

    if (reasonSelect) {
        reasonSelect.addEventListener('change', handleReasonChange);
        // Trigger initial state to show/hide fields based on pre-selected value
        handleReasonChange();
    }
}

// Toggle feedback fields based on status
function toggleFeedbackFields(feedbackKey) {
    const delayFields = document.getElementById(`delay-fields-${feedbackKey}`);
    const delayedRadio = document.querySelector(`input[name="status-${feedbackKey}"][value="delayed"]`);

    if (delayFields) {
        delayFields.style.display = delayedRadio.checked ? 'block' : 'none';
    }
}

// Handle predecessor task autocomplete
// 2. Enhanced autocomplete function with better positioning
function handlePredecessorAutocomplete(input, currentProduct) {
    const query = input.value.toLowerCase().trim();
    const feedbackKey = input.id.replace('predecessor-', '');
    let suggestionsContainer = document.getElementById(`predecessor-suggestions-${feedbackKey}`);

    // Create suggestions container if it doesn't exist
    if (!suggestionsContainer) {
        suggestionsContainer = document.createElement('div');
        suggestionsContainer.id = `predecessor-suggestions-${feedbackKey}`;
        suggestionsContainer.className = 'autocomplete-suggestions';
        input.parentNode.appendChild(suggestionsContainer);
    }

    if (query.length < 2) {
        suggestionsContainer.innerHTML = '';
        suggestionsContainer.style.display = 'none';
        return;
    }

    // Get relevant tasks from the same aircraft/product
    const relevantTasks = window.aircraftTasks[currentProduct] || [];

    console.log(`Searching for "${query}" in ${relevantTasks.length} tasks for ${currentProduct}`);

    // Enhanced matching: prioritize partial matches anywhere in task ID
    const matches = relevantTasks
        .filter(task => {
            const taskId = task.taskId.toLowerCase();
            const taskType = task.type.toLowerCase();
            const taskTeam = (task.team || '').toLowerCase();

            // Match anywhere in task ID (most common)
            if (taskId.includes(query)) return true;

            // Match task type
            if (taskType.includes(query)) return true;

            // Match team name
            if (taskTeam.includes(query)) return true;

            // Special handling for numeric queries (common pattern)
            if (query.match(/^\d+$/)) {
                // Split task ID by common separators and check each part
                const parts = taskId.split(/[_\-\s]+/);
                return parts.some(part => part.includes(query));
            }

            return false;
        })
        .sort((a, b) => {
            const taskIdA = a.taskId.toLowerCase();
            const taskIdB = b.taskId.toLowerCase();

            // Priority 1: Exact substring match in task ID
            const aTaskMatch = taskIdA.indexOf(query);
            const bTaskMatch = taskIdB.indexOf(query);

            if (aTaskMatch !== -1 && bTaskMatch === -1) return -1;
            if (aTaskMatch === -1 && bTaskMatch !== -1) return 1;
            if (aTaskMatch !== -1 && bTaskMatch !== -1) {
                // Prefer matches earlier in the string
                if (aTaskMatch !== bTaskMatch) return aTaskMatch - bTaskMatch;
            }

            // Priority 2: Earlier start times (more likely predecessors)
            return new Date(a.startTime) - new Date(b.startTime);
        })
        .slice(0, 8); // Limit to 8 suggestions

    if (matches.length === 0) {
        suggestionsContainer.innerHTML = `
            <div style="padding: 8px; color: #6b7280; font-size: 12px; background: white; border: 1px solid #e5e7eb;">
                <strong>No matches found for "${query}"</strong>
                <div style="font-size: 11px; margin-top: 4px; color: #9ca3af;">
                    Try typing:
                    <br>• Task ID numbers (e.g., "401", "25")
                    <br>• Task type (e.g., "production", "quality")
                    <br>• Partial task names
                </div>
            </div>
        `;
        suggestionsContainer.style.display = 'block';
        return;
    }

    console.log(`Found ${matches.length} matches for "${query}"`);

    const suggestionsHTML = matches.map(task => {
        const startDate = new Date(task.startTime);
        const taskIdMatch = task.taskId.toLowerCase().indexOf(query);

        // Highlight the matching part
        let displayTaskId = task.taskId;
        if (taskIdMatch !== -1) {
            const before = task.taskId.substring(0, taskIdMatch);
            const match = task.taskId.substring(taskIdMatch, taskIdMatch + query.length);
            const after = task.taskId.substring(taskIdMatch + query.length);
            displayTaskId = `${before}<mark style="background: #fef3c7; padding: 1px 2px;">${match}</mark>${after}`;
        }

        return `
            <div class="autocomplete-item"
                 onclick="selectPredecessorTask('${feedbackKey}', '${task.taskId.replace(/'/g, "\\'")}')"
                 style="padding: 8px; cursor: pointer; border-bottom: 1px solid #f3f4f6; font-size: 12px; background: white; transition: background 0.2s;">
                <div style="font-weight: 600; margin-bottom: 2px;">
                    ${displayTaskId} - ${task.type}
                </div>
                <div style="color: #6b7280; font-size: 11px; display: flex; gap: 8px;">
                    <span>📋 ${task.team}</span>
                    <span>📅 ${startDate.toLocaleDateString()}</span>
                    <span>⏰ ${startDate.toLocaleTimeString('en-US', {hour: '2-digit', minute: '2-digit'})}</span>
                </div>
            </div>
        `;
    }).join('');

    suggestionsContainer.innerHTML = suggestionsHTML;

    // Position the suggestions container
    const inputRect = input.getBoundingClientRect();
    suggestionsContainer.style.cssText = `
        display: block;
        position: absolute;
        background: white;
        border: 1px solid #d1d5db;
        border-radius: 6px;
        box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -1px rgb(0 0 0 / 0.06);
        z-index: 1000;
        max-height: 300px;
        overflow-y: auto;
        width: ${Math.max(input.offsetWidth, 350)}px;
        margin-top: 2px;
        left: 0;
        top: 100%;
    `;

    // Add hover effects
    suggestionsContainer.addEventListener('mouseover', function(e) {
        if (e.target.classList.contains('autocomplete-item')) {
            e.target.style.background = '#f3f4f6';
        }
    });

    suggestionsContainer.addEventListener('mouseout', function(e) {
        if (e.target.classList.contains('autocomplete-item')) {
            e.target.style.background = 'white';
        }
    });
}

// Select predecessor task from autocomplete
// 3. Improved task selection function
function selectPredecessorTask(feedbackKey, taskId) {
    const input = document.getElementById(`predecessor-${feedbackKey}`);
    const suggestionsContainer = document.getElementById(`predecessor-suggestions-${feedbackKey}`);

    if (input) {
        input.value = taskId;
        // Add visual feedback
        input.style.background = '#f0fdf4';
        input.style.borderColor = '#10b981';
        setTimeout(() => {
            input.style.background = '';
            input.style.borderColor = '#d1d5db';
        }, 1500);
    }

    if (suggestionsContainer) {
        suggestionsContainer.style.display = 'none';
    }

    console.log(`Selected predecessor task: ${taskId}`);
}

// 4. Close suggestions when clicking outside
document.addEventListener('click', function(e) {
    const suggestions = document.querySelectorAll('.autocomplete-suggestions');
    suggestions.forEach(container => {
        if (!container.contains(e.target) && !e.target.classList.contains('predecessor-input')) {
            container.style.display = 'none';
        }
    });
});



// Show/hide predecessor field based on reason selection
function setupReasonChangeHandler(feedbackKey) {
    const reasonSelect = document.getElementById(`reason-${feedbackKey}`);
    const predecessorField = document.getElementById(`predecessor-field-${feedbackKey}`);

    if (reasonSelect && predecessorField) {
        reasonSelect.addEventListener('change', function() {
            predecessorField.style.display = this.value === 'predecessor' ? 'block' : 'none';
        });
    }
}

// Save feedback for a specific task
function saveFeedback(feedbackKey, taskId, mechanicId) {
    const sanitizedKey = sanitizeForQuerySelector(feedbackKey);
    console.log('[DEBUG] saveFeedback called with sanitizedKey:', sanitizedKey);

    const statusRadios = document.querySelectorAll(`input[name="status-${sanitizedKey}"]`);
    const reasonSelect = document.getElementById(`reason-${sanitizedKey}`);
    const generalNotesInput = document.getElementById(`notes-${sanitizedKey}`);
    const delayInput = document.getElementById(`delay-${sanitizedKey}`);

    let status = 'completed';
    for (const radio of statusRadios) {
        if (radio.checked) {
            status = radio.value;
            break;
        }
    }

    const feedbackData = {
        taskId: taskId,
        mechanicId: mechanicId,
        status: status,
        timestamp: new Date().toISOString(),
        scenario: currentScenario,
        mechanicName: getMechanicDisplayName(mechanicId)
    };

    if (status === 'delayed') {
        const reason = reasonSelect ? reasonSelect.value : '';
        if (!reason) {
            alert('Please select a reason for the delay.');
            reasonSelect.focus();
            return;
        }

        feedbackData.reason = reason;
        feedbackData.reasonText = getReasonDisplayText(reason);
        feedbackData.notes = generalNotesInput ? generalNotesInput.value.trim() : '';
        feedbackData.delayMinutes = delayInput ? parseInt(delayInput.value) || 0 : 0;

        if (reason === 'predecessor') {
            const selector = `#predecessor-list-${sanitizedKey} .predecessor-entry`;
            console.log('[DEBUG] Querying for predecessor entries with selector:', selector);
            const predecessorEntries = document.querySelectorAll(selector);
            console.log('[DEBUG] Found predecessor entries:', predecessorEntries);
            const predecessors = [];

            if (predecessorEntries.length === 0) {
                alert('Please add at least one predecessor entry for the selected reason.');
                return;
            }

            for (const entry of predecessorEntries) {
                const predecessorTaskInput = entry.querySelector('.predecessor-task-id');
                const notesInput = entry.querySelector('.predecessor-notes');
                const predecessorTask = predecessorTaskInput.value.trim();
                const notes = notesInput.value.trim();

                if (!notes) {
                    alert('Notes are required for each predecessor entry to provide justification.');
                    notesInput.focus();
                    return;
                }
                predecessors.push({ predecessorTask, notes });
            }
            feedbackData.predecessors = predecessors;
        }
    }

    // Save feedback object locally using the original key
    if (!window.taskFeedback[currentScenario]) {
        window.taskFeedback[currentScenario] = {};
    }
    window.taskFeedback[currentScenario][feedbackKey] = feedbackData;
    try {
        localStorage.setItem(`taskFeedback_${currentScenario}`, JSON.stringify(window.taskFeedback[currentScenario]));
    } catch (e) {
        console.warn('Could not save feedback to localStorage:', e);
    }

    // Flag tasks for IE review if the status is 'delayed'
    if (status === 'delayed') {
        const task = scenarioData.tasks.find(t => t.taskId === taskId);
        const priority = task ? task.priority : 999;

        const payload = {
            taskId: taskId,
            priority: priority,
            scenario: currentScenario,
            reason: feedbackData.reason,
            generalNotes: feedbackData.notes, // 'notes' in feedbackData is the general notes
            predecessors: feedbackData.predecessors || [],
            delayMinutes: feedbackData.delayMinutes,
            mechanicName: feedbackData.mechanicName
        };

        console.log('[DEBUG] Flagging task with consolidated payload:', payload);

        fetch('/api/ie/flag_task', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification(data.message, 'info');
            } else {
                showNotification(`Error flagging task: ${data.error}`, 'error');
            }
        })
        .catch(err => {
            console.error('Error flagging task:', err);
            showNotification(`Error flagging task: ${err.message}`, 'error');
        });
    }

    // Visual feedback for saving
    showNotification('Feedback saved successfully!', 'success');
    const form = document.getElementById(`feedback-form-${sanitizedKey}`);
    if (form) {
        const statusSpan = form.parentElement.querySelector('span[style*="background"]');
        if (statusSpan) {
            statusSpan.style.background = '#10b981';
            statusSpan.textContent = 'Feedback Submitted';
        }
    }
    updateFeedbackSummary();
    console.log('Saved feedback:', feedbackData);
}

function getMechanicDisplayName(mechanicId) {
    // Use on-the-fly generated schedules instead of deprecated mechanicSchedules
    const assignments = savedAssignments[currentScenario] || {};
    const schedules = generateMechanicSchedulesFromAssignments(assignments);
    if (schedules[mechanicId] && schedules[mechanicId].displayName) {
        return schedules[mechanicId].displayName;
    }
    // Fallback: parse display name from mechanicId format "TEAM S{N} (SKILL)_N"
    const parts = mechanicId.split('_');
    const position = parseInt(parts[parts.length - 1], 10) || 1;
    const teamSkill = parts.slice(0, -1).join('_');
    const parsed = parseTeamSkill(teamSkill);
    return buildResourceLabel(parsed.baseTeam, position, parsed.shift, parsed.skill);
}

// Get display text for delay reasons
function getReasonDisplayText(reason) {
    const reasonMap = {
        'predecessor': 'Held by Predecessor Task',
        'awaiting-quality': 'Awaiting Quality Inspection',
        'awaiting-customer': 'Awaiting Customer Inspection',
        'found-parts': 'Searched for Parts but Found Them',
        'missing-parts': 'Missing Parts/Had to Order Parts',
        'caused-damage': 'Caused Damage/Need Rework Tag',
        'missing-tooling': 'Tooling Missing',
        'other': 'Other'
    };
    return reasonMap[reason] || reason;
}

// Update feedback summary at top of timeline
function updateFeedbackSummary() {
    const timeline = document.getElementById('mechanicTimeline');
    const summaryDiv = timeline.querySelector('.feedback-summary');

    if (summaryDiv) {
        const mechanicSelect = document.getElementById('mechanicSelect');
        const currentMechanic = mechanicSelect ? mechanicSelect.value : null;

        if (currentMechanic && !['all', 'all-mechanics', 'all-quality', 'all-customer', 'all-vendor', 'none'].includes(currentMechanic)) {
            const fbAssignments = savedAssignments[currentScenario] || {};
            const fbSchedules = generateMechanicSchedulesFromAssignments(fbAssignments);
            const schedule = fbSchedules[currentMechanic];
            const tasks = schedule ? schedule.tasks : [];

            const completedFeedback = tasks.filter(task => {
                const feedbackKey = `${currentMechanic}_${task.taskId}`;
                return window.taskFeedback[currentScenario] && window.taskFeedback[currentScenario][feedbackKey];
            }).length;

            const delayedTasks = tasks.filter(task => {
                const feedbackKey = `${currentMechanic}_${task.taskId}`;
                const feedback = window.taskFeedback[currentScenario] && window.taskFeedback[currentScenario][feedbackKey];
                return feedback && feedback.status === 'delayed';
            }).length;

            summaryDiv.innerHTML = `
                <strong>Feedback Status:</strong> ${completedFeedback}/${tasks.length} tasks have feedback
                ${delayedTasks > 0 ? ` • <span style="color: #ef4444;">${delayedTasks} delays reported</span>` : ''}
                <button onclick="exportMechanicFeedback('${currentMechanic}')"
                        style="float: right; padding: 4px 8px; background: #3b82f6; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px;">
                    Export My Feedback
                </button>
            `;
        }
    }
}

function handleReasonChange(feedbackKey) {
    const reasonSelect = document.getElementById(`reason-${feedbackKey}`);
    const predecessorField = document.getElementById(`predecessor-field-${feedbackKey}`);

    if (reasonSelect && predecessorField) {
        if (reasonSelect.value === 'predecessor') {
            predecessorField.style.display = 'block';
            // Focus the input field
            const input = document.getElementById(`predecessor-${feedbackKey}`);
            if (input) {
                setTimeout(() => input.focus(), 100);
            }
        } else {
            predecessorField.style.display = 'none';
        }
    }
}

// Load feedback from localStorage on page load
function loadSavedFeedback() {
    try {
        const saved = localStorage.getItem(`taskFeedback_${currentScenario}`);
        if (saved) {
            if (!window.taskFeedback) window.taskFeedback = {};
            window.taskFeedback[currentScenario] = JSON.parse(saved);
            console.log(`Loaded ${Object.keys(window.taskFeedback[currentScenario]).length} feedback entries for ${currentScenario}`);
        }
    } catch (e) {
        console.warn('Could not load feedback from localStorage:', e);
    }
}



// Clear feedback for a task
function clearFeedback(feedbackKey) {
    if (confirm('Are you sure you want to clear this feedback?')) {
        const sanitizedKey = sanitizeForQuerySelector(feedbackKey);

        // Delete data using original key
        if (taskFeedback[currentScenario]) {
            delete taskFeedback[currentScenario][feedbackKey];
        }

        // Reset form using sanitized key
        const statusRadios = document.querySelectorAll(`input[name="status-${sanitizedKey}"]`);
        if (statusRadios[0]) statusRadios[0].checked = true;

        const reasonSelect = document.getElementById(`reason-${sanitizedKey}`);
        if (reasonSelect) reasonSelect.value = '';

        const notesInput = document.getElementById(`notes-${sanitizedKey}`);
        if (notesInput) notesInput.value = '';

        const delayInput = document.getElementById(`delay-${sanitizedKey}`);
        if (delayInput) delayInput.value = '';

        // Manually trigger reason change to clear predecessor list
        if(reasonSelect) reasonSelect.dispatchEvent(new Event('change'));

        // Hide delay fields
        toggleFeedbackFields(sanitizedKey);

        showNotification('Feedback cleared', 'info');
    }
}

// Export individual mechanic's feedback
// Enhanced export function with better formatting
function exportMechanicFeedback(mechanicId) {
    const expAssignments = savedAssignments[currentScenario] || {};
    const expSchedules = generateMechanicSchedulesFromAssignments(expAssignments);
    const mechanicSchedule = expSchedules[mechanicId];
    if (!mechanicSchedule) {
        alert('No schedule found for this mechanic');
        return;
    }

    const tasks = mechanicSchedule.tasks || [];
    const feedbackData = window.taskFeedback[currentScenario] || {};

    let csvContent = `Individual Mechanic Task Feedback Report\n`;
    csvContent += `=".join('=', repeat=50}\n`;
    csvContent += `Mechanic: ${mechanicSchedule.displayName || mechanicId}\n`;
    csvContent += `Team: ${mechanicSchedule.team || 'Unknown'} (${mechanicSchedule.skill || 'No Skill'})\n`;
    csvContent += `Scenario: ${currentScenario.toUpperCase()}\n`;
    csvContent += `Report Generated: ${new Date().toLocaleString()}\n`;
    csvContent += `Total Assigned Tasks: ${tasks.length}\n\n`;

    // Summary statistics
    const completedTasks = tasks.filter(task => {
        const feedback = feedbackData[`${mechanicId}_${task.taskId}`];
        return feedback && feedback.status === 'completed';
    }).length;

    const delayedTasks = tasks.filter(task => {
        const feedback = feedbackData[`${mechanicId}_${task.taskId}`];
        return feedback && feedback.status === 'delayed';
    }).length;

    const noFeedbackTasks = tasks.length - completedTasks - delayedTasks;

    const totalDelayMinutes = tasks.reduce((sum, task) => {
        const feedback = feedbackData[`${mechanicId}_${task.taskId}`];
        return sum + (feedback?.delayMinutes || 0);
    }, 0);

    csvContent += `PERFORMANCE SUMMARY:\n`;
    csvContent += `Completed On Time: ${completedTasks} (${(completedTasks/tasks.length*100).toFixed(1)}%)\n`;
    csvContent += `Delayed/Issues: ${delayedTasks} (${(delayedTasks/tasks.length*100).toFixed(1)}%)\n`;
    csvContent += `No Feedback: ${noFeedbackTasks} (${(noFeedbackTasks/tasks.length*100).toFixed(1)}%)\n`;
    csvContent += `Total Delay Time: ${totalDelayMinutes} minutes (${(totalDelayMinutes/60).toFixed(1)} hours)\n\n`;

    // Detailed task data
    csvContent += `DETAILED TASK FEEDBACK:\n`;
    csvContent += `Task ID,Type,Product,Scheduled Start,Duration (min),Status,Delay Reason,Predecessor Task,Delay Duration (min),Notes,Feedback Submitted\n`;

    tasks
        .sort((a, b) => new Date(a.startTime) - new Date(b.startTime))
        .forEach(task => {
            const feedbackKey = `${mechanicId}_${task.taskId}`;
            const feedback = feedbackData[feedbackKey];

            const status = feedback ? feedback.status : 'No Feedback';
            const reason = feedback?.reasonText || '';
            const predecessorTask = feedback?.predecessorTask || '';
            const delayMinutes = feedback?.delayMinutes || '';
            const notes = (feedback?.notes || '').replace(/"/g, '""'); // Escape quotes
            const feedbackDate = feedback ? new Date(feedback.timestamp).toLocaleString() : '';

            csvContent += `"${task.taskId}","${task.type}","${task.product}","${new Date(task.startTime).toLocaleString()}","${task.duration}","${status}","${reason}","${predecessorTask}","${delayMinutes}","${notes}","${feedbackDate}"\n`;
        });

    // Delay analysis by reason
    const delayReasons = {};
    tasks.forEach(task => {
        const feedback = feedbackData[`${mechanicId}_${task.taskId}`];
        if (feedback && feedback.status === 'delayed' && feedback.reasonText) {
            delayReasons[feedback.reasonText] = (delayReasons[feedback.reasonText] || 0) + 1;
        }
    });

    if (Object.keys(delayReasons).length > 0) {
        csvContent += `\nDELAY REASONS BREAKDOWN:\n`;
        Object.entries(delayReasons)
            .sort(([,a], [,b]) => b - a)
            .forEach(([reason, count]) => {
                csvContent += `${reason}: ${count} occurrence${count !== 1 ? 's' : ''}\n`;
            });
    }

    // Download the CSV
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    const url = URL.createObjectURL(blob);
    link.setAttribute('href', url);

    const mechanicName = (mechanicSchedule.displayName || mechanicId).replace(/[^a-zA-Z0-9]/g, '_');
    const dateStr = new Date().toISOString().slice(0, 10);
    link.setAttribute('download', `mechanic_feedback_${mechanicName}_${currentScenario}_${dateStr}.csv`);

    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    showNotification(`Feedback report exported for ${mechanicSchedule.displayName}`, 'success');
}


// Add this function to populate the mechanic dropdown
// Cache for the full mechanic hierarchy (rebuilt on scenario change)
let _mechanicHierarchyCache = null;
let _mechanicHierarchyScenario = null;

/**
 * Build a hierarchical structure of all workers from teamCapacities,
 * grouped by category (mechanic/quality/customer), superintendent, and team.
 */
function _buildMechanicHierarchy() {
    if (_mechanicHierarchyScenario === currentScenario && _mechanicHierarchyCache) {
        return _mechanicHierarchyCache;
    }

    const teamCapacities = scenarioData.teamCapacities || {};
    const baseTeams = new Map();

    // Aggregate capacities per base team
    Object.entries(teamCapacities).forEach(([teamSkill, capacity]) => {
        const parsed = parseTeamSkill(teamSkill);
        const baseTeam = parsed.baseTeam;
        const skill = parsed.skill;

        if (!baseTeams.has(baseTeam)) {
            baseTeams.set(baseTeam, { totalCapacity: 0, skills: new Map() });
        }
        const entry = baseTeams.get(baseTeam);
        entry.totalCapacity += capacity;
        if (!entry.skills.has(teamSkill)) {
            entry.skills.set(teamSkill, { skill: skill, capacity: capacity });
        }
    });

    // Build team-to-superintendent mapping from tasks
    const teamToSuperintendent = new Map();
    if (scenarioData.tasks && scenarioData.tasks.length > 0) {
        scenarioData.tasks.forEach(task => {
            const taskTeam = task.team || '';
            const taskSuper = task.superintendent || '';
            if (taskTeam && taskSuper && taskSuper !== 'UNKNOWN') {
                teamToSuperintendent.set(taskTeam, taskSuper);
            }
        });
    }

    const mechanicsBySuper = new Map();
    const qualityBySuper = new Map();
    const customerTeams = new Map();
    const vendorTeams = new Map();

    baseTeams.forEach((data, team) => {
        const isQuality = isQualityTeam(team);
        const isCustomer = isCustomerTeam(team);
        const isVendor = isVendorTeam(team);

        if (isCustomer) {
            customerTeams.set(team, data);
        } else if (isVendor) {
            vendorTeams.set(team, data);
        } else {
            // Determine superintendent
            let superintendent = teamToSuperintendent.get(team);
            if (!superintendent) {
                const superMatch = team.match(/^(P\d{2}|POSITION\s*\d+)/i);
                if (superMatch) {
                    const prefix = superMatch[1].toUpperCase();
                    if (prefix.startsWith('P') && prefix.length >= 2) {
                        const posNum = parseInt(prefix.substring(1, 2));
                        superintendent = `S-CF-POSITION ${posNum}`;
                    }
                }
            }
            if (!superintendent) superintendent = 'Other';

            const targetMap = isQuality ? qualityBySuper : mechanicsBySuper;
            if (!targetMap.has(superintendent)) {
                targetMap.set(superintendent, []);
            }
            targetMap.get(superintendent).push({ name: team, capacity: data.totalCapacity, skills: data.skills });
        }
    });

    // Sort
    const sortSupers = (map) => {
        const sorted = new Map([...map.entries()].sort((a, b) => a[0].localeCompare(b[0])));
        sorted.forEach(teams => teams.sort((a, b) => a.name.localeCompare(b.name)));
        return sorted;
    };

    _mechanicHierarchyCache = {
        mechanics: sortSupers(mechanicsBySuper),
        quality: sortSupers(qualityBySuper),
        customers: new Map([...customerTeams.entries()].sort((a, b) => a[0].localeCompare(b[0]))),
        vendors: new Map([...vendorTeams.entries()].sort((a, b) => a[0].localeCompare(b[0])))
    };
    _mechanicHierarchyScenario = currentScenario;
    return _mechanicHierarchyCache;
}

/**
 * Populate the Team, Skill, Shift filter dropdowns from teamCapacities.
 */
function populateMechanicFilterDropdowns() {
    const teamCapacities = scenarioData?.teamCapacities || {};
    const teams = new Set();
    const skills = new Set();

    Object.entries(teamCapacities).forEach(([teamSkill, capacity]) => {
        const parsed = parseTeamSkill(teamSkill);
        const baseTeam = parsed.baseTeam;
        const skill = parsed.skill;
        teams.add(baseTeam);
        if (skill) skills.add(skill);
    });

    const teamFilter = document.getElementById('mechFilterTeam');
    if (teamFilter) {
        const currentVal = teamFilter.value;
        teamFilter.innerHTML = '<option value="all">All Teams</option>';
        [...teams].sort().forEach(t => {
            const opt = document.createElement('option');
            opt.value = t;
            opt.textContent = t;
            teamFilter.appendChild(opt);
        });
        if (currentVal && Array.from(teamFilter.options).some(o => o.value === currentVal)) {
            teamFilter.value = currentVal;
        }
        teamFilter.removeEventListener('change', applyMechanicFilters);
        teamFilter.addEventListener('change', applyMechanicFilters);
    }

    const skillFilter = document.getElementById('mechFilterSkill');
    if (skillFilter) {
        const currentVal = skillFilter.value;
        skillFilter.innerHTML = '<option value="all">All Skills</option>';
        [...skills].sort().forEach(s => {
            const opt = document.createElement('option');
            opt.value = s;
            opt.textContent = s;
            skillFilter.appendChild(opt);
        });
        if (currentVal && Array.from(skillFilter.options).some(o => o.value === currentVal)) {
            skillFilter.value = currentVal;
        }
        skillFilter.removeEventListener('change', applyMechanicFilters);
        skillFilter.addEventListener('change', applyMechanicFilters);
    }

    const shiftFilter = document.getElementById('mechFilterShift');
    if (shiftFilter) {
        const currentVal = shiftFilter.value;
        shiftFilter.innerHTML = '<option value="all">All Shifts</option>';
        ['1', '2', '3'].forEach(s => {
            const opt = document.createElement('option');
            opt.value = s;
            opt.textContent = `Shift ${s}`;
            shiftFilter.appendChild(opt);
        });
        if (currentVal && Array.from(shiftFilter.options).some(o => o.value === currentVal)) {
            shiftFilter.value = currentVal;
        }
        shiftFilter.removeEventListener('change', applyMechanicFilters);
        shiftFilter.addEventListener('change', applyMechanicFilters);
    }

    const searchInput = document.getElementById('mechFilterSearch');
    if (searchInput) {
        searchInput.removeEventListener('input', applyMechanicFilters);
        searchInput.addEventListener('input', applyMechanicFilters);
    }
}

/**
 * Called on filter change / search input; rebuilds the mechanic dropdown
 * showing only matching options.
 */
function applyMechanicFilters() {
    const teamFilter = document.getElementById('mechFilterTeam')?.value || 'all';
    const skillFilter = document.getElementById('mechFilterSkill')?.value || 'all';
    const shiftFilter = document.getElementById('mechFilterShift')?.value || 'all';
    const searchTerm = (document.getElementById('mechFilterSearch')?.value || '').toLowerCase().trim();

    // Re-populate dropdown with filters applied
    populateMechanicDropdown(teamFilter, skillFilter, shiftFilter, searchTerm);
}

function populateMechanicDropdown(filterTeam, filterSkill, filterShift, filterSearch) {
    filterTeam = filterTeam || 'all';
    filterSkill = filterSkill || 'all';
    filterShift = filterShift || 'all';
    filterSearch = filterSearch || '';

    const mechanicSelect = document.getElementById('mechanicSelect');
    if (!mechanicSelect || !scenarioData?.teamCapacities) return;

    const currentSelection = mechanicSelect.value;
    console.log('Populating mechanic dropdown, current selection:', currentSelection);

    const hierarchy = _buildMechanicHierarchy();
    const teamCapacities = scenarioData.teamCapacities;

    // Helper: check if a teamSkill entry passes the filters
    function passesFilter(baseTeam, skill, teamSkill) {
        if (filterTeam !== 'all' && baseTeam !== filterTeam) return false;
        if (filterSkill !== 'all' && skill !== filterSkill) return false;
        if (filterShift !== 'all') {
            // Extract shift from teamSkill key (format: "TEAM S{N} (SKILL)")
            // Filter values are "1", "2", "3" (from mechFilterShift dropdown)
            const parsed = parseTeamSkill(teamSkill);
            if (parsed.shift) {
                if (String(parsed.shift) !== filterShift) return false;
            }
        }
        return true;
    }

    function passesSearch(displayName, mechId) {
        if (!filterSearch) return true;
        return displayName.toLowerCase().includes(filterSearch) || mechId.toLowerCase().includes(filterSearch);
    }

    // Build workers per team with filter applied
    function getFilteredWorkers(teamData) {
        const workers = [];
        teamData.skills.forEach((skillData, teamSkill) => {
            const parsed = parseTeamSkill(teamSkill);
            const baseTeam = parsed.baseTeam;
            const skill = parsed.skill;
            const shift = parsed.shift;

            if (!passesFilter(baseTeam, skill, teamSkill)) return;

            for (let i = 1; i <= skillData.capacity; i++) {
                const mechId = `${teamSkill}_${i}`;
                const displayName = buildResourceLabel(baseTeam, i, shift, skill);
                if (passesSearch(displayName, mechId)) {
                    workers.push({ id: mechId, displayName: displayName });
                }
            }
        });
        workers.sort((a, b) => a.displayName.localeCompare(b.displayName));
        return workers;
    }

    // Clear dropdown
    mechanicSelect.innerHTML = '<option value="none">Select a resource...</option>';

    // Add aggregate options
    const allOpt = document.createElement('option');
    allOpt.value = 'all';
    allOpt.textContent = 'All Workers';
    mechanicSelect.appendChild(allOpt);

    // === MECHANICS SECTION ===
    const mechSuperEntries = [...hierarchy.mechanics.entries()];
    let hasMechanics = false;
    const mechWorkersBySuper = new Map();
    const mechWorkersByTeam = new Map();

    mechSuperEntries.forEach(([superintendent, teams]) => {
        const superWorkers = [];
        teams.forEach(team => {
            const workers = getFilteredWorkers(team);
            if (workers.length > 0) {
                mechWorkersByTeam.set(team.name, workers);
                superWorkers.push(...workers);
            }
        });
        if (superWorkers.length > 0) {
            mechWorkersBySuper.set(superintendent, { teams, workers: superWorkers });
            hasMechanics = true;
        }
    });

    if (hasMechanics) {
        const mfgOptgroup = document.createElement('optgroup');
        mfgOptgroup.label = '--- MECHANICS ---';
        mfgOptgroup.disabled = true;
        mechanicSelect.appendChild(mfgOptgroup);

        const allMechOpt = document.createElement('option');
        allMechOpt.value = 'all-mechanics';
        allMechOpt.textContent = 'All Mechanics';
        mechanicSelect.appendChild(allMechOpt);

        mechWorkersBySuper.forEach(({ teams, workers }, superintendent) => {
            // Superintendent level
            const superOpt = document.createElement('option');
            superOpt.value = `super-mech-${superintendent}`;
            const teamCount = teams.filter(t => mechWorkersByTeam.has(t.name)).length;
            superOpt.textContent = `  All ${superintendent} (${teamCount} teams, ${workers.length} workers)`;
            mechanicSelect.appendChild(superOpt);

            // Team level
            teams.forEach(team => {
                const teamWorkers = mechWorkersByTeam.get(team.name);
                if (!teamWorkers || teamWorkers.length === 0) return;

                const teamOpt = document.createElement('option');
                teamOpt.value = `team-mech-${team.name}`;
                teamOpt.textContent = `    All ${team.name} (${teamWorkers.length})`;
                mechanicSelect.appendChild(teamOpt);

                // Individual workers
                teamWorkers.forEach(w => {
                    const opt = document.createElement('option');
                    opt.value = w.id;
                    opt.textContent = `      ${w.displayName}`;
                    mechanicSelect.appendChild(opt);
                });
            });
        });
    }

    // === QUALITY INSPECTORS SECTION ===
    const qualSuperEntries = [...hierarchy.quality.entries()];
    let hasQuality = false;
    const qualWorkersBySuper = new Map();
    const qualWorkersByTeam = new Map();

    qualSuperEntries.forEach(([superintendent, teams]) => {
        const superWorkers = [];
        teams.forEach(team => {
            const workers = getFilteredWorkers(team);
            if (workers.length > 0) {
                qualWorkersByTeam.set(team.name, workers);
                superWorkers.push(...workers);
            }
        });
        if (superWorkers.length > 0) {
            qualWorkersBySuper.set(superintendent, { teams, workers: superWorkers });
            hasQuality = true;
        }
    });

    if (hasQuality) {
        const qualOptgroup = document.createElement('optgroup');
        qualOptgroup.label = '--- QUALITY INSPECTORS ---';
        qualOptgroup.disabled = true;
        mechanicSelect.appendChild(qualOptgroup);

        const allQualOpt = document.createElement('option');
        allQualOpt.value = 'all-quality';
        allQualOpt.textContent = 'All Quality Inspectors';
        mechanicSelect.appendChild(allQualOpt);

        qualWorkersBySuper.forEach(({ teams, workers }, superintendent) => {
            const superOpt = document.createElement('option');
            superOpt.value = `super-qual-${superintendent}`;
            const teamCount = teams.filter(t => qualWorkersByTeam.has(t.name)).length;
            superOpt.textContent = `  All ${superintendent} Quality (${teamCount} teams, ${workers.length} workers)`;
            mechanicSelect.appendChild(superOpt);

            teams.forEach(team => {
                const teamWorkers = qualWorkersByTeam.get(team.name);
                if (!teamWorkers || teamWorkers.length === 0) return;

                const teamOpt = document.createElement('option');
                teamOpt.value = `team-qual-${team.name}`;
                teamOpt.textContent = `    All ${team.name} (${teamWorkers.length})`;
                mechanicSelect.appendChild(teamOpt);

                teamWorkers.forEach(w => {
                    const opt = document.createElement('option');
                    opt.value = w.id;
                    opt.textContent = `      ${w.displayName}`;
                    mechanicSelect.appendChild(opt);
                });
            });
        });
    }

    // === CUSTOMER INSPECTORS SECTION ===
    let hasCustomer = false;
    const custWorkersByTeam = new Map();

    hierarchy.customers.forEach((data, team) => {
        const workers = getFilteredWorkers(data);
        if (workers.length > 0) {
            custWorkersByTeam.set(team, workers);
            hasCustomer = true;
        }
    });

    if (hasCustomer) {
        const custOptgroup = document.createElement('optgroup');
        custOptgroup.label = '--- CUSTOMER INSPECTORS ---';
        custOptgroup.disabled = true;
        mechanicSelect.appendChild(custOptgroup);

        const allCustOpt = document.createElement('option');
        allCustOpt.value = 'all-customer';
        allCustOpt.textContent = 'All Customer Inspectors';
        mechanicSelect.appendChild(allCustOpt);

        custWorkersByTeam.forEach((workers, team) => {
            const teamOpt = document.createElement('option');
            teamOpt.value = `team-cust-${team}`;
            teamOpt.textContent = `  All ${team} (${workers.length})`;
            mechanicSelect.appendChild(teamOpt);

            workers.forEach(w => {
                const opt = document.createElement('option');
                opt.value = w.id;
                opt.textContent = `    ${w.displayName}`;
                mechanicSelect.appendChild(opt);
            });
        });
    }

    // === VENDORS SECTION ===
    let hasVendors = false;
    const vendorWorkersByTeam = new Map();

    hierarchy.vendors.forEach((data, team) => {
        const workers = getFilteredWorkers(data);
        if (workers.length > 0) {
            vendorWorkersByTeam.set(team, workers);
            hasVendors = true;
        }
    });

    if (hasVendors) {
        const vendorOptgroup = document.createElement('optgroup');
        vendorOptgroup.label = '--- VENDORS ---';
        vendorOptgroup.disabled = true;
        mechanicSelect.appendChild(vendorOptgroup);

        const allVendorOpt = document.createElement('option');
        allVendorOpt.value = 'all-vendor';
        allVendorOpt.textContent = 'All Vendors';
        mechanicSelect.appendChild(allVendorOpt);

        vendorWorkersByTeam.forEach((workers, team) => {
            const teamOpt = document.createElement('option');
            teamOpt.value = `team-vendor-${team}`;
            teamOpt.textContent = `  All ${team} (${workers.length})`;
            mechanicSelect.appendChild(teamOpt);

            workers.forEach(w => {
                const opt = document.createElement('option');
                opt.value = w.id;
                opt.textContent = `    ${w.displayName}`;
                mechanicSelect.appendChild(opt);
            });
        });
    }

    // Restore selection if it still exists
    if (currentSelection && Array.from(mechanicSelect.options).some(opt => opt.value === currentSelection)) {
        mechanicSelect.value = currentSelection;
        console.log('Restored selection:', currentSelection);
    } else if (currentSelection && currentSelection !== 'none') {
        console.log('Previous selection no longer available:', currentSelection);
        mechanicSelect.value = 'none';
    }

    // Remove any existing event listeners to prevent duplicates
    mechanicSelect.removeEventListener('change', handleMechanicSelection);
    mechanicSelect.addEventListener('change', handleMechanicSelection);

    const totalWorkers = (hasMechanics ? [...mechWorkersByTeam.values()].reduce((s, w) => s + w.length, 0) : 0)
        + (hasQuality ? [...qualWorkersByTeam.values()].reduce((s, w) => s + w.length, 0) : 0)
        + (hasCustomer ? [...custWorkersByTeam.values()].reduce((s, w) => s + w.length, 0) : 0)
        + (hasVendors ? [...vendorWorkersByTeam.values()].reduce((s, w) => s + w.length, 0) : 0);
    console.log(`Populated tiered mechanic dropdown with ${totalWorkers} workers`);
}

/**
 * Get aggregated tasks for all workers under a given superintendent.
 */
function getAggregatedTasksBySuperintendent(selection) {
    const assignments = savedAssignments[currentScenario] || {};
    const schedules = generateMechanicSchedulesFromAssignments(assignments);

    // Parse superintendent from selection (e.g., "super-mech-S-CF-POSITION 3")
    const isMech = selection.startsWith('super-mech-');
    const superintendent = selection.replace(/^super-(mech|qual)-/, '');

    const hierarchy = _buildMechanicHierarchy();
    const superMap = isMech ? hierarchy.mechanics : hierarchy.quality;
    const teams = superMap.get(superintendent) || [];
    const teamNames = new Set(teams.map(t => t.name));

    const allTasks = [];
    const mechanicsSummary = {};
    let totalMechanics = 0;

    Object.entries(schedules).forEach(([mechanicId, schedule]) => {
        if (!teamNames.has(schedule.team)) return;

        // Filter by category
        const isQual = isQualityTeam(schedule.team);
        const isCust = isCustomerTeam(schedule.team);
        const isVend = isVendorTeam(schedule.team);
        if (isMech && (isQual || isCust || isVend)) return;
        if (!isMech && !isQual) return;

        totalMechanics++;
        mechanicsSummary[mechanicId] = {
            displayName: schedule.displayName,
            team: schedule.team,
            skill: schedule.skill,
            taskCount: schedule.tasks.length
        };
        schedule.tasks.forEach(task => {
            allTasks.push({ ...task, assignedTo: schedule.displayName, mechanicId });
        });
    });

    allTasks.sort((a, b) => new Date(a.startTime) - new Date(b.startTime));

    return { tasks: allTasks, mechanics: mechanicsSummary, totalMechanics };
}

/**
 * Get aggregated tasks for all workers on a specific team.
 */
function getAggregatedTasksByTeam(teamName) {
    const assignments = savedAssignments[currentScenario] || {};
    const schedules = generateMechanicSchedulesFromAssignments(assignments);

    const allTasks = [];
    const mechanicsSummary = {};
    let totalMechanics = 0;

    Object.entries(schedules).forEach(([mechanicId, schedule]) => {
        if (schedule.team !== teamName) return;

        totalMechanics++;
        mechanicsSummary[mechanicId] = {
            displayName: schedule.displayName,
            team: schedule.team,
            skill: schedule.skill,
            taskCount: schedule.tasks.length
        };
        schedule.tasks.forEach(task => {
            allTasks.push({ ...task, assignedTo: schedule.displayName, mechanicId });
        });
    });

    allTasks.sort((a, b) => new Date(a.startTime) - new Date(b.startTime));

    return { tasks: allTasks, mechanics: mechanicsSummary, totalMechanics, teamName };
}

function handleMechanicSelection() {
    console.log('=== MECHANIC SELECTION ===');
    console.log('Selected value:', this.value);

    const selection = this.value;

    if (!selection || selection === 'none') {
        displayNoSelection();
        return;
    }

    if (selection === 'all' || selection === 'all-mechanics' || selection === 'all-quality' || selection === 'all-customer' || selection === 'all-vendor') {
        console.log('Loading aggregated view for:', selection);
        const viewData = getAggregatedTasks(selection, 'all');
        displayAggregatedView(viewData, 'aggregate', selection);
    } else if (selection.startsWith('super-mech-') || selection.startsWith('super-qual-')) {
        console.log('Loading superintendent aggregated view for:', selection);
        const viewData = getAggregatedTasksBySuperintendent(selection);
        displayAggregatedView(viewData, 'aggregate', selection);
    } else if (selection.startsWith('team-mech-') || selection.startsWith('team-qual-') || selection.startsWith('team-cust-') || selection.startsWith('team-vendor-')) {
        const teamName = selection.replace(/^team-(mech|qual|cust|vendor)-/, '');
        console.log('Loading team aggregated view for:', teamName);
        const viewData = getAggregatedTasksByTeam(teamName);
        displayAggregatedView(viewData, 'aggregate', selection);
    } else {
        console.log('Loading individual view for:', selection);
        const mechanicSchedule = getIndividualMechanicTasks(selection);
        displayIndividualViewWithFeedback(mechanicSchedule, selection);
    }
}

/**
 * Generates a mechanic-keyed schedule object from the task-keyed assignment data.
 * This is a non-destructive, on-the-fly generator.
 * @param {Object} taskAssignments - The source of truth for assignments, keyed by taskId.
 * @returns {Object} A new object keyed by mechanicId, for use in views.
 */
function generateMechanicSchedulesFromAssignments(taskAssignments) {
    const schedules = {};
    if (!taskAssignments) return schedules;

    // Process all task assignments to build up the mechanic-keyed view
    Object.entries(taskAssignments).forEach(([taskId, assignment]) => {
        if (taskId === 'mechanicSchedules') return; // Should not happen anymore, but good guard
        if (!assignment.mechanics || assignment.mechanics.length === 0) return;

        const task = scenarioData.tasks.find(t => t.taskId === taskId);
        if (!task) return;

        assignment.mechanics.forEach(mechanicId => {
            if (!mechanicId) return;

            if (!schedules[mechanicId]) {
                const parts = mechanicId.split('_');
                const teamSkill = parts.slice(0, -1).join('_');
                const position = parseInt(parts[parts.length - 1], 10) || 1;
                const parsed = parseTeamSkill(teamSkill);
                const baseTeam = parsed.baseTeam;
                const skill = parsed.skill;
                const shift = parsed.shift;
                const isCustomer = isCustomerTeam(baseTeam);
                const isQuality = isQualityTeam(baseTeam);
                const isVendor = isVendorTeam(baseTeam);
                const displayName = buildResourceLabel(baseTeam, position, shift, skill);

                schedules[mechanicId] = {
                    mechanicId: mechanicId,
                    displayName: displayName,
                    team: baseTeam,
                    skill: skill,
                    isCustomer: isCustomer,
                    isQuality: isQuality,
                    isVendor: isVendor,
                    tasks: []
                };
            }

            schedules[mechanicId].tasks.push(task);
        });
    });

    // Sort tasks within each schedule
    Object.values(schedules).forEach(schedule => {
        schedule.tasks.sort((a, b) => new Date(a.startTime) - new Date(b.startTime));
    });

    return schedules;
}

// DEPRECATED: This function was replaced by the on-the-fly generateMechanicSchedulesFromAssignments
// function updateMechanicSchedulesFromAssignments() { ... }

// Enhanced updateMechanicView that uses the new feedback system
function updateMechanicView() {
    if (!scenarioData) return;

    console.log('updateMechanicView called with feedback system');

    // Initialize feedback system
    initializeFeedbackSystem();

    // Populate dropdown only if it's empty or needs updating
    const mechanicSelect = document.getElementById('mechanicSelect');
    if (!mechanicSelect) {
        console.warn('updateMechanicView: mechanicSelect element not found');
        return;
    }
    const needsPopulating = mechanicSelect.options.length <= 1 ||
                           !mechanicSelect.hasAttribute('data-populated-for-scenario') ||
                           mechanicSelect.getAttribute('data-populated-for-scenario') !== currentScenario;

    if (needsPopulating) {
        populateMechanicFilterDropdowns();
        populateMechanicDropdown();
        if (mechanicSelect) {
            mechanicSelect.setAttribute('data-populated-for-scenario', currentScenario);
        }
    }

    // After populating, or if it was already populated,
    // get a fresh reference and call the handler with the correct `this` context.
    const freshMechanicSelect = document.getElementById('mechanicSelect');
    if (freshMechanicSelect) {
        handleMechanicSelection.call(freshMechanicSelect);
    }
}

// Add CSS for autocomplete
const feedbackCSS = `
<style>
.task-feedback-item {
    transition: all 0.2s ease;
}

.task-feedback-item:hover {
    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
}

.autocomplete-suggestions {
    position: relative;
}

.autocomplete-item:hover {
    background: #f3f4f6;
}

/* Hide autocomplete when clicking outside */
.autocomplete-suggestions {
    display: none;
}

.autocomplete-suggestions.active {
    display: block;
}

/* Form styling improvements */
input[type="radio"] {
    margin-right: 8px;
}

select, input, textarea {
    font-family: inherit;
}

button {
    transition: all 0.2s ease;
}

button:hover {
    opacity: 0.9;
    transform: translateY(-1px);
}
</style>
`;

// Add CSS to document head
if (!document.querySelector('#feedback-styles')) {
    const styleElement = document.createElement('style');
    styleElement.id = 'feedback-styles';
    styleElement.innerHTML = feedbackCSS.replace(/<\/?style>/g, '');
    document.head.appendChild(styleElement);
}

// Make functions globally available
window.toggleFeedbackFields = toggleFeedbackFields;
window.handlePredecessorAutocomplete = handlePredecessorAutocomplete;
window.selectPredecessorTask = selectPredecessorTask;
window.saveFeedback = saveFeedback;
window.clearFeedback = clearFeedback;
window.exportMechanicFeedback = exportMechanicFeedback;

// Make functions globally available
window.initializeFeedbackSystem = initializeFeedbackSystem;
window.buildAircraftTaskCache = buildAircraftTaskCache;
window.handlePredecessorAutocomplete = handlePredecessorAutocomplete;
window.saveFeedback = saveFeedback;
window.exportMechanicFeedback = exportMechanicFeedback;
window.updateFeedbackSummary = updateFeedbackSummary;
window.loadSavedFeedback = loadSavedFeedback;
window.onScenarioChange = onScenarioChange;

// Make functions globally available
window.handlePredecessorAutocomplete = handlePredecessorAutocomplete;


// --- SCENARIO PLANNING (Ranked Priority Impact Estimation) ---

// Session cache: all scenario estimates for comparison
let _savedPriorityEstimates = [];
let _scenarioProducts = [];  // Product names loaded from API

function initScenarioView() {
    console.log('Initializing Priority Impact Analysis view');
    loadProductsAndBuildUI();
    setupScenarioEventListeners();
    renderSavedEstimates();
    renderSideBySideComparison();
}

function setupScenarioEventListeners() {
    const runBtn = document.getElementById('runScenarioBtn');
    if (runBtn && !runBtn.hasAttribute('data-listener-added')) {
        runBtn.setAttribute('data-listener-added', 'true');
        runBtn.addEventListener('click', runPriorityEstimation);
    }
    const clearBtn = document.getElementById('clearScenariosBtn');
    if (clearBtn && !clearBtn.hasAttribute('data-listener-added')) {
        clearBtn.setAttribute('data-listener-added', 'true');
        clearBtn.addEventListener('click', () => {
            _savedPriorityEstimates = [];
            renderSavedEstimates();
            renderSideBySideComparison();
            const resultDiv = document.getElementById('scenarioResult');
            if (resultDiv) resultDiv.style.display = 'none';
        });
    }
}

async function loadProductsAndBuildUI() {
    try {
        const response = await fetch('/api/products');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        _scenarioProducts = await response.json();
    } catch (error) {
        console.error('Failed to load products:', error);
        _scenarioProducts = [];
    }

    // Also populate hidden dropdown for any legacy compatibility
    const select = document.getElementById('scenarioProductSelect');
    if (select) {
        select.innerHTML = '<option value="">-- Select --</option>';
        _scenarioProducts.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p; opt.textContent = p;
            select.appendChild(opt);
        });
    }

    // Build the priority assignment grid
    buildPriorityGrid();
}

function buildPriorityGrid() {
    const grid = document.getElementById('priorityAssignmentGrid');
    if (!grid || _scenarioProducts.length === 0) return;

    const maxRank = _scenarioProducts.length;
    // Build rank options: "Any", 1, 2, 3, ...N
    const rankOptions = ['Any'];
    for (let i = 1; i <= maxRank; i++) rankOptions.push(String(i));

    grid.innerHTML = '';
    _scenarioProducts.forEach(product => {
        // Product label
        const label = document.createElement('div');
        label.style.cssText = 'font-weight: 500; font-size: 14px; padding: 4px 0;';
        label.textContent = product;
        grid.appendChild(label);

        // Rank dropdown
        const sel = document.createElement('select');
        sel.id = `rank_${product.replace(/\s+/g, '_')}`;
        sel.setAttribute('data-product', product);
        sel.className = 'scenario-rank-select';
        sel.style.cssText = 'padding: 6px 12px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; min-width: 80px; background: white; cursor: pointer;';

        rankOptions.forEach(opt => {
            const option = document.createElement('option');
            option.value = opt;
            option.textContent = opt === 'Any' ? 'Any' : `#${opt}`;
            sel.appendChild(option);
        });

        // Auto-validate: warn on duplicate ranks
        sel.addEventListener('change', validateRankSelections);
        grid.appendChild(sel);
    });
}

function validateRankSelections() {
    const selects = document.querySelectorAll('.scenario-rank-select');
    const usedRanks = {};
    selects.forEach(sel => {
        sel.style.borderColor = '#ccc';
        const val = sel.value;
        if (val !== 'Any') {
            if (usedRanks[val]) {
                sel.style.borderColor = '#dc3545';
                usedRanks[val].style.borderColor = '#dc3545';
            } else {
                usedRanks[val] = sel;
            }
        }
    });
}

function getPriorityOrder() {
    const order = {};
    const selects = document.querySelectorAll('.scenario-rank-select');
    selects.forEach(sel => {
        const product = sel.getAttribute('data-product');
        const val = sel.value;
        if (val !== 'Any') {
            order[product] = parseInt(val, 10);
        }
    });
    return order;
}

async function runPriorityEstimation() {
    const priorityOrder = getPriorityOrder();

    // Check for duplicate ranks
    const ranks = Object.values(priorityOrder);
    const uniqueRanks = new Set(ranks);
    if (ranks.length !== uniqueRanks.size) {
        alert('Duplicate ranks detected. Each rank can only be assigned to one aircraft.');
        return;
    }

    // Must have at least one ranked aircraft
    if (Object.keys(priorityOrder).length === 0) {
        alert('Please assign at least one aircraft a priority rank (not "Any").');
        return;
    }

    const labelInput = document.getElementById('scenarioLabelInput');
    let scenarioLabel = labelInput ? labelInput.value.trim() : '';

    // Auto-generate label if empty
    if (!scenarioLabel) {
        const parts = Object.entries(priorityOrder)
            .sort((a, b) => a[1] - b[1])
            .map(([name, rank]) => `#${rank} ${name}`);
        scenarioLabel = parts.join(', ');
    }

    const resultDiv = document.getElementById('scenarioResult');
    const spinner = document.getElementById('scenarioSpinner');
    const runBtn = document.getElementById('runScenarioBtn');

    if (resultDiv) resultDiv.style.display = 'none';
    if (spinner) spinner.style.display = 'block';
    if (runBtn) { runBtn.disabled = true; runBtn.textContent = 'Estimating...'; }

    try {
        const response = await fetch('/api/scenarios/estimate_priority', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                priority_order: priorityOrder,
                scenario_label: scenarioLabel,
            }),
        });

        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.error || `Estimation failed with status: ${response.status}`);
        }

        data.estimated_at = new Date().toISOString();
        renderPriorityEstimation(data);

        // Save to session cache for comparison
        _savedPriorityEstimates.unshift(data);
        if (_savedPriorityEstimates.length > 20) _savedPriorityEstimates.pop();
        renderSavedEstimates();
        renderSideBySideComparison();

        // Clear label input for next scenario
        if (labelInput) labelInput.value = '';

    } catch (error) {
        console.error('Error running priority estimation:', error);
        alert(`Error: ${error.message || 'An unknown error occurred.'}`);
    } finally {
        if (spinner) spinner.style.display = 'none';
        if (runBtn) { runBtn.disabled = false; runBtn.textContent = 'Estimate Impact'; }
    }
}

function renderPriorityEstimation(data) {
    const resultDiv = document.getElementById('scenarioResult');
    const titleEl = document.getElementById('scenarioResultTitle');
    const dateEl = document.getElementById('scenarioResultDate');
    const comparisonBody = document.getElementById('comparison-body');
    const summaryCards = document.getElementById('scenarioSummaryCards');
    const contentionDiv = document.getElementById('contentionDetails');
    const noteEl = document.getElementById('scenarioNote');

    if (!resultDiv || !comparisonBody) return;

    const label = data.scenario_label || data.ordering_description || 'Scenario';
    if (titleEl) titleEl.textContent = label;
    if (dateEl) dateEl.textContent = new Date(data.estimated_at || Date.now()).toLocaleString();

    // Summary cards
    if (summaryCards) {
        const rankedProducts = data.products.filter(p => p.is_prioritized);
        const anyProducts = data.products.filter(p => !p.is_prioritized);
        const maxDelayAny = anyProducts.length > 0 ? Math.max(...anyProducts.map(p => p.delay_days), 0) : 0;
        const avgDelayAny = anyProducts.length > 0
            ? (anyProducts.reduce((s, p) => s + p.delay_days, 0) / anyProducts.length).toFixed(1) : 0;
        const bestRankedGain = rankedProducts.length > 0
            ? Math.min(...rankedProducts.map(p => p.delay_days)) : 0;

        const rankedLabel = rankedProducts.map(p => `#${p.priority_rank} ${p.name}`).join(', ');

        summaryCards.innerHTML = `
            <div style="background: white; padding: 16px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); text-align: center;">
                <div style="font-size: 0.85rem; color: #666; margin-bottom: 4px;">Ranked Aircraft</div>
                <div style="font-size: 1rem; font-weight: 700; color: #003581;">${rankedLabel || 'None'}</div>
                <div style="font-size: 0.8rem; color: ${bestRankedGain <= 0 ? '#28a745' : '#dc3545'};">Best: ${bestRankedGain} days</div>
            </div>
            <div style="background: white; padding: 16px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); text-align: center;">
                <div style="font-size: 0.85rem; color: #666; margin-bottom: 4px;">Max Delay (Any)</div>
                <div style="font-size: 1.5rem; font-weight: 700; color: ${maxDelayAny > 0 ? '#dc3545' : '#28a745'};">${maxDelayAny > 0 ? '+' + maxDelayAny : maxDelayAny}</div>
                <div style="font-size: 0.8rem; color: #999;">days</div>
            </div>
            <div style="background: white; padding: 16px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); text-align: center;">
                <div style="font-size: 0.85rem; color: #666; margin-bottom: 4px;">Avg Delay (Any)</div>
                <div style="font-size: 1.5rem; font-weight: 700; color: ${avgDelayAny > 0 ? '#F59E0B' : '#28a745'};">${avgDelayAny > 0 ? '+' + avgDelayAny : avgDelayAny}</div>
                <div style="font-size: 0.8rem; color: #999;">days</div>
            </div>
            <div style="background: white; padding: 16px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); text-align: center;">
                <div style="font-size: 0.85rem; color: #666; margin-bottom: 4px;">Est. Makespan</div>
                <div style="font-size: 1.5rem; font-weight: 700; color: #17a2b8;">${data.estimated_makespan}</div>
                <div style="font-size: 0.8rem; color: #999;">days (was ${data.baseline_makespan})</div>
            </div>
        `;
    }

    // Comparison table rows
    comparisonBody.innerHTML = '';
    data.products.forEach(product => {
        const delay = product.delay_days;
        let impactColor = '#6c757d', impactBg = '#f8f9fa';
        let impactText = `${delay > 0 ? '+' : ''}${delay} days`;
        if (delay > 0) { impactColor = '#dc3545'; impactBg = '#FFF5F5'; }
        else if (delay < 0) { impactColor = '#28a745'; impactBg = '#F0FFF4'; }

        const isRanked = product.priority_rank !== null && product.priority_rank !== undefined;
        const rowBg = isRanked ? '#EBF5FF' : '';
        const rankBadge = isRanked
            ? `<span style="background:#003581;color:white;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;">#${product.priority_rank}</span>`
            : '<span style="color:#999;font-size:12px;">Any</span>';

        const row = document.createElement('tr');
        row.style.borderBottom = '1px solid #dee2e6';
        if (rowBg) row.style.background = rowBg;
        row.innerHTML = `
            <td style="padding: 12px; font-weight: ${isRanked ? '700' : '400'};">${product.name}</td>
            <td style="padding: 12px; text-align: center;">${rankBadge}</td>
            <td style="padding: 12px; text-align: right;">${product.totalTasks}</td>
            <td style="padding: 12px; text-align: right;">Day ${product.baseline_completion_day}</td>
            <td style="padding: 12px; text-align: right; font-weight: 600;">Day ${product.estimated_completion_day}</td>
            <td style="padding: 12px; text-align: right;">${product.baseline_lateness}d</td>
            <td style="padding: 12px; text-align: right; font-weight: 600;">${product.estimated_lateness}d</td>
            <td style="padding: 12px; text-align: center;">
                <span style="display:inline-block;padding:4px 12px;border-radius:20px;font-weight:600;font-size:13px;color:${impactColor};background:${impactBg};">${impactText}</span>
            </td>
        `;
        comparisonBody.appendChild(row);
    });

    // Contention details
    if (contentionDiv && data.contention_details) {
        const cd = data.contention_details;
        if (cd.total_contention_slots === 0) {
            contentionDiv.innerHTML = '<p style="color: #28a745;">No significant resource contention found.</p>';
        } else {
            let html = `<p style="margin-bottom: 12px;"><strong>${cd.total_contention_slots}</strong> resource slots with contention (&gt;50% utilization).</p>`;
            if (cd.high_contention_slots && cd.high_contention_slots.length > 0) {
                html += '<table style="width:100%;border-collapse:collapse;font-size:13px;">';
                html += '<thead><tr style="background:#f8f9fa;border-bottom:1px solid #dee2e6;">';
                html += '<th style="padding:8px;text-align:left;">Team</th><th style="padding:8px;text-align:center;">Shift</th><th style="padding:8px;text-align:center;">Day</th><th style="padding:8px;text-align:right;">Util%</th><th style="padding:8px;text-align:left;">Affected</th>';
                html += '</tr></thead><tbody>';
                cd.high_contention_slots.forEach(slot => {
                    const c = slot.utilization_pct > 90 ? '#dc3545' : slot.utilization_pct > 80 ? '#F59E0B' : '#6c757d';
                    html += `<tr style="border-bottom:1px solid #eee;"><td style="padding:8px;">${slot.team}</td><td style="padding:8px;text-align:center;">S${slot.shift}</td><td style="padding:8px;text-align:center;">D${slot.day}</td><td style="padding:8px;text-align:right;color:${c};font-weight:600;">${slot.utilization_pct}%</td><td style="padding:8px;">${slot.affected_products.join(', ')}</td></tr>`;
                });
                html += '</tbody></table>';
            }
            contentionDiv.innerHTML = html;
        }
    }

    if (noteEl) noteEl.textContent = data.note || '';
    resultDiv.style.display = 'block';
}

// === Side-by-Side Scenario Comparison Table ===

function renderSideBySideComparison() {
    const section = document.getElementById('scenarioComparisonSection');
    const table = document.getElementById('scenarioSideBySideTable');
    if (!section || !table) return;

    if (_savedPriorityEstimates.length < 1) {
        section.style.display = 'none';
        return;
    }

    section.style.display = 'block';

    // Get consistent product list (from first estimate or _scenarioProducts)
    const productNames = _scenarioProducts.length > 0
        ? _scenarioProducts
        : (_savedPriorityEstimates[0].products || []).map(p => p.name);

    // Also include baseline as the first "scenario"
    const scenarios = _savedPriorityEstimates;
    const numScenarios = scenarios.length;

    // Build table: rows = products, columns = scenarios
    let html = '<thead><tr style="background:#f0f4f8;border-bottom:2px solid #dee2e6;">';
    html += '<th style="padding:10px 12px;text-align:left;font-weight:600;position:sticky;left:0;background:#f0f4f8;min-width:110px;">Aircraft</th>';
    // Baseline column
    html += '<th style="padding:10px 12px;text-align:center;font-weight:600;min-width:100px;border-left:2px solid #003581;background:#EBF5FF;">Baseline<br><span style="font-size:11px;font-weight:400;color:#666;">Optimized</span></th>';
    // One column per scenario
    scenarios.forEach((s, idx) => {
        const label = s.scenario_label || `Scenario ${idx + 1}`;
        const shortLabel = label.length > 25 ? label.substring(0, 25) + '...' : label;
        html += `<th style="padding:10px 12px;text-align:center;font-weight:600;min-width:100px;border-left:1px solid #dee2e6;" title="${label}">S${idx + 1}<br><span style="font-size:11px;font-weight:400;color:#666;">${shortLabel}</span></th>`;
    });
    html += '</tr></thead><tbody>';

    // Metric rows: Est. Completion Day and Est. Lateness per product
    productNames.forEach(productName => {
        // Completion Day row
        html += `<tr style="border-bottom:1px solid #eee;">`;
        html += `<td style="padding:8px 12px;font-weight:600;position:sticky;left:0;background:white;">${productName}<br><span style="font-size:11px;font-weight:400;color:#888;">Completion Day</span></td>`;

        // Baseline value
        const baselineProduct = scenarios[0] ? scenarios[0].products.find(p => p.name === productName) : null;
        const baselineDay = baselineProduct ? baselineProduct.baseline_completion_day : '-';
        html += `<td style="padding:8px 12px;text-align:center;font-weight:600;border-left:2px solid #003581;background:#FAFCFF;">Day ${baselineDay}</td>`;

        scenarios.forEach(s => {
            const p = s.products.find(pp => pp.name === productName);
            if (!p) { html += '<td style="padding:8px 12px;text-align:center;border-left:1px solid #dee2e6;">-</td>'; return; }
            const delay = p.delay_days;
            const isRanked = p.priority_rank !== null && p.priority_rank !== undefined;
            let bg = '', color = '#333';
            if (delay < 0) { bg = '#F0FFF4'; color = '#28a745'; }
            else if (delay > 0) { bg = '#FFF5F5'; color = '#dc3545'; }
            const rankTag = isRanked ? `<span style="font-size:10px;background:#003581;color:white;padding:1px 5px;border-radius:3px;margin-left:4px;">#${p.priority_rank}</span>` : '';
            html += `<td style="padding:8px 12px;text-align:center;border-left:1px solid #dee2e6;background:${bg};color:${color};font-weight:600;">Day ${p.estimated_completion_day}${rankTag}<br><span style="font-size:11px;font-weight:400;">${delay > 0 ? '+' : ''}${delay}d</span></td>`;
        });
        html += '</tr>';

        // Lateness row
        html += `<tr style="border-bottom:2px solid #eee;">`;
        html += `<td style="padding:8px 12px;position:sticky;left:0;background:white;"><span style="font-size:12px;color:#888;">Lateness</span></td>`;
        const baselineLateness = baselineProduct ? baselineProduct.baseline_lateness : '-';
        html += `<td style="padding:8px 12px;text-align:center;border-left:2px solid #003581;background:#FAFCFF;font-size:12px;">${baselineLateness}d</td>`;

        scenarios.forEach(s => {
            const p = s.products.find(pp => pp.name === productName);
            if (!p) { html += '<td style="padding:8px 12px;text-align:center;border-left:1px solid #dee2e6;font-size:12px;">-</td>'; return; }
            const diff = p.estimated_lateness - p.baseline_lateness;
            let color = '#333';
            if (diff < 0) color = '#28a745';
            else if (diff > 0) color = '#dc3545';
            html += `<td style="padding:8px 12px;text-align:center;border-left:1px solid #dee2e6;font-size:12px;color:${color};">${p.estimated_lateness}d</td>`;
        });
        html += '</tr>';
    });

    // Makespan summary row
    html += `<tr style="background:#f8f9fa;border-top:2px solid #333;">`;
    html += `<td style="padding:10px 12px;font-weight:700;position:sticky;left:0;background:#f8f9fa;">Makespan</td>`;
    html += `<td style="padding:10px 12px;text-align:center;font-weight:700;border-left:2px solid #003581;background:#EBF5FF;">${scenarios[0] ? scenarios[0].baseline_makespan : '-'}d</td>`;
    scenarios.forEach(s => {
        const diff = s.estimated_makespan - s.baseline_makespan;
        let color = diff < 0 ? '#28a745' : diff > 0 ? '#dc3545' : '#333';
        html += `<td style="padding:10px 12px;text-align:center;font-weight:700;border-left:1px solid #dee2e6;color:${color};">${s.estimated_makespan}d <span style="font-size:11px;font-weight:400;">(${diff > 0 ? '+' : ''}${diff})</span></td>`;
    });
    html += '</tr>';

    html += '</tbody>';
    table.innerHTML = html;
}

function renderSavedEstimates() {
    const listDiv = document.getElementById('savedScenariosList');
    if (!listDiv) return;

    if (_savedPriorityEstimates.length === 0) {
        listDiv.innerHTML = '<p style="color: #999;">No estimates run yet.</p>';
        return;
    }

    listDiv.innerHTML = '';
    _savedPriorityEstimates.forEach((estimate, idx) => {
        const age = timeSince(new Date(estimate.estimated_at));
        const label = estimate.scenario_label || `Scenario ${idx + 1}`;
        const anyProds = estimate.products.filter(p => !p.is_prioritized);
        const maxDelay = anyProds.length > 0 ? Math.max(...anyProds.map(p => p.delay_days), 0) : 0;

        const item = document.createElement('div');
        item.style.cssText = 'display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; border-bottom: 1px solid #eee;';
        item.innerHTML = `
            <div style="flex:1;">
                <strong style="color: #003581;">S${idx + 1}:</strong> ${label}
                <span style="color: ${maxDelay > 0 ? '#dc3545' : '#28a745'}; font-size: 13px; margin-left: 8px;">
                    ${maxDelay > 0 ? 'Max +' + maxDelay + 'd impact' : 'No negative impact'}
                </span>
                <small style="color: #999; margin-left: 8px;">${age} ago</small>
            </div>
            <button class="view-saved-btn" data-idx="${idx}" style="padding: 4px 12px; background: #003581; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px;">View</button>
        `;

        item.querySelector('.view-saved-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            renderPriorityEstimation(estimate);
            const container = document.querySelector('.main-content') || document.querySelector('.ios-main-content');
            if (container) container.scrollTop = 0;
        });

        listDiv.appendChild(item);
    });
}

// Backwards-compatible aliases
function populateProductDropdown() { loadProductsAndBuildUI(); }
function runWhatIfScenario() { runPriorityEstimation(); }
function renderScenarioComparison(data) {
    if (data.products && data.products[0] && 'delay_days' in data.products[0]) {
        renderPriorityEstimation(data);
    }
}
async function loadSavedScenarios() { renderSavedEstimates(); }

// Helper function to calculate time since a date
function timeSince(date) {
    const seconds = Math.floor((new Date() - date) / 1000);
    let interval = seconds / 31536000;
    if (interval > 1) return Math.floor(interval) + " years";
    interval = seconds / 2592000;
    if (interval > 1) return Math.floor(interval) + " months";
    interval = seconds / 86400;
    if (interval > 1) return Math.floor(interval) + " days";
    interval = seconds / 3600;
    if (interval > 1) return Math.floor(interval) + " hours";
    interval = seconds / 60;
    if (interval > 1) return Math.floor(interval) + " minutes";
    return Math.floor(seconds) + " seconds";
}
window.selectPredecessorTask = selectPredecessorTask;
window.setupReasonDropdownHandler = setupReasonDropdownHandler;

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    // Load saved feedback for current scenario
    loadSavedFeedback();

    console.log('Enhanced Task Feedback System initialized');
});

console.log('Task Feedback System initialized successfully!');

window.handleReasonChange = handleReasonChange;

// --- Task Dependency Chain Modal ---

// Show the modal and fetch dependency chain data
async function showTaskChain(taskId) {
    const modal = document.getElementById('task-chain-modal');
    const modalContent = document.getElementById('task-chain-content');
    const modalTask = document.getElementById('modal-task-id');
    const modalClose = document.querySelector('.chain-modal-close');

    if (!modal || !modalContent || !modalTask || !modalClose) {
        console.error('Task chain modal elements not found!');
        return;
    }

    // Show modal with loading state
    modalTask.textContent = taskId;
    modalContent.innerHTML = '<p>Loading dependency chain...</p>';
    modal.style.display = 'block';

    // Close modal event listeners
    modalClose.onclick = () => modal.style.display = 'none';
    window.onclick = (event) => {
        if (event.target == modal) {
            modal.style.display = 'none';
        }
    };

    try {
        const response = await fetch(`/api/task/${currentScenario}/${taskId}/chain`);
        if (!response.ok) {
            throw new Error(`API request failed with status ${response.status}`);
        }
        const data = await response.json();

        // Render the chains
        renderTaskChain(data);

    } catch (error) {
        console.error('Error fetching task chain:', error);
        modalContent.innerHTML = `<p style="color: red;">Error: Could not load dependency chain. ${error.message}</p>`;
    }
}

// Render the predecessor and successor chains into the modal
function renderTaskChain(data) {
    const modalContent = document.getElementById('task-chain-content');
    if (!modalContent) return;

    let html = `
        <div class="chain-column">
            <h4 style="background-color: #eef2ff; color: #4338ca; padding: 8px; border-bottom: 2px solid #c7d2fe; border-top-left-radius: 6px; border-top-right-radius: 6px;">Upstream (Predecessors)</h4>
            ${formatChainList(data.predecessors, data.task_id)}
        </div>
        <div class="chain-column">
            <h4 style="background-color: #f0fdf4; color: #15803d; padding: 8px; border-bottom: 2px solid #bbf7d0; border-top-left-radius: 6px; border-top-right-radius: 6px;">Downstream (Successors)</h4>
            ${formatChainList(data.successors, data.task_id)}
        </div>
    `;

    modalContent.innerHTML = html;
}

// Helper to format a list of tasks for the chain display
function formatChainList(tasks, mainTaskId) {
    if (!tasks || tasks.length === 0) {
        return '<p style="padding: 10px; color: #6b7280;">None</p>';
    }

    // The API returns the chain in order, so we can just display it.
    let listHtml = '<ul class="chain-list">';
    tasks.forEach((task, index) => {
        const isLast = index === tasks.length - 1;
        const isMainTask = task.taskId === mainTaskId;

        let itemClass = isMainTask ? 'main-task' : '';
        let arrowHtml = !isLast ? '<div class="chain-arrow">↓</div>' : '';

        // Format scheduled time
        let scheduledTime = '';
        if (task.startTime) {
            try {
                // Use existing helper function for consistent formatting
                scheduledTime = `(${formatDateTime(new Date(task.startTime))})`;
            } catch (e) { /* Ignore if date is invalid */ }
        }

        // Get resource name and type from the task's actual schedule data.
        // The API now returns mechanic_id, workGroup, isCustomerTask, isQualityTask
        // so we use that instead of relying solely on savedAssignments.
        let resourceName = 'Unassigned';
        let taskTypeLabel = '';

        // Priority 1: Use the scheduled mechanic_id from the API response
        if (task.mechanic_id && task.mechanic_id !== 'Unassigned') {
            resourceName = task.mechanic_id;
        } else {
            // Priority 2: Fall back to savedAssignments (frontend auto-assign cache)
            const assignment = savedAssignments[currentScenario] && savedAssignments[currentScenario][task.taskId];
            if (assignment && assignment.mechanics && assignment.mechanics.length > 0) {
                const mechanicId = assignment.mechanics[0];
                if (mechanicId) {
                    const fullName = getMechanicDisplayName(mechanicId);
                    const match = fullName.match(/^(Mechanic|Inspector|Customer|Vendor) #\d+/);
                    resourceName = match ? match[0] : fullName.split(' - ')[0];
                }
            }
        }

        // Determine the correct resource role label from task type flags
        // This ensures QA tasks show "Inspector", CC tasks show "Customer Rep",
        // vendor tasks show "Vendor", and production tasks show "Mechanic"
        if (task.isCustomerTask || task.workGroup === 'customer') {
            taskTypeLabel = 'Customer Rep';
        } else if (task.isQualityTask || task.workGroup === 'quality') {
            taskTypeLabel = 'Inspector';
        } else if (task.isVendorTask || task.workGroup === 'vendor') {
            taskTypeLabel = 'Vendor';
        } else {
            taskTypeLabel = 'Mechanic';
        }

        // Use resource_team (staffing/execution team) if available, else fall back to team
        const displayTeam = task.resource_team || task.team || '';

        // Build info line, handling null values for tasks not in current schedule
        const productLabel = task.product || 'Not Scheduled';
        const infoLine = displayTeam
            ? `${productLabel} - ${displayTeam} - ${taskTypeLabel}: ${resourceName}`
            : `${productLabel} - ${taskTypeLabel}: ${resourceName}`;

        listHtml += `
            <li class="${itemClass}">
                <div class="chain-task">
                    <strong>${task.taskId}</strong> <span style="font-weight: normal; color: #4b5563; font-size: 0.9em;">${scheduledTime}</span>
                    <br>
                    <small style="color: #6b7280;">${infoLine}</small>
                </div>
                ${arrowHtml}
            </li>
        `;
    });
    listHtml += '</ul>';

    return listHtml;
}

// Global cache for late parts data
let latePartsCache = [];

// Global sort state for the late parts table
let latePartsSortConfig = {
    key: 'impact_score',
    direction: 'desc'
};

// New, enhanced function for Supply Chain View
async function updateSupplyChainView() {
    console.log('Updating Supply Chain View with Impact Analysis...');
    const tableBody = document.getElementById('late-parts-table-body');
    const totalLatePartsSpan = document.getElementById('total-late-parts');

    if (!tableBody || !totalLatePartsSpan) {
        console.error('Supply chain view elements not found');
        return;
    }

    // Set loading state
    tableBody.innerHTML = '<tr><td colspan="9" class="text-center" style="padding: 20px;">Loading supply chain impact data...</td></tr>';

    try {
        const response = await fetch('/api/supply_chain/late_parts_analysis');
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        latePartsCache = await response.json(); // Store data in cache

        totalLatePartsSpan.textContent = latePartsCache.length;
        renderLatePartsTable(); // Render the table with initial sort
        setupLatePartsTableSorting(); // Setup sorting listeners

    } catch (error) {
        console.error('Failed to update supply chain view:', error);
        tableBody.innerHTML = '<tr><td colspan="9" class="text-center" style="color: red; padding: 20px;">Error loading data. Please try again.</td></tr>';
    }
}

function renderLatePartsTable() {
    const tableBody = document.getElementById('late-parts-table-body');
    if (!tableBody) return;

    if (latePartsCache.length === 0) {
        tableBody.innerHTML = '<tr><td colspan="9" class="text-center" style="padding: 20px;">No late parts found.</td></tr>';
        return;
    }

    // Sort the data based on the current config
    const sortedData = [...latePartsCache].sort((a, b) => {
        const key = latePartsSortConfig.key;
        const dir = latePartsSortConfig.direction === 'asc' ? 1 : -1;

        let valA = a[key];
        let valB = b[key];

        // Handle date strings
        if (key === 'on_dock_date' || key === 'scheduled_start') {
            valA = a[key] ? new Date(a[key]).getTime() : 0;
            valB = b[key] ? new Date(b[key]).getTime() : 0;
        }

        // Handle null or undefined values
        if (valA === null || valA === undefined) valA = -Infinity;
        if (valB === null || valB === undefined) valB = -Infinity;

        if (valA < valB) return -1 * dir;
        if (valA > valB) return 1 * dir;
        return 0;
    });

    let rowsHtml = '';
    sortedData.forEach(part => {
        const onDockDate = part.on_dock_date ? new Date(part.on_dock_date).toLocaleDateString() : 'N/A';
        const scheduledStart = part.scheduled_start ? new Date(part.scheduled_start).toLocaleString() : 'Not Scheduled';

        rowsHtml += `
            <tr class="${part.impact_score > 500 ? 'impact-critical' : part.impact_score > 100 ? 'impact-warning' : ''}">
                <td><strong>${part.part_id}</strong></td>
                <td>${part.product}</td>
                <td>${onDockDate}</td>
                <td>${scheduledStart}</td>
                <td class="impact-score">${part.impact_score.toFixed(2)}</td>
                <td>${part.affected_task_count}</td>
                <td>${part.total_downstream_duration_hours.toFixed(2)}</td>
                <td>${part.dependent_tasks.join(', ') || 'None'}</td>
                <td>${part.team || 'N/A'}</td>
            </tr>
        `;
    });

    tableBody.innerHTML = rowsHtml;
    updateSortIndicators();
}

function setupLatePartsTableSorting() {
    const headers = document.querySelectorAll('#late-parts-table th.sortable');
    headers.forEach(header => {
        // Prevent multiple listeners
        if (header.dataset.listenerAttached) return;
        header.dataset.listenerAttached = 'true';

        header.addEventListener('click', () => {
            const sortKey = header.dataset.sort;
            if (latePartsSortConfig.key === sortKey) {
                latePartsSortConfig.direction = latePartsSortConfig.direction === 'asc' ? 'desc' : 'asc';
            } else {
                latePartsSortConfig.key = sortKey;
                latePartsSortConfig.direction = 'desc'; // Default to descending for new column
            }
            renderLatePartsTable();
        });
    });
}

function updateSortIndicators() {
    document.querySelectorAll('#late-parts-table th.sortable').forEach(header => {
        const sortKey = header.dataset.sort;
        let headerText = header.textContent.replace(/ ▲| ▼/g, '').trim();

        if (latePartsSortConfig.key === sortKey) {
            const arrow = latePartsSortConfig.direction === 'asc' ? '▲' : '▼';
            header.innerHTML = `${headerText} ${arrow}`;
        } else {
             if (header.dataset.sort === 'impact_score') {
                header.innerHTML = `${headerText} &#x25B2;`;
            } else {
                 header.textContent = headerText;
            }
        }
    });
}

// New function for Industrial Engineering View
async function updateIEView() {
    console.log('Updating Industrial Engineering View...');
    const tableBody = document.getElementById('ie-review-table-body');
    if (!tableBody) return;

    tableBody.innerHTML = '<tr><td colspan="9" class="text-center" style="padding: 20px;">Loading IE review queue...</td></tr>';

    try {
        const response = await fetch('/api/ie/review_queue');
        if (!response.ok) throw new Error('Failed to fetch review queue');
        const queue = await response.json();

        if (queue.length === 0) {
            tableBody.innerHTML = '<tr><td colspan="9" class="text-center" style="padding: 20px;">No tasks in the review queue.</td></tr>';
            return;
        }

        let rowsHtml = '';
        queue.forEach(item => {
            const flaggedAt = new Date(item.flagged_at).toLocaleString();

            if (item.predecessors && item.predecessors.length > 0) {
                // If there are predecessors, create a row for each one
                item.predecessors.forEach((p, index) => {
                    const safeId = `${item.flagged_at.replace(/[^a-zA-Z0-9-_]/g, '')}-${index}`;
                    const predecessorTask = p.predecessorTask || '';
                    const predecessorNotes = p.notes || '';
                    const predecessorTaskDisplay = predecessorTask ? `<strong>${predecessorTask}</strong>` : '<em>(No task specified)</em>';

                    // Combine predecessor task and notes into a single cell
                    const predecessorHtml = `${predecessorTaskDisplay}<br><small style="color: #555;">${predecessorNotes}</small>`;

                    rowsHtml += `
                        <tr id="ie-task-${safeId}">
                            <td>${item.priority}</td>
                            <td><strong>${item.task_id}</strong></td>
                            <td>${item.details.product || 'N/A'}</td>
                            <td>${item.details.team || 'N/A'}</td>
                            <td>${item.mechanic_name || 'N/A'}</td>
                            <td>${flaggedAt}</td>
                            <td>${predecessorHtml}</td>
                            <td>${item.general_notes || ''}</td>
                            <td>
                                <button class="btn-ie-action" onclick='resolveIETask("${item.flagged_at}", "agree", ${JSON.stringify(predecessorTask)}, ${JSON.stringify(predecessorNotes)})'>Agree & Resolve</button>
                                <button class="btn-ie-action" onclick='resolveIETask("${item.flagged_at}", "disagree", ${JSON.stringify(predecessorTask)}, ${JSON.stringify(predecessorNotes)})'>Disagree</button>
                            </td>
                        </tr>
                    `;
                });
            }
            // Per user feedback, only predecessor-related delays are shown, so no 'else' block is needed.
        });

        if (!rowsHtml) {
            tableBody.innerHTML = '<tr><td colspan="9" class="text-center" style="padding: 20px;">No tasks with predecessor delays in the review queue.</td></tr>';
        } else {
            tableBody.innerHTML = rowsHtml;
        }

    } catch (error) {
        console.error('Error updating IE view:', error);
        tableBody.innerHTML = '<tr><td colspan="9" class="text-center" style="color: red; padding: 20px;">Error loading data.</td></tr>';
    }
}

async function resolveIETask(itemId, action, predecessorTask = null, predecessorNotes = null) {
    console.log(`Resolving IE task item:`, { itemId, action, predecessorTask, predecessorNotes });
    if (action === 'disagree') {
        alert('Disagree functionality is not yet implemented. The task will be removed from the queue for now.');
    }

    try {
        const response = await fetch(`/api/ie/resolve_task`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                flagged_at: itemId,
                predecessor_task: predecessorTask,
                predecessor_notes: predecessorNotes
            })
        });

        const result = await response.json();

        if (!response.ok) {
            // Use the error from the JSON response if available
            throw new Error(result.error || 'Failed to resolve task');
        }

        if (result.success) {
            showNotification(result.message, 'success');
            // Find the row to remove. Since multiple rows can share an itemId, we need a more specific selector.
            // This is a bit tricky without changing the ID structure more. For now, we'll try to find the button's parent row.
            // This part is not robust. A better way would be to pass the unique row ID to resolveIETask.
            // For now, we'll just refresh the view.
            updateIEView();

        } else {
            throw new Error(result.error || 'An unknown error occurred.');
        }
    } catch (error) {
        console.error('Error resolving IE task:', error);
        showNotification(`Error: ${error.message}`, 'error');
    }
}



// ============================================================================
// 3-STAGE SCHEDULER: STAFFING REQUIREMENTS PANEL
// ============================================================================

async function load3StageStaffingData() {
    const staffingPanel = document.getElementById('staffingPanel');
    const staffingContent = document.getElementById('staffingContent');
    
    if (!staffingPanel || !staffingContent) return;
    
    try {
        staffingPanel.style.display = 'block';
        staffingContent.innerHTML = '<p class="loading-text">Loading staffing data...</p>';
        
        const response = await fetch('/api/scenario/3stage/staffing');
        if (!response.ok) {
            throw new Error('Failed to load staffing data');
        }
        
        const data = await response.json();
        
        // Build staffing table
        let html = `
            <div class="staffing-summary">
                <div class="summary-stat">
                    <strong>Total Gaps:</strong> ${data.total_gaps} pools short
                </div>
                <div class="summary-stat">
                    <strong>Required:</strong> ${data.total_required} mechanics
                </div>
                <div class="summary-stat">
                    <strong>Current:</strong> ${data.total_current} mechanics
                </div>
            </div>
            <table class="staffing-table">
                <thead>
                    <tr>
                        <th>Team</th>
                        <th>Shift</th>
                        <th>Skill</th>
                        <th>Current</th>
                        <th>Required</th>
                        <th>Gap</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
        `;
        
        data.staffing_gaps.forEach(pool => {
            const statusClass = pool.status === 'shortage' ? 'status-shortage' : 
                               (pool.status === 'surplus' ? 'status-surplus' : 'status-balanced');
            const gapDisplay = pool.gap > 0 ? `+${pool.gap}` : pool.gap;
            
            html += `
                <tr class="${statusClass}">
                    <td>${pool.team}</td>
                    <td>Shift ${pool.shift}</td>
                    <td>${pool.skill}</td>
                    <td>${pool.current}</td>
                    <td>${pool.required}</td>
                    <td><strong>${gapDisplay}</strong></td>
                    <td><span class="status-badge ${statusClass}">${pool.status}</span></td>
                </tr>
            `;
        });
        
        html += `
                </tbody>
            </table>
        `;
        
        staffingContent.innerHTML = html;
    } catch (error) {
        console.error('Error loading staffing data:', error);
        staffingContent.innerHTML = '<p class="error-text">Failed to load staffing data.</p>';
    }
}

function toggleStaffingPanel() {
    const staffingPanel = document.getElementById('staffingPanel');
    if (staffingPanel) {
        staffingPanel.style.display = staffingPanel.style.display === 'none' ? 'block' : 'none';
    }
}

// ============================================================================
// 3-STAGE SCHEDULER: AIRCRAFT LATENESS PANEL
// ============================================================================

async function load3StageAircraftData() {
    const aircraftPanel = document.getElementById('aircraftLatenessPanel');
    const aircraftContent = document.getElementById('aircraftLatenessContent');

    if (!aircraftPanel || !aircraftContent) return;

    try {
        aircraftPanel.style.display = 'block';
        aircraftContent.innerHTML = '<p class="loading-text">Loading aircraft status...</p>';

        const response = await fetch('/api/scenario/3stage/aircraft');
        if (!response.ok) {
            throw new Error('Failed to load aircraft data');
        }

        const data = await response.json();

        // Build aircraft status cards
        let html = `
            <div class="aircraft-summary">
                <div class="summary-stat">
                    <strong>${data.total_aircraft}</strong> Aircraft (Group 1)
                </div>
                <div class="summary-stat">
                    <strong>${data.total_days_late}</strong> Total Days Late
                </div>
                <div class="summary-stat">
                    <strong>${Math.round(data.avg_days_late)}</strong> Avg Days Late
                </div>
            </div>
            <div class="aircraft-grid">
        `;

        data.aircraft.forEach(aircraft => {
            const statusClass = aircraft.status === 'critical' ? 'aircraft-critical' :
                               (aircraft.status === 'high' ? 'aircraft-high' :
                               (aircraft.status === 'moderate' ? 'aircraft-moderate' : 'aircraft-on-time'));

            html += `
                <div class="aircraft-card ${statusClass}">
                    <div class="aircraft-header">
                        <span class="aircraft-number">Line ${aircraft.line_number}</span>
                        <span class="aircraft-status-badge ${statusClass}">${aircraft.status.replace('_', ' ')}</span>
                    </div>
                    <div class="aircraft-metric">
                        <span class="metric-label">Days Late:</span>
                        <span class="metric-value ${aircraft.days_late > 0 ? 'late' : 'early'}">${aircraft.days_late > 0 ? '+' + aircraft.days_late : aircraft.days_late}</span>
                    </div>
                    <div class="aircraft-metric">
                        <span class="metric-label">CS 744 Latest:</span>
                        <span class="metric-value">${aircraft.cs_744_latest}</span>
                    </div>
                    <div class="aircraft-metric">
                        <span class="metric-label">Tasks Scheduled:</span>
                        <span class="metric-value">${aircraft.task_count}</span>
                    </div>
                    <div class="completion-bar">
                        <div class="completion-fill" style="width: ${aircraft.completion_pct}%;"></div>
                    </div>
                    <div class="completion-text">${aircraft.completion_pct}% Complete</div>
                </div>
            `;
        });

        html += `</div>`;
        aircraftContent.innerHTML = html;
    } catch (error) {
        console.error('Error loading aircraft data:', error);
        aircraftContent.innerHTML = '<p class="error-text">Failed to load aircraft data.</p>';
    }
}

function toggleAircraftPanel() {
    const aircraftPanel = document.getElementById('aircraftLatenessPanel');
    if (aircraftPanel) {
        aircraftPanel.style.display = aircraftPanel.style.display === 'none' ? 'block' : 'none';
    }
}

// ============================================================================
// DASHBOARD ENHANCEMENTS v1.4.2 - Risk Traffic Lights, Forecast Accuracy,
// Utilization Alerts, Predecessor Blocking Analysis
// ============================================================================

/**
 * Update Risk Traffic Light Cards in Management View
 * Shows aircraft risk status: Green (on-time), Yellow (1-5 days late), Red (>5 days late)
 */
async function updateRiskTrafficLights() {
    try {
        const response = await fetch('/api/scenario/3stage/aircraft');
        if (!response.ok) return;

        const data = await response.json();
        const aircraft = data.aircraft || [];

        // Categorize aircraft by risk level
        let greenCount = 0, yellowCount = 0, redCount = 0;
        const criticalAircraft = [];

        aircraft.forEach(a => {
            const daysLate = a.days_late || 0;
            if (daysLate <= 1) {
                greenCount++;
            } else if (daysLate <= 5) {
                yellowCount++;
            } else {
                redCount++;
                criticalAircraft.push({ line: a.line_number, daysLate: daysLate });
            }
        });

        // Update counts
        document.getElementById('riskGreenCount').textContent = greenCount;
        document.getElementById('riskYellowCount').textContent = yellowCount;
        document.getElementById('riskRedCount').textContent = redCount;
        document.getElementById('riskLastUpdated').textContent = `Updated: ${new Date().toLocaleTimeString()}`;

        // Show critical aircraft list if any
        const criticalList = document.getElementById('criticalAircraftList');
        const criticalItems = document.getElementById('criticalAircraftItems');
        if (criticalAircraft.length > 0 && criticalList && criticalItems) {
            criticalList.style.display = 'block';
            criticalItems.innerHTML = criticalAircraft
                .sort((a, b) => b.daysLate - a.daysLate)
                .slice(0, 10)
                .map(a => `
                    <span style="background: #FEE2E2; color: #991B1B; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 600;">
                        Line ${a.line}: +${a.daysLate}d
                    </span>
                `).join('');
        } else if (criticalList) {
            criticalList.style.display = 'none';
        }
    } catch (error) {
        console.error('Error updating risk traffic lights:', error);
    }
}

/**
 * Update Forecast Accuracy Widget in Management View
 * Shows schedule success rate and coverage metrics
 */
async function updateForecastAccuracy() {
    try {
        const response = await fetch('/api/scenario/3stage/summary');
        if (!response.ok) return;

        const data = await response.json();

        // Update forecast metrics
        // Backend field is 'scheduled_tasks'; also accept 'tasks_scheduled' for compatibility
        const scheduled = data.scheduled_tasks || data.tasks_scheduled || 0;
        const total = data.total_tasks || 0;
        const unscheduled = total - scheduled;
        const successRate = total > 0 ? ((scheduled / total) * 100).toFixed(1) : 0;

        // Compute average lateness from products if available, else use summary field
        let lateness = data.avg_lateness_days || data.total_lateness_days || 0;
        if (!lateness && scenarioData && scenarioData.products) {
            const lateProducts = scenarioData.products.filter(p => p.latenessDays > 0);
            if (lateProducts.length > 0) {
                lateness = lateProducts.reduce((sum, p) => sum + p.latenessDays, 0) / scenarioData.products.length;
            }
        }

        document.getElementById('forecastScheduled').textContent = scheduled.toLocaleString();
        document.getElementById('forecastSuccessRate').textContent = successRate + '%';
        document.getElementById('forecastUnscheduled').textContent = unscheduled.toLocaleString();
        document.getElementById('forecastLateness').textContent = lateness.toFixed(1) + 'd';

        // Update coverage bar
        document.getElementById('forecastCoveragePercent').textContent = successRate + '%';
        document.getElementById('forecastCoverageBar').style.width = successRate + '%';

        // Color the bar based on success rate
        const bar = document.getElementById('forecastCoverageBar');
        if (successRate >= 95) {
            bar.style.background = 'linear-gradient(90deg, #059669, #34D399)';
        } else if (successRate >= 80) {
            bar.style.background = 'linear-gradient(90deg, #D97706, #FBBF24)';
        } else {
            bar.style.background = 'linear-gradient(90deg, #DC2626, #F87171)';
        }
    } catch (error) {
        console.error('Error updating forecast accuracy:', error);
    }
}

/**
 * Update Utilization Alert Badges in Staffing View
 * Shows warning when teams exceed 90% utilization threshold
 */
function updateUtilizationAlerts(utilizationData) {
    const alertPanel = document.getElementById('utilizationAlertPanel');
    const teamsList = document.getElementById('overloadedTeamsList');

    if (!alertPanel || !teamsList) return;

    // Find teams with >90% utilization
    const overloadedTeams = [];

    if (utilizationData && typeof utilizationData === 'object') {
        Object.entries(utilizationData).forEach(([team, utilization]) => {
            if (utilization >= 90) {
                overloadedTeams.push({ team, utilization });
            }
        });
    }

    if (overloadedTeams.length > 0) {
        alertPanel.style.display = 'block';
        teamsList.innerHTML = overloadedTeams
            .sort((a, b) => b.utilization - a.utilization)
            .map(t => {
                const bgColor = t.utilization >= 100 ? '#FEE2E2' : '#FEF3C7';
                const textColor = t.utilization >= 100 ? '#991B1B' : '#92400E';
                const icon = t.utilization >= 100 ? '🔴' : '🟡';
                return `
                    <span style="background: ${bgColor}; color: ${textColor}; padding: 6px 12px; border-radius: 6px; font-size: 12px; font-weight: 600;">
                        ${icon} ${t.team}: ${t.utilization.toFixed(0)}%
                    </span>
                `;
            }).join('');
    } else {
        alertPanel.style.display = 'none';
    }
}

/**
 * Load Predecessor Blocking Analysis for Industrial Engineering View
 * Shows top tasks that are most frequently blocking downstream work
 */
async function loadBlockingAnalysis() {
    const tbody = document.getElementById('blockingTasksBody');
    const totalBlockersEl = document.getElementById('totalBlockers');
    const tasksAffectedEl = document.getElementById('tasksAffected');
    const topTeamEl = document.getElementById('topBlockingTeam');

    if (!tbody) return;

    tbody.innerHTML = `
        <tr>
            <td colspan="6" style="padding: 30px; text-align: center; color: #9CA3AF;">
                <div style="font-size: 24px; margin-bottom: 8px;">⏳</div>
                Loading blocking analysis...
            </td>
        </tr>
    `;

    try {
        // Get current schedule data to analyze predecessors
        const response = await fetch('/api/scenario/3stage');
        if (!response.ok) throw new Error('Failed to load schedule data');

        const data = await response.json();
        const tasks = data.tasks || [];
        const predecessorsMap = data.predecessors_map || {};

        // Analyze blocking patterns
        const blockingCounts = {};
        const taskTeams = {};

        // Build task team lookup
        tasks.forEach(t => {
            taskTeams[t.task_id || t.soi] = t.team || t.mechanic_team || 'Unknown';
        });

        // Count how many times each task appears as a predecessor
        Object.values(predecessorsMap).forEach(predecessors => {
            predecessors.forEach(predId => {
                blockingCounts[predId] = (blockingCounts[predId] || 0) + 1;
            });
        });

        // Sort by blocking frequency
        const sortedBlockers = Object.entries(blockingCounts)
            .map(([taskId, count]) => ({
                taskId,
                count,
                team: taskTeams[taskId] || 'Unknown',
                impactScore: count * 10 // Simple impact score
            }))
            .sort((a, b) => b.count - a.count)
            .slice(0, 10);

        // Calculate summary stats
        const totalBlockers = Object.keys(blockingCounts).length;
        const totalAffected = Object.values(blockingCounts).reduce((a, b) => a + b, 0);

        // Find top blocking team
        const teamCounts = {};
        sortedBlockers.forEach(b => {
            teamCounts[b.team] = (teamCounts[b.team] || 0) + b.count;
        });
        const topTeam = Object.entries(teamCounts).sort((a, b) => b[1] - a[1])[0];

        // Update summary stats
        if (totalBlockersEl) totalBlockersEl.textContent = totalBlockers.toLocaleString();
        if (tasksAffectedEl) tasksAffectedEl.textContent = totalAffected.toLocaleString();
        if (topTeamEl) topTeamEl.textContent = topTeam ? topTeam[0] : '-';

        // Build table
        if (sortedBlockers.length === 0) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="6" style="padding: 30px; text-align: center; color: #9CA3AF;">
                        <div style="font-size: 24px; margin-bottom: 8px;">✅</div>
                        No significant blocking patterns detected
                    </td>
                </tr>
            `;
            return;
        }

        tbody.innerHTML = sortedBlockers.map((blocker, idx) => {
            const statusColor = blocker.count > 50 ? '#DC2626' : (blocker.count > 20 ? '#D97706' : '#059669');
            const statusLabel = blocker.count > 50 ? 'Critical' : (blocker.count > 20 ? 'High' : 'Normal');
            return `
                <tr style="border-bottom: 1px solid #E2E8F0;">
                    <td style="padding: 10px; font-weight: 600; color: #6366F1;">#${idx + 1}</td>
                    <td style="padding: 10px; font-family: monospace; font-size: 12px;">${blocker.taskId.substring(0, 30)}${blocker.taskId.length > 30 ? '...' : ''}</td>
                    <td style="padding: 10px;">${blocker.team}</td>
                    <td style="padding: 10px; text-align: center; font-weight: 700; color: #DC2626;">${blocker.count}</td>
                    <td style="padding: 10px; text-align: center;">
                        <span style="background: #EEF2FF; color: #4F46E5; padding: 2px 8px; border-radius: 4px; font-weight: 600;">${blocker.impactScore}</span>
                    </td>
                    <td style="padding: 10px; text-align: center;">
                        <span style="background: ${statusColor}22; color: ${statusColor}; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 11px;">${statusLabel}</span>
                    </td>
                </tr>
            `;
        }).join('');
    } catch (error) {
        console.error('Error loading blocking analysis:', error);
        tbody.innerHTML = `
            <tr>
                <td colspan="6" style="padding: 30px; text-align: center; color: #EF4444;">
                    <div style="font-size: 24px; margin-bottom: 8px;">❌</div>
                    Failed to load blocking analysis data
                </td>
            </tr>
        `;
    }
}

// Initialize dashboard enhancements when switching to relevant views
document.addEventListener('DOMContentLoaded', function() {
    // Hook into view changes to load enhancement data
    const originalUpdateManagementView = window.updateManagementView;
    if (originalUpdateManagementView) {
        window.updateManagementView = function() {
            originalUpdateManagementView.apply(this, arguments);
            // Load enhancement widgets
            updateRiskTrafficLights();
            updateForecastAccuracy();
        };
    }

    // Load blocking analysis when IE view is shown
    const ieView = document.getElementById('industrial-engineering-view');
    if (ieView) {
        const observer = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => {
                if (mutation.type === 'attributes' && mutation.attributeName === 'style') {
                    if (ieView.style.display !== 'none') {
                        loadBlockingAnalysis();
                    }
                }
            });
        });
        observer.observe(ieView, { attributes: true });
    }
});

// ============================================================================
// SHIFT PERFORMANCE
// All shift performance functionality has been moved to shift-performance.js
// ============================================================================


// ============================================================================
// DEVELOPMENT BOARD (design v3.2 §9) — /api/development/*
// ============================================================================

function devLead() {
    const el = document.getElementById('dev-lead-name');
    const v = el ? el.value.trim() : '';
    if (v) localStorage.setItem('devLeadName', v);
    return v || localStorage.getItem('devLeadName') || '';
}

function devWindow(b) {
    const hh = m => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
    return `${hh(b.startMinute || 0)}\u2013${hh(b.endMinute || 0)}`;
}

function devStatusCell(b) {
    const led = b.ledger;
    if (!led) return '<span style="color:#6B7280;">planned</span>';
    const color = led.status === 'completed' ? '#059669' : '#DC2626';
    return `<span style="color:${color}; font-weight:600;">${led.status}</span>` +
           (led.lead ? `<div style="font-size:11px; color:#9CA3AF;">${led.lead}</div>` : '');
}

function devActionCell(b) {
    if (b.ledger) return '';
    const id = (b.bookingId || '').replace(/'/g, "\\'");
    return `<button onclick="devCheckoff('${id}','completed')" style="padding:4px 10px; background:#059669; color:white; border:none; border-radius:5px; cursor:pointer; font-size:12px; margin-right:4px;">Done</button>` +
           `<button onclick="devCheckoff('${id}','skipped')" style="padding:4px 10px; background:#DC2626; color:white; border:none; border-radius:5px; cursor:pointer; font-size:12px;">Skip</button>`;
}

async function devCheckoff(bookingId, status) {
    const lead = devLead();
    if (!lead) { alert('Enter your lead name first — check-offs are recorded.'); return; }
    let reason = '';
    if (status === 'skipped') reason = prompt('Why was it skipped? (recorded)') || '';
    try {
        const r = await fetch('/api/development/checkoff', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({booking_id: bookingId, status, reason, lead})
        });
        const j = await r.json();
        if (!r.ok) { alert(j.error || 'check-off failed'); return; }
        updateDevelopmentView();
    } catch (e) { console.error('checkoff failed', e); }
}

async function updateDevelopmentView() {
    const leadEl = document.getElementById('dev-lead-name');
    if (leadEl && !leadEl.value) leadEl.value = localStorage.getItem('devLeadName') || '';
    try {
        const [bRes, benchRes] = await Promise.all([
            fetch('/api/development/board'), fetch('/api/development/bench')]);
        const board = await bRes.json();
        const bench = await benchRes.json();

        const banner = document.getElementById('dev-status-banner');
        if (banner) {
            if (board.mockData) {
                banner.style.display = 'block';
                banner.style.background = '#FEF3C7'; banner.style.color = '#92400E';
                banner.textContent = 'DEMONSTRATION DATA — fictitious roster and proficiency tiers.';
            } else if (board.developmentDegraded) {
                banner.style.display = 'block';
                banner.style.background = '#FEE2E2'; banner.style.color = '#991B1B';
                banner.textContent = 'Development pass degraded to baseline this run — see engine log.';
            } else if (!board.developmentEffective) {
                banner.style.display = 'block';
                banner.style.background = '#E5E7EB'; banner.style.color = '#374151';
                banner.textContent = 'Development pass is disabled or had insufficient data coverage this run.';
            } else banner.style.display = 'none';
        }

        const pairs = board.pairs || [], certs = board.certTime || [];
        const done = [...pairs, ...certs].filter(b => b.ledger && b.ledger.status === 'completed').length;
        const depth = bench.benchDepth || {};
        const thin = Object.values(depth).filter(d => (d.fleet || 0) < 2).length;
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
        set('devPairCount', pairs.length); set('devCertCount', certs.length);
        set('devDoneCount', done); set('devThinBench', thin);

        const pb = document.getElementById('dev-pairs-body');
        if (pb) pb.innerHTML = pairs.map(b => `<tr style="border-bottom:1px solid #F3F4F6;">
            <td style="padding:8px; font-weight:600;">${b.bemsId}</td>
            <td>${b.mentorBemsId || ''}</td><td>${b.taskId || ''}</td>
            <td>${b.family || ''}</td><td>D${b.day} S${b.shift}</td>
            <td>${devWindow(b)}</td><td>${b.minutes}</td>
            <td>${devStatusCell(b)}</td><td>${devActionCell(b)}</td></tr>`).join('')
            || '<tr><td colspan="9" style="padding:12px; color:#9CA3AF;">No shadow pairings in this schedule.</td></tr>';

        const cb = document.getElementById('dev-certs-body');
        if (cb) cb.innerHTML = certs.map(b => `<tr style="border-bottom:1px solid #F3F4F6;">
            <td style="padding:8px; font-weight:600;">${b.bemsId}</td>
            <td>${b.certReqId || ''}</td><td>${b.family || ''}</td>
            <td>D${b.day} S${b.shift}</td><td>${devWindow(b)}</td><td>${b.minutes}</td>
            <td>${devStatusCell(b)}</td><td>${devActionCell(b)}</td></tr>`).join('')
            || '<tr><td colspan="8" style="padding:12px; color:#9CA3AF;">No cert-time blocks in this schedule.</td></tr>';

        const bb = document.getElementById('dev-bench-body');
        if (bb) bb.innerHTML = Object.entries(depth)
            .sort((a, z) => (a[1].fleet || 0) - (z[1].fleet || 0))
            .map(([fam, d]) => {
                const teams = Object.entries(d.byTeam || {})
                    .map(([t, n]) => `${t}:${n}`).join(', ');
                const color = (d.fleet || 0) < 2 ? '#DC2626' : '#059669';
                return `<tr style="border-bottom:1px solid #F3F4F6;">
                    <td style="padding:8px;">${fam}</td>
                    <td style="font-weight:700; color:${color};">${d.fleet || 0}</td>
                    <td style="font-size:12px; color:#6B7280;">${teams}</td></tr>`;
            }).join('')
            || '<tr><td colspan="3" style="padding:12px; color:#9CA3AF;">No bench data (development pass off).</td></tr>';
    } catch (e) { console.error('development board load failed', e); }
}

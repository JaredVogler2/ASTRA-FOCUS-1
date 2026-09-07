/* ============================================================================
   FOCUS - Help Modal System
   Factory Optimized Capacity & Utilization System
   ============================================================================
   Provides contextual help for each dashboard view with a fixed TOC sidebar
   and scrollable help content. Each view has its own curated help guide.
   ============================================================================ */

// ─── Help Content Definitions ───────────────────────────────────────────────
// Each key matches a dashboard view ID (e.g. "team-lead", "staffing", etc.)
// Each entry has: title, intro, and sections[] with id, title, and body (HTML).

const HELP_CONTENT = {

    // ═══════════════════════════════════════════════════════════════════════
    // TEAM LEAD VIEW
    // ═══════════════════════════════════════════════════════════════════════
    'team-lead': {
        title: 'Team Lead View',
        intro: 'The Team Lead View is your primary workspace for managing daily task assignments. It shows what needs to be done, in what order, and lets you assign mechanics to each task.',
        sections: [
            {
                id: 'tl-purpose',
                title: 'What This View Shows',
                body: `
                    <p>This view displays a <strong>prioritized list of tasks</strong> for your team's current shift. Tasks are ordered by scheduling priority so you can see what should be worked on first.</p>
                    <p>Six summary cards at the top give you a quick snapshot:</p>
                    <ul>
                        <li><strong>Team Capacity</strong> &mdash; How many resources are available for the current filter selection</li>
                        <li><strong>Tasks (Shift)</strong> &mdash; Total number of tasks to be completed this shift</li>
                        <li><strong>Utilization</strong> &mdash; Percentage of available capacity that is scheduled (planned work / capacity)</li>
                        <li><strong>Critical Tasks</strong> &mdash; Number of high-priority tasks that could delay aircraft delivery if not completed on time</li>
                        <li><strong>Aircraft</strong> &mdash; Number of aircraft line numbers with active work</li>
                        <li><strong>By Type</strong> &mdash; Task breakdown by type (Production, Quality, Customer, etc.)</li>
                    </ul>
                `
            },
            {
                id: 'tl-filters',
                title: 'Using Filters',
                body: `
                    <p>Filters let you narrow the task list to exactly what you need:</p>
                    <ul>
                        <li><strong>Superintendent</strong> &mdash; Filter by superintendent area. Selecting a superintendent automatically narrows the team list below it.</li>
                        <li><strong>Team</strong> &mdash; Multi-select (hold Ctrl/Cmd and click) to choose one or more teams. "All Mechanic Teams" and "All Quality Teams" are shortcut options.</li>
                        <li><strong>Skill</strong> &mdash; Filter by required skill code</li>
                        <li><strong>Shift</strong> &mdash; Show tasks for Shift 1, 2, or 3 only</li>
                        <li><strong>Work Group</strong> &mdash; Toggle between Mechanic, Quality, Customer, or Vendor tasks</li>
                        <li><strong>Aircraft</strong> &mdash; Show tasks for a specific aircraft line number</li>
                        <li><strong>Customer</strong> &mdash; Filter by airline customer (e.g., UAL, DLH, AAL)</li>
                        <li><strong>Search Task</strong> &mdash; Type a task ID or SOI to jump directly to it. Suggestions appear as you type.</li>
                    </ul>
                `
            },
            {
                id: 'tl-table',
                title: 'The Task Table',
                body: `
                    <p>Each row in the table represents one task. The columns are:</p>
                    <ul>
                        <li><strong>Priority</strong> &mdash; Lower numbers = higher priority. Work these first.</li>
                        <li><strong>Line #</strong> &mdash; The aircraft line number this task belongs to</li>
                        <li><strong>Customer</strong> &mdash; The airline customer for this aircraft</li>
                        <li><strong>Task</strong> &mdash; Click the task ID to view its full dependency chain (predecessors and successors)</li>
                        <li><strong>Task Type</strong> &mdash; Color-coded badge: Production (green), Quality Inspection (blue), Customer Inspection (purple), Rework (red), Late Part (orange)</li>
                        <li><strong>Team</strong> &mdash; Which team is responsible</li>
                        <li><strong>Super</strong> &mdash; Superintendent area</li>
                        <li><strong>Start Date & Time</strong> &mdash; Scheduled start (calendar date, shift, and time)</li>
                        <li><strong>Duration</strong> &mdash; How long the task takes in minutes</li>
                        <li><strong>#Res</strong> &mdash; How many resources are needed simultaneously</li>
                        <li><strong>Assignment</strong> &mdash; Dropdown(s) to assign specific mechanics to this task</li>
                    </ul>
                `
            },
            {
                id: 'tl-assignments',
                title: 'Assigning Mechanics',
                body: `
                    <p>Each task has one or more assignment dropdowns (one per required resource). You can:</p>
                    <ul>
                        <li><strong>Manual Assign</strong> &mdash; Select a mechanic from the dropdown for each slot</li>
                        <li><strong>Smart Auto-Assign</strong> &mdash; Click to automatically distribute work across available mechanics using a balanced workload approach</li>
                        <li><strong>Save / Load</strong> &mdash; Save your assignments to your browser so they persist across page reloads, or load previously saved assignments</li>
                        <li><strong>Export</strong> &mdash; Download the task list and assignments as a spreadsheet</li>
                    </ul>
                    <p>The <strong>Assignment Status</strong> panel (bottom right) tracks how many tasks are fully assigned, partially assigned, or unassigned, with a progress bar.</p>
                `
            },
            {
                id: 'tl-assumptions',
                title: 'Key Assumptions',
                body: `
                    <p>This view operates under the following assumptions:</p>
                    <ul>
                        <li><strong>Shift Schedule</strong> &mdash; The factory operates three shifts:
                            <ul>
                                <li>Shift 1: 6:10 AM &ndash; 1:50 PM</li>
                                <li>Shift 2: 2:40 PM &ndash; 10:20 PM</li>
                                <li>Shift 3: 11:10 PM &ndash; 5:20 AM (next day)</li>
                            </ul>
                        </li>
                        <li><strong>Non-Working Time</strong> &mdash; Each shift has 50 minutes of non-working time: 10 min pre-shift meeting at the start, plus 30 min lunch and 10 min cleanup at the end</li>
                        <li><strong>Task Ordering</strong> &mdash; Tasks are ordered by the optimizer's priority, which respects dependency chains (predecessor tasks are always scheduled before their successors)</li>
                        <li><strong>Weekdays Only</strong> &mdash; No weekend scheduling</li>
                        <li><strong>Resources</strong> &mdash; If a task requires 2 resources, that means 2 mechanics work on it at the same time for the full duration</li>
                    </ul>
                `
            },
            {
                id: 'tl-pagination',
                title: 'Loading More Tasks',
                body: `
                    <p>To keep the page responsive, tasks load in batches of 500. Use the buttons at the bottom of the table:</p>
                    <ul>
                        <li><strong>Load Next 500 Tasks</strong> &mdash; Fetch the next batch</li>
                        <li><strong>Show All</strong> &mdash; Load every remaining task at once (may be slow for very large schedules)</li>
                    </ul>
                `
            }
        ]
    },

    // ═══════════════════════════════════════════════════════════════════════
    // STAFFING DASHBOARD
    // ═══════════════════════════════════════════════════════════════════════
    'staffing': {
        title: 'Staffing Dashboard',
        intro: 'The Staffing Dashboard helps you understand workforce demand and how effectively your teams are being used over time. Use it to identify days with peak demand, under-utilization, or potential staffing shortages.',
        sections: [
            {
                id: 'st-purpose',
                title: 'What This View Shows',
                body: `
                    <p>This dashboard answers key staffing questions:</p>
                    <ul>
                        <li>How many mechanics do I need on any given day?</li>
                        <li>Which days have the highest workload?</li>
                        <li>How well are my teams being utilized?</li>
                        <li>Are there days where mechanics are idle or overloaded?</li>
                    </ul>
                `
            },
            {
                id: 'st-metrics',
                title: 'Summary Metrics',
                body: `
                    <p>The four summary cards at the top show:</p>
                    <ul>
                        <li><strong>Avg Peak Demand</strong> &mdash; Average number of mechanics needed per day</li>
                        <li><strong>Max Peak Demand</strong> &mdash; The single highest headcount required on any day (highlighted in red)</li>
                        <li><strong>Avg Utilization</strong> &mdash; Average percentage of available capacity that is being used</li>
                        <li><strong>Total Work Hours</strong> &mdash; Sum of all scheduled work in hours</li>
                    </ul>
                `
            },
            {
                id: 'st-charts',
                title: 'Charts',
                body: `
                    <p><strong>Peak Demand Headcount per Day</strong> &mdash; A line chart showing how many mechanics are needed each day. Spikes indicate potential staffing bottlenecks.</p>
                    <p><strong>Utilization per Day</strong> &mdash; A bar chart showing what percentage of available mechanic time is actually scheduled. Days above 85% may indicate overloading; days well below 50% suggest under-utilization.</p>
                `
            },
            {
                id: 'st-filters',
                title: 'Using Filters',
                body: `
                    <p>All filters are multi-select (hold Ctrl/Cmd to choose several). Click <strong>Apply Filters</strong> to refresh the data:</p>
                    <ul>
                        <li><strong>Superintendent</strong> &mdash; Focus on specific superintendent areas</li>
                        <li><strong>Shift</strong> &mdash; View demand for Shift 1, 2, 3, or any combination</li>
                        <li><strong>Team</strong> &mdash; Narrow to specific teams</li>
                        <li><strong>Role</strong> &mdash; Filter by Mechanic (production), Quality (QA inspections), or Customer (customer acceptance)</li>
                    </ul>
                    <p>Click <strong>Clear All</strong> to reset all filters back to defaults.</p>
                `
            },
            {
                id: 'st-table',
                title: 'Daily Breakdown Table',
                body: `
                    <p>Below the charts, a table shows day-by-day details:</p>
                    <ul>
                        <li><strong>Day</strong> &mdash; The schedule day number</li>
                        <li><strong>Peak Demand</strong> &mdash; Maximum simultaneous mechanics needed that day</li>
                        <li><strong>Work Minutes</strong> &mdash; Total minutes of scheduled work</li>
                        <li><strong>Capacity Minutes</strong> &mdash; Total available minutes based on headcount and shift length</li>
                        <li><strong>Utilization %</strong> &mdash; Work minutes divided by capacity minutes</li>
                    </ul>
                `
            },
            {
                id: 'st-assumptions',
                title: 'Key Assumptions',
                body: `
                    <ul>
                        <li><strong>Shift Effective Minutes</strong> &mdash; Shift 1 and 2 each have 460 effective minutes; Shift 3 has 370 effective minutes (shorter overnight shift)</li>
                        <li><strong>Non-Working Time</strong> &mdash; 50 minutes per shift: 10 min pre-shift meeting, 30 min lunch, and 10 min cleanup</li>
                        <li><strong>Utilization Cap</strong> &mdash; The system targets 85% utilization per mechanic per shift. Above this, fatigue and context-switching reduce productivity.</li>
                        <li><strong>Quality & Customer Teams</strong> &mdash; Treated as having unlimited capacity because inspectors rotate across multiple lines</li>
                        <li><strong>Peak Demand</strong> &mdash; Represents the maximum number of mechanics working simultaneously at any moment during that day, not the total headcount</li>
                    </ul>
                `
            }
        ]
    },

    // ═══════════════════════════════════════════════════════════════════════
    // MANAGEMENT VIEW
    // ═══════════════════════════════════════════════════════════════════════
    'management': {
        title: 'Management Dashboard',
        intro: 'The Management Dashboard provides a high-level executive overview of production health. It has two sub-tabs: <strong>Dashboard</strong> (KPIs, risk status, delivery projections) and <strong>Burndown Chart</strong> (visual task completion forecast over time).',
        sections: [
            {
                id: 'mg-purpose',
                title: 'What This View Shows',
                body: `
                    <p>This view is designed for managers and executives who need to quickly assess:</p>
                    <ul>
                        <li>Overall production schedule health</li>
                        <li>Which aircraft are on-time, at risk, or critically late</li>
                        <li>Workforce size and average utilization</li>
                        <li>How well the optimizer was able to schedule all tasks</li>
                        <li>How work burns down over the schedule horizon</li>
                    </ul>
                `
            },
            {
                id: 'mg-kpis',
                title: 'Dashboard Tab &mdash; Key Performance Indicators',
                body: `
                    <p>The four KPI cards across the top show:</p>
                    <ul>
                        <li><strong>Total Workforce</strong> &mdash; Total number of mechanics and inspectors used in the schedule</li>
                        <li><strong>Makespan</strong> &mdash; How many working days the full schedule takes from start to finish</li>
                        <li><strong>On-Time Delivery</strong> &mdash; Percentage of aircraft that finish on or before their delivery target</li>
                        <li><strong>Avg Utilization</strong> &mdash; Average percentage of available mechanic capacity that is used across all teams</li>
                    </ul>
                `
            },
            {
                id: 'mg-risk',
                title: 'Aircraft Risk Status',
                body: `
                    <p>The traffic light panel categorizes every aircraft into three buckets:</p>
                    <ul>
                        <li><strong style="color: #059669;">Green (On-Time)</strong> &mdash; Aircraft finishing 0&ndash;1 days late. These are on track.</li>
                        <li><strong style="color: #D97706;">Yellow (At Risk)</strong> &mdash; Aircraft finishing 1&ndash;5 days late. These need attention to prevent further slippage.</li>
                        <li><strong style="color: #DC2626;">Red (Critical)</strong> &mdash; Aircraft finishing more than 5 days late. These require immediate intervention.</li>
                    </ul>
                    <p>If any aircraft are in the red category, a list of critical aircraft appears below the traffic lights.</p>
                `
            },
            {
                id: 'mg-forecast',
                title: 'Schedule Forecast Accuracy',
                body: `
                    <p>This section shows how complete and successful the schedule is:</p>
                    <ul>
                        <li><strong>Tasks Scheduled</strong> &mdash; Total number of tasks placed on the schedule</li>
                        <li><strong>Success Rate</strong> &mdash; Percentage of tasks successfully scheduled (target: >99%)</li>
                        <li><strong>Unscheduled</strong> &mdash; Tasks that could not be placed (conflicts, missing data, etc.)</li>
                        <li><strong>Avg Lateness</strong> &mdash; Average days late across all aircraft</li>
                        <li><strong>Coverage Bar</strong> &mdash; Visual indicator of how many tasks were scheduled vs. total</li>
                    </ul>
                `
            },
            {
                id: 'mg-delivery',
                title: 'Product Delivery Status',
                body: `
                    <p>Each product card shows an individual aircraft's delivery progress, including task completion percentage, scheduled end date, and whether it's on-time or late. Click on a product card to see more details.</p>
                `
            },
            {
                id: 'mg-burndown',
                title: 'Burndown Chart Tab',
                body: `
                    <p>The <strong>Burndown Chart</strong> sub-tab visualizes how the remaining task backlog decreases over time. It shows a stacked bar chart where each day's bar represents tasks scheduled for that day.</p>

                    <h5>Color Modes</h5>
                    <p>Toggle between three coloring modes using the buttons above the chart:</p>
                    <ul>
                        <li><strong>By Aircraft</strong> &mdash; Each aircraft line number gets a unique color, so you can see which aircraft dominate each day's workload</li>
                        <li><strong>By Team</strong> &mdash; Colors represent teams, showing how work is distributed across your workforce</li>
                        <li><strong>By Type</strong> &mdash; Colors represent task types (Production, Quality, Customer, Rework, Late Parts)</li>
                    </ul>

                    <h5>Filters</h5>
                    <ul>
                        <li><strong>Aircraft</strong> &mdash; Multi-select to show only specific aircraft</li>
                        <li><strong>Team</strong> &mdash; Multi-select to show only specific teams</li>
                        <li><strong>Task Type</strong> &mdash; Multi-select to show only specific task types</li>
                    </ul>

                    <h5>Summary Stats</h5>
                    <ul>
                        <li><strong>Total Jobs</strong> &mdash; True unique jobs (excluding duration segments)</li>
                        <li><strong>Schedule Span</strong> &mdash; Total working days in the schedule</li>
                        <li><strong>Peak Day</strong> &mdash; The day with the most jobs scheduled</li>
                        <li><strong>Avg/Day</strong> &mdash; Average number of jobs per working day</li>
                    </ul>
                    <p>The <strong>Breakdown Summary</strong> table below the chart shows totals per category (aircraft, team, or type depending on color mode).</p>
                `
            },
            {
                id: 'mg-assumptions',
                title: 'Key Assumptions',
                body: `
                    <ul>
                        <li><strong>Delivery Targets</strong> &mdash; Each aircraft has a target delivery date based on the production contract</li>
                        <li><strong>Lateness</strong> &mdash; Measured as the number of working days between the last task's scheduled completion and the aircraft's delivery target. Positive = late, zero or negative = on-time.</li>
                        <li><strong>Makespan</strong> &mdash; Counted in working days only (weekends excluded)</li>
                        <li><strong>Utilization</strong> &mdash; Aggregated across all teams and all shifts for the full schedule duration</li>
                    </ul>
                `
            }
        ]
    },

    // ═══════════════════════════════════════════════════════════════════════
    // MECHANIC (INDIVIDUAL) VIEW
    // ═══════════════════════════════════════════════════════════════════════
    'mechanic': {
        title: 'Individual Schedule View',
        intro: 'The Individual Schedule View lets you look up any mechanic or inspector and see their personal task timeline. Use it to check workload, confirm assignments, or identify scheduling conflicts.',
        sections: [
            {
                id: 'mc-purpose',
                title: 'What This View Shows',
                body: `
                    <p>Select a resource (mechanic or inspector) from the dropdown to see their assigned tasks displayed as a timeline. Each task shows:</p>
                    <ul>
                        <li>Task ID and type (production, quality, customer)</li>
                        <li>Aircraft line number</li>
                        <li>Scheduled start and end time</li>
                        <li>Duration</li>
                        <li>Which shift it falls on</li>
                    </ul>
                `
            },
            {
                id: 'mc-filters',
                title: 'Finding a Resource',
                body: `
                    <p>The filter controls help you narrow down the resource dropdown:</p>
                    <ul>
                        <li><strong>Team</strong> &mdash; Filter by team to show only mechanics on that team</li>
                        <li><strong>Skill</strong> &mdash; Filter by skill code</li>
                        <li><strong>Shift</strong> &mdash; Show only resources working a specific shift</li>
                        <li><strong>Search</strong> &mdash; Type a name or mechanic ID to quickly find them</li>
                    </ul>
                    <p>Once you select a resource, their full task schedule appears as a timeline below.</p>
                `
            },
            {
                id: 'mc-timeline',
                title: 'Reading the Timeline',
                body: `
                    <p>Tasks are displayed chronologically along a timeline. Each task bar is color-coded by type:</p>
                    <ul>
                        <li><strong>Green</strong> &mdash; Production work</li>
                        <li><strong>Blue</strong> &mdash; Quality inspection</li>
                        <li><strong>Purple</strong> &mdash; Customer inspection</li>
                        <li><strong>Red</strong> &mdash; Rework</li>
                    </ul>
                    <p>Hover over a task bar to see full details. Gaps between tasks represent available time or non-working periods.</p>
                `
            },
            {
                id: 'mc-assumptions',
                title: 'Key Assumptions',
                body: `
                    <ul>
                        <li><strong>No Overlap</strong> &mdash; A mechanic cannot be assigned to two tasks at the same time. If you see overlapping tasks, it indicates a scheduling conflict.</li>
                        <li><strong>Shift Assignment</strong> &mdash; Each mechanic works one primary shift, but may appear on adjacent shifts if overtime is scheduled</li>
                        <li><strong>Resource ID</strong> &mdash; Resources are identified by their team, skill, shift, and an index number (e.g., "FGI-CF-CUSREADY | S1 | Mechanic #3")</li>
                    </ul>
                `
            }
        ]
    },

    // ═══════════════════════════════════════════════════════════════════════
    // SHIFT PERFORMANCE VIEW
    // ═══════════════════════════════════════════════════════════════════════
    'shift-performance': {
        title: 'Shift Performance',
        intro: 'The Shift Performance dashboard measures shift effectiveness using three dimensions: <strong>Throughput</strong> (did you do enough?), <strong>Alignment</strong> (did you do the right things?), and <strong>Impact</strong> (did it actually improve delivery projections?). These combine into a single <strong>Performance Index</strong> (PI) that balances volume, quality, and outcome.',
        sections: [
            {
                id: 'sp-overview',
                title: 'Schedule Overview Panel',
                body: `
                    <p>At the top of the dashboard, the <strong>Schedule Overview</strong> panel shows a side-by-side comparison of the "before" (plan) and "after" (actual) schedules:</p>
                    <ul>
                        <li><strong>Before / After total</strong> &mdash; Total tasks in each schedule</li>
                        <li><strong>Completed</strong> &mdash; Tasks present in the before schedule that are missing from the after schedule (work that was done)</li>
                        <li><strong>New</strong> &mdash; Tasks in the after schedule that weren't in the before schedule (new work that appeared)</li>
                        <li><strong>Net delta</strong> &mdash; Overall change in task count</li>
                    </ul>
                    <p>A per-shift breakdown table shows how tasks are distributed across shifts, with the evaluated shift highlighted. If the before and after schedules contain the exact same tasks, a yellow warning appears indicating no work was completed between the two runs.</p>
                `
            },
            {
                id: 'sp-purpose',
                title: 'How It Works',
                body: `
                    <p>Performance is measured by comparing two schedule snapshots:</p>
                    <ul>
                        <li><strong>Before Schedule</strong> (the plan) &mdash; The optimized schedule generated before the shift starts</li>
                        <li><strong>After Schedule</strong> (actual results) &mdash; The updated schedule generated after the shift ends</li>
                    </ul>
                    <p>Task status is determined by comparing the two:</p>
                    <ul>
                        <li><strong>Completed</strong> &mdash; Task was in the plan but is <em>not</em> in the after schedule (it was removed because it's done)</li>
                        <li><strong>Incomplete</strong> &mdash; Task appears in both schedules (still pending)</li>
                        <li><strong>Unscheduled</strong> &mdash; Task appears only in the after schedule (bonus work not on the original plan)</li>
                    </ul>
                    <p>Only tasks planned for the <em>current day</em> (earliest day in the schedule) are measured. Future tasks are excluded.</p>
                `
            },
            {
                id: 'sp-resource-types',
                title: 'Resource Type Selector',
                body: `
                    <p>The colored pills below the schedule info let you switch between resource-type views. Performance is measured <strong>separately by resource type</strong> because a single SOI (Statement of Intent) expands into multiple scheduled segments (mechanic work, QA inspections, customer inspections):</p>
                    <ul>
                        <li><strong>Production (Mechanic)</strong> &mdash; The primary KPI. Each base SOI produces exactly one mechanic task, so Production throughput equals true SOI-level throughput. This is the default view.</li>
                        <li><strong>Quality</strong> &mdash; In-process (INPROC_INSP) and final (FINAL_INSP) inspection tasks. Typically 1&ndash;2 per base SOI.</li>
                        <li><strong>Customer</strong> &mdash; Customer acceptance inspections (CC_UAL, CC_DLH, etc.). 0&ndash;1 per base SOI.</li>
                        <li><strong>Vendor</strong> &mdash; Rare external work. Low volume, so metrics may be sparse.</li>
                        <li><strong>All Resources</strong> &mdash; Combined view of all types. Task counts are higher than the SOI count because of segment expansion.</li>
                    </ul>
                `
            },
            {
                id: 'sp-threemetric',
                title: 'Three-Metric Scoring Model',
                body: `
                    <h5>1. Throughput (Quantity)</h5>
                    <p>What percentage of today's planned tasks were completed?</p>
                    <ul>
                        <li>Only measures against <em>today's</em> planned work, not the entire multi-day horizon</li>
                        <li>Capped at 100%. Unscheduled tasks (extra work) are tracked separately as bonus productivity.</li>
                        <li>Formula: <code>Throughput = min(100%, completed / planned_today &times; 100)</code></li>
                    </ul>

                    <h5>2. Alignment (Quality)</h5>
                    <p>Were completed tasks the ones the schedule valued most? Uses the optimizer's <strong>priority_score</strong> (a composite of delivery urgency, critical path status, predecessor readiness, and successor impact).</p>
                    <ul>
                        <li>Formula: <code>Alignment = schedule_value_earned / schedule_value_planned &times; 100</code></li>
                        <li>If you complete the top 50 tasks by value and skip the bottom 50, alignment is high. Skip the top 50? Alignment is low.</li>
                        <li>If no tasks were completed, alignment is 0 (undefined &mdash; you can't measure quality of work that wasn't done).</li>
                    </ul>

                    <h5>3. Impact (Outcome)</h5>
                    <p>Did aircraft delivery projections actually improve?</p>
                    <ul>
                        <li>Compares projected lateness between the before and after schedules for each aircraft</li>
                        <li>Produces an <strong>Impact Factor</strong>: improved lateness gives up to 1.5x bonus, worsened lateness penalizes down to 0.5x</li>
                    </ul>
                `
            },
            {
                id: 'sp-pi',
                title: 'Performance Index Formula',
                body: `
                    <p style="text-align:center; font-size:16px; font-weight:700; margin:16px 0; padding:12px; background:#f0f9ff; border-radius:8px;">
                        PI = Throughput &times; (Alignment / 100) &times; Impact Factor
                    </p>
                    <p><strong>Examples:</strong></p>
                    <table style="width:100%; border-collapse:collapse; font-size:13px;">
                        <thead><tr style="background:#f1f5f9;"><th style="padding:6px;text-align:left;">Scenario</th><th>Throughput</th><th>Alignment</th><th>Impact</th><th>PI</th></tr></thead>
                        <tbody>
                            <tr><td style="padding:6px;">Perfect shift</td><td>100%</td><td>100%</td><td>1.0x</td><td><strong>100</strong></td></tr>
                            <tr><td style="padding:6px;">Did everything, wrong priorities</td><td>100%</td><td>50%</td><td>1.0x</td><td><strong>50</strong></td></tr>
                            <tr><td style="padding:6px;">Did half, but the right half</td><td>50%</td><td>90%</td><td>1.0x</td><td><strong>45</strong></td></tr>
                            <tr><td style="padding:6px;">Good work + delivery improved</td><td>80%</td><td>85%</td><td>1.2x</td><td><strong>82</strong></td></tr>
                            <tr><td style="padding:6px;">Some work but delivery slipped</td><td>60%</td><td>70%</td><td>0.7x</td><td><strong>29</strong></td></tr>
                        </tbody>
                    </table>
                    <p style="margin-top:12px;"><strong>Letter grades:</strong> A+ (&ge;95), A (&ge;90), A- (&ge;85), B+ (&ge;80), B (&ge;75), B- (&ge;70), C+ (&ge;65), C (&ge;60), D+ (&ge;55), D (&ge;50), F (&lt;50)</p>
                `
            },
            {
                id: 'sp-tabs',
                title: 'Audience Tabs',
                body: `
                    <p>Five tabs tailor the view for different leadership levels:</p>
                    <ul>
                        <li><strong>Floor Manager</strong> &mdash; Team-level cards sorted by Performance Index (worst first). Each card shows throughput %, alignment %, and PI with progress bars. Click any team for task-level drill-down. Filter by superintendent.</li>
                        <li><strong>Production Manager</strong> &mdash; Superintendent-level rollup cards with three-metric display. "Needs Improvement" table of bottom teams. Delay reason breakdown chart. Cross-superintendent comparison chart.</li>
                        <li><strong>Executive Summary</strong> &mdash; Factory-wide hero display with large Performance Index, letter grade, and three-metric breakdown. Aircraft delivery impact table. Team grade distribution chart.</li>
                        <li><strong>Schedule Impact</strong> &mdash; Aircraft delivery projection comparison. Downstream blocking analysis: cascade count per incomplete task (BFS, 5 levels deep). Top 10 most impactful misses. Per-aircraft blocking breakdown. Full incomplete task list with delay reasons.</li>
                        <li><strong>Shift Progress</strong> &mdash; Intra-shift timeline view showing task completion cadence during the shift. Helps identify when work stalled or surged.</li>
                    </ul>
                `
            },
            {
                id: 'sp-workload',
                title: 'Workload Weighting',
                body: `
                    <p>A team with 2 planned tasks shouldn't have the same influence on the factory score as a team carrying 200 tasks.</p>
                    <p>The system handles this through <strong>workload-weighted rollups</strong>:</p>
                    <ul>
                        <li>When rolling up team scores to superintendent or factory level, each team's contribution is <strong>weighted by their planned task count</strong></li>
                        <li>A team with 200 planned tasks contributes 100x more to the superintendent's score than a team with 2 tasks</li>
                        <li>This means a small team getting a perfect score barely moves the needle, while a large team's performance dominates</li>
                    </ul>
                    <p>Individual team cards still show unweighted scores so team leads see their own performance honestly.</p>
                `
            },
            {
                id: 'sp-delays',
                title: 'Delay Reasons',
                body: `
                    <p>When a task is not completed, team leads can record a delay reason by clicking the task in the Floor Manager drill-down:</p>
                    <ul>
                        <li><strong>Held by Predecessor</strong> &mdash; A predecessor task was not finished (supports multiple predecessor entries with notes)</li>
                        <li><strong>Parts Shortage, Equipment Failure, Quality Issue, Rework Required</strong></li>
                        <li><strong>Insufficient Staffing, Tooling Unavailable, Engineering Hold, Safety Issue</strong></li>
                        <li><strong>Training Needed, Priority Changed, Other</strong></li>
                    </ul>
                    <p>Delay reasons appear on team drill-down views, in the Production Manager delay breakdown chart, and in the Schedule Impact tab. Entering reasons helps identify systemic issues (e.g., if 40% of delays are "Parts Shortage", that points to supply chain problems).</p>
                `
            },
            {
                id: 'sp-assumptions',
                title: 'Key Assumptions',
                body: `
                    <ul>
                        <li><strong>Today Only</strong> &mdash; Performance is measured against today's planned tasks only (earliest day in the schedule), not the entire multi-day horizon</li>
                        <li><strong>Completion = Removal</strong> &mdash; A task is "completed" when it no longer appears in the after schedule. Removed tasks are assumed finished, not deleted.</li>
                        <li><strong>Unscheduled Tasks</strong> &mdash; Extra work that appears only in the after schedule. Tracked separately, contributes 25% of average schedule value to alignment, does NOT inflate throughput above 100%.</li>
                        <li><strong>Two Schedules Required</strong> &mdash; A "before" and "after" schedule must exist. The system auto-selects the two most recent schedule files for the evaluated shift.</li>
                        <li><strong>Zero Alignment When Zero Throughput</strong> &mdash; If no tasks were completed and no unscheduled work was done, alignment is forced to 0% (N/A). You cannot measure the quality of work that wasn't done.</li>
                    </ul>
                `
            }
        ]
    },

    // ═══════════════════════════════════════════════════════════════════════
    // PROJECT VIEW
    // ═══════════════════════════════════════════════════════════════════════
    'project': {
        title: 'Project Gantt View',
        intro: 'The Project Gantt View displays all scheduled tasks as a visual timeline, organized by aircraft. It shows how work flows across products and teams, and highlights critical path tasks that drive delivery dates.',
        sections: [
            {
                id: 'pj-purpose',
                title: 'What This View Shows',
                body: `
                    <p>This is a traditional Gantt chart where each row represents a task and columns represent time periods. Task bars are positioned according to their scheduled start and end dates.</p>
                    <p>The summary cards at the top show:</p>
                    <ul>
                        <li><strong>Total Tasks</strong> &mdash; Number of tasks currently displayed</li>
                        <li><strong>Total Duration</strong> &mdash; Span of the visible schedule in working days</li>
                        <li><strong>Critical Tasks</strong> &mdash; Tasks on the critical path (any delay here delays the aircraft)</li>
                        <li><strong>On Schedule</strong> &mdash; Percentage of tasks that are on-time</li>
                    </ul>
                `
            },
            {
                id: 'pj-filters',
                title: 'Filters & Controls',
                body: `
                    <ul>
                        <li><strong>Product</strong> &mdash; Show tasks for one aircraft or all aircraft</li>
                        <li><strong>Team</strong> &mdash; Narrow to a specific team's tasks</li>
                        <li><strong>Critical Path</strong> &mdash; Toggle between all tasks, critical path only, or non-critical only</li>
                        <li><strong>View Scale</strong> &mdash; Zoom the timeline: 1 Day, 1 Week, 2 Weeks, or 1 Month per column</li>
                        <li><strong>Sort By</strong> &mdash; Reorder rows by Start Date, Product, Team, or Priority</li>
                        <li><strong>Refresh</strong> &mdash; Reload the Gantt with current filter settings</li>
                        <li><strong>Export</strong> &mdash; Download the Gantt data</li>
                        <li><strong>Fit All</strong> &mdash; Adjust the view to show the entire schedule</li>
                    </ul>
                `
            },
            {
                id: 'pj-colors',
                title: 'Color Legend',
                body: `
                    <p>Task bars are color-coded by type:</p>
                    <ul>
                        <li><span style="color: #34C759; font-weight: 700;">Green</span> &mdash; Production tasks (actual build work)</li>
                        <li><span style="color: #0033A0; font-weight: 700;">Blue</span> &mdash; Quality inspection tasks</li>
                        <li><span style="color: #FF3B30; font-weight: 700;">Red</span> &mdash; Rework tasks</li>
                        <li><span style="color: #FF9500; font-weight: 700;">Orange</span> &mdash; Late parts (waiting on material)</li>
                        <li><span style="color: #AF52DE; font-weight: 700;">Purple</span> &mdash; Customer inspection tasks</li>
                        <li><span style="border: 2px solid #FFCC00; padding: 0 4px;">Yellow Border</span> &mdash; Critical path task</li>
                    </ul>
                `
            },
            {
                id: 'pj-assumptions',
                title: 'Key Assumptions',
                body: `
                    <ul>
                        <li><strong>Task Dependencies</strong> &mdash; Tasks respect a strict predecessor-successor ordering. A production task must complete before its quality inspection can begin.</li>
                        <li><strong>Critical Path</strong> &mdash; The longest chain of dependent tasks that determines the earliest possible completion date for each aircraft. Delays to critical path tasks directly delay delivery.</li>
                        <li><strong>Task Segmentation</strong> &mdash; Tasks longer than one shift are automatically split into segments that fit within individual shifts.</li>
                        <li><strong>Weekdays Only</strong> &mdash; The timeline only includes working days (Monday&ndash;Friday)</li>
                    </ul>
                `
            }
        ]
    },

    // ═══════════════════════════════════════════════════════════════════════
    // WORKER GANTT VIEW
    // ═══════════════════════════════════════════════════════════════════════
    'worker-gantt': {
        title: 'Worker Gantt View',
        intro: 'The Worker Gantt shows the schedule organized by individual worker rather than by task. Each row represents one mechanic, and you can see exactly what they are working on during each shift.',
        sections: [
            {
                id: 'wg-purpose',
                title: 'What This View Shows',
                body: `
                    <p>Unlike the Project Gantt (organized by task), this view is organized by <strong>worker</strong>. Each row is a mechanic or inspector, and the horizontal axis is time broken into shifts.</p>
                    <p>This makes it easy to see:</p>
                    <ul>
                        <li>What each person is working on right now</li>
                        <li>Whether anyone has gaps or is overloaded</li>
                        <li>How work is distributed across the team</li>
                    </ul>
                `
            },
            {
                id: 'wg-controls',
                title: 'Controls',
                body: `
                    <ul>
                        <li><strong>Team</strong> &mdash; Filter to a specific team</li>
                        <li><strong>Shift</strong> &mdash; Show workers on 1st, 2nd, or 3rd shift</li>
                        <li><strong>Skillset</strong> &mdash; Filter by skillset</li>
                        <li><strong>Worker</strong> &mdash; Focus on a single worker</li>
                        <li><strong>View Date</strong> &mdash; Select which date to center the view on</li>
                        <li><strong>Show</strong> &mdash; Display 1 Day, 2 Days, 1 Week, or 2 Weeks</li>
                        <li><strong>Back / Forward</strong> &mdash; Navigate through time</li>
                        <li><strong>Refresh</strong> &mdash; Reload the chart with current filters</li>
                    </ul>
                `
            },
            {
                id: 'wg-reading',
                title: 'Reading the Chart',
                body: `
                    <p>Each colored bar represents one task assigned to that worker. Tasks are color-coded by aircraft line number so you can see which aircraft each person is working on.</p>
                    <p>Hover over any bar to see task details including task ID, aircraft, duration, and shift.</p>
                    <p>Special indicators:</p>
                    <ul>
                        <li><strong>Red border</strong> &mdash; Critical path task</li>
                        <li><strong>Yellow highlight</strong> &mdash; Upstream or downstream dependency of a selected task</li>
                    </ul>
                    <p>The legend at the bottom shows which color corresponds to which aircraft.</p>
                `
            },
            {
                id: 'wg-assumptions',
                title: 'Key Assumptions',
                body: `
                    <ul>
                        <li><strong>Shift Boundaries</strong> &mdash;
                            <ul>
                                <li>1st Shift: 6:10 AM &ndash; 1:50 PM</li>
                                <li>2nd Shift: 2:40 PM &ndash; 10:20 PM</li>
                                <li>3rd Shift: 11:10 PM &ndash; 5:20 AM (crosses midnight)</li>
                            </ul>
                        </li>
                        <li><strong>Worker Assignment</strong> &mdash; Each worker is assigned to tasks by the optimizer to balance workload evenly. The same worker will not have overlapping tasks.</li>
                        <li><strong>Cross-Aircraft Work</strong> &mdash; A single mechanic may work on multiple different aircraft during the same shift</li>
                    </ul>
                `
            }
        ]
    },

    // ═══════════════════════════════════════════════════════════════════════
    // SUPPLY CHAIN VIEW
    // ═══════════════════════════════════════════════════════════════════════
    'supply-chain': {
        title: 'Supply Chain Analysis',
        intro: 'The Supply Chain view identifies parts that are arriving late and shows how those delays ripple through the schedule. Use it to prioritize which late parts need the most urgent attention based on their downstream impact.',
        sections: [
            {
                id: 'sc-purpose',
                title: 'What This View Shows',
                body: `
                    <p>This view lists all parts where the expected on-dock date is later than the scheduled task start date. For each late part, it calculates the total downstream impact, helping supply chain managers prioritize expediting efforts.</p>
                    <p>The total count of late parts is shown at the bottom of the view.</p>
                `
            },
            {
                id: 'sc-table',
                title: 'Late Parts Table',
                body: `
                    <p>Each row represents one late part with the following columns:</p>
                    <ul>
                        <li><strong>Part ID</strong> &mdash; The identifier of the late part / task</li>
                        <li><strong>Product</strong> &mdash; Which aircraft line number is affected</li>
                        <li><strong>On-Dock Date</strong> &mdash; When the part is expected to arrive</li>
                        <li><strong>Scheduled Start</strong> &mdash; When the task was originally supposed to begin</li>
                        <li><strong>Impact Score</strong> &mdash; A calculated severity metric (affected tasks &times; downstream hours). Higher is worse.</li>
                        <li><strong>Affected Tasks</strong> &mdash; How many downstream tasks are blocked by this late part</li>
                        <li><strong>Downstream (Hours)</strong> &mdash; Total work hours of all blocked downstream tasks</li>
                        <li><strong>Direct Successor(s)</strong> &mdash; The immediate next tasks waiting on this part</li>
                        <li><strong>Team</strong> &mdash; Which team owns the affected work</li>
                    </ul>
                `
            },
            {
                id: 'sc-impact',
                title: 'Understanding Impact Score',
                body: `
                    <p>The <strong>Impact Score</strong> helps you prioritize which late parts to chase first. It is calculated as:</p>
                    <p style="text-align: center; font-weight: 600; font-size: 16px; margin: 12px 0;">Impact = Number of Affected Tasks &times; Total Downstream Hours</p>
                    <p>A part blocking many tasks with long durations gets a high score. Focus expediting efforts on the highest-impact parts first.</p>
                `
            },
            {
                id: 'sc-assumptions',
                title: 'Key Assumptions',
                body: `
                    <ul>
                        <li><strong>Late Identification</strong> &mdash; A part is considered "late" when its on-dock date falls after the scheduled start of the task that needs it</li>
                        <li><strong>Downstream Cascade</strong> &mdash; Impact includes not just the directly blocked task, but all successor tasks in the dependency chain</li>
                        <li><strong>Static Snapshot</strong> &mdash; This data reflects the schedule as currently loaded. Actual part arrival dates may change.</li>
                    </ul>
                `
            }
        ]
    },

    // ═══════════════════════════════════════════════════════════════════════
    // INDUSTRIAL ENGINEERING VIEW
    // ═══════════════════════════════════════════════════════════════════════
    'industrial-engineering': {
        title: 'Industrial Engineering',
        intro: 'The IE view provides a review queue for tasks that may need duration adjustments or process improvements. It helps industrial engineers identify tasks where standard times may not match actual performance.',
        sections: [
            {
                id: 'ie-purpose',
                title: 'What This View Shows',
                body: `
                    <p>This dashboard surfaces tasks for industrial engineering review. Tasks may be flagged due to:</p>
                    <ul>
                        <li>Scheduling conflicts or resource constraints</li>
                        <li>Duration variance between standard and actual times</li>
                        <li>Predecessor blocking issues</li>
                        <li>Manual flags from team leads or supervisors</li>
                    </ul>
                `
            },
            {
                id: 'ie-table',
                title: 'IE Review Queue',
                body: `
                    <p>The table shows flagged tasks with columns for:</p>
                    <ul>
                        <li><strong>Task ID</strong> &mdash; The specific task under review</li>
                        <li><strong>Product</strong> &mdash; Which aircraft it belongs to</li>
                        <li><strong>Standard Duration</strong> &mdash; The expected time per engineering standards</li>
                        <li><strong>Actual Duration</strong> &mdash; How long the task actually took (when data is available)</li>
                        <li><strong>Variance %</strong> &mdash; Percentage difference between standard and actual time. Large variances suggest the standard needs updating.</li>
                        <li><strong>Frequency</strong> &mdash; How often this task appears across aircraft</li>
                        <li><strong>Actions</strong> &mdash; Options to review, resolve, or escalate the flag</li>
                    </ul>
                `
            },
            {
                id: 'ie-assumptions',
                title: 'Key Assumptions',
                body: `
                    <ul>
                        <li><strong>Standard Durations</strong> &mdash; Based on historical averages and engineering estimates. These are the durations used by the optimizer to build the schedule.</li>
                        <li><strong>Flag Categories</strong> &mdash; Tasks can be flagged as: Scheduling Conflict, Resource Unavailable, Predecessor Blocking, or Other</li>
                        <li><strong>Review Workflow</strong> &mdash; Flagged items remain in the queue until an IE engineer reviews and resolves or dismisses them</li>
                    </ul>
                `
            }
        ]
    },

    // ═══════════════════════════════════════════════════════════════════════
    // SCHEDULE BUDGET VIEW
    // ═══════════════════════════════════════════════════════════════════════
    'schedule-budget': {
        title: 'Schedule Budget',
        intro: 'The Schedule Budget view provides resource allocation and labor budget analysis. It helps managers understand the cost implications of the current schedule.',
        sections: [
            {
                id: 'sb-purpose',
                title: 'What This View Shows',
                body: `
                    <p>This view analyzes the schedule from a budget and resource cost perspective, helping answer questions like:</p>
                    <ul>
                        <li>How much labor is being scheduled per team?</li>
                        <li>What are the total labor hours for each aircraft?</li>
                        <li>Where are the biggest resource cost drivers?</li>
                    </ul>
                    <p><em>This module is under active development. Additional budget analytics will be added in future releases.</em></p>
                `
            },
            {
                id: 'sb-assumptions',
                title: 'Key Assumptions',
                body: `
                    <ul>
                        <li><strong>Labor Hours</strong> &mdash; Derived from scheduled task durations and resource counts</li>
                        <li><strong>Team Budgets</strong> &mdash; Each team has allocated capacity that can be compared against scheduled demand</li>
                        <li><strong>Overtime</strong> &mdash; Shift 1 and 2 may include overtime extending the shift by up to 60 minutes; Shift 3 may extend by up to 60 minutes</li>
                    </ul>
                `
            }
        ]
    },

    // ═══════════════════════════════════════════════════════════════════════
    // SCENARIO VIEW
    // ═══════════════════════════════════════════════════════════════════════
    'scenario': {
        title: 'Priority Impact Analysis (Scenarios)',
        intro: 'The Scenario view lets you perform "what-if" analysis by changing aircraft priority rankings and instantly seeing the estimated impact on delivery dates. It helps planners evaluate trade-offs before committing to schedule changes.',
        sections: [
            {
                id: 'sn-purpose',
                title: 'What This View Shows',
                body: `
                    <p>This view helps you answer: <em>"What happens if we prioritize Aircraft X over Aircraft Y?"</em></p>
                    <p>By assigning priority ranks to specific aircraft, you can estimate how the schedule would change without running a full re-optimization. This provides quick, directional guidance for priority decisions.</p>
                `
            },
            {
                id: 'sn-howto',
                title: 'How to Use It',
                body: `
                    <ol>
                        <li><strong>Set Priority Ranks</strong> &mdash; In the "Aircraft Priority Order" section, assign rank 1 (highest priority) through N for aircraft you want to prioritize. Leave aircraft as "Any" to let the system decide their placement.</li>
                        <li><strong>Add a Label</strong> (optional) &mdash; Give your scenario a descriptive name like "Rush Line 1256"</li>
                        <li><strong>Click "Estimate Impact"</strong> &mdash; The system instantly calculates the estimated schedule changes</li>
                        <li><strong>Review Results</strong> &mdash; The comparison table shows baseline vs. estimated completion for each aircraft</li>
                    </ol>
                `
            },
            {
                id: 'sn-results',
                title: 'Reading the Results',
                body: `
                    <p>The results table shows for each aircraft:</p>
                    <ul>
                        <li><strong>Aircraft</strong> &mdash; The aircraft line number</li>
                        <li><strong>Rank</strong> &mdash; The priority rank you assigned</li>
                        <li><strong>Tasks</strong> &mdash; Number of tasks for this aircraft</li>
                        <li><strong>Baseline Day</strong> &mdash; Current scheduled completion day</li>
                        <li><strong>Est. Day</strong> &mdash; Estimated completion day with new priorities</li>
                        <li><strong>Baseline Late</strong> &mdash; Current days late (from delivery target)</li>
                        <li><strong>Est. Late</strong> &mdash; Estimated days late with new priorities</li>
                        <li><strong>Impact</strong> &mdash; Shows whether the aircraft improves, worsens, or stays the same</li>
                    </ul>
                    <p>The <strong>Resource Contention Details</strong> section explains which teams would experience scheduling conflicts under the new priority arrangement.</p>
                `
            },
            {
                id: 'sn-comparison',
                title: 'Comparing Scenarios',
                body: `
                    <p>Each time you run an estimate, it's saved to the "Previous Estimates" list. You can compare multiple scenarios side by side in the <strong>Scenario Comparison</strong> table to see which priority arrangement produces the best overall outcome.</p>
                    <p>Click <strong>Clear All</strong> to remove saved scenarios and start fresh.</p>
                `
            },
            {
                id: 'sn-assumptions',
                title: 'Key Assumptions',
                body: `
                    <ul>
                        <li><strong>Estimate, Not Re-optimization</strong> &mdash; The impact estimate uses heuristic calculations, not a full schedule rebuild. Results are directional (showing trends) rather than exact.</li>
                        <li><strong>Zero-Sum Trade-offs</strong> &mdash; Prioritizing one aircraft generally means other aircraft may be delayed. The system highlights these trade-offs.</li>
                        <li><strong>Resource Contention</strong> &mdash; When multiple aircraft compete for the same team's capacity, changing priority affects which aircraft gets scheduled first on that team.</li>
                        <li><strong>Baseline</strong> &mdash; The currently loaded schedule is used as the baseline for all comparisons</li>
                    </ul>
                `
            }
        ]
    }
};


// ─── Help Modal Functions ────────────────────────────────────────────────────

/**
 * Open the help modal for the currently active dashboard view.
 */
function openHelpModal() {
    const view = typeof currentView !== 'undefined' ? currentView : 'team-lead';
    const content = HELP_CONTENT[view];
    if (!content) {
        console.warn(`No help content for view: ${view}`);
        return;
    }

    // Build TOC
    const tocHTML = content.sections.map(s =>
        `<a class="help-toc-link" href="#${s.id}" onclick="scrollHelpTo('${s.id}'); return false;">${s.title}</a>`
    ).join('');

    // Build body content
    const bodyHTML = `
        <div id="help-content-top"></div>
        <div class="help-intro">${content.intro}</div>
        ${content.sections.map(s => `
            <div class="help-section" id="${s.id}">
                <h3 class="help-section-title">${s.title}</h3>
                <div class="help-section-body">${s.body}</div>
                <a href="#help-content-top" class="help-return-top" onclick="scrollHelpTo('help-content-top'); return false;">Return to top</a>
            </div>
        `).join('')}
    `;

    // Set content
    document.getElementById('help-modal-title').textContent = content.title + ' — Help Guide';
    document.getElementById('help-toc-body').innerHTML = tocHTML;
    document.getElementById('help-body-content').innerHTML = bodyHTML;

    // Show modal
    const modal = document.getElementById('help-modal');
    modal.style.display = 'flex';
    modal.classList.add('show');

    // Reset scroll position
    document.getElementById('help-body-content').scrollTop = 0;

    // Highlight first TOC item
    updateActiveTocLink();
}

/**
 * Close the help modal.
 */
function closeHelpModal() {
    const modal = document.getElementById('help-modal');
    modal.classList.remove('show');
    modal.style.display = 'none';
}

/**
 * Scroll help content to a specific section.
 */
function scrollHelpTo(sectionId) {
    const container = document.getElementById('help-body-content');
    const target = document.getElementById(sectionId);
    if (container && target) {
        const offset = target.offsetTop - container.offsetTop;
        container.scrollTo({ top: offset, behavior: 'smooth' });
    }
}

/**
 * Update the active TOC link based on scroll position.
 */
function updateActiveTocLink() {
    const container = document.getElementById('help-body-content');
    if (!container) return;

    const sections = container.querySelectorAll('.help-section');
    const tocLinks = document.querySelectorAll('.help-toc-link');
    let activeId = '';

    sections.forEach(section => {
        const rect = section.getBoundingClientRect();
        const containerRect = container.getBoundingClientRect();
        if (rect.top <= containerRect.top + 80) {
            activeId = section.id;
        }
    });

    tocLinks.forEach(link => {
        const linkTarget = link.getAttribute('href').replace('#', '');
        link.classList.toggle('active', linkTarget === activeId);
    });
}

// Set up scroll listener on the help content area once the DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    // Help modal close on backdrop click
    const helpModal = document.getElementById('help-modal');
    if (helpModal) {
        helpModal.addEventListener('click', function(e) {
            if (e.target === this) {
                closeHelpModal();
            }
        });
    }

    // Scroll-spy for TOC
    const helpContent = document.getElementById('help-body-content');
    if (helpContent) {
        helpContent.addEventListener('scroll', updateActiveTocLink);
    }

    // Keyboard: Escape to close
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            const modal = document.getElementById('help-modal');
            if (modal && modal.classList.contains('show')) {
                closeHelpModal();
            }
        }
    });
});

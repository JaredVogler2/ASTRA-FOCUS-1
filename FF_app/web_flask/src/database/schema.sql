-- FOCUS Production Scheduling Database Schema
-- Tracks shift performance, delay reasons, and accountability metrics

-- Task Delay Reasons Table
-- Records why tasks were not completed during their assigned shift
CREATE TABLE IF NOT EXISTS task_delay_reasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_soi VARCHAR(50) NOT NULL,
    line_number INTEGER NOT NULL,
    shift_date DATE NOT NULL,
    shift_number INTEGER NOT NULL CHECK (shift_number IN (1, 2, 3)),
    team VARCHAR(50) NOT NULL,
    delay_reason VARCHAR(100) NOT NULL,
    notes TEXT,
    entered_by VARCHAR(100),
    entered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Ensure one delay reason per task per shift
    UNIQUE(task_soi, line_number, shift_date, shift_number)
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_delay_shift
ON task_delay_reasons(shift_date, shift_number, team);

CREATE INDEX IF NOT EXISTS idx_delay_task
ON task_delay_reasons(task_soi, line_number);

CREATE INDEX IF NOT EXISTS idx_delay_entered
ON task_delay_reasons(entered_at);

-- Delay Reason Categories Reference Table
CREATE TABLE IF NOT EXISTS delay_reason_categories (
    category_code VARCHAR(50) PRIMARY KEY,
    category_name VARCHAR(100) NOT NULL,
    description TEXT,
    requires_notes BOOLEAN DEFAULT 0,
    is_active BOOLEAN DEFAULT 1,
    display_order INTEGER DEFAULT 0
);

-- Insert standard delay reason categories
INSERT OR IGNORE INTO delay_reason_categories (category_code, category_name, description, requires_notes, display_order) VALUES
('PARTS_SHORTAGE', 'Parts Shortage', 'Required parts not available', 0, 1),
('EQUIPMENT_FAILURE', 'Equipment Failure', 'Tools or equipment malfunction', 0, 2),
('QUALITY_ISSUE', 'Quality Issue', 'Quality problem discovered', 0, 3),
('REWORK_REQUIRED', 'Rework Required', 'Task needs rework before proceeding', 0, 4),
('INSUFFICIENT_STAFFING', 'Insufficient Staffing', 'Not enough mechanics available', 0, 5),
('TOOLING_UNAVAILABLE', 'Tooling Unavailable', 'Required tools not accessible', 0, 6),
('ENGINEERING_HOLD', 'Engineering Hold', 'Engineering has placed hold on task', 1, 7),
('SAFETY_ISSUE', 'Safety Issue', 'Safety concern prevents task completion', 1, 8),
('TRAINING_NEEDED', 'Training Needed', 'Mechanic requires additional training', 0, 9),
('HELD_PREDECESSOR', 'Held by Predecessor Task(s)', 'Waiting on predecessor task(s) to complete', 1, 10),
('PRIORITY_CHANGED', 'Priority Changed', 'Task reprioritized by management', 1, 11),
('OTHER', 'Other', 'Other reason (specify in notes)', 1, 12);

-- Held Predecessor Details Table
-- Stores specific predecessor task information when delay reason is HELD_PREDECESSOR
-- Supports multiple predecessor entries per delayed task
-- NOTE: Predecessors are ALWAYS on the same line number as the delayed task
--       (line_number inherited from parent task_delay_reasons record)
CREATE TABLE IF NOT EXISTS held_predecessor_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delay_reason_id INTEGER NOT NULL,

    -- Predecessor task identification (optional - mechanic may not know task number)
    -- Predecessor is ALWAYS on same line_number as delayed task (no need to store separately)
    predecessor_task_soi VARCHAR(50),

    -- Description of what they're waiting for (REQUIRED)
    notes TEXT NOT NULL,

    -- Metadata
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (delay_reason_id) REFERENCES task_delay_reasons(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_held_predecessor_delay
ON held_predecessor_details(delay_reason_id);

CREATE INDEX IF NOT EXISTS idx_held_predecessor_task
ON held_predecessor_details(predecessor_task_soi);

-- Shift Performance Scores Table
-- Stores calculated performance scores for teams/superintendents/factory
CREATE TABLE IF NOT EXISTS shift_performance_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_date DATE NOT NULL,
    shift_number INTEGER NOT NULL CHECK (shift_number IN (1, 2, 3)),
    level VARCHAR(20) NOT NULL CHECK (level IN ('team', 'superintendent', 'factory')),
    entity_name VARCHAR(100) NOT NULL,  -- Team name, Superintendent name, or 'Factory'

    -- Scoring metrics
    planned_points INTEGER NOT NULL,
    earned_points INTEGER NOT NULL,
    score_percentage REAL NOT NULL,
    grade VARCHAR(5) NOT NULL,

    -- Task breakdown
    total_tasks_planned INTEGER NOT NULL,
    tasks_completed INTEGER NOT NULL,
    tasks_incomplete INTEGER NOT NULL,
    tasks_incomplete_with_reason INTEGER NOT NULL,
    tasks_incomplete_no_reason INTEGER NOT NULL,
    unscheduled_tasks_completed INTEGER NOT NULL,

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    schedule_before_file VARCHAR(255),
    schedule_after_file VARCHAR(255),

    UNIQUE(shift_date, shift_number, level, entity_name)
);

-- Indexes for performance tracking
CREATE INDEX IF NOT EXISTS idx_performance_shift
ON shift_performance_scores(shift_date, shift_number);

CREATE INDEX IF NOT EXISTS idx_performance_entity
ON shift_performance_scores(level, entity_name);

-- Performance Details Table
-- Stores individual task performance for drill-down analysis
CREATE TABLE IF NOT EXISTS shift_performance_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    performance_score_id INTEGER NOT NULL,
    task_soi VARCHAR(50) NOT NULL,
    line_number INTEGER NOT NULL,
    team VARCHAR(50) NOT NULL,

    -- Task status
    was_on_plan BOOLEAN NOT NULL,
    was_completed BOOLEAN NOT NULL,
    priority_rank INTEGER,
    successor_count INTEGER DEFAULT 0,  -- Number of successors from DAG (bottleneck indicator)

    -- Scoring
    points_earned INTEGER NOT NULL,

    -- Delay info (if not completed)
    has_delay_reason BOOLEAN DEFAULT 0,
    delay_reason VARCHAR(100),

    FOREIGN KEY (performance_score_id) REFERENCES shift_performance_scores(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_performance_details_score
ON shift_performance_details(performance_score_id);

CREATE INDEX IF NOT EXISTS idx_performance_details_task
ON shift_performance_details(task_soi, line_number);

-- Schedule Completion History Table
-- Tracks per-line-number projected completion datetime across shifts
-- to detect cumulative schedule slide when shifts don't adhere to optimized plans.
-- Data comes directly from the optimizer's products array (projectedCompletion field).
CREATE TABLE IF NOT EXISTS schedule_completion_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_number INTEGER NOT NULL,
    shift_date DATE NOT NULL,
    shift_number INTEGER NOT NULL CHECK (shift_number IN (1, 2, 3)),

    -- The optimizer's projected completion for this aircraft at this point in time
    projected_completion TEXT NOT NULL,       -- datetime string from products array
    completion_abs_minutes INTEGER NOT NULL,  -- numeric completionAbsMinutes from products

    -- Delivery deadline for context
    delivery_date TEXT,
    lateness_days INTEGER DEFAULT 0,
    total_tasks_remaining INTEGER DEFAULT 0,

    -- Source schedule file
    schedule_file VARCHAR(255),
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(line_number, shift_date, shift_number)
);

CREATE INDEX IF NOT EXISTS idx_completion_history_line
ON schedule_completion_history(line_number, shift_date, shift_number);

CREATE INDEX IF NOT EXISTS idx_completion_history_date
ON schedule_completion_history(shift_date, shift_number);

# src/blueprints/scenarios.py

from flask import Blueprint, jsonify, current_app, request
from datetime import datetime, timedelta
from collections import defaultdict

# The in-process what-if machinery (run_what_if_scenario, dependency-map
# constraints) was not ported — this dashboard's schedules come from the
# MAX (Fable) engine, run out of process. Routes that needed the live
# scheduler now return 501 explicitly.


scenarios_bp = Blueprint('scenarios', __name__, url_prefix='/api')

@scenarios_bp.route('/scenarios')
def get_scenarios():
    """Get list of available scenarios with descriptions"""
    scheduler = current_app.scheduler
    return jsonify({
        'scenarios': [
            {
                'id': 'baseline',
                'name': 'Baseline',
                'description': 'Schedule with CSV-defined headcount using product-task instances'
            },
            {
                'id': 'scenario1',
                'name': 'Scenario 1: CSV Headcount',
                'description': 'Schedule with CSV-defined team capacities'
            },
            {
                'id': 'scenario2',
                'name': 'Scenario 2: Minimize Makespan',
                'description': 'Find uniform headcount for shortest schedule'
            },
            {
                'id': 'scenario3',
                'name': 'Scenario 3: Multi-Dimensional',
                'description': 'Optimize per-team capacity using simulated annealing to achieve target delivery (1 day early)'
            }
        ],
        'architecture': 'Product-Task Instances with Customer Inspections',
        'totalInstances': len(scheduler.tasks) if scheduler else 0,
        'inspectionLayers': {
            'quality': len(scheduler.quality_team_capacity) if scheduler else 0,
            'customer': len(scheduler.customer_team_capacity) if scheduler else 0
        }
    })

@scenarios_bp.route('/refresh', methods=['POST'])
def refresh_data():
    """Re-scan outputs/schedules/ and reload the newest two envelopes.

    Run the MAX (Fable) engine (`python run.py run` in MAX/) to produce a
    new plan, then hit this endpoint — no server restart needed."""
    try:
        reload_schedules = getattr(current_app, 'reload_schedules', None)
        if reload_schedules is None:
            return jsonify({
                'success': False,
                'error': 'Reload hook unavailable. Restart the server to reload.'
            }), 501
        if reload_schedules():
            return jsonify({
                'success': True,
                'message': f'Reloaded schedules. Current: {current_app.current_schedule_file}',
                'current': current_app.current_schedule_file,
                'previous': current_app.previous_schedule_file,
            })
        return jsonify({
            'success': False,
            'error': 'No schedules found in outputs/schedules/. '
                     'Generate one with the MAX engine: python run.py run'
        }), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@scenarios_bp.route('/scenario_progress/<scenario_id>')
def get_scenario_progress(scenario_id):
    # This logic for progress tracking might need to be re-evaluated
    # as computation_progress is not defined here.
    # For now, returning a placeholder.
    computation_progress = {}
    return jsonify({
        'progress': computation_progress.get(scenario_id, 0),
        'status': 'computing' if scenario_id in computation_progress else 'idle'
    })

@scenarios_bp.route('/scenario/<scenario_id>')
def get_scenario_data(scenario_id):
    scenario_results = current_app.scenario_results
    if scenario_id not in scenario_results:
        return jsonify({'error': f'Scenario {scenario_id} not found'}), 404

    # Make a copy to avoid modifying the cached results
    scenario_data = scenario_results[scenario_id].copy()

    return jsonify(scenario_data)

@scenarios_bp.route('/scenario/<scenario_id>/summary')
def get_scenario_summary(scenario_id):
    """Get summary statistics for a scenario"""
    scenario_results = current_app.scenario_results
    if scenario_id not in scenario_results:
        return jsonify({'error': 'Scenario not found'}), 404

    data = scenario_results[scenario_id]

    product_summaries = []
    for product in data.get('products', []):
        product_summaries.append({
            'name': product['name'],
            'status': 'On Time' if product['onTime'] else f"Late by {product['latenessDays']} days",
            'taskRange': product.get('taskRange', 'Unknown'),
            'remainingCount': product.get('remainingCount', 0),
            'totalTasks': product['totalTasks'],
            'taskBreakdown': product.get('taskBreakdown', {})
        })

    summary = {
        'scenarioName': data['scenarioId'],
        'totalWorkforce': data['totalWorkforce'],
        'makespan': data['makespan'],
        'onTimeRate': data['onTimeRate'],
        'avgUtilization': data['avgUtilization'],
        'maxLateness': data.get('maxLateness', 0),
        'totalLateness': data.get('totalLateness', 0),
        'achievedMaxLateness': data.get('achievedMaxLateness', data.get('maxLateness', 0)),
        'totalTaskInstances': data.get('totalTaskInstances', 0),
        'scheduledTaskInstances': data.get('scheduledTaskInstances', 0),
        'taskTypeSummary': data.get('taskTypeSummary', {}),
        'productSummaries': product_summaries,
        'instanceBased': True
    }

    return jsonify(summary)


@scenarios_bp.route('/scenarios/run_what_if', methods=['POST'])
def run_what_if():
    """Legacy in-process what-if — not available in this port."""
    return jsonify({
        'error': 'run_what_if requires the in-process scheduler, which was '
                 'replaced by the MAX (Fable) engine in this port. Use '
                 'POST /api/scenarios/estimate_priority for priority '
                 'impact estimation.'
    }), 501


@scenarios_bp.route('/products')
def get_products():
    """Get a list of all unique product lines for scenario planning."""
    # First try production mode: loaded schedule data (FGI-5 or 3-stage)
    schedule_data = getattr(current_app, 'current_schedule_data', None)
    if schedule_data and 'products' in schedule_data and schedule_data['products']:
        product_names = sorted(list(set(p['name'] for p in schedule_data['products'])))
        return jsonify(product_names)

    # Fallback to baseline scenario results (old CP-SAT mode)
    if 'baseline' in current_app.scenario_results:
        baseline_results = current_app.scenario_results['baseline']
        if 'products' in baseline_results and baseline_results['products']:
            product_names = sorted(list(set(p['name'] for p in baseline_results['products'])))
            return jsonify(product_names)

    # Fallback if neither source is available
    return jsonify([])


@scenarios_bp.route('/scenarios/saved')
def get_saved_scenarios():
    """Get a list of all saved what-if scenarios."""
    return jsonify(current_app.saved_scenarios)


@scenarios_bp.route('/task/<scenario_id>/<task_id>/chain')
def get_task_chain(scenario_id, task_id):
    """
    Get the full upstream (predecessor) and downstream (successor) chain for a given task,
    using the pre-computed dynamic dependency maps.
    """
    # Handle 3-stage scenario specially
    if scenario_id == '3stage':
        # Check if we're in production mode (loaded from files)
        if hasattr(current_app, 'current_schedule_data'):
            active = getattr(current_app, 'active_schedule', 'current')
            if active == 'current':
                scenario_data = current_app.current_schedule_data
            else:
                scenario_data = getattr(current_app, 'previous_schedule_data', None)
                if not scenario_data:
                    return jsonify({'error': 'Previous schedule not available'}), 404

            # Dependency maps must come from the envelope itself — there
            # is no in-process DAG state in this port to rebuild them from.
            if 'predecessors_map' not in scenario_data or 'successors_map' not in scenario_data:
                return jsonify({
                    'error': 'Dependency maps not available in this schedule',
                    'message': 'This schedule was generated without dependency information. Please regenerate the schedule using the latest version.'
                }), 404
        else:
            # Development mode - export on the fly
            return jsonify({'error': 'In-process scheduler not available in this port; load a schedule generated by the MAX engine'}), 501
            # Unreachable legacy dev-mode path below (kept for reference):
            scenario_data = export_3stage_scenario(current_app)
    else:
        # Regular scenario from scenario_results
        scenario_data = current_app.scenario_results.get(scenario_id)
        if not scenario_data:
            return jsonify({'error': f'Scenario {scenario_id} not found'}), 404

    # Use the comprehensive dependency maps from the scenario data
    predecessors_map = scenario_data.get('predecessors_map', {})
    successors_map = scenario_data.get('successors_map', {})
    task_map = {t['taskId']: t for t in scenario_data.get('tasks', [])}

    # Debug logging
    print(f"\n=== Task Chain Debug for {task_id} ===")
    print(f"Total tasks in task_map: {len(task_map)}")
    print(f"Total entries in predecessors_map: {len(predecessors_map)}")
    print(f"Total entries in successors_map: {len(successors_map)}")
    print(f"Task {task_id} in task_map: {task_id in task_map}")
    print(f"Task {task_id} predecessors: {predecessors_map.get(task_id, [])}")
    print(f"Task {task_id} successors: {successors_map.get(task_id, [])}")
    if predecessors_map:
        sample_keys = list(predecessors_map.keys())[:3]
        print(f"Sample predecessor map keys: {sample_keys}")
    if successors_map:
        sample_keys = list(successors_map.keys())[:3]
        print(f"Sample successor map keys: {sample_keys}")
    print(f"===================================\n")

    target_task = task_map.get(task_id)
    if not target_task:
        # Fallback to searching all tasks if not in the dashboard's top 1000
        if current_app.scheduler is not None:
            all_tasks_from_scheduler = current_app.scheduler.tasks
            if task_id in all_tasks_from_scheduler:
                target_task = all_tasks_from_scheduler[task_id]
                target_task['taskId'] = task_id # Ensure taskId is present

        # If still not found, create minimal task info from dependency maps
        if not target_task:
            # Check if task exists in predecessor or successor maps
            has_predecessors = task_id in predecessors_map
            has_successors = task_id in successors_map
            if has_predecessors or has_successors:
                # Task exists in dependency maps, create minimal info
                target_task = {
                    'taskId': task_id,
                    'type': 'Unknown',
                    'product': None,
                    'team': None,
                    'startTime': None
                }
            else:
                return jsonify({'error': f'Task {task_id} not found in scenario {scenario_id}'}), 404


    def get_task_info(node_id):
        """Get task info for a node, with fallbacks for tasks not in dashboard view."""
        task_info = task_map.get(node_id)
        if not task_info and current_app.scheduler is not None:
            raw_task = current_app.scheduler.tasks.get(node_id, {})
            task_info = {
                'taskId': node_id,
                'type': raw_task.get('type', 'Unknown'),
                'product': raw_task.get('product'),
                'team': raw_task.get('team'),
                'startTime': None,
                'mechanic_id': raw_task.get('mechanic_id'),
                'resource_team': raw_task.get('resource_team'),
                'workGroup': raw_task.get('workGroup'),
                'isCustomerTask': raw_task.get('isCustomerTask', False),
                'isQualityTask': raw_task.get('isQualityTask', False),
            }
        elif not task_info:
            # Extract line number from taskId suffix (e.g., "FADCEILGN0003_SEG1_1256" -> "Line 1256")
            import re
            line_match = re.search(r'_(\d{4})$', node_id)
            product_label = f"Line {line_match.group(1)}" if line_match else None

            # Infer task type AND work group from taskId naming patterns
            task_type = 'Unknown'
            work_group = 'mechanic'
            is_customer = False
            is_quality = False
            upper_id = node_id.upper()
            if '_INPROC_INSP_' in upper_id:
                task_type = 'QA Inspection'
                work_group = 'quality'
                is_quality = True
            elif '_FINAL_INSP_' in upper_id:
                task_type = 'QA Inspection'
                work_group = 'quality'
                is_quality = True
            elif '_CC_' in upper_id or '_CUST_INSP_' in upper_id:
                task_type = 'Customer Inspection'
                work_group = 'customer'
                is_customer = True
            elif '_SEG' in upper_id or '_DURSEG' in upper_id:
                task_type = 'Production'

            task_info = {
                'taskId': node_id,
                'type': task_type,
                'product': product_label,
                'team': None,
                'startTime': None,
                'mechanic_id': None,
                'resource_team': None,
                'workGroup': work_group,
                'isCustomerTask': is_customer,
                'isQualityTask': is_quality,
            }
        return task_info

    def get_all_dependencies(start_node_id, graph, max_depth=10):
        """
        BFS traversal to get ALL dependencies (all branches), not just first.
        Returns list of tasks with their dependency relationships.
        """
        from collections import deque

        all_tasks = []
        visited = {start_node_id}
        queue = deque([(start_node_id, 0)])  # (node_id, depth)

        while queue:
            current_node_id, depth = queue.popleft()

            if depth > max_depth:
                continue

            # Get all next nodes (predecessors or successors depending on graph)
            next_nodes = graph.get(current_node_id, [])

            for next_node_id in next_nodes:
                if next_node_id in visited:
                    continue  # Already processed

                task_info = get_task_info(next_node_id)
                if task_info:
                    # Add branch info to track which dependency path this came from
                    task_info_copy = dict(task_info)
                    task_info_copy['depth'] = depth + 1
                    task_info_copy['parentId'] = current_node_id
                    task_info_copy['branchCount'] = len(next_nodes)  # How many siblings
                    all_tasks.append(task_info_copy)

                    visited.add(next_node_id)
                    queue.append((next_node_id, depth + 1))

        return all_tasks

    # Get ALL dependencies (all branches) using BFS
    upstream_tasks = get_all_dependencies(task_id, predecessors_map, max_depth=10)
    downstream_tasks = get_all_dependencies(task_id, successors_map, max_depth=10)

    # Sort upstream by depth (deepest first so chain reads chronologically)
    upstream_chain = sorted(upstream_tasks, key=lambda x: -x.get('depth', 0))
    # Sort downstream by depth (shallowest first)
    downstream_chain = sorted(downstream_tasks, key=lambda x: x.get('depth', 0))

    # Filter chains to a directional 5-day window from the target task's start time
    # Tasks without startTime are included (they may be outside the loaded pagination)
    if target_task.get('startTime'):
        try:
            target_start_time = datetime.fromisoformat(target_task['startTime'])
            time_window = timedelta(days=5)

            # Filter upstream chain: -5 days from target start
            # Include tasks without startTime (they're outside loaded pagination but still relevant)
            # Use <= so tasks at the exact same time as target are included
            # (e.g., DURSEG siblings or inspections scheduled at same time)
            filtered_upstream = []
            for task in upstream_chain:
                task_start = task.get('startTime')
                if task_start:
                    task_start_time = datetime.fromisoformat(task_start)
                    if target_start_time - time_window <= task_start_time <= target_start_time:
                        filtered_upstream.append(task)
                else:
                    # Include tasks without startTime (minimal task info from traversal)
                    filtered_upstream.append(task)
            upstream_chain = filtered_upstream

            # Filter downstream chain: +5 days from target start
            # Include tasks without startTime (they're outside loaded pagination but still relevant)
            # Use >= so tasks at the exact same time as target are included
            # (e.g., DURSEG successors or inspections scheduled at same time)
            filtered_downstream = []
            for task in downstream_chain:
                task_start = task.get('startTime')
                if task_start:
                    task_start_time = datetime.fromisoformat(task_start)
                    if target_start_time <= task_start_time <= target_start_time + time_window:
                        filtered_downstream.append(task)
                else:
                    # Include tasks without startTime (minimal task info from traversal)
                    filtered_downstream.append(task)
            downstream_chain = filtered_downstream

        except (ValueError, TypeError):
            # If date parsing fails, fall back to unfiltered chains
            pass


    # Add the main task to both chains for context
    upstream_chain.append(target_task)
    downstream_chain.insert(0, target_task)

    def format_task_details(task_list):
        # Include branch info for tree rendering and resource info for correct display
        return [
            {
                'taskId': t.get('taskId'),
                'type': t.get('type', 'Unknown'),
                'product': t.get('product', 'Unknown'),
                'team': t.get('team', 'Unknown'),
                'startTime': t.get('startTime'),
                'depth': t.get('depth', 0),
                'parentId': t.get('parentId'),
                'branchCount': t.get('branchCount', 1),
                # Resource info for correct mechanic/inspector/customer display
                'mechanic_id': t.get('mechanic_id'),
                'resource_team': t.get('resource_team'),
                'workGroup': t.get('workGroup', 'mechanic'),
                'isCustomerTask': t.get('isCustomerTask', False),
                'isQualityTask': t.get('isQualityTask', False),
            } for t in task_list
        ]

    # Count total dependencies (not just filtered)
    total_predecessors = len(predecessors_map.get(task_id, []))
    total_successors = len(successors_map.get(task_id, []))

    return jsonify({
        'task_id': task_id,
        'predecessors': format_task_details(upstream_chain),
        'successors': format_task_details(downstream_chain),
        'product_line': target_task.get('product'),
        'total_predecessor_count': total_predecessors,
        'total_successor_count': total_successors,
        'filtered_predecessor_count': len(upstream_chain) - 1,  # Exclude target task
        'filtered_successor_count': len(downstream_chain) - 1   # Exclude target task
    })


# ============================================================================
# 3-STAGE SCHEDULER API ENDPOINTS
# ============================================================================

@scenarios_bp.route('/scenario/3stage', methods=['GET'])
def get_3stage_scenario():
    """
    Get 3-stage scheduler results with optional filtering and pagination

    Query Parameters:
    - offset: Starting position (default: 0)
    - limit: Number of tasks to return (default: 500, 0 = all tasks)
    - team: Filter by team (optional)
    - skill: Filter by skill (optional)
    - shift: Filter by shift (optional)
    - workgroup: Filter by work group - 'mechanic', 'quality', or 'customer' (optional)
    - product: Filter by product/line number (optional)
    - superintendent: Filter by superintendent (optional, e.g., "S-CF-POSITION 1")
    - search: Search term for task ID, SOI, or keywords (optional)
    """
    try:
        # Get pagination and filter parameters
        offset = request.args.get('offset', 0, type=int)
        limit = request.args.get('limit', 500, type=int)
        filter_team = request.args.get('team', None)
        filter_skill = request.args.get('skill', None)
        filter_shift_raw = request.args.get('shift', None)
        filter_shift = int(filter_shift_raw) if filter_shift_raw and filter_shift_raw.isdigit() else None
        filter_workgroup = request.args.get('workgroup', None)
        filter_product = request.args.get('product', None)
        filter_superintendent = request.args.get('superintendent', None)
        search_term = request.args.get('search', '').strip().lower()

        # Check if we're in production mode (loaded from files)
        if hasattr(current_app, 'current_schedule_data'):
            # Production mode - return data from loaded JSON
            active = getattr(current_app, 'active_schedule', 'current')

            if active == 'current':
                data = current_app.current_schedule_data.copy()
            else:
                data = getattr(current_app, 'previous_schedule_data', None)
                if not data:
                    return jsonify({'error': 'Previous schedule not available'}), 404
                data = data.copy()

            # Get all tasks
            all_tasks = data.get('tasks', [])
            total_tasks = len(all_tasks)

            # Apply filters if provided (search first for best performance)
            filtered_tasks = all_tasks
            if search_term or filter_team or filter_skill or filter_shift or filter_workgroup or filter_product or filter_superintendent:
                filtered_tasks = []
                for task in all_tasks:
                    # Apply search filter first (most selective)
                    if search_term:
                        # Search across multiple fields (case-insensitive)
                        task_id = str(task.get('taskId', '')).lower()
                        soi = str(task.get('soi', '')).lower()
                        task_type = str(task.get('type', '')).lower()
                        team_skill = str(task.get('teamSkill', '')).lower()

                        # Check if search term appears in any searchable field
                        if not (search_term in task_id or
                                search_term in soi or
                                search_term in task_type or
                                search_term in team_skill):
                            continue  # Skip task if search doesn't match

                    # Parse team and skill from teamSkill field (use regex for robustness)
                    team_skill = task.get('teamSkill', task.get('team', ''))
                    task_team = task.get('team', '')
                    task_skill = task.get('skill', 'ANY')

                    if '(' in team_skill:
                        import re
                        # Handle shift-aware format "TEAM S{N} (SKILL)" and legacy "TEAM (SKILL)"
                        ts_match = re.match(r'^(.+?)\s+S(\d)\s*\((.+?)\)\s*$', team_skill)
                        if ts_match:
                            task_team = ts_match.group(1).strip()
                            task_skill = ts_match.group(3).strip()
                        else:
                            ts_match_legacy = re.match(r'^(.+?)\s*\((.+?)\)\s*$', team_skill)
                            if ts_match_legacy:
                                task_team = ts_match_legacy.group(1).strip()
                                task_skill = ts_match_legacy.group(2).strip()

                    # Apply superintendent filter
                    if filter_superintendent and filter_superintendent != 'all':
                        task_superintendent = task.get('superintendent', '')
                        if task_superintendent != filter_superintendent:
                            continue

                    # Apply other filters (support comma-separated multi-team)
                    if filter_team and filter_team != 'all':
                        filter_teams = [t.strip() for t in filter_team.split(',')]
                        if task_team not in filter_teams:
                            continue
                    if filter_skill and filter_skill != 'all' and task_skill != filter_skill:
                        continue
                    if filter_shift and task.get('shift') != filter_shift:
                        continue

                    # Apply work group filter (mechanic, quality, customer, vendor)
                    if filter_workgroup and filter_workgroup != 'all':
                        # Use workGroup field if available, otherwise derive from flags
                        task_workgroup = task.get('workGroup')
                        if not task_workgroup:
                            # Derive work group from task flags
                            if task.get('isCustomerTask'):
                                task_workgroup = 'customer'
                            elif task.get('isQualityTask'):
                                task_workgroup = 'quality'
                            elif task.get('isVendorTask'):
                                task_workgroup = 'vendor'
                            else:
                                task_workgroup = 'mechanic'
                        if task_workgroup != filter_workgroup:
                            continue

                    if filter_product and filter_product != 'all' and task.get('product') != filter_product:
                        continue

                    filtered_tasks.append(task)

            filtered_count = len(filtered_tasks)

            # Apply pagination (only if limit > 0)
            if limit > 0:
                paginated_tasks = filtered_tasks[offset:offset + limit]
            else:
                paginated_tasks = filtered_tasks

            # Update data with paginated tasks
            data['tasks'] = paginated_tasks
            data['pagination'] = {
                'offset': offset,
                'limit': limit,
                'returned': len(paginated_tasks),
                'filtered_total': filtered_count,
                'total': total_tasks,
                'has_more': (offset + len(paginated_tasks)) < filtered_count
            }

            return jsonify(data)
        else:
            # Development mode - export from app context
            return jsonify({'error': 'In-process scheduler not available in this port; load a schedule generated by the MAX engine'}), 501
            # Unreachable legacy dev-mode path below (kept for reference):
            result = export_3stage_scenario(current_app)

            # Apply same filters as production mode
            all_tasks = result.get('tasks', [])
            total_tasks = len(all_tasks)

            filtered_tasks = all_tasks
            if search_term or filter_team or filter_skill or filter_shift or filter_workgroup or filter_product or filter_superintendent:
                filtered_tasks = []
                for task in all_tasks:
                    if search_term:
                        task_id_s = str(task.get('taskId', '')).lower()
                        soi_s = str(task.get('soi', '')).lower()
                        task_type_s = str(task.get('type', '')).lower()
                        team_skill_s = str(task.get('teamSkill', '')).lower()
                        if not (search_term in task_id_s or search_term in soi_s or
                                search_term in task_type_s or search_term in team_skill_s):
                            continue

                    task_team = task.get('team', '')
                    task_skill = task.get('skill', 'ANY')

                    if filter_superintendent and filter_superintendent != 'all':
                        if task.get('superintendent', '') != filter_superintendent:
                            continue
                    if filter_team and filter_team != 'all':
                        filter_teams = [t.strip() for t in filter_team.split(',')]
                        if task_team not in filter_teams:
                            continue
                    if filter_skill and filter_skill != 'all' and task_skill != filter_skill:
                        continue
                    if filter_shift and task.get('shift') != filter_shift:
                        continue
                    if filter_workgroup and filter_workgroup != 'all':
                        task_workgroup = task.get('workGroup')
                        if not task_workgroup:
                            if task.get('isCustomerTask'):
                                task_workgroup = 'customer'
                            elif task.get('isQualityTask'):
                                task_workgroup = 'quality'
                            elif task.get('isVendorTask'):
                                task_workgroup = 'vendor'
                            else:
                                task_workgroup = 'mechanic'
                        if task_workgroup != filter_workgroup:
                            continue
                    if filter_product and filter_product != 'all' and task.get('product') != filter_product:
                        continue

                    filtered_tasks.append(task)

            filtered_count = len(filtered_tasks)

            if limit > 0:
                paginated_tasks = filtered_tasks[offset:offset + limit]
            else:
                paginated_tasks = filtered_tasks

            result['tasks'] = paginated_tasks
            result['pagination'] = {
                'offset': offset,
                'limit': limit,
                'returned': len(paginated_tasks),
                'filtered_total': filtered_count,
                'total': total_tasks,
                'has_more': (offset + len(paginated_tasks)) < filtered_count
            }

            return jsonify(result)
    except Exception as e:
        return jsonify({
            'error': str(e),
            'message': '3-stage scheduler not initialized'
        }), 500


@scenarios_bp.route('/scenario/3stage/aircraft/<int:line_number>', methods=['GET'])
def get_aircraft_tasks_3stage(line_number):
    """Get all tasks for specific aircraft"""
    try:
        # Check if we're in production mode (loaded from files)
        if hasattr(current_app, 'current_schedule_data'):
            # Production mode - filter tasks from loaded JSON
            active = getattr(current_app, 'active_schedule', 'current')

            if active == 'current':
                data = current_app.current_schedule_data
            else:
                data = getattr(current_app, 'previous_schedule_data', None)
                if not data:
                    return jsonify({'error': 'Previous schedule not available'}), 404

            # Filter tasks for this aircraft
            all_tasks = data.get('tasks', [])
            aircraft_tasks = [t for t in all_tasks if t.get('line_number') == line_number]

            return jsonify({
                'line_number': line_number,
                'tasks': aircraft_tasks,
                'dag': None  # DAG not available in production mode
            })
        else:
            # Development mode - filter from app context
            aircraft_tasks = [
                t for k, t in current_app.final_schedule.items()
                if k[1] == line_number
            ]

            # Get DAG for this aircraft (if available)
            dag = current_app.dags.get(line_number)
            dag_data = None
            if dag:
                import networkx as nx
                dag_data = nx.node_link_data(dag)

            return jsonify({
                'line_number': line_number,
                'tasks': aircraft_tasks,
                'dag': dag_data
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@scenarios_bp.route('/scenario/3stage/mechanic/<mechanic_id>', methods=['GET'])
def get_mechanic_timeline_3stage(mechanic_id):
    """Get specific mechanic's timeline"""
    try:
        # Check if we're in production mode (loaded from files)
        if hasattr(current_app, 'current_schedule_data'):
            # Production mode - find mechanic in loaded JSON
            active = getattr(current_app, 'active_schedule', 'current')

            if active == 'current':
                data = current_app.current_schedule_data
            else:
                data = getattr(current_app, 'previous_schedule_data', None)
                if not data:
                    return jsonify({'error': 'Previous schedule not available'}), 404

            # Find mechanic in timelines
            mechanic_timelines = data.get('mechanic_timelines', [])
            timeline = None
            for m in mechanic_timelines:
                if str(m.get('mechanic_id')) == str(mechanic_id):
                    timeline = m
                    break

            if not timeline:
                return jsonify({'error': 'Mechanic not found'}), 404

            return jsonify({
                'mechanic_id': mechanic_id,
                'timeline': timeline
            })
        else:
            # Development mode - get from app context
            timeline = current_app.mechanic_timelines.get(mechanic_id)

            if not timeline:
                return jsonify({'error': 'Mechanic not found'}), 404

            return jsonify({
                'mechanic_id': mechanic_id,
                'timeline': timeline
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@scenarios_bp.route('/scenario/3stage/staffing', methods=['GET'])
def get_staffing_requirements_3stage():
    """Get staffing gap analysis"""
    try:
        # Check if we're in production mode (loaded from files)
        if hasattr(current_app, 'current_schedule_data'):
            # Production mode - return data from loaded JSON
            active = getattr(current_app, 'active_schedule', 'current')

            if active == 'current':
                data = current_app.current_schedule_data
            else:
                data = getattr(current_app, 'previous_schedule_data', None)
                if not data:
                    return jsonify({'error': 'Previous schedule not available'}), 404

            staffing = data.get('staffing_requirements', [])

            return jsonify({
                'staffing_gaps': staffing,
                'total_gaps': len([s for s in staffing if s.get('gap', 0) < 0]),
                'total_required': sum(s.get('required', 0) for s in staffing),
                'total_current': sum(s.get('current', 0) for s in staffing)
            })
        else:
            # Development mode - format from app context
            return jsonify({'error': 'In-process scheduler not available in this port; load a schedule generated by the MAX engine'}), 501
            # Unreachable legacy dev-mode path below (kept for reference):

            staffing = format_staffing_requirements(
                current_app.calculated_pool_sizes,
                current_app.loader
            )

            return jsonify({
                'staffing_gaps': staffing,
                'total_gaps': len([s for s in staffing if s['gap'] < 0]),
                'total_required': sum(s['required'] for s in staffing),
                'total_current': sum(s['current'] for s in staffing)
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@scenarios_bp.route('/scenario/3stage/aircraft', methods=['GET'])
def get_aircraft_status_3stage():
    """Get aircraft status with lateness metrics"""
    try:
        # Check if we're in production mode (loaded from files)
        if hasattr(current_app, 'current_schedule_data'):
            # Production mode - return data from loaded JSON
            active = getattr(current_app, 'active_schedule', 'current')

            if active == 'current':
                data = current_app.current_schedule_data
            else:
                data = getattr(current_app, 'previous_schedule_data', None)
                if not data:
                    return jsonify({'error': 'Previous schedule not available'}), 404

            aircraft = data.get('aircraft_status', [])

            return jsonify({
                'aircraft': aircraft,
                'total_aircraft': len(aircraft),
                'total_days_late': sum(a.get('days_late', 0) for a in aircraft),
                'avg_days_late': sum(a.get('days_late', 0) for a in aircraft) / len(aircraft) if aircraft else 0
            })
        else:
            # Development mode - format from app context
            return jsonify({'error': 'In-process scheduler not available in this port; load a schedule generated by the MAX engine'}), 501
            # Unreachable legacy dev-mode path below (kept for reference):

            aircraft = format_aircraft_status(
                current_app.focus_aircraft,
                current_app.final_schedule,
                current_app.loader
            )

            return jsonify({
                'aircraft': aircraft,
                'total_aircraft': len(aircraft),
                'total_days_late': sum(a['days_late'] for a in aircraft),
                'avg_days_late': sum(a['days_late'] for a in aircraft) / len(aircraft) if aircraft else 0
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@scenarios_bp.route('/scenario/3stage/burndown', methods=['GET'])
def get_burndown_data():
    """
    Get burndown chart data: daily scheduled task counts and running remaining total.

    Returns only true jobs (parent tasks, not duration segments).
    Provides breakdowns by aircraft, team, and task type for color-coding.

    The chart starts from today's date.  Jobs scheduled before today are
    folded into the initial remaining count so the burndown line begins
    at `total_jobs - already_completed`.

    Returns:
        {
            reference_date: str,
            today: str,
            total_jobs: int,
            completed_before_today: int,
            days: [{day, date, scheduled, remaining, by_aircraft, by_team, by_type}, ...],
            aircraft_list: [int, ...],
            team_list: [str, ...],
            type_list: [str, ...],
            aircraft_deadlines: {str: str, ...}
        }
    """
    try:
        from collections import defaultdict
        from datetime import datetime, timedelta, date as _date_cls

        # Respect active_schedule toggle (consistent with other endpoints)
        active = getattr(current_app, 'active_schedule', 'current')
        if active == 'current':
            schedule_data = getattr(current_app, 'current_schedule_data', None)
        else:
            schedule_data = getattr(current_app, 'previous_schedule_data', None)
            if not schedule_data:
                schedule_data = getattr(current_app, 'current_schedule_data', None)

        if not schedule_data:
            return jsonify({'error': 'No schedule data loaded'}), 404

        all_tasks = schedule_data.get('tasks', [])
        reference_date_str = schedule_data.get('metadata', {}).get('reference_date') or \
                             schedule_data.get('summary', {}).get('reference_date')

        if not reference_date_str:
            return jsonify({'error': 'No reference_date in schedule'}), 500

        ref_date = datetime.strptime(reference_date_str, '%Y-%m-%d')

        # Today as the anchor for the burndown chart
        today = _date_cls.today()
        today_str = today.strftime('%Y-%m-%d')
        today_day_offset = (today - ref_date.date()).days  # calendar-day offset

        # Filter to true jobs only: exclude duration segments (keep segment_id=1
        # or non-segments). When a task is segmented, segment_id=1 represents the
        # parent job start; we count it once.
        true_jobs = []
        seen_parent_sois = set()
        for task in all_tasks:
            is_segment = task.get('is_duration_segment', False)

            if is_segment:
                parent_soi = task.get('parent_soi', '')
                line_number = task.get('line_number', 0)
                key = (parent_soi, line_number)
                seg_id = task.get('segment_id', 1)

                # Only count segment_id 1 (first segment = the parent job start)
                if seg_id != 1:
                    continue
                if key in seen_parent_sois:
                    continue
                seen_parent_sois.add(key)

            true_jobs.append(task)

        total_jobs = len(true_jobs)

        # Build per-day aggregation
        aircraft_set = set()
        team_set = set()
        type_set = set()
        daily_data = defaultdict(lambda: {
            'by_aircraft': defaultdict(int),
            'by_team': defaultdict(int),
            'by_type': defaultdict(int),
            'count': 0
        })

        for task in true_jobs:
            day = task.get('day', 0)
            line_number = task.get('line_number', 0)
            team = task.get('team', 'Unknown')
            is_rework = task.get('isReworkTask', False)
            task_type_raw = task.get('type', 'Production')

            # Classify type: production baseline vs rework
            if is_rework or task_type_raw == 'Rework':
                task_type = 'Rework'
            elif task.get('isQualityTask') or task_type_raw == 'Quality Inspection':
                task_type = 'Quality'
            elif task.get('isCustomerTask') or task_type_raw == 'Customer':
                task_type = 'Customer'
            else:
                task_type = 'Production'

            daily_data[day]['count'] += 1
            daily_data[day]['by_aircraft'][str(line_number)] += 1
            daily_data[day]['by_team'][team] += 1
            daily_data[day]['by_type'][task_type] += 1

            aircraft_set.add(line_number)
            team_set.add(team)
            type_set.add(task_type)

        # ── Build the burndown starting from today ──
        # The chart x-axis begins at today.  Any jobs scheduled on days
        # before today are counted as already completed so the remaining
        # line starts at (total_jobs - completed_before_today).
        completed_before_today = 0
        for day_offset in sorted(daily_data.keys()):
            if day_offset < today_day_offset:
                completed_before_today += daily_data[day_offset]['count']

        if daily_data:
            max_day = max(daily_data.keys())
        else:
            max_day = today_day_offset

        # Start from today
        start_day = today_day_offset

        days_result = []
        remaining = total_jobs - completed_before_today

        for day in range(start_day, max_day + 1):
            calendar_date = ref_date + timedelta(days=day)

            # Skip weekends
            if calendar_date.weekday() >= 5:
                continue

            dd = daily_data.get(day)
            scheduled = dd['count'] if dd else 0
            date_str = calendar_date.strftime('%Y-%m-%d')

            # Store remaining BEFORE subtracting this day's scheduled work
            # so the burndown line starts at the full job count
            days_result.append({
                'day': day,
                'date': date_str,
                'scheduled': scheduled,
                'remaining': max(0, remaining),
                'by_aircraft': dict(dd['by_aircraft']) if dd else {},
                'by_team': dict(dd['by_team']) if dd else {},
                'by_type': dict(dd['by_type']) if dd else {}
            })
            remaining -= scheduled

        # Sort lists for consistent ordering
        aircraft_list = sorted(aircraft_set)
        team_list = sorted(team_set)
        type_list = sorted(type_set)

        # Build aircraft delivery deadlines (CS 744) from products or aircraft_status
        aircraft_deadlines = {}
        products = schedule_data.get('products', [])
        for prod in products:
            ln = prod.get('line_number')
            dd = prod.get('deliveryDate')
            if ln and dd:
                aircraft_deadlines[str(ln)] = dd

        # Fallback to aircraft_status if products missing delivery dates
        if not aircraft_deadlines:
            ac_status = schedule_data.get('aircraft_status', [])
            for ac in ac_status:
                ln = ac.get('line_number')
                dd = ac.get('cs_744_latest')
                if ln and dd:
                    aircraft_deadlines[str(ln)] = dd

        return jsonify({
            'reference_date': reference_date_str,
            'today': today_str,
            'total_jobs': total_jobs,
            'completed_before_today': completed_before_today,
            'total_all_tasks': len(all_tasks),
            'segments_excluded': len(all_tasks) - total_jobs,
            'days': days_result,
            'aircraft_list': aircraft_list,
            'team_list': team_list,
            'type_list': type_list,
            'aircraft_deadlines': aircraft_deadlines
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@scenarios_bp.route('/scenario/3stage/dependencies/<soi>', methods=['GET'])
def get_task_dependencies_3stage(soi):
    """Get task dependencies for dependency modal"""
    try:
        # Check if we're in production mode (loaded from files)
        schedule_data = getattr(current_app, 'current_schedule_data', None)
        if schedule_data:
            # Production mode - use predecessors/successors maps from loaded JSON
            predecessors_map = schedule_data.get('predecessors_map', {})
            successors_map = schedule_data.get('successors_map', {})

            predecessors = predecessors_map.get(soi, [])
            successors = successors_map.get(soi, [])

            # Find task info from loaded tasks
            task_info = None
            all_tasks = schedule_data.get('tasks', [])
            task_lookup = {}
            for t in all_tasks:
                task_lookup[t.get('soi', '')] = t
                task_lookup[t.get('taskId', '')] = t

            task_info = task_lookup.get(soi, {})

            # Build dependency info for each predecessor/successor
            def build_dep_info(dep_soi):
                dep_task = task_lookup.get(dep_soi, {})
                return {
                    'soi': dep_soi,
                    'taskId': dep_task.get('taskId', dep_soi),
                    'team': dep_task.get('team', ''),
                    'type': dep_task.get('type', ''),
                    'duration': dep_task.get('duration_minutes', dep_task.get('duration', 0)),
                    'startTime': dep_task.get('startTime', ''),
                    'endTime': dep_task.get('endTime', ''),
                    'shift': dep_task.get('shift', ''),
                }

            return jsonify({
                'soi': soi,
                'taskId': task_info.get('taskId', soi),
                'team': task_info.get('team', ''),
                'predecessors': [build_dep_info(p) for p in predecessors],
                'successors': [build_dep_info(s) for s in successors],
                'total_predecessors': len(predecessors),
                'total_successors': len(successors),
            })
        else:
            # Development mode - use DAGs from app context
            return jsonify({'error': 'In-process scheduler not available in this port; load a schedule generated by the MAX engine'}), 501
            # Unreachable legacy dev-mode path below (kept for reference):

            line_number = None
            for k in current_app.final_schedule.keys():
                if k[0] == soi:
                    line_number = k[1]
                    break

            if line_number is None:
                return jsonify({'error': 'Task not found'}), 404

            dag = current_app.dags.get(line_number)
            if not dag:
                return jsonify({'error': 'DAG not found'}), 404

            dependencies = format_task_dependencies(
                soi,
                dag,
                current_app.segment_groups
            )

            return jsonify(dependencies)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@scenarios_bp.route('/scenario/3stage/summary', methods=['GET'])
def get_3stage_summary():
    """Get 3-stage scheduler summary metrics"""
    try:
        # Check dev mode summary first
        summary = current_app.summary_metrics
        if summary:
            return jsonify(summary)

        # Production mode - extract summary from loaded schedule data
        schedule_data = getattr(current_app, 'current_schedule_data', None)
        if schedule_data:
            summary = schedule_data.get('summary', {})
            metadata = schedule_data.get('metadata', {})
            # Merge metadata into summary for completeness
            if metadata:
                summary.setdefault('generated_at', metadata.get('generated_at', ''))
                summary.setdefault('success_rate', metadata.get('success_rate', 0))
            return jsonify(summary)

        return jsonify({})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================================
# SCHEDULE MANAGEMENT API ENDPOINTS (Production Batch Mode)
# ============================================================================

@scenarios_bp.route('/schedules/available', methods=['GET'])
def get_available_schedules():
    """Get list of all available pre-computed schedules"""
    try:
        available = getattr(current_app, 'available_schedules', [])

        schedules = []
        for filepath, filename in available:
            # Parse filename to extract metadata
            parts = filename.replace('.json', '').split('_')
            if len(parts) >= 4:
                date_str = parts[2]  # 20251107
                identifier = parts[3]  # shift1 or 143542

                # Format date for display
                try:
                    display_date = f"{date_str[4:6]}/{date_str[6:8]}/{date_str[0:4]}"  # MM/DD/YYYY
                except:
                    display_date = date_str

                schedules.append({
                    'filename': filename,
                    'date': date_str,
                    'identifier': identifier,
                    'display_date': display_date,
                    'is_shift': identifier.startswith('shift')
                })

        return jsonify({
            'schedules': schedules,
            'total': len(schedules),
            'current': getattr(current_app, 'current_schedule_file', None),
            'previous': getattr(current_app, 'previous_schedule_file', None),
            'active': getattr(current_app, 'active_schedule', 'current')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@scenarios_bp.route('/schedules/active', methods=['GET'])
def get_active_schedule_info():
    """Get information about the currently active schedule"""
    try:
        active = getattr(current_app, 'active_schedule', 'current')

        if active == 'current':
            filename = getattr(current_app, 'current_schedule_file', None)
            data = getattr(current_app, 'current_schedule_data', None)
        else:
            filename = getattr(current_app, 'previous_schedule_file', None)
            data = getattr(current_app, 'previous_schedule_data', None)

        if not data:
            return jsonify({'error': 'No active schedule data'}), 404

        metadata = data.get('metadata', {})
        summary = data.get('summary', {})

        return jsonify({
            'active': active,
            'filename': filename,
            'metadata': metadata,
            'summary': summary,
            'can_switch_to_previous': hasattr(current_app, 'previous_schedule_data') and current_app.previous_schedule_data is not None
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@scenarios_bp.route('/schedules/active', methods=['POST'])
def set_active_schedule():
    """Switch between current and previous schedule"""
    try:
        data = request.get_json()
        requested = data.get('schedule', 'current')  # 'current' or 'previous'

        if requested not in ['current', 'previous']:
            return jsonify({'error': 'Invalid schedule selection. Use "current" or "previous"'}), 400

        if requested == 'previous':
            if not hasattr(current_app, 'previous_schedule_data') or current_app.previous_schedule_data is None:
                return jsonify({'error': 'No previous schedule available'}), 404

        # Switch active schedule
        current_app.active_schedule = requested

        return jsonify({
            'success': True,
            'active': requested,
            'filename': current_app.previous_schedule_file if requested == 'previous' else current_app.current_schedule_file
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@scenarios_bp.route('/schedule/budget', methods=['GET'])
def get_schedule_budget():
    """
    Get schedule budget data for Schedule Budget dashboard view

    Returns daily hours breakdown by:
    - Budget Type: Baseline, Rework, Quality Inspection, Customer Inspection
    - Budget Subtype: NC/PU/CI for rework, in-process/final for QA
    - Line Number: Aircraft line number
    - Date: Date of work

    Supports filtering by date range, aircraft, and task types.
    """
    try:
        # Check if we're in production mode (loaded from files)
        if hasattr(current_app, 'current_schedule_data'):
            # Production mode - get data from loaded JSON
            active = getattr(current_app, 'active_schedule', 'current')

            if active == 'current':
                data = current_app.current_schedule_data
            else:
                data = getattr(current_app, 'previous_schedule_data', None)
                if not data:
                    return jsonify({'error': 'Previous schedule not available'}), 404
        else:
            # Development mode - not implemented yet
            return jsonify({'error': 'Budget API only available in production mode'}), 501

        # Get all tasks
        all_tasks = data.get('tasks', [])

        if not all_tasks:
            return jsonify({
                'budgetData': [],
                'aircraft': [],
                'dateRange': {'min': None, 'max': None},
                'budgetTypes': []
            })

        # Build budget data structure
        budget_records = []
        aircraft_set = set()
        dates = []

        for task in all_tasks:
            # Parse task data
            line_number = task.get('line_number')
            start_time = task.get('startTime')
            duration_minutes = task.get('duration_minutes', task.get('duration', 0))

            if not start_time or duration_minutes <= 0:
                continue

            # Parse date
            try:
                task_date = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                date_str = task_date.strftime('%Y-%m-%d')
                dates.append(date_str)
            except:
                continue

            # Determine budget type and subtype
            budget_type = None
            budget_subtype = None

            task_type = task.get('type', 'Production')
            is_rework = task.get('isReworkTask', False)
            is_quality = task.get('isQualityTask', False) or task_type == 'Quality Inspection'
            is_customer = task.get('isCustomerTask', False) or task_type == 'Customer'
            is_inspection = task.get('is_inspection', False)
            soi = task.get('soi', '')
            task_id = task.get('taskId', '')

            # Categorize task
            if is_customer:
                budget_type = 'Customer Inspection'
                budget_subtype = None
            elif is_quality or is_inspection:
                budget_type = 'QA Inspection'
                # Determine if in-process or final
                if 'INPROC' in soi or 'IN_PROC' in task_id:
                    budget_subtype = 'in-process'
                elif 'FINAL' in soi or 'FINAL' in task_id:
                    budget_subtype = 'final'
                else:
                    budget_subtype = 'general'
            elif is_rework or (soi and ('_' in soi) and not any(seg in soi for seg in ['_INPROC_INSP', '_FINAL_INSP', '_DURSEG', '_SEG'])):
                # Rework tasks often have parent references (exclude inspection/duration segments)
                budget_type = 'Rework'
                # Determine subtype from task ID prefix
                if soi.startswith('NC'):
                    budget_subtype = 'NC'
                elif soi.startswith('PU'):
                    budget_subtype = 'PU'
                elif soi.startswith('CI') and not '_CC_' in soi:  # CI but not Customer Inspection
                    budget_subtype = 'CI'
                else:
                    budget_subtype = 'Other'
            else:
                # Baseline production work
                budget_type = 'Baseline'
                budget_subtype = None

            # Convert minutes to hours
            total_hours = duration_minutes / 60.0

            # Add to budget records
            budget_records.append({
                'lineNumber': line_number,
                'budgetType': budget_type,
                'budgetSubtype': budget_subtype,
                'date': date_str,
                'totalHours': round(total_hours, 2),
                'taskId': task_id,
                'soi': soi
            })

            aircraft_set.add(line_number)

        # Get unique aircraft and sort
        aircraft = sorted(list(aircraft_set))

        # Get date range
        unique_dates = sorted(list(set(dates)))
        date_range = {
            'min': unique_dates[0] if unique_dates else None,
            'max': unique_dates[-1] if unique_dates else None
        }

        # Get unique budget types
        budget_types = list(set([r['budgetType'] for r in budget_records if r['budgetType']]))

        return jsonify({
            'budgetData': budget_records,
            'aircraft': aircraft,
            'dateRange': date_range,
            'budgetTypes': sorted(budget_types)
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ============================================================================
# FGI-5 PRIORITY ESTIMATION (Fast What-If)
# ============================================================================

@scenarios_bp.route('/scenarios/estimate_priority', methods=['POST'])
def estimate_priority():
    """
    Fast priority estimation for FGI-5 scheduler.
    Simulates the impact of a custom priority ordering of aircraft
    using a greedy resource contention heuristic (runs in milliseconds).

    Accepts:
        priority_order: dict mapping product name -> rank (1=first, 2=second, etc.)
                        Products not listed are treated as "Any" (scheduled after ranked ones)
        scenario_label: optional human-readable label for this scenario

    Also accepts legacy single-product format:
        prioritized_product: str - single product to prioritize (rank 1, rest "Any")
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid JSON body'}), 400

        # Get schedule data (production mode)
        schedule_data = getattr(current_app, 'current_schedule_data', None)
        if not schedule_data:
            return jsonify({'error': 'No schedule data loaded'}), 500

        tasks = schedule_data.get('tasks', [])
        team_capacities = schedule_data.get('teamCapacities', {})
        products = schedule_data.get('products', [])

        if not tasks or not products:
            return jsonify({'error': 'Schedule has no tasks or products'}), 500

        product_names = [p['name'] for p in products]

        # Parse priority ordering - support both new and legacy format
        priority_order = data.get('priority_order', {})
        scenario_label = data.get('scenario_label', '')

        if not priority_order:
            # Legacy: single product prioritization
            prioritized_product = data.get('prioritized_product')
            if not prioritized_product:
                return jsonify({'error': 'priority_order or prioritized_product is required'}), 400
            if prioritized_product not in product_names:
                return jsonify({'error': f'Product "{prioritized_product}" not found'}), 400
            priority_order = {prioritized_product: 1}
            if not scenario_label:
                scenario_label = f"Prioritize {prioritized_product}"

        # Validate all specified products exist
        for pname in priority_order:
            if pname not in product_names:
                return jsonify({'error': f'Product "{pname}" not found. Available: {product_names}'}), 400

        # Validate ranks are positive integers
        for pname, rank in priority_order.items():
            if not isinstance(rank, int) or rank < 1:
                return jsonify({'error': f'Rank for "{pname}" must be a positive integer, got {rank}'}), 400

        result = _run_ranked_priority_estimation(
            tasks, team_capacities, products, priority_order, scenario_label
        )

        return jsonify(result)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


def _run_ranked_priority_estimation(tasks, team_capacities, products, priority_order, scenario_label=''):
    """
    Simulate the impact of NOT following the optimized schedule.

    When leadership prioritizes aircraft in a specific order, they dedicate
    shared resource teams to finishing one aircraft before moving to the next.
    The CP-SAT optimizer interleaves aircraft across teams and days to minimize
    global lateness. Serializing production breaks this interleaving.

    Model (team lock-out):
        - Ranked aircraft keep their CP-SAT schedule (tasks stay on original days)
        - While a ranked aircraft has work on a shared team (first to last day),
          that team is LOCKED — exclusively dedicated to the ranked aircraft
        - Lower-ranked aircraft can only use each team after the higher-ranked
          aircraft finishes with it, shifting their entire schedule forward
        - Unranked ("Any") aircraft wait until all ranked aircraft are done
        - Dependencies cascade: pushed tasks push their dependents further
        - Capacity is checked after scheduling; overflow pushes to next day
    """
    # Parse team capacities down to the BASE team the tasks reference:
    # "P33-CF-CUSREADY S1 (ANY)" -> "P33-CF-CUSREADY". Capacity keys carry
    # a shift tag but the estimation below looks pools up by the task's
    # bare team name, so the tag must be stripped (keys summed across
    # shifts — the serial-priority heuristic models a team's working day).
    import re as _re
    team_cap = {}
    for key, cap in team_capacities.items():
        team_name = key.split(' (')[0] if ' (' in key else key
        team_name = _re.sub(r'\s+S[123]$', '', team_name)
        team_cap[team_name] = team_cap.get(team_name, 0) + cap

    SHIFT_MINUTES = 480  # 8-hour shifts

    # Find day/shift range
    max_day = max(t.get('day', 0) for t in tasks)
    all_shifts = sorted(set(t.get('shift', 1) for t in tasks))

    # Baseline: completion day per product
    baseline_completion = {}
    for p in products:
        pname = p['name']
        ptasks = [t for t in tasks if t.get('product') == pname]
        if ptasks:
            baseline_completion[pname] = max(t.get('day', 0) for t in ptasks)

    # Build processing order
    ranked_names = set(priority_order.keys())
    ranked_products = sorted(priority_order.items(), key=lambda x: x[1])
    any_products = [p['name'] for p in products if p['name'] not in ranked_names]
    rank_aircraft_order = [name for name, _rank in ranked_products]

    # --- Phase 1: Compute team lock-out periods ---
    # For each team, track when it becomes available for the next priority tier.
    # A ranked aircraft locks a team from its first task day to its last task day
    # on that team. The next-ranked aircraft's schedule is shifted forward on
    # teams that were locked by higher-ranked aircraft.
    team_available_day = defaultdict(int)  # team -> first day available after all ranked

    # Also compute per-aircraft per-team shift amounts for ranked aircraft
    rank_team_shifts = {}  # (product, team) -> shift_days
    temp_avail = defaultdict(int)

    for pname in rank_aircraft_order:
        aircraft_tasks = [t for t in tasks if t.get('product') == pname]

        # For each team this aircraft uses, find first and last day
        team_days = defaultdict(list)
        for t in aircraft_tasks:
            resource_team = t.get('resource_team', t.get('team', ''))
            team_days[resource_team].append(t.get('day', 0))

        for team, days in team_days.items():
            original_first = min(days)
            original_last = max(days)
            original_span = original_last - original_first

            # This aircraft starts using this team at max(original_first, when team opens)
            shift = max(0, temp_avail.get(team, 0) - original_first)
            rank_team_shifts[(pname, team)] = shift

            actual_last = original_last + shift
            # Lock team until day after this aircraft finishes with it
            temp_avail[team] = max(temp_avail.get(team, 0), actual_last + 1)

    # Final team availability for unranked aircraft
    team_available_day = dict(temp_avail)

    # --- Phase 2: Assign new day to each task ---
    # Process in topological order (by original day) so predecessors are placed first
    all_tasks_sorted = sorted(
        tasks, key=lambda t: (t.get('day', 0), t.get('shift', 1), t.get('start_minute', 0))
    )

    new_task_day = {}  # taskId -> new day

    for t in all_tasks_sorted:
        task_id = t.get('taskId', '')
        product = t.get('product', '')
        resource_team = t.get('resource_team', t.get('team', ''))
        original_day = t.get('day', 0)
        deps = t.get('dependencies', [])

        if product in ranked_names:
            # Ranked: shift by lock-out from higher-ranked aircraft
            shift = rank_team_shifts.get((product, resource_team), 0)
            new_day = original_day + shift
        else:
            # Unranked: can't use team until all ranked aircraft are done with it
            avail = team_available_day.get(resource_team, 0)
            new_day = max(original_day, avail)

        # Dependency constraint: must be >= any predecessor's new day
        for dep_id in deps:
            if dep_id in new_task_day:
                new_day = max(new_day, new_task_day[dep_id])

        new_task_day[task_id] = new_day

    # --- Phase 3: Capacity overflow check ---
    # Verify tasks fit within team capacity; push overflows to next day
    EXTRA_DAYS = 30
    total_days = max_day + EXTRA_DAYS + 1

    remaining_capacity = {}
    for team in team_cap:
        for shift in all_shifts:
            for day in range(total_days):
                remaining_capacity[(team, shift, day)] = team_cap[team] * SHIFT_MINUTES

    # Process in new-day order
    tasks_by_new_day = sorted(
        tasks, key=lambda t: (new_task_day.get(t['taskId'], 0), t.get('shift', 1))
    )

    for t in tasks_by_new_day:
        task_id = t['taskId']
        resource_team = t.get('resource_team', t.get('team', ''))
        shift = t.get('shift', 1)
        duration = t.get('duration_minutes', t.get('duration', 0))
        target_day = new_task_day[task_id]

        slot = (resource_team, shift, target_day)
        if slot in remaining_capacity and remaining_capacity[slot] >= duration:
            remaining_capacity[slot] -= duration
        else:
            # Overflow: find next day with capacity
            for day in range(target_day + 1, total_days):
                slot2 = (resource_team, shift, day)
                if slot2 in remaining_capacity and remaining_capacity[slot2] >= duration:
                    remaining_capacity[slot2] -= duration
                    new_task_day[task_id] = day
                    break
            else:
                new_task_day[task_id] = total_days - 1

    # --- Build results ---
    new_completion = {}
    for p in products:
        pname = p['name']
        ptask_ids = [t.get('taskId') for t in tasks if t.get('product') == pname]
        if ptask_ids:
            new_completion[pname] = max(new_task_day.get(tid, 0) for tid in ptask_ids)

    product_results = []
    for p in products:
        pname = p['name']
        baseline_day = baseline_completion.get(pname, 0)
        new_day = new_completion.get(pname, 0)
        delay = new_day - baseline_day

        baseline_lateness = p.get('latenessDays', 0)
        estimated_lateness = max(0, baseline_lateness + delay)

        rank = priority_order.get(pname)

        product_results.append({
            'name': pname,
            'line_number': p.get('line_number'),
            'baseline_completion_day': baseline_day,
            'estimated_completion_day': new_day,
            'baseline_lateness': baseline_lateness,
            'estimated_lateness': estimated_lateness,
            'delay_days': delay,
            'priority_rank': rank,  # None means "Any"
            'is_prioritized': rank is not None,
            'totalTasks': p.get('totalTasks', 0),
        })

    # Sort: by rank (ranked first in order), then unranked by delay descending
    product_results.sort(key=lambda x: (
        0 if x['priority_rank'] is not None else 1,
        x['priority_rank'] if x['priority_rank'] is not None else 999,
        -x['delay_days']
    ))

    # Build human-readable description of the priority ordering
    rank_desc_parts = []
    for pname, rank in ranked_products:
        rank_desc_parts.append(f"#{rank} {pname}")
    any_desc = ', '.join(any_products) if any_products else 'None'
    ordering_description = '; '.join(rank_desc_parts) + f" | Any: {any_desc}"

    # Build resource contention summary
    primary_product = ranked_products[0][0] if ranked_products else None
    contention_details = _build_contention_details(
        tasks, team_cap, primary_product, SHIFT_MINUTES
    ) if primary_product else {'total_contention_slots': 0, 'high_contention_slots': [], 'shared_resource_pools': []}

    # Compute total lateness for baseline vs scenario
    baseline_total_lateness = sum(p.get('latenessDays', 0) for p in products)
    scenario_total_lateness = sum(pr['estimated_lateness'] for pr in product_results)

    return {
        'scenario_label': scenario_label,
        'priority_order': priority_order,
        'ordering_description': ordering_description,
        'products': product_results,
        'baseline_makespan': max_day,
        'estimated_makespan': max(new_completion.values()) if new_completion else 0,
        'baseline_total_lateness': baseline_total_lateness,
        'scenario_total_lateness': scenario_total_lateness,
        'additional_lateness': scenario_total_lateness - baseline_total_lateness,
        'contention_details': contention_details,
        'algorithm': 'team_lockout_serial_priority',
        'note': 'Simulates leadership dedicating shared resource teams to finishing '
                'one aircraft before moving workers to the next. While a ranked aircraft '
                'has work on a team, that team is exclusively reserved — other aircraft '
                'cannot use it until the ranked aircraft is done. The CP-SAT baseline '
                'interleaves aircraft for global optimality; serializing production '
                'breaks this interleaving and increases total system lateness.',
    }


def _build_contention_details(tasks, team_cap, prioritized_product, shift_minutes):
    """Build a summary of resource contention between prioritized and other aircraft."""
    # Group tasks by (resource_team, shift, day, product)
    slot_by_product = defaultdict(lambda: defaultdict(int))  # (team,shift,day) -> {product: minutes}

    for t in tasks:
        resource_team = t.get('resource_team', t.get('team', ''))
        key = (resource_team, t.get('shift', 1), t.get('day', 0))
        product = t.get('product', '')
        duration = t.get('duration_minutes', t.get('duration', 0))
        slot_by_product[key][product] += duration

    # Compute per-team resource sharing stats
    team_sharing = defaultdict(lambda: {'products': set(), 'total_minutes': 0})
    for t in tasks:
        resource_team = t.get('resource_team', t.get('team', ''))
        product = t.get('product', '')
        duration = t.get('duration_minutes', t.get('duration', 0))
        team_sharing[resource_team]['products'].add(product)
        team_sharing[resource_team]['total_minutes'] += duration

    # Find shared resource pools
    shared_teams = []
    for team, info in team_sharing.items():
        if prioritized_product in info['products'] and len(info['products']) > 1:
            cap = team_cap.get(team, 0)
            shared_teams.append({
                'team': team,
                'capacity': cap,
                'shared_with': sorted([p for p in info['products'] if p != prioritized_product]),
                'total_demand_minutes': info['total_minutes'],
            })

    # Find contention slots: where prioritized product AND at least one other product compete
    contention_slots = []
    for slot_key, product_demands in slot_by_product.items():
        team, shift, day = slot_key
        if prioritized_product not in product_demands:
            continue
        other_products = {p: m for p, m in product_demands.items() if p != prioritized_product}
        if not other_products:
            continue

        total_demand = sum(product_demands.values())
        capacity = team_cap.get(team, 0) * shift_minutes

        if total_demand > capacity * 0.5:  # Flag slots with moderate+ contention
            contention_slots.append({
                'team': team,
                'shift': shift,
                'day': day,
                'prioritized_demand': product_demands[prioritized_product],
                'other_demand': sum(other_products.values()),
                'capacity': capacity,
                'utilization_pct': round(total_demand / capacity * 100, 1) if capacity > 0 else 0,
                'affected_products': sorted(other_products.keys()),
            })

    contention_slots.sort(key=lambda x: -x['utilization_pct'])

    return {
        'total_contention_slots': len(contention_slots),
        'high_contention_slots': contention_slots[:10],
        'shared_resource_pools': shared_teams,
    }

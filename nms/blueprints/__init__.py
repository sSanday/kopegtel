"""nms.blueprints.api — API endpoints blueprint."""

import logging
from flask import Blueprint, jsonify, request
from flask_login import login_required

from nms.db import (
    get_db,
    get_target_hosts,
    get_active_alerts,
)
from nms.query_helpers import (
    get_all_host_stats,
    get_all_agent_metrics,
    get_service_uptime_map,
)

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__, url_prefix='/api')


@api_bp.route('/dashboard/summary', methods=['GET'])
@login_required
def get_dashboard_summary():
    try:
        conn, c = get_db()
        try:
            targets = get_target_hosts()
            all_stats = get_all_host_stats(c, time_range_hours=24)
            
            total_hosts = len(targets)
            online_hosts = sum(1 for h in targets if all_stats.get(h, {}).get('up_count', 0) > 0)
            avg_uptime = sum(
                (all_stats.get(h, {}).get('up_count', 0) / max(1, all_stats.get(h, {}).get('total', 1)) * 100)
                for h in targets
            ) / max(1, total_hosts) if total_hosts > 0 else 0
            
            alerts = get_active_alerts(c)
            
            return jsonify({
                'success': True,
                'data': {
                    'total_hosts': total_hosts,
                    'online_hosts': online_hosts,
                    'offline_hosts': total_hosts - online_hosts,
                    'avg_uptime': round(avg_uptime, 1),
                    'active_alerts': len(alerts),
                }
            })
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error getting dashboard summary: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@api_bp.route('/hosts/metrics', methods=['GET'])
@login_required
def get_all_metrics():
    try:
        conn, c = get_db()
        try:
            all_agent_metrics = get_all_agent_metrics(c, time_range_minutes=5)
            all_ping_stats = get_all_host_stats(c, time_range_hours=24)
            
            return jsonify({
                'success': True,
                'data': {
                    'agents': all_agent_metrics,
                    'ping': all_ping_stats,
                }
            })
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error getting all metrics: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@api_bp.route('/services/uptime', methods=['GET'])
@login_required
def get_services_uptime():
    try:
        conn, c = get_db()
        try:
            uptime_map = get_service_uptime_map(c, time_range_hours=24)
            return jsonify({
                'success': True,
                'data': uptime_map
            })
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error getting service uptime: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

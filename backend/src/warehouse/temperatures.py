from decimal import Decimal

from fastapi import HTTPException

from ..db import query
from ..operations import audit, operational_transaction
from .picklists import assert_shippable, lock_lines, lock_picklist

# Internal demonstration policy for this fixture's single chilled storage class.
LOW, HIGH = Decimal('2.00'), Decimal('8.00')


def require_passing_measurement(cursor, pick_id):
    cursor.execute('''SELECT c.passed, c.measured_at >= NOW()-INTERVAL 60 MINUTE current_check,
                             c.measured_at > COALESCE((SELECT MAX(w.swapped_at) FROM warehouse_pick_line_swaps w
                                                       WHERE w.picklist_id=c.picklist_id), '1970-01-01') post_swap
                      FROM warehouse_temperature_checks c
                      WHERE c.picklist_id=%s ORDER BY c.id DESC LIMIT 1 FOR UPDATE''', (pick_id,))
    latest = cursor.fetchone()
    if not latest or not latest['passed'] or not latest['current_check']:
        raise HTTPException(409, '冷藏批号须有最近 60 分钟内合格的末次温度记录')
    if not latest['post_swap']:
        raise HTTPException(409, '拣货单换配后须重新测温合格方可发车')


def list_checks():
    picks, _ = query('''SELECT p.id, p.approval_id, a.store_code, p.status
        FROM warehouse_picklists p JOIN approval_orders a ON a.id=p.approval_id
        WHERE EXISTS (SELECT 1 FROM warehouse_pick_lines i JOIN warehouse_lots l ON l.id=i.lot_id
                      WHERE i.picklist_id=p.id AND l.temperature_band='chilled') ORDER BY p.id DESC''')
    reports, _ = query('''SELECT r.*,u.name observer FROM warehouse_temperature_checks r
                         JOIN users u ON u.id=r.measured_by ORDER BY r.id DESC''')
    return {'picklists': picks, 'measurements': reports, 'low_c': LOW, 'high_c': HIGH}


def record(pick_id, data, actor):
    passed = LOW <= data.observed_c <= HIGH
    with operational_transaction() as cursor:
        if lock_picklist(cursor, pick_id)['status'] not in {'reserved','packed'}:
            raise HTTPException(409, '发车或撤销后不可追加温控记录')
        lines = lock_lines(cursor, pick_id)
        assert_shippable(lines)
        if not any(line['temperature_band'] == 'chilled' for line in lines):
            raise HTTPException(422, '常温拣货单无需冷藏测温')
        cursor.execute('''INSERT INTO warehouse_temperature_checks
                          (picklist_id,instrument_ref,observed_c,passed,measured_by) VALUES(%s,%s,%s,%s,%s)''',
                       (pick_id, data.instrument_ref, data.observed_c, passed, actor))
        check_id = cursor.lastrowid
        audit(cursor, actor, 'warehouse.temperature_recorded', 'warehouse_picklist', pick_id,
              measurement=check_id, observed_c=str(data.observed_c), passed=passed)
    return {'id': check_id, 'passed': passed}

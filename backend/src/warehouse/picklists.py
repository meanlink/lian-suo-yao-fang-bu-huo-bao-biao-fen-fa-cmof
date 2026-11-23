import json

from fastapi import HTTPException

from ..db import query
from ..operations import audit, operational_transaction
from .lots import local_day


def lock_picklist(cursor, pick_id):
    cursor.execute('SELECT * FROM warehouse_picklists WHERE id=%s FOR UPDATE', (pick_id,))
    pick = cursor.fetchone()
    if pick is None:
        raise HTTPException(404, '拣货单不存在')
    return pick


def lock_lines(cursor, pick_id):
    cursor.execute('''SELECT i.lot_id,i.units,l.lot_code,l.state,l.expires_on,l.temperature_band
                      FROM warehouse_pick_lines i JOIN warehouse_lots l ON i.lot_id=l.id
                      WHERE i.picklist_id=%s ORDER BY l.id FOR UPDATE''', (pick_id,))
    return cursor.fetchall()


def list_board():
    picks, _ = query('''SELECT p.id,p.approval_id,p.status,a.store_code,s.sku,a.approved_qty,
                        p.packed_at FROM warehouse_picklists p JOIN approval_orders a ON a.id=p.approval_id
                        JOIN replenishment_recommendations r ON r.id=a.recommendation_id
                        JOIN inventory_snapshots s ON s.id=r.snapshot_id ORDER BY p.id DESC''')
    lines, _ = query('''SELECT i.*,l.lot_code,l.expires_on,l.temperature_band,l.state FROM warehouse_pick_lines i
                        JOIN warehouse_lots l ON l.id=i.lot_id ORDER BY l.expires_on,l.id''')
    orders, _ = query('''SELECT a.id,a.store_code,s.sku,a.approved_qty FROM approval_orders a
                         JOIN replenishment_recommendations r ON r.id=a.recommendation_id
                         JOIN inventory_snapshots s ON s.id=r.snapshot_id WHERE a.status='approved' AND a.approved_qty>0
                         AND NOT EXISTS (SELECT 1 FROM warehouse_picklists p WHERE p.approval_id=a.id AND p.active_slot=1)
                         ORDER BY a.id DESC''')
    swaps, _ = query('''SELECT w.id,w.picklist_id,w.recalled_units,w.replacement_lines,w.swapped_at,
                        l.lot_code recalled_lot_code,u.name operator
                        FROM warehouse_pick_line_swaps w JOIN warehouse_lots l ON l.id=w.recalled_lot_id
                        JOIN users u ON u.id=w.swapped_by ORDER BY w.id''')
    by_pick = {p['id']: p for p in picks}
    for pick in picks:
        pick['lines'] = []
        pick['swaps'] = []
    for line in lines:
        if line['picklist_id'] in by_pick:
            by_pick[line['picklist_id']]['lines'].append(line)
    for swap in swaps:
        swap['replacement_lines'] = json.loads(swap['replacement_lines'])
        if swap['picklist_id'] in by_pick:
            by_pick[swap['picklist_id']]['swaps'].append(swap)
    return {'eligible_orders': orders, 'picklists': picks}


def reserve(approval_id, actor):
    with operational_transaction() as cursor:
        cursor.execute('''SELECT a.status,a.approved_qty,s.sku FROM approval_orders a
                          JOIN replenishment_recommendations r ON r.id=a.recommendation_id
                          JOIN inventory_snapshots s ON s.id=r.snapshot_id WHERE a.id=%s FOR UPDATE OF a''', (approval_id,))
        order = cursor.fetchone()
        if not order or order['status'] != 'approved' or not order['approved_qty']:
            raise HTTPException(409, '仅可拣配已审批且数量为正的订单')
        cursor.execute('SELECT id FROM warehouse_picklists WHERE approval_id=%s AND active_slot=1', (approval_id,))
        if cursor.fetchone():
            raise HTTPException(409, '该订单已有有效拣货单')
        cursor.execute('''SELECT id,on_hand-reserved free_units,expires_on FROM warehouse_lots
                          WHERE sku=%s AND state='available' AND expires_on>%s ORDER BY id FOR UPDATE''',
                       (order['sku'], local_day()))
        lots = sorted(cursor.fetchall(), key=lambda item: (item['expires_on'], item['id']))
        if sum(lot['free_units'] for lot in lots) < order['approved_qty']:
            raise HTTPException(409, '未召回且未过期的可用库存不足，未占用任何批号')
        cursor.execute('INSERT INTO warehouse_picklists (approval_id,reserved_by) VALUES(%s,%s)', (approval_id, actor))
        pick_id = cursor.lastrowid
        remaining = order['approved_qty']
        for lot in lots:
            units = min(lot['free_units'], remaining)
            if not units:
                continue
            cursor.execute('UPDATE warehouse_lots SET reserved=reserved+%s WHERE id=%s', (units, lot['id']))
            cursor.execute('INSERT INTO warehouse_pick_lines (picklist_id,lot_id,units) VALUES(%s,%s,%s)',
                           (pick_id, lot['id'], units))
            remaining -= units
        audit(cursor, actor, 'warehouse.pick_reserved', 'warehouse_picklist', pick_id, quantity=order['approved_qty'])
    return {'id': pick_id, 'status': 'reserved'}


def assert_shippable(lines):
    if any(line['state'] != 'available' or line['expires_on'] <= local_day() for line in lines):
        raise HTTPException(409, '拣货单含召回或已过期批号，请撤销并重新拣配')


def pack(pick_id, actor):
    with operational_transaction() as cursor:
        pick = lock_picklist(cursor, pick_id)
        if pick['status'] != 'reserved':
            raise HTTPException(409, '只有待打包的拣货单可以确认')
        lines = lock_lines(cursor, pick_id)
        assert_shippable(lines)
        if any(line['temperature_band'] == 'chilled' for line in lines):
            from .temperatures import require_passing_measurement

            require_passing_measurement(cursor, pick_id)
        cursor.execute("UPDATE warehouse_picklists SET status='packed',packed_by=%s,packed_at=NOW() WHERE id=%s",
                       (actor, pick_id))
        audit(cursor, actor, 'warehouse.pick_packed', 'warehouse_picklist', pick_id)
    return {'id': pick_id, 'status': 'packed'}


def cancel(pick_id, actor):
    with operational_transaction() as cursor:
        pick = lock_picklist(cursor, pick_id)
        if pick['status'] not in {'reserved', 'packed'}:
            raise HTTPException(409, '只有发车前的拣货单可撤销')
        for line in lock_lines(cursor, pick_id):
            cursor.execute('UPDATE warehouse_lots SET reserved=reserved-%s WHERE id=%s', (line['units'], line['lot_id']))
        cursor.execute("UPDATE warehouse_picklists SET status='cancelled' WHERE id=%s", (pick_id,))
        audit(cursor, actor, 'warehouse.pick_cancelled', 'warehouse_picklist', pick_id)
    return {'id': pick_id, 'status': 'cancelled'}


def swap_recalled_lot(pick_id, lot_id, actor):
    """Replace a recalled lot line in place, keeping every other reservation and the original units.

    The picklist row lock serializes swaps against dispatch, so a recalled lot can never ship.
    The (picklist, lot) swap record makes client retries idempotent: no stock is reserved twice.
    """
    with operational_transaction() as cursor:
        pick = lock_picklist(cursor, pick_id)
        if pick['status'] not in {'reserved', 'packed'}:
            raise HTTPException(409, '只有发车前的拣货单可换配召回批号')
        cursor.execute('''SELECT w.id,w.recalled_units,w.replacement_lines,l.lot_code
                          FROM warehouse_pick_line_swaps w JOIN warehouse_lots l ON l.id=w.recalled_lot_id
                          WHERE w.picklist_id=%s AND w.recalled_lot_id=%s''', (pick_id, lot_id))
        prior = cursor.fetchone()
        if prior:
            return {'id': pick_id, 'status': pick['status'], 'swap_id': prior['id'],
                    'recalled_lot_code': prior['lot_code'], 'recalled_units': prior['recalled_units'],
                    'replacements': json.loads(prior['replacement_lines']), 'idempotent': True}
        cursor.execute('''SELECT i.units,l.sku,l.lot_code,l.state FROM warehouse_pick_lines i
                          JOIN warehouse_lots l ON l.id=i.lot_id
                          WHERE i.picklist_id=%s AND i.lot_id=%s FOR UPDATE''', (pick_id, lot_id))
        line = cursor.fetchone()
        if not line:
            raise HTTPException(404, '该拣货单不含此批号')
        if line['state'] != 'recalled':
            raise HTTPException(409, '仅可换配已召回的批号')
        needed = line['units']
        cursor.execute('''SELECT id,lot_code,on_hand-reserved free_units FROM warehouse_lots
                          WHERE sku=%s AND state='available' AND expires_on>%s AND id<>%s
                          ORDER BY expires_on,id FOR UPDATE''', (line['sku'], local_day(), lot_id))
        candidates = cursor.fetchall()
        if sum(candidate['free_units'] for candidate in candidates) < needed:
            raise HTTPException(409, '替补批号可用量不足，已保留原预留并继续拦截发车')
        cursor.execute('UPDATE warehouse_lots SET reserved=reserved-%s WHERE id=%s', (needed, lot_id))
        cursor.execute('DELETE FROM warehouse_pick_lines WHERE picklist_id=%s AND lot_id=%s', (pick_id, lot_id))
        remaining = needed
        replacements = []
        for candidate in candidates:
            units = min(candidate['free_units'], remaining)
            if not units:
                continue
            cursor.execute('UPDATE warehouse_lots SET reserved=reserved+%s WHERE id=%s', (units, candidate['id']))
            cursor.execute('''INSERT INTO warehouse_pick_lines (picklist_id,lot_id,units) VALUES(%s,%s,%s)
                              ON DUPLICATE KEY UPDATE units=units+VALUES(units)''',
                           (pick_id, candidate['id'], units))
            replacements.append({'lot_id': candidate['id'], 'lot_code': candidate['lot_code'], 'units': units})
            remaining -= units
        cursor.execute('''INSERT INTO warehouse_pick_line_swaps
                          (picklist_id,recalled_lot_id,recalled_units,replacement_lines,swapped_by)
                          VALUES(%s,%s,%s,%s,%s)''',
                       (pick_id, lot_id, needed, json.dumps(replacements, ensure_ascii=False), actor))
        swap_id = cursor.lastrowid
        cursor.execute('''UPDATE warehouse_recall_actions a
                          JOIN warehouse_recalls r ON r.id=a.recall_id AND r.lot_id=%s
                          SET a.isolated_units=0,a.outlet_ref=%s,a.handled_by=%s,a.handled_at=NOW()
                          WHERE a.picklist_id=%s AND a.handled_at IS NULL''',
                       (lot_id, f'原单换配#{swap_id}未发出', actor, pick_id))
        audit(cursor, actor, 'warehouse.pick_swapped', 'warehouse_picklist', pick_id,
              recalled_lot=line['lot_code'], recalled_units=needed,
              replacements='、'.join(f"{item['lot_code']}×{item['units']}" for item in replacements))
    return {'id': pick_id, 'status': pick['status'], 'swap_id': swap_id,
            'recalled_lot_code': line['lot_code'], 'recalled_units': needed,
            'replacements': replacements, 'idempotent': False}


def consume_for_dispatch(cursor, approval_id, actor):
    """Convert an approved warehouse reservation into stock movement inside dispatch's transaction."""
    cursor.execute('SELECT * FROM warehouse_picklists WHERE approval_id=%s AND active_slot=1 FOR UPDATE', (approval_id,))
    pick = cursor.fetchone()
    if not pick:
        cursor.execute('''SELECT l.id FROM approval_orders a
            JOIN replenishment_recommendations r ON r.id=a.recommendation_id
            JOIN inventory_snapshots s ON s.id=r.snapshot_id
            JOIN warehouse_lots l ON l.sku=s.sku WHERE a.id=%s LIMIT 1''', (approval_id,))
        if cursor.fetchone():
            raise HTTPException(409, '仓配 SKU 发车前须建立并确认拣货单')
        return
    if pick['status'] != 'packed':
        raise HTTPException(409, '仓配订单必须先确认打包')
    lines = lock_lines(cursor, pick['id'])
    assert_shippable(lines)
    if any(line['temperature_band'] == 'chilled' for line in lines):
        from .temperatures import require_passing_measurement

        require_passing_measurement(cursor, pick['id'])
    for line in lines:
        cursor.execute('UPDATE warehouse_lots SET on_hand=on_hand-%s,reserved=reserved-%s WHERE id=%s',
                       (line['units'], line['units'], line['lot_id']))
    cursor.execute("UPDATE warehouse_picklists SET status='dispatched' WHERE id=%s", (pick['id'],))
    audit(cursor, actor, 'warehouse.pick_dispatched', 'warehouse_picklist', pick['id'], approval_id=approval_id)

import {get, post, put} from '../api.js';
import {escape, form, listen, option, picker, table} from './view.js';

const labels = {reserved: '待打包', packed: '已打包', cancelled: '已撤销', dispatched: '已发车'};

function swapHistory(pick) {
  if (!pick.swaps || !pick.swaps.length) return '—';
  return pick.swaps.map(swap => {
    const incoming = swap.replacement_lines.map(line => `${escape(line.lot_code)} · ${escape(line.units)} 件`).join(' + ');
    return `<div>换出 ${escape(swap.recalled_lot_code)} · ${escape(swap.recalled_units)} 件 → 换入 ${incoming}</div>`;
  }).join('');
}

export async function pickPage({root, role, reload, toast}) {
  const {eligible_orders, picklists} = await get('/warehouse/picklists');
  const writable = ['shift_coordinator','regional_manager'].includes(role);
  const input = writable && eligible_orders.length ? form('按有效期分配仓库库存', [
    picker('approval_id', '已审批补货单', eligible_orders.map(order => option(order.id, `#${order.id} · ${order.store_code} · ${order.sku} · ${order.approved_qty} 件`))),
  ], '生成拣货单', 'data-action="reserve"') : '';
  const rows = picklists.map(pick => {
    const actions = [];
    if (writable && pick.status === 'reserved') actions.push(option('pack', '确认打包'));
    if (writable && ['reserved','packed'].includes(pick.status)) actions.push(option('cancel', '撤销拣货'));
    const transition = actions.length ? form('拣配操作', [picker('action','动作',actions)], '执行', `data-action="transition" data-id="${pick.id}"`) : '';
    const open = ['reserved','packed'].includes(pick.status);
    const swapForms = writable && open ? pick.lines.filter(line => line.state === 'recalled')
      .map(line => form(`换配召回批号 ${escape(line.lot_code)}`, [], `按原数量换配 ${escape(line.units)} 件`,
        `data-action="swap" data-id="${pick.id}" data-lot="${line.lot_id}"`)).join('') : '';
    return [escape(`#${pick.id} / #${pick.approval_id}`), escape(pick.store_code), escape(pick.sku),
      pick.lines.map(line => `<div>${escape(line.lot_code)} · ${escape(line.units)} 件${line.state === 'recalled' ? ' · 已召回' : ''}</div>`).join(''),
      swapHistory(pick), escape(labels[pick.status]), transition + swapForms || '—'];
  });
  root.innerHTML = input + table(['拣货 / 审批', '门店', 'SKU', '分配批号', '换配记录', '状态', '处理'], rows, '拣货进度');
  listen(root, 'form', (data, tags) => {
    if (tags.action === 'reserve') return post('/warehouse/picklists', {approval_id: Number(data.approval_id)});
    if (tags.action === 'swap') return put(`/warehouse/picklists/${tags.id}/swap`, {lot_id: Number(tags.lot)});
    return put(`/warehouse/picklists/${tags.id}/${data.action}`);
  }, reload, toast);
}

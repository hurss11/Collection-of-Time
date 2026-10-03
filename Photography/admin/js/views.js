/**
 * views.js —— 渲染层
 *
 * 输入是服务端已经算好的数据，输出 HTML 字符串。所有函数都不带副作用
 * （不改全局状态、不绑事件、不做业务判断），事件统一由 app.js 委托处理。
 *
 * 这里没有内置的字段定义、没有按集合分支的表格、没有搜索 / 排序 / 计数逻辑：
 *   - 表格：列来自 payload.columns，行来自 items[].cells[]，只按 cell.kind 分支；
 *   - 搜索 / 排序 / 分页 / 相册条目数：全部由 GET /api/items/{collection} 完成。
 */

import { assetUrl } from './config.js';
import { $, escapeHtml, humanSize } from './ui.js';

export const PANE_META = {
  overview: ['概览', '数据来源：<code>data/*.json</code>'],
  photos: ['照片', '搜索 / 排序由服务端完成，编辑后逐条写回 <code>data/photos.json</code>'],
  videos: ['视频', '本地文件与外链嵌入共用一份 <code>data/videos.json</code>'],
  albums: ['相册', '相册 id 被照片与视频引用'],
  upload: ['上传', '一次批量提交，服务端逐条返回结果'],
  backups: ['备份', '导出 / 导入 / 回滚历史版本'],
  account: ['账号', '修改密码与查看登录状态'],
};

export const EMPTY_TEXT = {
  photos: '没有匹配的照片',
  videos: '没有匹配的视频',
  albums: '还没有相册',
};

/* ============================================================
   单元格渲染：只按 cell.kind 分支，不按集合分支
   ============================================================ */

function thumbCell(cell) {
  const url = cell.remoteUrl || cell.url || '';
  const className = cell.wide ? 'thumb thumb--wide' : 'thumb';

  if (!url || cell.missing) {
    return `<span class="badge badge--missing">${escapeHtml(cell.fallback || '缺失')}</span>`;
  }
  // 加载失败由 app.js 的捕获阶段监听统一降级成角标（不用内联 onerror，便于收紧 CSP）
  return `<img class="${className}" src="${escapeHtml(assetUrl(url))}" alt="" loading="lazy"
      data-fallback="${escapeHtml(cell.fallback || '缺失')}" />`;
}

function taglist(tags) {
  if (!Array.isArray(tags) || !tags.length) return '<span class="muted">–</span>';
  return `<span class="taglist">${tags
    .slice(0, 4)
    .map((tag) => `<span>${escapeHtml(tag)}</span>`)
    .join('')}</span>`;
}

function actionsCell(cell) {
  // poster 动作只有视频行才有（由后端在 actions 单元格里给出）
  const poster = cell.poster
    ? `<button class="btn btn--sm" data-poster="${escapeHtml(cell.poster)}" type="button">封面</button>`
    : '';
  return `
    ${poster}
    <button class="btn btn--sm" data-edit="${escapeHtml(cell.edit || '')}" type="button">编辑</button>
    <button class="btn btn--sm btn--danger" data-del="${escapeHtml(cell.del || '')}" type="button">删除</button>`;
}

function renderCell(cell) {
  switch (cell.kind) {
    case 'thumb':
      return thumbCell(cell);
    case 'title':
      return `<div class="cell-title">${escapeHtml(cell.text || '未命名')}</div>`
        + (cell.sub ? `<div class="cell-sub">${escapeHtml(cell.sub)}</div>` : '');
    case 'tags':
      return taglist(cell.tags);
    case 'badge':
      return `<span class="badge badge--${escapeHtml(cell.tone || 'file')}" title="${escapeHtml(cell.title || '')}">${escapeHtml(cell.text || '')}</span>`;
    case 'sub':
      return `<span class="cell-sub">${escapeHtml(cell.text || '')}</span>`;
    case 'muted':
      return `<span class="muted">${escapeHtml(cell.text || '')}</span>`;
    case 'actions':
      return actionsCell(cell);
    case 'text':
    default:
      return escapeHtml(cell.text || '');
  }
}

/* ============================================================
   表格：一个渲染器覆盖所有集合
   ============================================================ */

/**
 * 渲染一张表格。
 * 表头取 payload.columns；每行取 items[].cells，按下标与列一一对应。
 * 每个 td 都带上 data-label（= 列名），窄屏下 CSS 用它把行变成卡片。
 * @param {HTMLTableElement} table
 * @param {{columns?: object[], items?: object[]}} payload GET /api/items/{collection} 的响应
 * @param {string} emptyText 无数据时的提示
 */
export function renderTable(table, payload, emptyText = '没有数据') {
  if (!table) return;

  const columns = payload?.columns || [];
  const items = payload?.items || [];

  const head = `<thead><tr>${columns
    .map((column) => `<th>${escapeHtml(column.label || '')}</th>`)
    .join('')}</tr></thead>`;

  const rows = items.length
    ? items.map((row) => {
      const cells = row.cells || [];
      const tds = columns.map((column, index) => {
        const cell = cells[index] || { kind: 'text', text: '' };
        const label = column.label || '';
        const cls = cell.kind === 'actions' ? ' class="cell-actions"' : '';
        return `<td${cls} data-label="${escapeHtml(label)}">${renderCell(cell)}</td>`;
      }).join('');
      return `<tr>${tds}</tr>`;
    }).join('')
    : `<tr class="empty-row"><td colspan="${columns.length || 1}">${escapeHtml(emptyText)}</td></tr>`;

  table.innerHTML = `${head}<tbody>${rows}</tbody>`;
}

/** 填充排序下拉：选项来自 schema.sorts[collection] */
export function renderSortOptions(select, sorts, selected) {
  if (!select) return '';
  const options = sorts || [];
  select.innerHTML = options
    .map((option) => `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`)
    .join('');
  const values = options.map((option) => option.value);
  const value = values.includes(selected) ? selected : values[0] || '';
  select.value = value;
  return value;
}

/* ============================================================
   概览
   ============================================================ */

export function renderCounts(counts) {
  document.querySelectorAll('.nav__count').forEach((node) => {
    node.textContent = counts?.[node.dataset.count] ?? 0;
  });
}

export function renderTools(tools) {
  const pill = $('#ffmpeg-pill');
  if (!pill) return;

  const source = tools.sourceLabel || '未知来源';
  const version = tools.version || '';

  if (tools.ffmpeg && tools.ffprobe) {
    pill.className = 'pill pill--ok';
    // 能看出用的是哪一份（内置 bin/ 还是系统安装）很关键，排查问题时一眼就能分清
    pill.textContent = tools.source === 'local' ? 'FFmpeg 就绪 · 内置' : 'FFmpeg 就绪';
    pill.title = `来源：${source}\n版本：${version || '未知'}\nffmpeg: ${tools.ffmpeg}\nffprobe: ${tools.ffprobe}`;
  } else if (tools.ffmpeg || tools.ffprobe) {
    pill.className = 'pill pill--warn';
    pill.textContent = 'FFmpeg 不完整';
    pill.title = `缺 ffmpeg 或 ffprobe，部分功能会跳过\nffmpeg: ${tools.ffmpeg || '缺失'}\nffprobe: ${tools.ffprobe || '缺失'}`;
  } else {
    pill.className = 'pill pill--warn';
    pill.textContent = 'FFmpeg 未安装';
    pill.title = '未安装时：跳过视频封面与图片缩略图生成，上传仍可用';
  }
}

export function renderStats(counts, info) {
  const missing = info.missingFileCount ?? 0;
  const orphans = (info.orphanAlbums || []).length;

  const cards = [
    { label: '照片', value: counts.photos ?? 0, note: 'data/photos.json' },
    { label: '视频', value: counts.videos ?? 0, note: 'data/videos.json' },
    { label: '相册', value: counts.albums ?? 0, note: 'data/albums.json' },
    { label: '缺失文件', value: missing, note: missing ? '条目引用的文件不存在' : '全部文件就位', tone: missing ? 'warn' : 'ok' },
    { label: '失效相册引用', value: orphans, note: orphans ? '照片 / 视频引用了不存在的相册' : '引用全部有效', tone: orphans ? 'warn' : 'ok' },
  ];

  $('#stat-grid').innerHTML = cards.map((card) => `
    <div class="stat${card.tone ? ` stat--${card.tone}` : ''}">
      <div class="stat__label">${escapeHtml(card.label)}</div>
      <div class="stat__value">${card.value}</div>
      <div class="stat__note">${escapeHtml(card.note)}</div>
    </div>`).join('');
}

export function renderIntegrity(info) {
  const rows = [];
  for (const issue of info.orphanAlbums || []) {
    rows.push(`<div class="issue">相册引用失效：<code>${escapeHtml(issue)}</code></div>`);
  }
  for (const issue of info.missingFiles || []) {
    rows.push(`<div class="issue">文件缺失：<code>${escapeHtml(issue)}</code></div>`);
  }
  if (info.missingFileCount > (info.missingFiles || []).length) {
    rows.push(`<div class="issue">…还有 ${info.missingFileCount - (info.missingFiles || []).length} 条未显示</div>`);
  }

  // FFmpeg 缺失不是错误，但会影响上传体验，这里提醒一下并给出启用命令
  const tools = info.tools || {};
  if (!tools.canTranscode) {
    rows.push(`<div class="issue">未启用 FFmpeg：上传仍可用，但不会生成缩略图与视频封面。
      启用：<code>./run.sh install-ffmpeg</code></div>`);
  }

  $('#integrity').innerHTML = rows.length
    ? rows.join('')
    : '<p class="muted">没有发现问题：所有条目的相册引用有效，引用的文件也都存在。</p>';
}

export function renderHistory(history) {
  $('#history').innerHTML = (history || []).length
    ? history.map((entry) => `
      <li>
        <span class="log__time">${escapeHtml(entry.at)}</span>
        <span class="log__action">${escapeHtml(entry.action)}</span>
        <span class="log__detail${entry.ok ? '' : ' log__detail--err'}">${escapeHtml(entry.detail)}</span>
      </li>`).join('')
    : '<li class="muted">本次启动后还没有操作记录。</li>';
}

/** 备份列表：体积用服务端给的 sizeText，前端不再换算 */
export function renderBackups(backups) {
  const table = $('#table-backups');
  if (!table) return;

  table.innerHTML = `
    <thead><tr><th>文件</th><th>集合</th><th>大小</th><th>时间</th><th></th></tr></thead>
    <tbody>${(backups || []).length ? backups.map((backup) => `
      <tr>
        <td class="cell-sub" data-label="文件">${escapeHtml(backup.name)}</td>
        <td data-label="集合">${escapeHtml(backup.collection)}</td>
        <td class="cell-sub" data-label="大小">${escapeHtml(backup.sizeText || humanSize(backup.size))}</td>
        <td class="cell-sub" data-label="时间">${escapeHtml(backup.modified)}</td>
        <td class="cell-actions" data-label=""><button class="btn btn--sm" data-restore="${escapeHtml(backup.name)}" type="button">恢复</button></td>
      </tr>`).join('') : '<tr class="empty-row"><td colspan="5">还没有备份（首次保存后自动生成）</td></tr>'}</tbody>`;
}

/* ============================================================
   上传
   ============================================================ */

/** 上传选项：相册下拉与 accept 都来自服务端 schema.upload */
export function renderUploadOptions(upload) {
  const select = $('#up-album');
  if (select) {
    const previous = select.value;
    const albums = upload?.albums || [];
    select.innerHTML = [
      '<option value="uncategorized">未分类</option>',
      ...albums.map((album) => `<option value="${escapeHtml(album.value)}">${escapeHtml(album.label)}</option>`),
    ].join('');
    if (previous) select.value = previous;
    if (!select.value) select.value = 'uncategorized';
  }

  const input = $('#file-input');
  if (input && Array.isArray(upload?.accept) && upload.accept.length) {
    input.accept = upload.accept.join(',');
  }
}

/** 待上传文件列表（只是本地选择，不代表服务端结果） */
export function renderQueue(files) {
  const host = $('#queue');
  if (!host) return;

  host.innerHTML = (files || []).map((file, index) => `
      <div class="queue__item">
        <span class="queue__name">${escapeHtml(file.name)}</span>
        <span class="queue__size">${humanSize(file.size)}</span>
        <button class="btn btn--sm" data-drop="${index}" type="button">移除</button>
      </div>`).join('');
}

/**
 * 上传进度：进度条 + 百分比 / 已传字节 / 实时网速 / 预计剩余。
 * 数据由 app.js 算好（loaded / total / speed / eta），这里只负责画。
 *
 * @param {null | {state: 'running'|'done'|'error', loaded: number, total: number,
 *                 speed: number, eta: number|null, percent: number, detail?: string}} data
 */
export function renderUploadProgress(data) {
  const box = $('#upload-progress');
  if (!box) return;

  if (!data || data.state === 'idle') {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.classList.toggle('is-done', data.state === 'done');
  box.classList.toggle('is-error', data.state === 'error');

  const percent = Math.max(0, Math.min(100, Number(data.percent) || 0));
  $('#progress-bar').style.width = `${percent}%`;
  const track = box.querySelector('.progress');
  if (track) track.setAttribute('aria-valuenow', String(Math.round(percent)));

  $('#progress-percent').textContent = percent >= 99.95
    ? '100%'
    : `${percent < 10 ? percent.toFixed(1) : Math.round(percent)}%`;
  $('#progress-bytes').textContent = `${humanSize(data.loaded)} / ${humanSize(data.total)}`;
  $('#progress-speed').textContent = data.speed > 0 ? `${humanSize(data.speed)}/s` : '– /s';

  const eta = Number(data.eta);
  $('#progress-eta').textContent = (Number.isFinite(eta) && eta > 0 && data.state === 'running')
    ? `剩余约 ${eta >= 60 ? `${Math.round(eta / 60)} 分 ${Math.round(eta % 60)} 秒` : `${Math.ceil(eta)} 秒`}`
    : '';
  $('#progress-detail').textContent = data.detail || '';
}

/** 上传结果：results / summary 由服务端给出，原样展示 */
export function renderUploadResult(payload) {
  const box = $('#upload-result');
  if (!box) return;
  box.hidden = false;

  const results = payload?.results || [];
  const summary = payload?.summary || { total: results.length, ok: 0, failed: 0 };

  const summaryNode = $('#upload-result-summary');
  if (summaryNode) {
    summaryNode.className = `upload-summary${summary.failed ? ' is-warn' : ' is-ok'}`;
    summaryNode.textContent = `共 ${summary.total} 个文件：成功 ${summary.ok}，失败 ${summary.failed}`;
  }

  $('#upload-result-body').innerHTML = results.map((result) => {
    if (!result.ok) {
      return `<div class="result-item">
        <div class="result-item__head"><strong>${escapeHtml(result.name)}</strong>
          <span class="badge badge--missing">失败</span></div>
        <p class="muted">${escapeHtml(result.error || '上传失败')}</p>
      </div>`;
    }

    const meta = [result.kind === 'video' ? '视频' : '图片', result.collection, result.exifSummary]
      .filter(Boolean).join(' · ');

    return `<div class="result-item">
      <div class="result-item__head">
        <strong>${escapeHtml(result.name)}</strong>
        <span class="badge badge--file">${escapeHtml(result.id || '')}</span>
      </div>
      ${result.src ? `<div class="cell-sub">${escapeHtml(result.src)}</div>` : ''}
      ${result.poster ? `<div class="cell-sub">封面：${escapeHtml(result.poster)}</div>` : ''}
      ${result.thumb && result.thumb !== result.src ? `<div class="cell-sub">缩略图：${escapeHtml(result.thumb)}</div>` : ''}
      ${meta ? `<div class="cell-sub">${escapeHtml(meta)}</div>` : ''}
      ${(result.warnings || []).length ? `<ul class="warnings">${result.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join('')}</ul>` : ''}
    </div>`;
  }).join('');
}

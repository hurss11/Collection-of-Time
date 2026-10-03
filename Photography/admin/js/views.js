/**
 * views.js —— 渲染层
 *
 * 输入是数据、输出是 HTML 字符串，所有函数都不带副作用（不改全局状态、不绑事件），
 * 事件统一由 app.js 用委托处理。这样视图可以独立测试与复用。
 */

import { assetUrl } from './config.js';
import { $, escapeHtml, humanSize } from './ui.js';

/* ============================================================
   表单 schema
   ============================================================ */

export const PROVIDERS = [
  { value: 'file', label: '本地视频文件' },
  { value: 'bilibili', label: '哔哩哔哩' },
  { value: 'youtube', label: 'YouTube' },
  { value: 'vimeo', label: 'Vimeo' },
  { value: 'embed', label: '其它外链' },
];

export const SCHEMAS = {
  albums: {
    noun: '相册',
    fields: [
      { key: 'id', label: 'ID', type: 'text', required: true, mono: true,
        hint: '唯一标识，被照片 / 视频的 album 字段引用' },
      { key: 'name', label: '名称', type: 'text', required: true },
      { key: 'description', label: '描述', type: 'text' },
      { key: 'cover', label: '封面图路径', type: 'text', mono: true, placeholder: 'assets/img/ph-01.svg' },
    ],
  },
  photos: {
    noun: '照片',
    fields: [
      { key: 'title', label: '标题', type: 'text', required: true },
      { key: 'album', label: '相册', type: 'album' },
      { key: 'date', label: '拍摄时间', type: 'text', placeholder: '2026-01-18 06:42:00' },
      { key: 'location', label: '地点', type: 'text' },
      { key: 'tags', label: '标签（逗号分隔）', type: 'tags' },
      { key: 'src', label: '大图路径', type: 'text', mono: true, placeholder: 'assets/img/photos/xxx.jpg' },
      { key: 'thumb', label: '缩略图路径', type: 'text', mono: true, hint: '留空则使用大图' },
      { key: 'description', label: '描述', type: 'textarea' },
      { key: 'exif.camera', label: '相机', type: 'text', group: 'EXIF 拍摄参数' },
      { key: 'exif.lens', label: '镜头', type: 'text', group: 'EXIF 拍摄参数' },
      { key: 'exif.focalLength', label: '焦距', type: 'text', group: 'EXIF 拍摄参数' },
      { key: 'exif.aperture', label: '光圈', type: 'text', group: 'EXIF 拍摄参数' },
      { key: 'exif.shutter', label: '快门', type: 'text', group: 'EXIF 拍摄参数' },
      { key: 'exif.iso', label: 'ISO', type: 'text', group: 'EXIF 拍摄参数' },
      { key: 'exif.dimensions', label: '尺寸', type: 'text', group: 'EXIF 拍摄参数' },
    ],
  },
  videos: {
    noun: '视频',
    fields: [
      { key: 'title', label: '标题', type: 'text', required: true },
      { key: 'album', label: '相册', type: 'album' },
      { key: 'provider', label: '来源', type: 'select', options: PROVIDERS },
      { key: 'src', label: '文件路径 / 视频链接 / BV 号', type: 'text', mono: true,
        placeholder: 'assets/video/xxx.mp4 或 BV1xxxxxxxxx' },
      { key: 'poster', label: '封面图', type: 'text', mono: true, hint: '外链视频必填：跨域 iframe 无法自动抓帧' },
      { key: 'posterTime', label: '抓帧时间（秒）', type: 'number' },
      { key: 'duration', label: '时长（秒）', type: 'number' },
      { key: 'resolution', label: '分辨率', type: 'text', placeholder: '3840 × 2160' },
      { key: 'date', label: '拍摄时间', type: 'text', placeholder: '2026-01-18 06:20:00' },
      { key: 'location', label: '地点', type: 'text' },
      { key: 'tags', label: '标签（逗号分隔）', type: 'tags' },
      { key: 'description', label: '描述', type: 'textarea' },
      { key: 'exif.camera', label: '设备', type: 'text', group: '视频参数' },
      { key: 'exif.fps', label: '帧率', type: 'text', group: '视频参数' },
      { key: 'exif.codec', label: '编码', type: 'text', group: '视频参数' },
    ],
  },
};

export const PANE_META = {
  overview: ['概览', '数据来源：<code>data/*.json</code>'],
  photos: ['照片', '编辑后逐条写回 <code>data/photos.json</code>，服务端会自动备份'],
  videos: ['视频', '本地文件与外链嵌入共用一份 <code>data/videos.json</code>'],
  albums: ['相册', '相册 id 被照片与视频引用'],
  upload: ['上传', '文件落位到 <code>assets/</code>，条目自动写入对应 JSON'],
  backups: ['备份', '导出 / 导入 / 回滚历史版本'],
  account: ['账号', '修改密码与查看登录状态'],
};

/* ============================================================
   小组件
   ============================================================ */

export function albumName(albums, id) {
  const found = albums.find((a) => a.id === id);
  return found ? found.name : id || '未分类';
}

/** 缩略图；文件缺失时降级成一个角标而不是碎图 */
function thumbCell(url, className, missingText) {
  if (!url) return `<span class="badge badge--missing">${escapeHtml(missingText)}</span>`;
  if (/^https?:\/\//i.test(url)) return '<span class="badge badge--embed">外链</span>';

  return `<img class="${className}" src="${escapeHtml(assetUrl(url))}" alt="" loading="lazy"
      onerror="this.replaceWith(Object.assign(document.createElement('span'),
        {className:'badge badge--missing',textContent:'缺失'}))" />`;
}

function taglist(tags) {
  if (!Array.isArray(tags) || !tags.length) return '<span class="muted">–</span>';
  return `<span class="taglist">${tags.slice(0, 4).map((t) => `<span>${escapeHtml(t)}</span>`).join('')}</span>`;
}

function actions(collection, id) {
  return `
    <button class="btn btn--sm" data-edit="${collection}:${escapeHtml(id)}" type="button">编辑</button>
    <button class="btn btn--sm btn--danger" data-del="${collection}:${escapeHtml(id)}" type="button">删除</button>`;
}

function emptyRow(colspan, text) {
  return `<tr class="empty-row"><td colspan="${colspan}">${escapeHtml(text)}</td></tr>`;
}

/* ============================================================
   概览
   ============================================================ */

export function renderCounts(state) {
  const counts = {
    photos: state.photos.length,
    videos: state.videos.length,
    albums: state.albums.length,
  };
  document.querySelectorAll('.nav__count').forEach((node) => {
    node.textContent = counts[node.dataset.count] ?? 0;
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

export function renderStats(state, info) {
  const missing = info.missingFileCount ?? 0;
  const orphans = (info.orphanAlbums || []).length;

  const cards = [
    { label: '照片', value: state.photos.length, note: 'data/photos.json' },
    { label: '视频', value: state.videos.length, note: 'data/videos.json' },
    { label: '相册', value: state.albums.length, note: 'data/albums.json' },
    { label: '缺失文件', value: missing, note: missing ? '条目引用的文件不存在' : '全部文件就位', tone: missing ? 'warn' : 'ok' },
    { label: '失效相册引用', value: orphans, note: orphans ? '条目指向了不存在的相册' : '相册引用正常', tone: orphans ? 'warn' : 'ok' },
  ];

  $('#stat-grid').innerHTML = cards.map((c) => `
    <div class="stat${c.tone ? ` stat--${c.tone}` : ''}">
      <div class="stat__label">${escapeHtml(c.label)}</div>
      <div class="stat__value">${c.value}</div>
      <div class="stat__note">${escapeHtml(c.note)}</div>
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
    ? history.map((h) => `
      <li>
        <span class="log__time">${escapeHtml(h.at)}</span>
        <span class="log__action">${escapeHtml(h.action)}</span>
        <span class="log__detail" style="${h.ok ? '' : 'color:var(--danger)'}">${escapeHtml(h.detail)}</span>
      </li>`).join('')
    : '<li class="muted">本次启动后还没有操作记录。</li>';
}

/* ============================================================
   表格
   ============================================================ */

export function matchQuery(item, collection, query) {
  const keyword = (query || '').trim().toLowerCase();
  if (!keyword) return true;

  const haystack = [
    item.id, item.title, item.album, item.location, item.description,
    item.exif?.camera, item.exif?.lens, item.resolution,
    ...(item.tags || []),
  ].filter(Boolean).join(' ').toLowerCase();

  return keyword.split(/\s+/).every((term) => haystack.includes(term));
}

export function renderTable(state, collection) {
  const table = $(`#table-${collection}`);
  if (!table) return;

  if (collection === 'photos') {
    const rows = state.photos.filter((item) => matchQuery(item, 'photos', state.query.photos));
    table.innerHTML = `
      <thead><tr><th>预览</th><th>标题</th><th>相册</th><th>时间</th><th>标签</th><th>EXIF</th><th></th></tr></thead>
      <tbody>${rows.length ? rows.map((item) => `
        <tr>
          <td>${thumbCell(item.thumb || item.src, 'thumb', '无图')}</td>
          <td>
            <div class="cell-title">${escapeHtml(item.title || '未命名')}</div>
            <div class="cell-sub">${escapeHtml(item.id)}</div>
          </td>
          <td>${escapeHtml(albumName(state.albums, item.album))}</td>
          <td class="cell-sub">${escapeHtml(item.date || '–')}</td>
          <td>${taglist(item.tags)}</td>
          <td class="cell-sub">${escapeHtml(
            [item.exif?.camera, item.exif?.shutter, item.exif?.aperture, item.exif?.iso].filter(Boolean).join(' · ') || '–',
          )}</td>
          <td class="actions">${actions('photos', item.id)}</td>
        </tr>`).join('') : emptyRow(7, '没有匹配的照片')}</tbody>`;
    return;
  }

  if (collection === 'videos') {
    const rows = state.videos.filter((item) => matchQuery(item, 'videos', state.query.videos));
    table.innerHTML = `
      <thead><tr><th>封面</th><th>标题</th><th>来源</th><th>时长</th><th>分辨率</th><th>相册</th><th></th></tr></thead>
      <tbody>${rows.length ? rows.map((item) => `
        <tr>
          <td>${thumbCell(item.poster, 'thumb thumb--wide', '无封面')}</td>
          <td>
            <div class="cell-title">${escapeHtml(item.title || '未命名')}</div>
            <div class="cell-sub">${escapeHtml(item.id)}</div>
          </td>
          <td><span class="badge ${item.provider === 'file' || !item.provider ? 'badge--file' : 'badge--embed'}">${escapeHtml(item.provider || 'file')}</span></td>
          <td class="cell-sub">${escapeHtml(item.duration ?? '–')}</td>
          <td class="cell-sub">${escapeHtml(item.resolution || '–')}</td>
          <td>${escapeHtml(albumName(state.albums, item.album))}</td>
          <td class="actions">${actions('videos', item.id)}</td>
        </tr>`).join('') : emptyRow(7, '没有匹配的视频')}</tbody>`;
    return;
  }

  if (collection === 'albums') {
    const counts = new Map();
    [...state.photos, ...state.videos].forEach((item) => {
      counts.set(item.album, (counts.get(item.album) || 0) + 1);
    });

    table.innerHTML = `
      <thead><tr><th>封面</th><th>ID</th><th>名称</th><th>描述</th><th>条目</th><th></th></tr></thead>
      <tbody>${state.albums.length ? state.albums.map((item) => `
        <tr>
          <td>${thumbCell(item.cover, 'thumb', '无封面')}</td>
          <td class="cell-sub">${escapeHtml(item.id)}</td>
          <td class="cell-title">${escapeHtml(item.name || '')}</td>
          <td class="muted">${escapeHtml(item.description || '–')}</td>
          <td>${counts.get(item.id) || 0}</td>
          <td class="actions">${actions('albums', item.id)}</td>
        </tr>`).join('') : emptyRow(6, '还没有相册')}</tbody>`;
  }
}

export function renderBackups(backups) {
  const table = $('#table-backups');
  if (!table) return;

  table.innerHTML = `
    <thead><tr><th>文件</th><th>集合</th><th>大小</th><th>时间</th><th></th></tr></thead>
    <tbody>${(backups || []).length ? backups.map((b) => `
      <tr>
        <td class="cell-sub">${escapeHtml(b.name)}</td>
        <td>${escapeHtml(b.collection)}</td>
        <td class="cell-sub">${humanSize(b.size)}</td>
        <td class="cell-sub">${escapeHtml(b.modified)}</td>
        <td class="actions"><button class="btn btn--sm" data-restore="${escapeHtml(b.name)}" type="button">恢复</button></td>
      </tr>`).join('') : emptyRow(5, '还没有备份（首次保存后自动生成）')}</tbody>`;
}

export function renderAlbumOptions(albums, selected = '') {
  const select = $('#up-album');
  if (!select) return;

  select.innerHTML = [
    '<option value="uncategorized">未分类</option>',
    ...albums.map((a) => `<option value="${escapeHtml(a.id)}">${escapeHtml(a.name)}（${escapeHtml(a.id)}）</option>`),
  ].join('');
  if (selected) select.value = selected;
}

/** 上传结果面板 */
export function renderUploadResult(results) {
  const box = $('#upload-result');
  box.hidden = false;

  $('#upload-result-body').innerHTML = results.map((r) => {
    if (!r.ok) {
      return `<div class="result-item">
        <div class="result-item__head"><strong>${escapeHtml(r.file)}</strong>
          <span class="badge badge--missing">失败</span></div>
        <p class="muted">${escapeHtml(r.error)}</p>
      </div>`;
    }

    const item = r.item;
    const exif = item.exif || {};
    const summary = [exif.camera, exif.lens, exif.focalLength, exif.aperture, exif.shutter, exif.iso, exif.fps, exif.codec]
      .filter(Boolean).join(' · ');

    return `<div class="result-item">
      <div class="result-item__head">
        <strong>${escapeHtml(item.title)}</strong>
        <span class="badge badge--file">${escapeHtml(item.id)}</span>
        <span class="muted">${escapeHtml(item.album)}</span>
      </div>
      <div class="cell-sub">${escapeHtml(item.src || '')}</div>
      ${item.poster ? `<div class="cell-sub">封面：${escapeHtml(item.poster)}</div>` : ''}
      ${item.thumb && item.thumb !== item.src ? `<div class="cell-sub">缩略图：${escapeHtml(item.thumb)}</div>` : ''}
      ${summary ? `<div class="cell-sub">${escapeHtml(summary)}</div>` : ''}
      ${r.warnings.length ? `<ul class="warnings">${r.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join('')}</ul>` : ''}
    </div>`;
  }).join('');
}

/** 上传队列 */
export function renderQueue(queue, meta) {
  $('#queue').innerHTML = queue.map((file, index) => {
    const item = meta?.[index];
    return `
      <div class="queue__item">
        <span class="queue__name">${escapeHtml(file.name)}</span>
        <span class="queue__size">${humanSize(file.size)}</span>
        <span class="queue__state ${item?.className || ''}">${escapeHtml(item?.state || '待上传')}</span>
        <button class="btn btn--sm" data-drop="${index}" type="button">移除</button>
      </div>`;
  }).join('');
}

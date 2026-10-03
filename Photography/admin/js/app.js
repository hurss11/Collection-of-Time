/**
 * app.js —— 后台界面的控制器
 *
 * 装配：api（后端） + auth（认证视图） + views（渲染） + ui（通用组件）。
 * 薄客户端：不缓存业务数据、不搜索、不排序、不校验、不格式化；
 * 只做「按服务端给的参数发请求 → 把响应交给渲染层 → 绑定事件」。
 *
 * 本地仅保留三类状态：当前筛选参数（q / sort）、上传中选中的文件、以及
 * 服务端下发的 schema / 计数 / 工具信息（用于渲染，不是数据副本）。
 */

import { api, setUnauthorizedHandler } from './api.js';
import * as auth from './auth.js';
import { CONFIG, assetUrl } from './config.js';
import { $, $$, clearFieldErrors, escapeHtml, modal, readForm, renderForm, showFieldErrors, toast } from './ui.js';
import * as views from './views.js';
import { EMPTY_TEXT, PANE_META } from './views.js';

/* ============================================================
   状态
   ============================================================ */

const COLLECTIONS = ['photos', 'videos', 'albums'];

const state = {
  schema: null,                                   // GET /api/schema
  counts: { photos: 0, videos: 0, albums: 0 },    // GET /api/state → counts
  tools: {},
  user: null,
  filters: {                                      // 只保存查询参数，数据交给服务端
    photos: { q: '', sort: '' },
    videos: { q: '', sort: '' },
    albums: { q: '', sort: '' },
  },
  selected: [],                                   // 待上传的本地 File 对象
};

let editing = null;                               // { collection, originalId }

function setHint(text, kind = '') {
  const node = $('#save-hint');
  if (!node) return;
  node.textContent = text;
  node.className = `save-hint${kind ? ` is-${kind}` : ''}`;
}

function paramsFor(collection) {
  const { q, sort } = state.filters[collection] || {};
  const params = {};
  if (q) params.q = q;
  if (sort) params.sort = sort;
  return params;
}

/* ============================================================
   导航
   ============================================================ */

function switchPane(name) {
  $$('.nav__item').forEach((node) => node.classList.toggle('is-active', node.dataset.pane === name));
  $$('.pane').forEach((node) => node.classList.toggle('is-active', node.dataset.pane === name));

  const [title, sub] = PANE_META[name] || ['后台', ''];
  $('#pane-title').textContent = title;
  $('#pane-sub').innerHTML = sub;
}

/* ============================================================
   数据加载（全部来自服务端）
   ============================================================ */

function renderTables(schema, payloads) {
  const [photos, videos, albums] = payloads;

  for (const collection of COLLECTIONS) {
    const sort = views.renderSortOptions(
      $(`#sort-${collection}`), schema.sorts?.[collection], state.filters[collection].sort,
    );
    if (sort) state.filters[collection].sort = sort;
  }

  views.renderTable($('#table-photos'), photos, EMPTY_TEXT.photos);
  views.renderTable($('#table-videos'), videos, EMPTY_TEXT.videos);
  views.renderTable($('#table-albums'), albums, EMPTY_TEXT.albums);
}

async function reload(silent = false) {
  try {
    const [schema, info, photos, videos, albums] = await Promise.all([
      api.schema(),
      api.state(),
      api.items('photos', paramsFor('photos')),
      api.items('videos', paramsFor('videos')),
      api.items('albums', paramsFor('albums')),
    ]);

    state.schema = schema;
    state.tools = info.tools || {};
    state.user = info.user || state.user;
    state.counts = info.counts || state.counts;

    views.renderCounts(state.counts);
    views.renderTools(state.tools);
    views.renderStats(state.counts, info);
    views.renderIntegrity(info);
    views.renderHistory(info.history);
    views.renderBackups(info.backups);
    views.renderUploadOptions(schema.upload || {});
    renderTables(schema, [photos, videos, albums]);

    const username = state.user?.username || '—';
    $('#current-user').textContent = username;
    $('#account-name').textContent = username;
    $('#current-user-since').textContent = state.user?.createdAt ? `创建于 ${state.user.createdAt}` : '';

    if (!silent) {
      setHint(`已加载 ${state.counts.photos ?? 0} 照片 / ${state.counts.videos ?? 0} 视频 / ${state.counts.albums ?? 0} 相册`);
    }
  } catch (error) {
    setHint(error.message, 'err');
    toast(error.message, 'err', 6000);
  }
}

/** 只刷新一张表（搜索 / 排序用，参数由服务端解释） */
async function refreshTable(collection) {
  try {
    const payload = await api.items(collection, paramsFor(collection));
    views.renderTable($(`#table-${collection}`), payload, EMPTY_TEXT[collection]);
  } catch (error) {
    toast(error.message, 'err', 6000);
  }
}

/* ============================================================
   条目编辑
   ============================================================ */

async function openPosterDialog(itemId) {
  let current = '';
  try {
    const payload = await api.item('videos', itemId);
    current = payload.item?.poster || '';
  } catch (error) {
    toast(error.message, 'err', 6000);
    return;
  }

  const preview = current
    ? `<img class="thumb thumb--wide" id="poster-preview" src="${escapeHtml(assetUrl(current))}" alt="" />`
    : '<span class="badge badge--missing" id="poster-preview">暂无封面</span>';

  modal.open({
    title: `更换封面 · ${itemId}`,
    hint: '上传一张图片，或从视频里抓一帧',
    bodyHtml: `
      <div class="field"><span>当前封面</span>${preview}</div>
      <label class="field">
        <span>抓帧时间（秒）</span>
        <input class="input" id="poster-time" type="number" min="0" step="0.1" value="0" />
        <span class="muted">0 = 第一帧（默认）</span>
      </label>
      <div class="row">
        <button class="btn btn--primary" id="poster-capture" type="button">抓取该帧</button>
        <label class="btn" for="poster-file">上传封面图片…</label>
        <input id="poster-file" type="file" accept="image/*" hidden />
      </div>`,
    onMount: (root) => {
      const busy = (on) => {
        root.querySelectorAll('button, input').forEach((node) => { node.disabled = on; });
      };

      const apply = async (form, okText) => {
        busy(true);
        try {
          const payload = await api.setPoster(itemId, form);
          (payload.warnings || []).forEach((warning) => toast(warning, 'warn', 5000));
          toast(`${okText}：${payload.posterUrl || ''}`, 'ok');
          modal.close();
          await reload(true);
        } catch (error) {
          toast(error.message, 'err', 6000);
          busy(false);
        }
      };

      root.querySelector('#poster-capture').addEventListener('click', () => {
        const form = new FormData();
        form.append('time', root.querySelector('#poster-time').value || '0');
        apply(form, '封面已更新');
      });

      root.querySelector('#poster-file').addEventListener('change', (event) => {
        const file = event.target.files?.[0];
        event.target.value = '';
        if (!file) return;
        const form = new FormData();
        form.append('file', file, file.name);
        apply(form, '封面已上传');
      });
    },
  });
}

async function openEditor(collection, id, isNew) {
  const fields = state.schema.collections[collection]?.fields || [];
  const albums = state.schema.upload?.albums || [];
  const noun = state.schema.nouns?.[collection] || '条目';

  let values = {};
  if (!isNew) {
    try {
      const payload = await api.item(collection, id);       // values 已摊平，直接填控件
      values = payload.values || {};
    } catch (error) {
      toast(error.message, 'err', 6000);
      return;
    }
  }

  editing = { collection, originalId: isNew ? null : id };
  modal.open({
    title: `${isNew ? '新增' : '编辑'}${noun}${isNew ? '' : ` · ${id}`}`,
    hint: isNew ? '保存后由服务端分配 id 并写入数据文件' : '保存会立即写回数据文件',
    bodyHtml: renderForm(fields, values, albums),
    onSave: saveEditor,
  });
}

/**
 * 保存一条记录。
 *
 * 契约：POST /api/items/{collection}，请求体是扁平表单值。
 * 服务端负责归一化与校验，并且**保留 payload 里的 id**——photos / videos 的 schema
 * 虽然没有 id 字段，服务端仍会单独处理它，所以编辑是原地更新，不会新增重复条目。
 * 相册例外：id 本身就是它的字段，改了 id 等于换了一个键，旧记录需要手动删掉。
 */
async function persist(collection, originalId, values) {
  const fields = state.schema.collections[collection]?.fields || [];
  const idIsField = fields.some((field) => field.key === 'id');
  const payload = originalId && !idIsField ? { ...values, id: originalId } : values;

  const result = await api.saveItem(collection, payload);
  const saved = result.item || {};

  if (originalId && idIsField && String(saved.id) !== String(originalId)) {
    await api.removeItem(collection, originalId, false);
  }
  return result;
}

async function saveEditor() {
  if (!editing) return;
  const { collection, originalId } = editing;
  const form = $('#modal-form');
  clearFieldErrors(form);
  const values = readForm(form);                               // 原样扁平值，不做类型转换

  modal.setBusy(true);
  try {
    const result = await persist(collection, originalId, values);

    (result.warnings || []).forEach((warning) => toast(warning, 'warn', 5000));
    toast(`已保存 ${result.item?.id ?? ''}`, 'ok');
    modal.close();
    editing = null;
    await reload(true);
    setHint(`已保存于 ${new Date().toLocaleTimeString()}`, 'ok');
  } catch (error) {
    const fieldErrors = error?.payload?.fieldErrors;
    if (error?.status === 400 && Array.isArray(fieldErrors) && fieldErrors.length) {
      const marked = showFieldErrors(form, fieldErrors);
      setHint('提交内容有误，请检查标红的字段', 'err');
      toast(marked ? error.message : `${error.message}（未找到对应控件）`, 'err', 6000);
    } else {
      toast(error.message, 'err', 6000);
      setHint(error.message, 'err');
    }
  } finally {
    modal.setBusy(false);
  }
}

async function removeItem(collection, id) {
  const noun = state.schema?.nouns?.[collection] || '条目';
  if (!window.confirm(`确定删除${noun} ${id} 吗？\n（JSON 条目会被移除，媒体文件默认保留）`)) return;

  const alsoFile = ['photos', 'videos'].includes(collection)
    && window.confirm('是否同时删除对应的媒体文件？\n确定 = 一并删除文件，取消 = 只删除记录');

  try {
    const result = await api.removeItem(collection, id, alsoFile);
    const removed = result.removedFiles?.length ?? 0;
    toast(`已删除 ${id}${removed ? `，同时清理 ${removed} 个文件` : ''}`, 'ok');
    await reload(true);
  } catch (error) {
    toast(error.message, 'err', 6000);
  }
}

/* ============================================================
   上传：本地只负责按 maxBatchBytes 分批，其余交给服务端
   ============================================================ */

function queueFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;

  state.selected.push(...files);
  views.renderQueue(state.selected);
  $('#up-start').disabled = false;
}

/** 按服务端给出的单批上限切分文件（单文件超限时自己成批，由服务端报错） */
function makeBatches(files, maxBatchBytes) {
  const limit = Number(maxBatchBytes) > 0 ? Number(maxBatchBytes) : Infinity;
  const batches = [];
  let current = [];
  let size = 0;

  for (const file of files) {
    if (current.length && size + file.size > limit) {
      batches.push(current);
      current = [];
      size = 0;
    }
    current.push(file);
    size += file.size;
  }
  if (current.length) batches.push(current);
  return batches;
}

async function startUpload() {
  if (!state.selected.length) return;

  const button = $('#up-start');
  button.disabled = true;
  button.textContent = '上传中…';
  $('#upload-result').hidden = true;

  const groups = makeBatches(state.selected, state.schema?.upload?.maxBatchBytes);
  const cover = $('#up-cover')?.files?.[0] || null;
  const options = {
    kind: 'auto',
    album: $('#up-album').value,
    tags: $('#up-tags').value,
    date: $('#up-date').value,
    location: $('#up-location').value,
    description: $('#up-description').value,
    readExif: $('#up-exif').checked ? '1' : '0',
    makeThumb: $('#up-thumb').checked ? '1' : '0',
    makePoster: $('#up-poster').checked ? '1' : '0',
  };

  const results = [];
  const summary = { total: 0, ok: 0, failed: 0 };

  try {
    for (const [index, group] of groups.entries()) {
      const form = new FormData();
      // 字段名 files 可重复，一次请求带一批文件
      group.forEach((file) => form.append('files', file, file.name));
      Object.entries(options).forEach(([key, value]) => form.append(key, value));
      // 选了封面图片就带上：服务端把它作为本批视频的封面（否则抓第一帧）
      if (cover) form.append('posterFile', cover, cover.name);

      setHint(`正在上传第 ${index + 1} / ${groups.length} 批…`);
      const payload = await api.upload('/api/upload', form);

      results.push(...(payload.results || []));
      const batch = payload.summary || {};
      summary.total += batch.total ?? 0;
      summary.ok += batch.ok ?? 0;
      summary.failed += batch.failed ?? 0;
    }

    views.renderUploadResult({ results, summary });
    toast(`上传结束：成功 ${summary.ok} / ${summary.total}`, summary.failed ? 'warn' : 'ok');
  } catch (error) {
    toast(`上传失败：${error.message}`, 'err', 6000);
    setHint(error.message, 'err');
    if (results.length) views.renderUploadResult({ results, summary });
  } finally {
    state.selected = [];
    views.renderQueue(state.selected);
    const coverInput = $('#up-cover');
    if (coverInput) coverInput.value = '';
    button.textContent = '开始上传';
    button.disabled = true;
    await reload(true);
  }
}

/* ============================================================
   备份 / 导入导出
   ============================================================ */

async function exportBundle() {
  try {
    const response = await api.exportBundle();
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `photography-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast('备份已下载', 'ok');
  } catch (error) {
    toast(`导出失败：${error.message}`, 'err', 6000);
  }
}

async function importBundle(file) {
  const merge = $('#import-merge').checked;
  const ok = window.confirm(merge
    ? '按 id 合并导入？（同 id 覆盖，其它保留）'
    : '整体覆盖导入？\n当前的三份 JSON 都会被替换，请确认已有备份。');
  if (!ok) return;

  const form = new FormData();
  form.append('file', file);
  form.append('mode', merge ? 'merge' : 'replace');

  try {
    const payload = await api.importBundle(form);
    toast(`导入完成：${JSON.stringify(payload.imported)}`, 'ok', 5000);
    await reload(true);
  } catch (error) {
    toast(`导入失败：${error.message}`, 'err', 6000);
  }
}

async function restoreBackup(name) {
  if (!window.confirm(`用备份 ${name} 覆盖当前数据？\n（覆盖前会再自动存一份当前状态，可回滚）`)) return;
  try {
    const payload = await api.restore(name);
    toast(`已恢复 ${payload.restored}`, 'ok');
    await reload(true);
  } catch (error) {
    toast(error.message, 'err', 6000);
  }
}

/* ============================================================
   事件
   ============================================================ */

function bindEvents() {
  $('#nav').addEventListener('click', (event) => {
    const item = event.target.closest('.nav__item');
    if (item) switchPane(item.dataset.pane);
  });

  document.addEventListener('click', (event) => {
    const edit = event.target.closest('[data-edit]');
    if (edit) {
      const [collection, id] = edit.dataset.edit.split(':');
      if (collection && id) openEditor(collection, id, false);
      return;
    }

    const del = event.target.closest('[data-del]');
    if (del) {
      const [collection, id] = del.dataset.del.split(':');
      if (collection && id) removeItem(collection, id);
      return;
    }

    const poster = event.target.closest('[data-poster]');
    if (poster) {
      const [, id] = poster.dataset.poster.split(':');
      if (id) openPosterDialog(id);
      return;
    }

    const add = event.target.closest('[data-action="add"]');
    if (add) {
      openEditor(add.dataset.collection, null, true);
      return;
    }

    const restore = event.target.closest('[data-restore]');
    if (restore) restoreBackup(restore.dataset.restore);
  });

  // 弹窗
  $('#modal').addEventListener('click', (event) => {
    if (event.target.closest('[data-close]')) modal.close();
  });

  // 缩略图加载失败 → 降级成角标（error 事件不冒泡，必须在捕获阶段监听）
  document.addEventListener('error', (event) => {
    const node = event.target;
    if (!(node instanceof HTMLImageElement) || !node.dataset.fallback) return;
    const badge = document.createElement('span');
    badge.className = 'badge badge--missing';
    badge.textContent = node.dataset.fallback;
    node.replaceWith(badge);
  }, true);
  $('#modal-save').addEventListener('click', () => modal.triggerSave());
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && modal.isOpen) modal.close();
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && modal.isOpen) {
      event.preventDefault();
      modal.triggerSave();
    }
  });

  // 搜索 / 排序：参数发给服务端，前端只负责展示结果
  for (const collection of COLLECTIONS) {
    const search = $(`#search-${collection}`);
    let timer = 0;
    search.addEventListener('input', () => {
      state.filters[collection].q = search.value;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => refreshTable(collection), 260);
    });

    $(`#sort-${collection}`).addEventListener('change', (event) => {
      state.filters[collection].sort = event.target.value;
      refreshTable(collection);
    });
  }

  // 上传区
  const dropzone = $('#dropzone');
  const fileInput = $('#file-input');

  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener('change', () => {
    queueFiles(fileInput.files);
    fileInput.value = '';
  });

  ['dragenter', 'dragover'].forEach((type) => dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    dropzone.classList.add('is-over');
  }));
  ['dragleave', 'drop'].forEach((type) => dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    dropzone.classList.remove('is-over');
  }));
  dropzone.addEventListener('drop', (event) => queueFiles(event.dataTransfer.files));

  $('#queue').addEventListener('click', (event) => {
    const drop = event.target.closest('[data-drop]');
    if (!drop) return;
    state.selected.splice(Number(drop.dataset.drop), 1);
    views.renderQueue(state.selected);
    $('#up-start').disabled = state.selected.length === 0;
  });

  $('#up-start').addEventListener('click', startUpload);

  // 备份
  $('#btn-export').addEventListener('click', exportBundle);
  $('#import-input').addEventListener('change', (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (file) importBundle(file);
  });

  // 账号（侧边栏用户卡片与「账号」页各有一个入口）
  $('#btn-change-password').addEventListener('click', auth.changePassword);
  $('#btn-change-password-side').addEventListener('click', auth.changePassword);
  $('#btn-logout').addEventListener('click', () => {
    if (window.confirm('确定退出登录吗？')) auth.logout();
  });
}

/* ============================================================
   启动
   ============================================================ */

function resetState() {
  state.schema = null;
  state.counts = { photos: 0, videos: 0, albums: 0 };
  state.user = null;
  state.selected = [];
  state.filters = {
    photos: { q: '', sort: '' },
    videos: { q: '', sort: '' },
    albums: { q: '', sort: '' },
  };
  editing = null;
  for (const collection of COLLECTIONS) {
    const search = $(`#search-${collection}`);
    if (search) search.value = '';
  }
  modal.close();
}

async function boot() {
  // 版本号同时用于登录页页脚与「账号」页
  $$('[data-version]').forEach((node) => { node.textContent = CONFIG.version; });

  bindEvents();
  switchPane('overview');

  // 任何请求收到 401 → 立刻回到登录页（会话过期 / 被改密踢下线）
  setUnauthorizedHandler(() => {
    resetState();
    auth.showLogin('登录状态已失效，请重新登录');
  });

  await auth.bootstrapAuth({
    onAuthenticated: async () => {
      switchPane('overview');
      await reload(true);
      setHint('已登录');
    },
    onLoginRequired: resetState,
  });
}

boot();

/**
 * app.js —— 后台界面的控制器
 *
 * 装配：api（后端） + auth（认证视图） + views（渲染） + ui（通用组件）。
 * 这里是唯一持有状态的地方：所有数据变更都先落服务端，再回来重渲染。
 */

import { api, setUnauthorizedHandler } from './api.js';
import * as auth from './auth.js';
import { CONFIG } from './config.js';
import { $, $$, collectForm, modal, renderForm, toast } from './ui.js';
import * as views from './views.js';
import { PANE_META, SCHEMAS } from './views.js';

/* ============================================================
   状态
   ============================================================ */

const state = {
  albums: [],
  photos: [],
  videos: [],
  tools: {},
  user: null,
  query: { photos: '', videos: '' },
  queue: [],
  queueMeta: undefined,
};

function setHint(text, kind = '') {
  const node = $('#save-hint');
  if (!node) return;
  node.textContent = text;
  node.className = `save-hint${kind ? ` is-${kind}` : ''}`;
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

  if (name === 'upload') views.renderAlbumOptions(state.albums, $('#up-album')?.value);
}

/* ============================================================
   数据加载
   ============================================================ */

async function reload(silent = false) {
  try {
    const [info, albums, photos, videos] = await Promise.all([
      api.state(),
      api.list('albums'),
      api.list('photos'),
      api.list('videos'),
    ]);

    state.albums = albums.items || [];
    state.photos = photos.items || [];
    state.videos = videos.items || [];
    state.tools = info.tools || {};
    state.user = info.user || state.user;

    views.renderCounts(state);
    views.renderTools(state.tools);
    views.renderStats(state, info);
    views.renderIntegrity(info);
    views.renderHistory(info.history);
    views.renderBackups(info.backups);
    views.renderTable(state, 'photos');
    views.renderTable(state, 'videos');
    views.renderTable(state, 'albums');
    views.renderAlbumOptions(state.albums, $('#up-album')?.value);

    if (!$('#current-user')?.textContent || $('#current-user').textContent === '未登录') {
      $('#current-user').textContent = state.user?.username || '—';
    }
    const accountName = $('#account-name');
    if (accountName) accountName.textContent = state.user?.username || '—';

    if (!silent) {
      setHint(`已加载 ${state.photos.length} 照片 / ${state.videos.length} 视频 / ${state.albums.length} 相册`);
    }
  } catch (error) {
    setHint(error.message, 'err');
    toast(error.message, 'err', 6000);
  }
}

/* ============================================================
   条目编辑
   ============================================================ */

let editing = null;   // { collection, originalId, isNew, draft }

function openEditor(collection, item, isNew) {
  const schema = SCHEMAS[collection];
  const draft = item ? JSON.parse(JSON.stringify(item)) : { id: '', tags: [], exif: {} };

  editing = { collection, originalId: item?.id ?? null, isNew, draft };

  modal.open({
    title: `${isNew ? '新增' : '编辑'}${schema.noun}${isNew ? '' : ` · ${draft.id}`}`,
    hint: isNew ? '保存后由服务端分配 id 并写入 JSON' : '保存会立即写回数据文件',
    bodyHtml: renderForm(schema.fields, draft, state.albums),
    onSave: saveEditor,
  });
}

async function saveEditor() {
  if (!editing) return;
  const { collection, originalId, isNew, draft } = editing;

  let payload;
  try {
    payload = collectForm($('#modal-form'), SCHEMAS[collection].fields, draft);
  } catch (error) {
    toast(error.message, 'err');
    return;
  }

  modal.setBusy(true);
  try {
    let result;
    if (isNew && !payload.id) {
      result = await api.save(collection, payload);
    } else if (!isNew && originalId && payload.id !== originalId) {
      // 改了 id：整表替换，保持原有顺序
      const list = state[collection];
      const index = list.findIndex((i) => i.id === originalId);
      const next = [...list];
      next[index] = payload;
      result = await api.replaceAll(collection, next);
    } else {
      result = await api.save(collection, payload);
    }

    (result.warnings || []).forEach((w) => toast(w, 'warn', 5000));
    toast(isNew ? `已新增 ${result.item?.id ?? ''}` : `已保存 ${payload.id}`, 'ok');
    modal.close();
    editing = null;
    await reload(true);
    setHint(`已保存于 ${new Date().toLocaleTimeString()}`, 'ok');
  } catch (error) {
    toast(error.message, 'err', 6000);
    setHint(error.message, 'err');
  } finally {
    modal.setBusy(false);
  }
}

async function removeItem(collection, id) {
  const noun = SCHEMAS[collection].noun;
  if (!window.confirm(`确定删除${noun} ${id} 吗？\n（JSON 条目会被移除，媒体文件默认保留）`)) return;

  const alsoFile = ['photos', 'videos'].includes(collection)
    && window.confirm('是否同时删除对应的媒体文件？\n确定 = 一并删除文件，取消 = 只删除记录');

  try {
    const result = await api.remove(collection, id, alsoFile);
    const removed = result.removedFiles?.length ?? 0;
    toast(`已删除 ${id}${removed ? `，同时清理 ${removed} 个文件` : ''}`, 'ok');
    await reload(true);
  } catch (error) {
    toast(error.message, 'err', 6000);
  }
}

/* ============================================================
   上传
   ============================================================ */

function queueFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;

  state.queue.push(...files);
  views.renderQueue(state.queue, state.queueMeta);
  $('#up-start').disabled = false;
}

async function startUpload() {
  if (!state.queue.length) return;

  const button = $('#up-start');
  button.disabled = true;
  button.textContent = '上传中…';
  $('#upload-result').hidden = true;

  const options = {
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
  state.queueMeta = state.queue.map(() => ({ state: '排队中', className: '' }));

  for (const [index, file] of state.queue.entries()) {
    state.queueMeta[index] = { state: '上传中…', className: 'is-busy' };
    views.renderQueue(state.queue, state.queueMeta);

    const form = new FormData();
    form.append('file', file);
    form.append('kind', 'auto');
    Object.entries(options).forEach(([key, value]) => form.append(key, value));

    try {
      const payload = await api.upload('/api/upload', form);
      state.queueMeta[index] = { state: `✓ ${payload.item.id}`, className: 'is-ok' };
      results.push({ file: file.name, ok: true, item: payload.item, warnings: payload.warnings || [] });
    } catch (error) {
      state.queueMeta[index] = { state: `✗ ${error.message}`, className: 'is-err' };
      results.push({ file: file.name, ok: false, error: error.message });
    }
    views.renderQueue(state.queue, state.queueMeta);
  }

  views.renderUploadResult(results);
  const failed = results.filter((r) => !r.ok).length;
  toast(`上传结束：成功 ${results.length - failed} / ${results.length}`, failed ? 'warn' : 'ok');

  state.queue = [];
  state.queueMeta = undefined;
  button.textContent = '开始上传';
  button.disabled = true;
  await reload(true);
}

/* ============================================================
   备份
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
      const item = state[collection].find((i) => i.id === id);
      if (item) openEditor(collection, item, false);
      return;
    }

    const del = event.target.closest('[data-del]');
    if (del) {
      const [collection, id] = del.dataset.del.split(':');
      removeItem(collection, id);
      return;
    }

    const add = event.target.closest('[data-action="add"]');
    if (add) {
      const collection = add.dataset.collection;
      openEditor(collection, collection === 'albums'
        ? { id: '', name: '', description: '', cover: '' }
        : { title: '', album: '', tags: [] }, true);
      return;
    }

    const restore = event.target.closest('[data-restore]');
    if (restore) restoreBackup(restore.dataset.restore);
  });

  // 弹窗
  $('#modal').addEventListener('click', (event) => {
    if (event.target.closest('[data-close]')) modal.close();
  });
  $('#modal-save').addEventListener('click', () => modal.triggerSave());
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && modal.isOpen) modal.close();
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && modal.isOpen) {
      event.preventDefault();
      modal.triggerSave();
    }
  });

  // 表格筛选
  $('#search-photos').addEventListener('input', (event) => {
    state.query.photos = event.target.value;
    views.renderTable(state, 'photos');
  });
  $('#search-videos').addEventListener('input', (event) => {
    state.query.videos = event.target.value;
    views.renderTable(state, 'videos');
  });

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
    state.queue.splice(Number(drop.dataset.drop), 1);
    views.renderQueue(state.queue, state.queueMeta);
    $('#up-start').disabled = state.queue.length === 0;
  });

  $('#up-start').addEventListener('click', startUpload);

  // 备份
  $('#btn-export').addEventListener('click', exportBundle);
  $('#import-input').addEventListener('change', (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (file) importBundle(file);
  });

  // 账号
  $('#btn-change-password').addEventListener('click', auth.changePassword);
  $('#btn-logout').addEventListener('click', () => {
    if (window.confirm('确定退出登录吗？')) auth.logout();
  });
}

/* ============================================================
   启动
   ============================================================ */

function resetState() {
  state.albums = [];
  state.photos = [];
  state.videos = [];
  state.queue = [];
  state.query = { photos: '', videos: '' };
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

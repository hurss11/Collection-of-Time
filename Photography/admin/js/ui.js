/**
 * ui.js —— 通用 UI 层
 *
 * 只放与业务无关的东西：DOM 选择器、转义、提示、弹窗、schema 驱动的表单渲染。
 * 表单值一律「扁平进出」：读出来是 { 'exif.camera': '...' }，写进去也是同样形状，
 * 归一化（exif 嵌套、数字、数组）与校验都由服务端负责。
 */

export const $ = (selector, scope = document) => scope.querySelector(selector);
export const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];

/* ============================================================
   基础工具
   ============================================================ */

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

export function humanSize(bytes) {
  if (!Number.isFinite(bytes)) return '–';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

export function formatCountdown(seconds) {
  const total = Math.max(0, Math.ceil(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m} 分 ${String(s).padStart(2, '0')} 秒` : `${s} 秒`;
}

/* ============================================================
   提示
   ============================================================ */

export function toast(message, kind = 'info', timeout = 3200) {
  const host = $('#toasts');
  if (!host) return;

  const node = document.createElement('div');
  node.className = `toast toast--${kind}`;
  node.textContent = message;
  host.append(node);

  setTimeout(() => {
    node.style.transition = 'opacity .2s, transform .2s';
    node.style.opacity = '0';
    node.style.transform = 'translateX(14px)';
    setTimeout(() => node.remove(), 220);
  }, timeout);
}

/* ============================================================
   弹窗
   ============================================================ */

let modalSaveHandler = null;

export const modal = {
  /** @param {{title:string, hint?:string, bodyHtml:string, saveText?:string, onSave?:Function, onMount?:Function}} options */
  open(options) {
    const root = $('#modal');
    if (!root) return;

    $('#modal-title').textContent = options.title;
    $('#modal-hint').textContent = options.hint || '';
    $('#modal-form').innerHTML = options.bodyHtml;

    const saveButton = $('#modal-save');
    saveButton.textContent = options.saveText || '保存';
    saveButton.hidden = !options.onSave;

    modalSaveHandler = options.onSave || null;
    root.hidden = false;
    options.onMount?.($('#modal-form'));
  },

  close() {
    const root = $('#modal');
    if (root) root.hidden = true;
    modalSaveHandler = null;
  },

  get isOpen() {
    return !$('#modal')?.hidden;
  },

  async triggerSave() {
    if (modalSaveHandler) await modalSaveHandler();
  },

  setBusy(busy, text = '保存中…') {
    const button = $('#modal-save');
    if (!button) return;
    button.disabled = busy;
    button.textContent = busy ? text : '保存';
  },
};

/* ============================================================
   表单：schema 驱动的「扁平」读写
   ============================================================ */

function fieldMarkup(field, value, albums) {
  const common = `data-key="${escapeHtml(field.key)}" class="input${field.mono ? ' input--mono' : ''}"`;
  const hint = field.hint ? `<span class="muted">${escapeHtml(field.hint)}</span>` : '';
  const label = `<span>${escapeHtml(field.label)}${field.required ? ' <i class="req">*</i>' : ''}</span>`;

  if (field.type === 'select') {
    const options = (field.options || [])
      .map((o) => `<option value="${escapeHtml(o.value)}"${o.value === value ? ' selected' : ''}>${escapeHtml(o.label)}</option>`)
      .join('');
    return `<label class="field">${label}<select ${common}>${options}</select>${hint}</label>`;
  }

  if (field.type === 'album') {
    const list = albums || [];
    const options = ['<option value="">（未指定）</option>'];
    for (const album of list) {
      options.push(`<option value="${escapeHtml(album.value)}"${album.value === value ? ' selected' : ''}>${escapeHtml(album.label)}（${escapeHtml(album.value)}）</option>`);
    }
    if (value && !list.some((a) => a.value === value)) {
      options.push(`<option value="${escapeHtml(value)}" selected>${escapeHtml(value)}（相册不存在）</option>`);
    }
    return `<label class="field">${label}<select ${common}>${options.join('')}</select>${hint}</label>`;
  }

  if (field.type === 'textarea') {
    return `<label class="field field--wide">${label}<textarea ${common} rows="2">${escapeHtml(value ?? '')}</textarea>${hint}</label>`;
  }

  const type = field.type === 'number' ? 'number' : field.type === 'password' ? 'password' : 'text';
  const step = field.type === 'number' ? ' step="any"' : '';
  const autocomplete = field.type === 'password' ? ' autocomplete="new-password"' : '';
  return `<label class="field">${label}<input ${common} type="${type}"${step}${autocomplete}
    value="${escapeHtml(value ?? '')}" placeholder="${escapeHtml(field.placeholder || '')}" />${hint}</label>`;
}

/**
 * 依据 schema 生成表单 HTML（支持 group 分组）。
 * @param {object[]} fields 服务端 schema.fields
 * @param {object} values 已摊平的表单值（GET /api/item 的 values）
 * @param {object[]} albums 相册选项，[{value,label}]
 */
export function renderForm(fields, values = {}, albums = []) {
  // tags 在 values 里是数组，控件里用逗号串表示
  const valueOf = (field) => {
    const raw = values[field.key];
    if (field.type === 'tags') return Array.isArray(raw) ? raw.join(', ') : raw ?? '';
    return raw ?? '';
  };

  const plain = fields.filter((f) => !f.group);
  const groups = [...new Set(fields.filter((f) => f.group).map((f) => f.group))];

  return [
    `<div class="field-grid">${plain.map((f) => fieldMarkup(f, valueOf(f), albums)).join('')}</div>`,
    ...groups.map((group) => `<fieldset><legend>${escapeHtml(group)}</legend>
      <div class="field-grid">${fields
        .filter((f) => f.group === group)
        .map((f) => fieldMarkup(f, valueOf(f), albums))
        .join('')}</div></fieldset>`),
  ].join('');
}

/**
 * 读取表单值：原样取出每个控件的字符串，不做任何类型转换。
 * 服务端按 schema 归一化（数字 / 标签数组 / exif.* 嵌套）并校验。
 */
export function readForm(scope) {
  const values = {};
  for (const node of $$('[data-key]', scope)) values[node.dataset.key] = node.value;
  return values;
}

/** 清掉上一次的字段级错误标记 */
export function clearFieldErrors(scope) {
  for (const node of $$('[data-key]', scope)) {
    node.classList.remove('is-invalid');
    node.parentElement?.querySelectorAll('.field__error').forEach((el) => el.remove());
  }
}

/**
 * 把服务端的 fieldErrors（{field, message}，field 为扁平键）标到对应控件上。
 * @returns {boolean} 是否至少标红了一个控件
 */
export function showFieldErrors(scope, errors) {
  clearFieldErrors(scope);
  let first = null;

  for (const item of errors || []) {
    const key = String(item?.field ?? '');
    if (!key) continue;
    const node = $$('[data-key]', scope).find((el) => el.dataset.key === key);
    if (!node) continue;

    node.classList.add('is-invalid');
    const note = document.createElement('span');
    note.className = 'field__error';
    note.textContent = item.message || '该项有误';
    node.after(note);
    first = first || node;
  }

  if (first) first.focus();
  return Boolean(first);
}

/* ============================================================
   密码强度提示
   ============================================================ */

/**
 * 粗略的密码强度评估，只用于给用户反馈（真正的下限校验在后端）。
 * @returns {{score: 0|1|2|3, label: string, tips: string[]}}
 */
export function passwordStrength(password) {
  const tips = [];
  let score = 0;

  if (password.length >= 8) score += 1;
  else tips.push('至少 8 位');

  if (password.length >= 12) score += 1;

  if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score += 1;
  else tips.push('混合大小写');

  if (/\d/.test(password)) score += 1;
  else tips.push('包含数字');

  if (/[^A-Za-z0-9]/.test(password)) score += 1;
  else tips.push('包含符号');

  const level = score <= 2 ? 1 : score <= 3 ? 2 : 3;
  const label = { 1: '偏弱', 2: '一般', 3: '较强' }[level];
  return { score: level, label, tips: tips.slice(0, 3) };
}

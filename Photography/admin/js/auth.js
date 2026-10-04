/**
 * auth.js —— 认证视图控制器
 *
 * 负责登录页（含首次使用时的「创建管理员账号」）与改密弹窗，
 * 通过回调把「已登录 / 已登出」交给 app.js，自己不碰业务逻辑。
 */

import { api, ApiError } from './api.js';
import { CONFIG } from './config.js';
import { $, escapeHtml, modal, toast, formatCountdown, passwordStrength } from './ui.js';

let handlers = { onAuthenticated: () => {}, onLoginRequired: () => {} };
let mode = 'login';          // 'login' | 'setup'
let lockTimer = 0;
let lockUntil = 0;           // 限流锁定到期的时间戳，0 表示未锁定

/**
 * 这次访问是不是「明文 HTTP 且不是本机」。
 *
 * 后台会话 Cookie 只有在 HTTPS 下才带 `Secure`，口令本身更是每次都明文提交，
 * 所以从公网 IP 直接走 http 访问后台时，必须在拿到密码**之前**提醒一句 ——
 * 这正是安全测试里反复出现的「忘了配 HTTPS」那一类问题。
 */
export function insecureTransport() {
  if (location.protocol !== 'http:') return false;
  const host = (location.hostname || '').toLowerCase();
  return !(
    host === 'localhost' || host === '127.0.0.1' || host === '::1' || host === '[::1]'
    || host.endsWith('.localhost') || host.endsWith('.local') || host.endsWith('.internal')
    || /^10\./.test(host) || /^192\.168\./.test(host)
    || /^172\.(1[6-9]|2\d|3[01])\./.test(host)
  );
}

/* ============================================================
   视图切换
   ============================================================ */

function showError(message) {
  const node = $('#login-error');
  node.textContent = message;
  node.hidden = !message;
}

function idleButtonLabel() {
  return mode === 'setup' ? '创建并登录' : '登录';
}

/** 429 锁定时的倒计时提示 */
function startLockCountdown(seconds, message) {
  window.clearInterval(lockTimer);
  let remaining = Math.max(1, Math.ceil(seconds));
  lockUntil = Date.now() + remaining * 1000;

  const tick = () => {
    const left = Math.ceil((lockUntil - Date.now()) / 1000);

    if (left <= 0) {
      window.clearInterval(lockTimer);
      lockUntil = 0;
      showError('');
      $('#login-submit').disabled = false;
      $('#login-submit').textContent = idleButtonLabel();
      return;
    }

    showError(`${message}（${formatCountdown(left)} 后可重试）`);
    const button = $('#login-submit');
    button.disabled = true;
    button.textContent = idleButtonLabel();   // 不要在锁定时停在「登录中…」
  };

  tick();
  lockTimer = window.setInterval(tick, 1000);
}

function applyMode(next) {
  mode = next;
  const isSetup = mode === 'setup';

  $('#login-title').textContent = isSetup ? '创建管理员账号' : '登录后台';
  $('#login-sub').textContent = isSetup
    ? '这是第一次使用，请设置管理员账号。密码只保存哈希，一旦忘记需要服务器上执行 --reset-password 重置。'
    : '请输入管理员账号与密码。';
  $('#login-submit').textContent = isSetup ? '创建并登录' : '登录';
  $('#login-confirm-field').hidden = !isSetup;
  $('#login-strength').hidden = !isSetup;
  $('#login-username-field').hidden = false;

  $('#login-password').setAttribute('autocomplete', isSetup ? 'new-password' : 'current-password');
  showError('');
}

export function showLogin(message = '') {
  window.clearInterval(lockTimer);
  $('#view-app').hidden = true;
  $('#boot').hidden = true;
  $('#view-login').hidden = false;

  const warn = $('#login-insecure');
  if (warn) warn.hidden = !insecureTransport();

  // 仍处于限流锁定时不要解锁按钮
  if (!lockUntil) {
    $('#login-submit').disabled = false;
    $('#login-submit').textContent = idleButtonLabel();
  }

  if (message) showError(message);
  $('#login-username').focus();
}

function showApp(user) {
  window.clearInterval(lockTimer);
  $('#view-login').hidden = true;
  $('#boot').hidden = true;
  $('#view-app').hidden = false;

  $('#current-user').textContent = user?.username || '未登录';
  $('#current-user-since').textContent = user?.createdAt ? `创建于 ${user.createdAt}` : '';

  // 登录之后再补一句（这时人已经在用后台，比登录页那一条更容易被忽略）
  if (insecureTransport()) {
    toast('当前是明文 HTTP 连接：口令与凭据会裸传，建议改用 HTTPS（./run.sh https）或 SSH 隧道',
      'warn', 9000);
  }
}

/* ============================================================
   提交流程
   ============================================================ */

async function submit(event) {
  event.preventDefault();
  showError('');

  const username = $('#login-username').value.trim();
  const password = $('#login-password').value;
  const confirm = $('#login-confirm').value;

  if (!username) return showError('请输入用户名');
  if (!password) return showError('请输入密码');
  if (mode === 'setup') {
    if (password.length < 8) return showError('密码至少 8 位');
    if (password !== confirm) return showError('两次输入的密码不一致');
  }

  const button = $('#login-submit');
  button.disabled = true;
  button.textContent = mode === 'setup' ? '创建中…' : '登录中…';

  try {
    const payload = mode === 'setup'
      ? await api.setup({ username, password, confirm })
      : await api.login({ username, password });

    $('#login-password').value = '';
    $('#login-confirm').value = '';
    toast(mode === 'setup' ? '管理员账号已创建' : `欢迎回来，${payload.user?.username || ''}`, 'ok');
    showApp(payload.user);
    handlers.onAuthenticated(payload.user);
  } catch (error) {
    if (error instanceof ApiError && error.status === 429) {
      startLockCountdown(error.retryAfter || 60, error.message);
    } else {
      showError(error.message || '登录失败');
      if (error.status === 412) applyMode('setup');   // 后端提示还没有账号
      button.textContent = idleButtonLabel();
    }
  } finally {
    // 锁定时倒计时接管按钮状态，这里不要覆盖
    if (!lockUntil) {
      button.disabled = false;
      button.textContent = idleButtonLabel();
    }
  }
}

/* ============================================================
   改密 / 登出
   ============================================================ */

export function changePassword() {
  const fields = [
    { key: 'current', label: '当前密码', type: 'password', required: true },
    { key: 'password', label: '新密码（至少 8 位）', type: 'password', required: true },
    { key: 'confirm', label: '确认新密码', type: 'password', required: true },
  ];

  const body = `<div class="field-grid">${fields
    .map((f) => `<label class="field"><span>${escapeHtml(f.label)}</span>
      <input class="input" data-key="${f.key}" type="password" autocomplete="new-password" /></label>`)
    .join('')}</div><p class="muted">改密后，其它设备上的登录状态会立即失效，需要重新登录。</p>`;

  modal.open({
    title: '修改密码',
    hint: '密码只保存 PBKDF2 哈希',
    bodyHtml: body,
    saveText: '更新密码',
    onSave: async () => {
      const read = (key) => $(`#modal-form [data-key="${key}"]`).value;
      const current = read('current');
      const password = read('password');
      const confirm = read('confirm');

      if (!current) return toast('请输入当前密码', 'err');
      if (password.length < 8) return toast('新密码至少 8 位', 'err');
      if (password !== confirm) return toast('两次输入的新密码不一致', 'err');

      modal.setBusy(true, '更新中…');
      try {
        const payload = await api.changePassword({ current, password, confirm });
        toast(payload.message || '密码已更新', 'ok');
        modal.close();
      } catch (error) {
        toast(error.message, 'err', 5000);
      } finally {
        modal.setBusy(false, '更新密码');
      }
    },
  });
}

/**
 * 重新向后端确认该显示「登录」还是「首次创建账号」。
 * 退出登录、以及会话失效后都要调用——否则会停留在上一次的模式上。
 */
export async function refreshMode() {
  try {
    const payload = await api.session();
    applyMode(payload.setupRequired ? 'setup' : 'login');
    return payload;
  } catch {
    applyMode('login');
    return null;
  }
}

export async function logout() {
  try {
    await api.logout();
  } catch {
    /* 即使请求失败也把界面切回登录页 */
  }

  modal.close();
  handlers.onLoginRequired();

  // 先确认模式（applyMode 会清空错误提示），再显示退出提示，否则提示会被覆盖
  await refreshMode();
  showLogin('已退出登录');
}

/* ============================================================
   启动
   ============================================================ */

/**
 * 绑定登录页事件并检查当前会话。
 * @param {{onAuthenticated?: Function, onLoginRequired?: Function}} callbacks
 */
export async function bootstrapAuth(callbacks = {}) {
  handlers = { ...handlers, ...callbacks };

  $('#login-form').addEventListener('submit', submit);

  $('#login-pwd-toggle').addEventListener('click', () => {
    const input = $('#login-password');
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    $('#login-pwd-toggle').textContent = show ? '隐藏' : '显示';
  });

  // 创建账号时实时提示密码强度
  $('#login-password').addEventListener('input', (event) => {
    if (mode !== 'setup') return;
    const { score, label, tips } = passwordStrength(event.target.value);
    const bar = $('#login-strength');
    bar.dataset.level = String(score);
    $('.pwd-strength__label', bar).textContent = event.target.value
      ? `强度：${label}${tips.length ? `（建议：${tips.join('、')}）` : ''}`
      : '';
  });

  try {
    const payload = await api.session();

    if (payload.authenticated) {
      showApp(payload.user);
      handlers.onAuthenticated(payload.user);
      return true;
    }

    applyMode(payload.setupRequired ? 'setup' : 'login');
    if (payload.authDisabled) {
      showLogin('后端以 --no-auth 启动（仅限本机调试），无需登录');
    } else {
      showLogin('');
    }
    return false;
  } catch (error) {
    showLogin(`无法连接后端：${error.message}`);
    return false;
  }
}

export const AUTH_VERSION = CONFIG.version;

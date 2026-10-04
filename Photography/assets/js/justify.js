/**
 * justify.js —— 等高拼接（justified rows）
 *
 * 把一串作品按宽高比分行。一行就是一个 flex 容器，每件作品的
 * `flex-grow` 取自己的宽高比 —— 容器宽度按比例分下去，同一行的
 * 行高必然一致；盒子的 aspect-ratio 也等于图片比例，所以照片不被裁切。
 * 这里只负责决定「哪几件算一行」，以及最后一行要不要收窄。
 *
 * 宽高比的来源依次是：
 *   1. `data-ar`（服务端按原始尺寸算好的，见 query.py 的 media_size）
 *   2. 图片自己解码出来的尺寸（加载后若差别超过 2% 就地纠正一次）
 *   3. 兜底 3:2
 *
 * 因为第一步就有值，首屏不必等图片解码，行高不会跳。
 */

const DEFAULT_AR = 1.5;
/** 只用来挡住脏数据，正常照片不会碰到 */
const MIN_AR = 0.2;
const MAX_AR = 6;
/** 最后一行会被拉伸到这里倍数以上时，改为收窄整行，避免单张作品变成巨幅 */
const LOOSE_LIMIT = 1.7;

/** 每个容器记住上一次的行结构，结构没变就不动 DOM（只是重算比例） */
const structure = new WeakMap();

function clamp(value) {
  return Math.min(MAX_AR, Math.max(MIN_AR, value));
}

function aspectOf(node) {
  const raw = Number.parseFloat(node.dataset.ar || '');
  return Number.isFinite(raw) && raw > 0 ? clamp(raw) : DEFAULT_AR;
}

function gapOf(container) {
  const style = getComputedStyle(container);
  const gap = Number.parseFloat(style.columnGap || style.gap || '');
  return Number.isFinite(gap) ? gap : 8;
}

/** 目标行高：窄屏矮一点，宽屏 180–240 之间 */
function targetHeight(width) {
  if (width >= 1100) return 230;
  if (width >= 860) return 210;
  if (width >= 620) return 190;
  // 手机：一行放不下几张，就把目标抬到区间上限 —— 横幅照片独占一行，
  // 得到接近整屏宽的大图；竖幅会自动两三张并排，比例仍然不被裁切。
  return 240;
}

/** 贪心分行：加到这一行的高度掉到目标以下，就断开 */
function partition(nodes, width, gap, target) {
  const rows = [];
  let row = [];
  let sum = 0;

  for (const node of nodes) {
    const ar = aspectOf(node);
    row.push({ node, ar });
    sum += ar;
    const height = (width - gap * (row.length - 1)) / sum;
    if (height <= target) {
      rows.push(row);
      row = [];
      sum = 0;
    }
  }
  if (row.length) rows.push(row);
  return rows;
}

export function layoutRows(container) {
  if (!container) return;

  const nodes = [...container.querySelectorAll('.shot')];
  if (!nodes.length) {
    container.replaceChildren();
    structure.delete(container);
    return;
  }

  const width = container.clientWidth;
  if (!width) return;

  const gap = gapOf(container);
  const target = targetHeight(width);
  const rows = partition(nodes, width, gap, target);

  // 最后一行：撑得太高就固定行宽，让这一行自然留白（Flickr 的老规矩）
  const lastIndex = rows.length - 1;
  const last = rows[lastIndex];
  const lastSum = last.reduce((total, item) => total + item.ar, 0);
  const lastHeight = (width - gap * (last.length - 1)) / lastSum;
  const looseWidth = lastHeight > target * LOOSE_LIMIT
    ? Math.round(target * lastSum + gap * (last.length - 1))
    : 0;

  const desired = rows.map((row) => row.length);
  const current = [...container.children].map((child) =>
    child.classList && child.classList.contains('jrow') ? child.children.length : -1,
  );
  const same =
    current.length === desired.length &&
    current.every((count, index) => count === desired[index]) &&
    structure.get(container) === looseWidth;

  if (same) {
    for (const { node, ar } of rows.flat()) node.style.setProperty('--ar', String(ar));
    return;
  }

  const fragment = document.createDocumentFragment();
  rows.forEach((row, index) => {
    const element = document.createElement('div');
    element.className = 'jrow';
    // 行只是排版手段，读屏时应当被忽略，让 .shot 直接归 #gallery-grid 管
    element.setAttribute('role', 'presentation');
    if (looseWidth && index === lastIndex) {
      element.style.setProperty('--row-w', `${looseWidth}px`);
    }
    for (const { node, ar } of row) {
      node.style.setProperty('--ar', String(ar));
      element.appendChild(node);
    }
    fragment.appendChild(element);
  });

  container.replaceChildren(fragment);
  structure.set(container, looseWidth);
}

/** 宽度变了才重排；只看宽度，避免「改高度 → 又触发」的循环 */
export function watchLayout(container) {
  let lastWidth = 0;

  const run = () => {
    const width = container.clientWidth;
    if (!width || width === lastWidth) return;
    lastWidth = width;
    layoutRows(container);
  };

  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(run).observe(container);
  } else {
    window.addEventListener('resize', run);
  }
  run();
}

/**
 * 图片解码后核对比例：对不上就改写 data-ar 并重排一次。
 * 每件作品只核对一次，避免和 resize 打架来回抖。
 */
export function watchRatios(container, onRelayout) {
  container.addEventListener(
    'load',
    (event) => {
      const image = event.target;
      if (!(image instanceof HTMLImageElement)) return;

      const shot = image.closest('.shot');
      if (!shot || shot.dataset.arChecked === '1') return;
      shot.dataset.arChecked = '1';

      const { naturalWidth, naturalHeight } = image;
      if (!naturalWidth || !naturalHeight) return;

      const real = clamp(naturalWidth / naturalHeight);
      const known = aspectOf(shot);
      if (Math.abs(real - known) / known <= 0.02) return;

      shot.dataset.ar = String(real);
      (onRelayout || layoutRows)(container);
    },
    true, // load 不冒泡，只能捕获
  );
}

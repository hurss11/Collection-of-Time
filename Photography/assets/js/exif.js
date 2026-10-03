/**
 * exif.js —— 零依赖的 JPEG EXIF 解析器
 *
 * 用法：
 *   import { parseExifFromArrayBuffer, readExifFromUrl, formatExif } from './exif.js';
 *   const raw = await readExifFromUrl('assets/img/photos/demo.jpg');
 *   const items = formatExif(raw);      // [{ key, label, value }, ...]
 *
 * 说明：仅解析 JPEG 的 APP1(Exif) 段中的常用字段，输出可直接展示的字符串。
 *       解析失败会抛出 Error，调用方需自行 try/catch。
 */

/** TIFF 标签 → 展示名称 */
const TAG_LABELS = {
  0x010f: ['Make', '相机品牌'],
  0x0110: ['Model', '相机型号'],
  0x0112: ['Orientation', '方向'],
  0x0132: ['DateTime', '修改时间'],
  0x9003: ['DateTimeOriginal', '拍摄时间'],
  0x829a: ['ExposureTime', '快门'],
  0x829d: ['FNumber', '光圈'],
  0x8827: ['ISO', 'ISO'],
  0x920a: ['FocalLength', '焦距'],
  0x9209: ['Flash', '闪光灯'],
  0xa002: ['PixelXDimension', '宽度'],
  0xa003: ['PixelYDimension', '高度'],
  0xa434: ['LensModel', '镜头'],
  0x9291: ['SubSecTimeOriginal', '亚秒'],
};

const TAG_MAKE = 0x010f;
const TAG_MODEL = 0x0110;
const TAG_EXIF_IFD = 0x8769;

/* ---------- 字节读取工具（依据字节序） ---------- */

function readUint16(view, offset, little) {
  return view.getUint16(offset, little);
}

function readUint32(view, offset, little) {
  return view.getUint32(offset, little);
}

/** 读取 ASCII 字符串（以 NUL 结尾） */
function readAscii(view, offset, count) {
  let out = '';
  for (let i = 0; i < count; i += 1) {
    const code = view.getUint8(offset + i);
    if (code === 0) break;
    out += String.fromCharCode(code);
  }
  return out.trim();
}

/** 读取一个数值（按 TIFF 数据类型） */
function readValue(view, type, count, valueOffset, little) {
  const sizes = { 1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8 };
  const size = sizes[type];
  if (!size) return null;
  const total = size * count;

  // 值 <= 4 字节时直接内联在 IFD 条目中
  const dataOffset = total <= 4 ? valueOffset : readUint32(view, valueOffset, little);

  switch (type) {
    case 2: // ASCII
      return readAscii(view, dataOffset, count);
    case 3: // SHORT
      return count === 1
        ? readUint16(view, dataOffset, little)
        : Array.from({ length: count }, (_, i) => readUint16(view, dataOffset + i * 2, little));
    case 4: // LONG
      return readUint32(view, dataOffset, little);
    case 5: // RATIONAL（分子/分母，各 4 字节）
    case 10: {
      const num = readUint32(view, dataOffset, little);
      const den = readUint32(view, dataOffset + 4, little);
      return den === 0 ? null : num / den;
    }
    case 1:
    case 7:
      return Array.from({ length: count }, (_, i) => view.getUint8(dataOffset + i));
    default:
      return null;
  }
}

/** 解析单个 IFD，返回 { tag: value } 映射 */
function readIfd(view, tiffStart, dirStart, little) {
  const entries = readUint16(view, dirStart, little);
  const out = {};
  const base = tiffStart + dirStart;

  for (let i = 0; i < entries; i += 1) {
    const entry = dirStart + 2 + i * 12;
    if (entry + 12 > view.byteLength) break;

    const tag = readUint16(view, entry, little);
    const type = readUint16(view, entry + 2, little);
    const count = readUint32(view, entry + 4, little);
    const valueOffset = entry + 8;

    // 指向其它 IFD 的指针先跳过（单独处理）
    if (tag === TAG_EXIF_IFD) {
      out[tag] = readUint32(view, valueOffset, little);
      continue;
    }

    try {
      out[tag] = readValue(view, type, count, valueOffset, little);
    } catch {
      /* 单个标签损坏时忽略即可 */
    }
  }

  void base;
  return out;
}

/* ---------- 值格式化 ---------- */

/** 快门速度：1/250 s 或 2.5 s */
function formatExposure(value) {
  if (typeof value !== 'number' || value <= 0) return null;
  if (value >= 1) return `${Number(value.toFixed(2))} s`;
  return `1/${Math.round(1 / value)} s`;
}

/** 光圈：f/2.8 */
function formatAperture(value) {
  return typeof value === 'number' ? `f/${Number(value.toFixed(1))}` : null;
}

/** EXIF 时间：2026:01:18 17:42:05 → 2026-01-18 17:42:05 */
function formatDateTime(value) {
  if (typeof value !== 'string') return null;
  const m = value.match(/^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}:\d{2}:\d{2})/);
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}` : value;
}

const CUSTOM_FORMATTERS = {
  [0x829a]: formatExposure,
  [0x829d]: formatAperture,
  0x9003: formatDateTime,
  0x0132: formatDateTime,
  0x920a: (v) => (typeof v === 'number' ? `${Number(v.toFixed(1))} mm` : null),
  0x8827: (v) => (Array.isArray(v) ? v[0] : v) != null ? `ISO ${Array.isArray(v) ? v[0] : v}` : null,
};

/**
 * 解析 JPEG 的 EXIF。
 * @param {ArrayBuffer} buffer
 * @returns {Record<number, unknown>} 原始标签映射
 */
export function parseExifFromArrayBuffer(buffer) {
  const view = new DataView(buffer);

  if (view.byteLength < 4 || view.getUint16(0) !== 0xffd8) {
    throw new Error('不是有效的 JPEG 文件（缺少 SOI 标记）');
  }

  // 1) 遍历标记段，定位 APP1 / Exif
  let offset = 2;
  let tiffStart = -1;

  while (offset + 4 <= view.byteLength) {
    if (view.getUint8(offset) !== 0xff) {
      offset += 1;
      continue;
    }

    const marker = view.getUint8(offset + 1);

    // SOS(0xDA) 之后即为压缩数据，不再有 EXIF
    if (marker === 0xda || marker === 0xd9) break;

    const size = view.getUint16(offset + 2);
    const segmentStart = offset + 4;

    if (marker === 0xe1) {
      const sig = readAscii(view, segmentStart, 6);
      if (sig.startsWith('Exif')) {
        tiffStart = segmentStart + 6;
        break;
      }
    }

    offset = segmentStart + size - 2;
  }

  if (tiffStart < 0) throw new Error('该图片不包含 EXIF 信息');

  // 2) TIFF 头：字节序 + 魔数 42
  const endianMark = view.getUint16(tiffStart);
  const little = endianMark === 0x4949; // 'II' = Intel 小端，'MM' = Motorola 大端
  if (!little && endianMark !== 0x4d4d) throw new Error('未知的 TIFF 字节序');

  if (view.getUint16(tiffStart + 2, little) !== 42) throw new Error('TIFF 头校验失败');

  // 3) IFD0 → Exif IFD
  const ifd0Offset = readUint32(view, tiffStart + 4, little);
  const ifd0 = readIfd(view, tiffStart, tiffStart + ifd0Offset, little);

  let exif = {};
  const exifPointer = ifd0[TAG_EXIF_IFD];
  if (typeof exifPointer === 'number') {
    exif = readIfd(view, tiffStart, tiffStart + exifPointer, little);
  }

  return { ...ifd0, ...exif };
}

/**
 * 从 URL 读取图片并解析 EXIF。
 * @param {string} url
 * @returns {Promise<Record<number, unknown>>}
 */
export async function readExifFromUrl(url) {
  const response = await fetch(url, { cache: 'force-cache' });
  if (!response.ok) throw new Error(`读取图片失败：HTTP ${response.status}`);
  const buffer = await response.arrayBuffer();
  return parseExifFromArrayBuffer(buffer);
}

/** 从 File / Blob 解析 EXIF（用于「上传后本地解析」场景） */
export async function readExifFromBlob(blob) {
  return parseExifFromArrayBuffer(await blob.arrayBuffer());
}

/**
 * 把原始标签映射转换为可渲染的列表。
 * @param {Record<number, unknown>|null|undefined} raw
 * @returns {{key: string, label: string, value: string}[]}
 */
export function formatExif(raw) {
  if (!raw) return [];

  return Object.entries(TAG_LABELS)
    .map(([tagStr, [key, label]]) => {
      const tag = Number(tagStr);
      const value = raw[tag];
      if (value === undefined || value === null || value === '') return null;

      const formatter = CUSTOM_FORMATTERS[tag];
      const formatted = formatter ? formatter(value) : String(value);
      if (!formatted) return null;

      return { key, label, value: formatted };
    })
    .filter(Boolean);
}

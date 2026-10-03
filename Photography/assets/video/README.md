# assets/video —— 视频存放目录

```
assets/video/
├── landscape-sunrise.mp4      # 网页播放用的压缩版本（H.264 / H.265，建议 ≤ 20MB）
├── star-trails-timelapse.mp4
├── posters/                   # 封面图（由 tools/make_posters.py 生成）
│   ├── v-001.svg
│   └── landscape-sunrise.jpg
├── originals/                 # 原始素材，已在 .gitignore 中忽略
└── raw/                       # 未压缩导出，已在 .gitignore 中忽略
```

## 推荐导出参数

| 项目 | 建议值 |
| --- | --- |
| 容器 / 编码 | MP4 + H.264（兼容性最好）或 H.265（体积更小） |
| 分辨率 | 最长边 1920 或 3840 |
| 码率 | 1080p 约 8 Mbps；4K 约 30 Mbps |
| 音频 | AAC 128–192 kbps；纯延时可选无音轨 |
| 快速起播 | `-movflags +faststart`，把索引放到文件头部 |

```bash
ffmpeg -i original.mov -c:v libx264 -crf 20 -preset slow \
       -vf "scale=-2:1080" -movflags +faststart -c:a aac -b:a 160k output.mp4
```

## 封面

两种方式，二选一即可：

1. **预生成（推荐）**：`python tools/make_posters.py`，用 ffmpeg 抓帧写入 `posters/`，
   再把 `poster` 字段指向生成的 jpg。加载最快、不依赖浏览器解码。
2. **运行时抓帧**：`poster` 留空，`assets/js/poster.js` 会在卡片进入视口时
   把视频定位到 `posterTime` 秒并显示该帧。

> 外链嵌入的视频（YouTube / 哔哩哔哩等）**必须**提供 `poster`，
> 因为浏览器不允许跨域读取 iframe 内的画面。

## 大文件与版本控制

- 单个视频超过 50MB 时建议启用 Git LFS：
  `git lfs install && git lfs track "assets/video/*.mp4"`
- 原始素材请放在 `originals/` 或 `raw/`，这两个目录已被 `.gitignore` 忽略。

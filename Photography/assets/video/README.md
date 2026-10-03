# assets/video —— 视频存放目录

```
assets/video/
├── landscape-sunrise.mp4      # 占位样片（1280×720 / 4 秒 / H.264，见下方说明）
├── star-trails-timelapse.mp4  # 占位样片
├── posters/                   # 封面图
│   ├── v-001.svg
│   └── landscape-sunrise.jpg
├── originals/                 # 原始素材，已在 .gitignore 中忽略
└── raw/                       # 未压缩导出，已在 .gitignore 中忽略
```

> **两个 mp4 是占位样片**：为了让示例数据开箱即可播放，仓库里放了 4 秒的测试图案片段
> （`ffmpeg -f lavfi -i testsrc ...` 生成，各 7–33KB），不是真实作品。
> 换成自己的成片时：替换同名文件，并把 `data/videos.json` 里的 `duration` / `resolution`
> / `exif.fps` 改成实际值（页面上的「视频参数」直接读这些字段）。
> 建议码率与导出参数见下表。

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

封面统一由**后端**生成或接收上传（前端不做客户端抓帧）：

1. **上传时自动抓帧**：后台在上传本地视频时用 ffmpeg 抓取**第 0 秒（第一帧）**
   写入 `posters/`，并把 `poster` / `posterTime` 一起写进 `data/videos.json`。
2. **上传时指定封面**：上传面板的「视频封面（可选）」选一张图片，本批视频就用它，
   优先于自动抓帧。
3. **事后更换**：视频列表每行的「封面」按钮 → 上传图片，或填秒数后「抓取该帧」。
   同一个视频只保留一张封面（`posters/<id>.<ext>`），换新会自动清掉旧文件。
4. **批量补封面**：`python tools/make_posters.py`。

`poster` 留空的本地视频在前台只显示占位块，并会在后台「数据完整性」里被列出来。

> 外链嵌入的视频（YouTube / 哔哩哔哩等）**必须**提供 `poster`，
> 因为浏览器不允许跨域读取 iframe 内的画面，后端也无法抓帧——
> 请用「封面」按钮上传一张图片。

## 大文件与版本控制

- 单个视频超过 50MB 时建议启用 Git LFS：
  `git lfs install && git lfs track "assets/video/*.mp4"`
- 原始素材请放在 `originals/` 或 `raw/`，这两个目录已被 `.gitignore` 忽略。

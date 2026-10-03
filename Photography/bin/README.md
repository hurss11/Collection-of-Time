# bin/ —— 项目内置的可选二进制

放在这里的可执行文件会被后台**优先使用**（查找顺序：环境变量 → `bin/` → 系统 `PATH`）。

```
bin/
├── ffmpeg        # Linux / macOS
├── ffprobe
├── ffmpeg.exe    # Windows
└── ffprobe.exe
```

## 为什么要放这里

服务器上不需要再 `apt install ffmpeg`——把整个项目（含 `bin/`）打包上传即可，
后台会自动用这一份，缩略图与视频封面功能开箱可用。

## 怎么放

```bash
python tools/fetch_ffmpeg.py          # 自动识别平台，下载静态构建到 bin/
python tools/fetch_ffmpeg.py --check  # 只看当前用了哪个 ffmpeg
./run.sh install-ffmpeg               # 等价的一键命令
```

离线或内网环境：自己下载好压缩包，然后本地安装，不联网：

```bash
python tools/fetch_ffmpeg.py --file ffmpeg-release-amd64-static.tar.xz
python tools/fetch_ffmpeg.py --url http://内网镜像/ffmpeg.tar.xz --sha256 <摘要>
```

也可以直接把单个可执行文件拷进来（记得 `chmod +x ffmpeg ffprobe`）。

## 注意

- **不要把二进制提交进版本库**：体积大且与平台绑定，`.gitignore` 已忽略 `bin/*`（保留本文件）。
- 需要跟着项目一起打包时，直接在服务器上执行一次 `./run.sh install-ffmpeg`，
  或者用 `rsync` / `scp` 把本地的 `bin/` 一起传上去。
- `tools/fetch_ffmpeg.py` 默认从第三方静态构建源下载（johnvansickle / BtbN / evermeet），
  生产环境建议自行准备二进制并用 `--file` 安装，或用 `--sha256` 校验摘要。

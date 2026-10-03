# 部署到服务器

本文档随迁移包一起分发，描述**从零把站点跑起来**的完整步骤，以及后续升级、备份、排查的做法。

想了解项目本身，请看 `README.md`（架构、API、数据格式、安全模型）。

---

## 0. 前置条件

| 项目 | 要求 | 说明 |
| --- | --- | --- |
| 系统 | Linux x86_64 | 包内自带 `linux-x64` 的 FFmpeg；其它架构见下方「换 FFmpeg 平台」 |
| Python | **3.9+**（推荐 3.11+） | 只用标准库，**不需要 pip install 任何东西** |
| 权限 | 普通用户即可 | 装 systemd 常驻服务需要 `sudo` |
| 依赖 | 无 | 不依赖 nginx / node / 数据库 |

检查 Python：

```bash
python3 --version        # 需要 3.9 以上
```

没有的话装一个：

```bash
sudo apt update && sudo apt install -y python3        # Debian / Ubuntu
sudo dnf install -y python3                            # Fedora / RHEL
```

---

## 1. 上传

在**本地机器**上（把 `user@服务器` 换成你的实际登录信息）：

```bash
scp photography-linux-x64-*.tar.gz photography-linux-x64-*.tar.gz.sha256 user@服务器:/tmp/
```

文件大（含 FFmpeg，约 40MB）时 rsync 支持断点续传，更稳：

```bash
rsync -avP photography-linux-x64-*.tar.gz* user@服务器:/tmp/
```

---

## 2. 解压

```bash
cd /tmp
sha256sum -c photography-linux-x64-*.tar.gz.sha256      # 校验完整性（可选但推荐）
sudo mkdir -p /opt
sudo tar -xzf photography-linux-x64-*.tar.gz -C /opt
cd /opt/photography-linux-x64-*
sudo chown -R "$USER" .          # 让当前用户能写 data/ 与 assets/
chmod +x run.sh                  # 若经 Windows 中转，可执行位可能丢
cat PACKAGE-INFO.txt             # 看看包里有什么、数据多少条
```

解压后应当看到：

```
admin.py  run.sh  serve.py  index.html  DEPLOY.md  PACKAGE-INFO.txt
admin/  adminlib/  assets/  bin/  data/  tools/
```

---

## 3. 自检

```bash
./run.sh doctor
```

会检查：Python 版本、必需文件是否齐全、目录写权限、三份 JSON 是否合法、
FFmpeg 来源与版本、管理员账号、端口是否被占用。

**全绿再继续。** 有问题它会直接指出来。

---

## 4. 建号并启动

```bash
./run.sh setup
```

三步走：自检 → 确认 FFmpeg → 建管理员账号 → 启动服务。

首次运行会提示输入用户名与密码；非交互环境（脚本、容器）用环境变量：

```bash
ADMIN_USERNAME=me ADMIN_PASSWORD='你的强密码' ./run.sh setup
```

> 密码要求至少 8 位。账号信息写入 `admin.config.json`（权限 0600，**不要提交到版本库**）。
> 这个文件同时保存会话签名密钥，换服务器后重新建号即可，无需复制。

启动成功会打印管理后台地址、日志位置、停止方式，以及 FFmpeg 的当前状态。

常用命令：

```bash
./run.sh status          # 运行状态、运行时长、数据条数
./run.sh logs -f         # 持续跟踪日志
./run.sh restart         # 重启
./run.sh stop            # 停止
```

---

## 5. 访问

服务**默认只监听 `127.0.0.1`**，公网访问不到。这是刻意的：后台能改文件，不该直接暴露。

**推荐：SSH 隧道**（不占公网端口、不用配 HTTPS）

```bash
# 在本地电脑上执行
ssh -N -L 8080:127.0.0.1:8080 user@服务器
```

然后浏览器打开 `http://127.0.0.1:8080/admin/`。

想看站点前台是 `http://127.0.0.1:8080/`。

**备选：对外监听 + HTTPS 反代**

```bash
./run.sh start --host 0.0.0.0 --port 8080
```

必须放在 HTTPS 反向代理之后，否则会话 Cookie 与密码会以明文传输：

```nginx
location /admin/ { proxy_pass http://127.0.0.1:8080; proxy_set_header Host $host; }
location /api/   { proxy_pass http://127.0.0.1:8080; proxy_set_header Host $host; }
location /       { root /opt/photography-linux-x64-XXXX; }
```

反代会自动识别 `X-Forwarded-Proto: https` 给 Cookie 加 `Secure`，所以记得转发这个头：

```nginx
proxy_set_header X-Forwarded-Proto $scheme;
```

---

## 6. 常驻运行（systemd）

```bash
sudo ./run.sh systemd --install
```

会写好 `/etc/systemd/system/photography-admin.service` 并立即启用（开机自启 + 崩溃自动重启）。

```bash
systemctl status photography-admin
journalctl -u photography-admin -f
sudo systemctl restart photography-admin
```

不想用 systemd 也可以 `./run.sh start`，它会 `nohup` 到后台，PID 存在 `.run/admin.pid`。

---

## 7. 换 FFmpeg 平台 / 补装 FFmpeg

包内自带的是 `linux-x64` 的静态构建。如果服务器架构不同，或包里没带：

```bash
./run.sh ffmpeg-status                    # 先看现在用的是哪个
./run.sh install-ffmpeg                   # 自动识别本机平台并下载到 bin/
./run.sh install-ffmpeg --platform linux-arm64
```

没有外网时，在本地下载好压缩包再传过去：

```bash
# 本地
python tools/fetch_ffmpeg.py --platform linux-arm64 --keep-archive
# 传 bin/ffmpeg-*.tar.xz 到服务器，然后
./run.sh install-ffmpeg --file /tmp/ffmpeg-release-arm64-static.tar.xz
```

系统里本来就装了 ffmpeg（`apt install ffmpeg`）的话**什么都不用做**，
程序会按 `环境变量 → bin/ → PATH` 的顺序自动找到它。

---

## 8. 升级

**代码与数据是分开的**，升级只替换代码，不要动 `data/` 和 `assets/`。

```bash
./run.sh stop
cp -a data data.bak.$(date +%Y%m%d)          # 保险起见先备份数据
# 用新包覆盖代码文件（保留 data/、assets/、admin.config.json）
rsync -av --exclude data/ --exclude assets/ --exclude admin.config.json \
      /tmp/新包目录/ ./
./run.sh doctor && ./run.sh start
```

> 后台本身在每次保存前都会自动备份到 `data/.backups/`（保留最近 40 份），
> 保存用的是「写临时文件 + 原子替换」，进程被 kill 也不会留下半截 JSON。
> 改动想撤销，在后台「备份」页点一下恢复即可。

---

## 9. 备份

要备份的就三样：`data/`、`assets/`、`admin.config.json`。

```bash
tar -czf ~/photography-backup-$(date +%Y%m%d).tar.gz \
    data assets admin.config.json
```

媒体文件会越来越大，之后建议用 rsync 增量同步到另一台机器或对象存储。

---

## 10. 排查

| 现象 | 原因与处理 |
| --- | --- |
| 打不开页面 | `./run.sh status` 看是否在运行；`./run.sh logs` 看日志；确认隧道还开着 |
| `Permission denied` 启动不了 | `chmod +x run.sh`；确认对项目目录有写权限（`data/` 要能写） |
| 端口被占用 | `./run.sh start --port 8081`，或按提示释放占用进程 |
| 登录后马上掉线 | 会话密钥文件被删或权限不对；`ls -l admin.config.json` 应为 `600` |
| 忘了密码 | `./run.sh reset-password`（会踢掉所有已登录设备） |
| 上传图片没缩略图 | FFmpeg 未就绪，看 `./run.sh ffmpeg-status`；不影响上传本身 |
| FFmpeg 报架构不对 | 见上面第 7 节，重新装对应平台的构建 |
| 数据文件损坏 | 从 `data/.backups/` 里挑一份恢复，或后台「备份」页回滚 |
| 站点资源 404 但后台能用 | 从**项目目录**里启动（`cd` 进项目再 `./run.sh start`） |

排查时先跑一次 `./run.sh doctor`，多数问题它会直接点名。

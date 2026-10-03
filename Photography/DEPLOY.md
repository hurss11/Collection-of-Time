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

> 反代不要剥离后端的安全响应头（`Content-Security-Policy`、`X-Frame-Options`、
> `X-Content-Type-Options`、`Referrer-Policy`）。CSP 里脚本与样式都只允许同源外部文件——
> 前端不含内联脚本 / 样式，因此不需要放开 `unsafe-inline`；覆盖或追加 CSP 时请保留这条约束。

### 5.1 一条命令配好 HTTPS（推荐）

```bash
sudo ./run.sh https --domain photos.example.com --email you@example.com
```

内部按这个顺序做，任何一步失败都会停下并保留/回滚原配置：

| 步骤 | 做什么 | 失败时会怎样 |
|------|--------|--------------|
| 1 | 预检：域名、后端端口、nginx 是否就绪 | 缺 nginx 直接告诉你怎么装 |
| 2 | 装上「只有 80 端口」的配置（含 ACME 校验目录） | `nginx -t` 不过就回滚，不 reload |
| 3 | `certbot certonly --webroot` 签发证书 | 报出常见原因（DNS、80 被占） |
| 4 | 换成「80 跳转 + 443 反代」完整配置 | 同上，回滚到上一步的可用配置 |
| 5 | 跑 `tools/check_https.py` 做线上验收 | 有失败项会列出排查方向 |

不加 `sudo` 也能跑，脚本会自动加 `sudo`；`--dry-run` 只打印将要执行的命令与配置内容，不动任何东西。

生成的配置里已经包含：

- `X-Forwarded-Proto` / `X-Forwarded-For` 转发（前者决定 Cookie 是否带 `Secure`，后者决定
  后台登录限流按**真实客户端 IP** 计数）；
- **gzip**（首屏基本是 JS/CSS/JSON，压缩后约为原来三成）；
- **限流**：`/api/public/*` 每 IP 20 次/秒（burst 40）、`/api/auth/login` 1 次/秒（burst 5），
  都返回 429；
- `client_max_body_size 512m` 与关闭 `proxy_request_buffering`，避免大视频上传被截断；
- `/api/` 与 `/admin/` 独立代理（所以后面切静态托管时后台不会跟着失效）；
- 源码 / 配置文件的 404 兜底与 dotfile 拦截。

实测（真 nginx 1.24 + 真后端，`scripts` 之外的临时环境）：
首页/HSTS/CSP 透传、静态 `public, no-cache`、gzip、Cookie `Secure`、80→308、
`/api/public/` 连打 80 次出现 429 —— 15 项全通过。

> **并发提示**：内置的 Python 静态服务在几十人同时浏览时开始劣化（瓶颈是线程与 GIL）。
> 若要对外放开，让 nginx 直接托管前台静态、Python 只跑 API 与 `/admin/`：
>
> ```bash
> sudo ./run.sh https --domain photos.example.com --email you@example.com --static
> ./run.sh start --host 127.0.0.1 --no-static      # 后端只跑 API 与 /admin/
> ```
>
> `--static` 会把 `location /` 与 `/assets/` 换成 nginx 直接读磁盘（`expires -1`：每次都重验证，
> 命中 304 不传内容），并**补上 CSP、HSTS 等安全响应头**——注意 nginx 的 `add_header`
> 不与上层合并，location 里一旦写了 `add_header`，server 段的 HSTS 就失效，所以那里重复写了一遍。
> 同样实测 15 项全通过（首页 200 + HSTS + CSP、`/assets/` 由 nginx 直接回且带 ETag、
> `/api/*` 与 `/admin/` 仍走代理、限流生效）。

### 5.2 上线自检清单（P1：明文 HTTP 下 Cookie 不 Secure）

```bash
./run.sh https --check-only --domain photos.example.com     # 或：
python tools/check_https.py https://photos.example.com
```

逐项确认（`--username/--password` 可带上账号，连会话 Cookie 一起验）：

- [ ] `http://` 会 308 跳到 `https://`（否则密码仍可能走明文）
- [ ] 证书链可信、SAN 覆盖该域名、剩余有效期 > 14 天
- [ ] `Content-Security-Policy` / `X-Frame-Options` / `X-Content-Type-Options` / `Referrer-Policy` 都在（反代没剥掉）
- [ ] `HSTS` 已在「确认全站 HTTPS」之后开启
- [ ] **`Set-Cookie` 带 `Secure`**（漏配 `X-Forwarded-Proto` 时这里会失败，这正是 P1 的验收点）
- [ ] 会话 Cookie 带 `HttpOnly` + `SameSite`
- [ ] `/data/`、`/assets/img/` 返回 403（目录列表关闭）
- [ ] `/admin.config.json`、`/admin.py`、`/adminlib/query.py` 返回 404（源码与配置不可下载）
- [ ] `/api/state` 未登录返回 401（后台接口没被公开）
- [ ] 静态资源回 `Cache-Control: public, no-cache`（或 nginx 的 `no-cache`）且带
      `Last-Modified` / `ETag`（刷新应命中 304，而不是重下整包）
- [ ] `/api/**` 回 `no-store`（接口响应不落缓存）
- [ ] 连打 `/api/public/site` 60 次会出现 `429`（反代层限流生效）
- [ ] 连打写接口（例如 `POST /api/items/photos`）20 次以上会出现 `429`，而同一时刻
      连打 `GET /api/items/photos` 40 次**一个都不该**被限（写限流只按写方法计数）
- [ ] 发一个超过 `client_max_body_size` 的请求体会在**边缘**收到 413，且响应体是
      `{"ok":false,"error":…}` 这样的 JSON（把后端 `--no-auth` 日志或 nginx `access.log`
      对照一下，超限请求不应出现在后端）
- [ ] `/assets/js/app.js` 回 `Content-Encoding: gzip`（Python 3.12+ 下 `.js` 是
      `text/javascript`，`gzip_types` 漏了它就等于没压缩）
- [ ] 视频能**拖动进度条**：`curl -r 0-99 -D- https://<域名>/assets/video/<你的视频>.mp4`
      应回 `206` + `Content-Range` + `Accept-Ranges: bytes`
      （只回 200 就说明这一层不支持 Range，播放器会没有进度条）
- [ ] `README.md` 里 `assets/video/*.mp4` 两个占位样片存在（示例卡片不会点开就报错）

自检退出码：`0` 全通过、`1` 有失败项、`2` 参数或网络错误，方便放进 CI / 上线脚本里当门禁。

### 5.3 加固上线：照着做（含每步验证）

> 场景：服务已经在对公网跑（例如 `http://<公网IP>:8080/`），现在要收口成
> 「只有 80/443 对外、全站 HTTPS、静态交给 nginx」。全程约 15 分钟，建议先开一个
> 备用 SSH 会话，改到一半卡住时方便回滚。
>
> 下文 `<IP>` 换成你的公网 IP，`photos.example.com` 换成实际域名。

**第 0 步 · 备份 + 记下现状**（可回滚的前提）

```bash
cd /opt/photography-XXXX          # 解压后的项目目录
cp -a data /root/photography-data-backup-$(date +%F)      # 数据
cp -a admin.config.json /root/                             # 账号与会话密钥
ss -ltnp | grep -E ':(80|443|8080)\b'                      # 记下现在谁在监听
curl -sI http://127.0.0.1:8080/ | head -3                  # 记下当前版本行为
```

验证：上面两条命令都有输出，备份目录里能看到 `albums.json photos.json videos.json`。

**第 1 步 · 部署新构建**（旧构建仍把所有静态资源设成 `no-store`，示例视频也缺文件）

```bash
git pull                                  # 或上传并解压新的迁移包
./run.sh doctor                           # 环境自检：Python / FFmpeg / 权限 / 端口
```

验证：

```bash
curl -sI http://127.0.0.1:8080/assets/css/main.css | grep -i cache-control
ls -l assets/video/*.mp4                  # 应有 landscape-sunrise.mp4 与 star-trails-timelapse.mp4
```

预期：`Cache-Control: public, no-cache`；两个占位 mp4 存在（各几十 KB）。
若还是 `no-store` 或没有 mp4，说明跑的是旧代码。

**第 2 步 · 让后端只监听本机**（公网 8080 的口子先关掉）

```bash
./run.sh restart --host 127.0.0.1
ss -ltnp | grep 8080                      # 应显示 127.0.0.1:8080，而不是 0.0.0.0:8080
```

验证（**从你自己的电脑**，不是服务器上）：

```bash
curl -m 5 -v http://<IP>:8080/            # 预期：连接超时 / 拒绝
```

还要在云厂商控制台把**安全组**里的 8080 入方向规则删掉（阿里云/腾讯云默认放行什么就删什么），
只保留 80、443 与你的 SSH 端口。

**第 3 步 · 装 nginx 与 certbot，一条命令配好 HTTPS**

```bash
sudo apt-get update && sudo apt-get install -y nginx certbot
sudo ./run.sh https --domain photos.example.com --email you@example.com --dry-run   # 先看要做什么
sudo ./run.sh https --domain photos.example.com --email you@example.com             # 真跑
```

脚本会：装「只有 80 端口」的配置 → `certbot` 用 webroot 签发 → 换成「80 跳转 + 443 反代」
（带 `X-Forwarded-Proto` / `X-Forwarded-For` / gzip / 512MB 上传上限 / 源码 404 兜底）。
`nginx -t` 不过就自动回滚，不 reload。

验证（脚本最后会自动跑一次，也可以单独再跑）：

```bash
./run.sh https --check-only --domain photos.example.com
```

预期：`失败 0 项`（HTTPS 刚配好时 `HSTS`/`跳转` 那两项此时也应为通过）。

**第 4 步 · 并发：静态交给 nginx**（>50 人同时浏览时才会明显）

这两件事要**一起做**——只让后端 `--no-static`、却没让 nginx 接管 `/` 与 `/assets/`，
前台会直接 404。

```bash
sudo ./run.sh https --domain photos.example.com --email you@example.com --static   # 生成静态托管版配置
./run.sh restart --host 127.0.0.1 --no-static     # 后端只跑 API 与 /admin/
```

（RHEL / CentOS 是 `/etc/nginx/conf.d/photography.conf`。）

验证：

```bash
curl -sI https://photos.example.com/assets/css/main.css | grep -iE 'server|etag|cache-control'
curl -sI https://photos.example.com/ | grep -iE 'server|strict-transport|content-security'
curl -s  https://photos.example.com/api/public/site | head -c 60       # API 仍有数据
curl -sI https://photos.example.com/admin/ | grep -iE 'server|content-security'
```

预期：`/` 与 `/assets/**` 由 nginx 直接回（`ETag`、`Cache-Control: no-cache`，
且 **HSTS 与 CSP 都在**）；`/api/**` 与 `/admin/` 仍是后端在回。

顺手验证限流（`/api/public/` 每 IP 20 次/秒）：

```bash
for i in $(seq 1 60); do curl -s -o /dev/null -w '%{http_code}\n' https://photos.example.com/api/public/site; done \
  | sort | uniq -c
```

预期：出现一批 `429`（突发超过 burst 40 之后）。想看并发极限可以用 `ab` 或 `hey` 压同一 URL，
对比第 4 步前后。

**第 5 步 · 确认限流用的是真实访客 IP**

```bash
journalctl -u photography-admin -n 20 --no-pager | tail -5
```

用浏览器故意输错密码 5 次，再看日志与响应：

- 日志里应显示**你自己的公网 IP**（不是 `127.0.0.1`，也不是别人被连坐）；
- 第 5 次返回 `429`，并提示还需等待多少秒。

> 这条在反代下最关键：旧版本会把所有人算成同一个 IP，一个攻击者试错 5 次就能把管理员锁在门外。
> 现在只有「对端是本机/内网」时才采信 `X-Forwarded-For`，公网直连伪造该头无效。

**第 6 步 · 收尾自检**（对着 5.2 的清单逐条勾）

```bash
./run.sh https --check-only --domain photos.example.com --username admin --password '你的密码'
```

预期：`失败 0 项`，其中「登录并检查会话 Cookie」应显示 `Secure=True HttpOnly=True SameSite=Strict`。
最后把该 IP 的锁定等 5 分钟自动解除（上一步的试错会锁 300 秒）。

**出问题怎么回滚**

```bash
ls /etc/nginx/sites-available/photography.conf.bak.*      # 脚本每次改配置前的备份
sudo cp /etc/nginx/sites-available/photography.conf.bak.<时间戳> /etc/nginx/sites-available/photography.conf
sudo nginx -t && sudo systemctl reload nginx
./run.sh restart --host 127.0.0.1                         # 若第 4 步把 --no-static 也加上了，去掉它
sudo systemctl stop nginx                                 # 临时完全退回「直连后端」
./run.sh restart --host 0.0.0.0                           # 并从云安全组临时放行 8080（记得改完再关）
```

> **注意**：作品集前台的搜索 / 筛选 / 排序与 EXIF 解析现在都由后端提供
> （`/api/public/*`），上面的 `location /api/` 规则已经把它们一并代理过去。
> 如果只把静态目录裸露出去（不经后端），前台会拿不到数据——
> 要么保留 `/api/` 反代，要么直接用 `admin.py`（或 `serve.py`）托管整个站点。

---

## 6. 常驻运行（systemd）

```bash
sudo ./run.sh systemd --install
```

会写好 `/etc/systemd/system/photography-admin.service` 并立即启用（开机自启 + 崩溃自动重启）。

生成的单元文件**带内存约束**（`MemoryHigh` / `MemoryMax` / `MemorySwapMax=0` / `PrivateTmp`）：
小内存机器上最怕的不是服务崩，而是它把整机拖进 OOM——没有约束时内核会挑内存占用最大的
进程杀，可能顺手杀掉同机的 nginx / 网关 / 数据库。加上 `MemoryMax` 之后，最坏情况只是
这个服务自己重启一次（`Restart=on-failure`）。

```bash
# 默认 512M；机器宽裕或要经常导入大备份可以放宽
MEMORY_MAX=1G sudo ./run.sh systemd --install
python3 admin.py --print-systemd --memory-max 256M | sudo tee /etc/systemd/system/photography-admin.service
```

```bash
systemctl status photography-admin
journalctl -u photography-admin -f
sudo systemctl restart photography-admin
```

> **从旧版本升级过来的必做一步**：老单元文件里没有内存约束。升级代码后重新生成一次单元文件
> 并重载，否则这次的内存加固只在代码层生效、没有兜底：
>
> ```bash
> git pull                      # 或解压新的发布包
> MEMORY_MAX=512M sudo ./run.sh systemd --install
> systemctl show photography-admin -p MemoryMax    # 应输出 MemoryMax=536870912
> ```

不想用 systemd 也可以 `./run.sh start`，它会 `nohup` 到后台，PID 存在 `.run/admin.pid`。

> 用 `./run.sh start` + `nohup` 时没有 cgroup 约束，建议同时开一个 1~2GB 的 swap，
> 或者在 `systemd-run` 里跑：`systemd-run --scope -p MemoryMax=512M ./run.sh start`。

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

### 8.1 手动升级（迁移包）

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

### 8.2 自动热更新（推送完就自动生效）

想让「本地 `git push` → 服务器自己换代码并重启」，需要两件事：部署目录是 **git 检出**，
以及一个定时器。

**第一步：接管成 git 检出**（用迁移包解压出来的目录没有 `.git`，只做这一次）

```bash
cd /opt/photography-linux-x64-xxxx        # 部署目录
./run.sh autoupdate adopt --repo https://github.com/hurss11/Collection-of-Time.git
```

它会 `git init` → `remote add` → `fetch` → 把**代码**对齐到远端 `main`，并把代码路径之外的
19 类跟踪文件（`data/*.json`、`assets/video/*`、`assets/img/*` 等）标记成「本地状态」。
`data/`、`assets/` 里的上传内容、`admin.config.json`、`bin/`、`.run/` 一个字节都不会动。
执行前会打印将要做什么并要求确认（`--yes` 可跳过）。

**第二步：打开定时自动更新**

```bash
sudo ./run.sh autoupdate on                 # 每 5 分钟检查一次
sudo ./run.sh autoupdate on --interval 15min
./run.sh autoupdate status                  # 落后几个提交 / 定时器在不在
./run.sh update                             # 立刻更新一次，不等定时器
```

之后本地 `git push` 到 `main`，服务器 5 分钟内自己换代码、重启并做健康检查。

**它到底做了什么**（每一步都有意为之，别改成 `git pull`）：

| 环节 | 做法 | 为什么 |
| --- | --- | --- |
| 更新范围 | 只替换 `CODE_PATHS`（`admin.py`、`adminlib/`、`admin/`、`assets/css|js`、`tools/`、`run.sh`、`index.html`、文档等） | `data/*.json` 与 `assets/` 里是你的真实内容：`git pull` / `git reset --hard` 会把它们换成仓库里的演示数据，等于数据丢失 |
| 合并方式 | `git fetch` + **只快进**；本地有提交（分叉）直接拒绝 | 服务器上不该出现自动 merge / rebase 出来的意外提交 |
| 作者 | 新提交的作者必须在 `.run/autoupdate.authors` 里（首次安装时按当时的 `main` 作者快照生成，之后只增不减） | 仓库被推到不认识的作者时停下来等你确认 |
| 远端 | 地址与安装时记录的一致才继续 | 防误配 / 被换源 |
| 时机 | 正在上传时**推迟**到下一轮（服务端写 `.run/activity.json`） | 上传中途重启会在页面上留下指向不存在文件的卡片（曾经就这样一路 404） |
| 结果 | 重启后轮询 `/api/health`，不通就**自动回滚**到上一个版本并重新拉起 | 坏版本不会把站点留在打不开的状态 |
| 记录 | 已应用版本在 `.run/autoupdate.state`；完整输出在 `journalctl -u photography-update` | 出问题知道该回哪一版 |

**换机器 / 换了提交邮箱被拦下**时（提示「有未受信任的作者」）：

```bash
git log -1 --format=%ae          # 确认是你自己的邮箱
./run.sh update --accept-authors # 显式把这个作者加入白名单
```

**其它情况**：

- 服务器上有人手改过代码，更新会拒绝覆盖，提示「代码文件被本地改过」；
  确认要放弃那些改动就用 `./run.sh update --force`。
- 关掉自动更新：`sudo ./run.sh autoupdate off`（只删定时器，代码与数据都不动）。
- 没有 systemd 的机器（容器 / BSD / macOS）改用 cron：

  ```bash
  (crontab -l 2>/dev/null; echo "*/5 * * * * cd /opt/photography && ./run.sh update --quiet >> .run/update.log 2>&1") | crontab -
  ```

> **安全边界（务必读）**：能往 `main` 推代码的人，等于能在这台服务器上执行代码。
> 作者白名单、只快进、只换代码路径防的是「误配 / 意外覆盖 / 第三方扫到公开仓库」，
> **防不了仓库本身被入侵**。介意的话就别开定时器，改成每次手动 `./run.sh update`
> （同样只换代码、同样做健康检查与自动回滚）。
>
> 自动更新**不会**碰 `/etc/systemd/system/`：发布里如果改了单元模板（比如内存约束），
> 更新后需要重装一次：`MEMORY_MAX=512M sudo ./run.sh systemd --install`。

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
| 服务被 OOM 杀掉 / 整机卡死 | 见下面「内存与 OOM」 |
| 上传卡在 100% 不起作用 | 服务端在处理（读 EXIF / 生成缩略图与封面），大文件会花几秒；`journalctl -u photography-admin -f` 能看到耗时 |
| 日志里成片的 400 / 414 / 505 | 扫描器发的畸形请求。现版本正常回 4xx 且只记一行访问日志；若仍伴随 traceback，说明代码是旧的（`grep -n def\ request_path admin.py` 无输出就该更新） |
| 日志里 `GET /assets/... 404` | 上传途中有过一次服务重启，页面还引用着中断那次的旧文件名：强制刷新（手机端清站点数据）即可。先确认 `data/*.json` 里确实没有这条引用，再判断为前端残留 |
| `./run.sh update` 说「不是 git 检出」 | 迁移包解压的部署没有 `.git`：先 `./run.sh autoupdate adopt --repo <仓库地址>`（见第 8.2 节） |
| 更新提示「有未受信任的作者」 | 提交邮箱不在白名单里：`git log -1 --format=%ae` 确认是自己，然后 `./run.sh update --accept-authors` |
| 更新一直「推迟」 | 有上传正在进行（正常行为，下一轮会重试）；`./run.sh autoupdate status` 会显示。急的话 `./run.sh update --force` |
| 更新后服务起不来 | 已自动回滚并重新拉起，`journalctl -u photography-update -n 80` 看这次发布的问题；服务日志 `journalctl -u photography-admin -n 80` |

排查时先跑一次 `./run.sh doctor`，多数问题它会直接点名。

### 内存与 OOM

`dmesg -T | grep -i "out of memory"` 或 `journalctl -k | grep -i oom` 能确认是不是内核 OOM。
典型特征：`Out of memory: Killed process ... (python3)`，`task_memcg=/system.slice/photography-admin.service`，
`anon-rss` 几百 MB 到几 GB。

现版本（含流式上传）的占用与上传体积无关：正常情况下 RSS 只有 30～60MB，上传 300MB
文件也不会涨。所以一旦又出现几百 MB 的 RSS，按顺序查：

1. **单元文件是不是旧的**（老版本没有内存约束）：
   `systemctl show photography-admin -p MemoryMax` → 空或 `infinity` 就是旧的，按第 6 节重新生成；
2. **是不是旧代码**：`python3 admin.py --help | grep memory-max` 有输出说明是新版本
   （`adminlib/multipart.py` 是否存在可以直接判断）；
3. `journalctl -u photography-admin --since "-30min" | grep -E "POST|413|上游"` 看崩溃前后有没有
   超大请求；nginx 侧的 `access.log` 里 `$request_length` 更直观：
   `awk '{print $1, $4, $7, $10}' access.log | sort -k4 -n | tail`；
4. 用 `./run.sh start`（`nohup` 方式）跑时没有 cgroup 兜底，建议加 swap，
   或改用 `systemd-run --scope -p MemoryMax=512M ./run.sh start`。

要临时观察：`while sleep 1; do grep VmRSS /proc/$(cat .run/admin.pid)/status; done`。

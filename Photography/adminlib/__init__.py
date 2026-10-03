# -*- coding: utf-8 -*-
"""adminlib —— Photography 后台的服务端支撑模块。

只依赖 Python 标准库，便于直接部署到服务器（无需 pip install）。

- store     : JSON 数据文件的读写、原子写入与自动备份
- exifread  : 纯标准库的 JPEG EXIF 解析（与前端 assets/js/exif.js 行为一致）
- media     : ffmpeg / ffprobe 的可选封装（视频封面、图片缩略图、时长探测）
- auth      : 账号存储、PBKDF2 密码哈希、签名会话、CSRF 与登录限流
"""

__all__ = ["store", "exifread", "media", "auth"]

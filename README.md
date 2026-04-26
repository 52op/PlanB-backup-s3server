# py-s3server

一个基于 Python 标准库实现的 **轻量级 S3 兼容服务端**。  
适用于本地开发、接口联调、SDK 兼容性验证、集成测试等场景。

> 当前实现聚焦“实用兼容子集”，不是完整 AWS S3 功能替代。

---

## ✨ 项目特点

- 使用 Python 原生 `http.server`（线程版）快速实现
- 支持 **SigV4 鉴权**（含时间偏差校验）
- 支持基础对象操作（上传/下载/查询/删除）
- 支持 `Range` 分段下载
- 支持 `ListObjectsV2` 分页
- 支持 `CopyObject`
- 支持 `x-amz-metadata-directive`：`COPY` / `REPLACE`
- 支持对象元数据 `x-amz-meta-*`（sidecar 方案）
- 支持条件请求（`If-Match` / `If-None-Match` / `If-Modified-Since`）
- 关键链路日志输出清晰（请求、鉴权、对象操作、列表、复制）

---

## 📁 项目结构

```py-s3server/README.md#L1-999
py-s3server/
├─ app.py                      # 启动入口
├─ config.ini                  # 运行配置（本地）
├─ config.ini.example          # 配置示例
├─ s3server.log                # 日志文件
├─ s3data/                     # 对象存储目录（本地文件系统）
├─ s3server/
│  ├─ __init__.py
│  ├─ server.py                # 服务启动
│  ├─ config.py                # 配置加载
│  ├─ logger.py                # 统一日志
│  ├─ responses.py             # S3 XML 响应
│  ├─ auth.py                  # SigV4/V2 鉴权逻辑
│  ├─ storage.py               # 路径安全与存储工具
│  └─ handler.py               # S3 接口主处理逻辑
└─ tests/
   ├─ integration_smoke.py     # 集成烟雾测试
   ├─ requirements.txt         # 测试依赖
   └─ README.md                # 测试说明（中文）
```

---

## ✅ 已支持能力（兼容子集）

### Bucket 相关
- `PUT /{bucket}`：创建 bucket
- `DELETE /{bucket}`：删除空 bucket（非空返回 `BucketNotEmpty`）

### Object 相关
- `PUT /{bucket}/{key}`：上传对象
- `GET /{bucket}/{key}`：下载对象
- `HEAD /{bucket}/{key}`：查询对象元数据
- `DELETE /{bucket}/{key}`：删除对象（幂等）

### 列表相关
- `GET /{bucket}?list-type=2...`：`ListObjectsV2`（支持分页）
  - `prefix`
  - `max-keys`
  - `continuation-token`
  - `start-after`

### 下载增强
- `Range: bytes=...` 单区间下载（`206 Partial Content`）

### 条件请求
- `If-Match`
- `If-None-Match`
- `If-Modified-Since`

### 复制对象
- `PUT /dst-bucket/dst-key` + `x-amz-copy-source`
- `x-amz-metadata-directive`：
  - `COPY`：复制源元数据
  - `REPLACE`：使用请求中的新元数据

---

## 🔐 鉴权与安全

- 默认要求 `SigV4`（可配置）
- 校验 `x-amz-date` 时间偏差（默认 `900` 秒）
- 支持路径安全校验，防止目录穿越
- 返回 S3 风格 XML 错误响应（如 `AccessDenied`、`NoSuchKey` 等）

---

## ⚙️ 配置说明（`config.ini`）

```py-s3server/README.md#L1-999
[SERVER]
host = 0.0.0.0
port = 4431
data_dir = ./s3data
log_file = s3server.log

[AUTH]
access_key = s3admin
secret_key = 12345678

[SECURITY]
require_sigv4 = true
allow_v2 = false
max_skew_seconds = 900
allow_unsigned_payload = false
```

说明：
- `require_sigv4=true`：强制使用 SigV4
- `allow_v2=false`：默认关闭 V2（如需兼容可开启）
- `allow_unsigned_payload`：是否允许 `UNSIGNED-PAYLOAD`

---

## 🚀 快速开始

### 1) 安装 Python 环境
建议 Python 3.9+。

### 2) 配置
复制并编辑配置（如你需要）：
```py-s3server/README.md#L1-999
copy config.ini.example config.ini
```

### 3) 启动服务
```py-s3server/README.md#L1-999
python app.py
```

启动后默认监听：
- `http://0.0.0.0:4431`

---

## 🧪 运行集成测试

### 安装测试依赖
```py-s3server/README.md#L1-999
pip install -r tests/requirements.txt
```

### 执行
```py-s3server/README.md#L1-999
python tests/integration_smoke.py
```

测试覆盖：
- Bucket 创建/删除
- 对象上传/下载/删除/HEAD
- 条件请求
- Range 下载
- 列表分页
- CopyObject + 元数据策略
- 非空 bucket 删除失败校验

更多细节请见：`tests/README.md`

---

## 🪵 日志说明

服务端会同时输出：
- 控制台日志
- 文件日志（`s3server.log`）

关键日志包括：
- 请求开始（IP、method、path、UA）
- 鉴权结果（成功/失败原因）
- 对象操作结果（命中、大小、范围）
- 列表分页信息
- 复制操作信息

---

## 📦 GitHub 发布与自动打包（Tag 触发）

项目已配置 GitHub Actions：当你 push `v*` tag 时，会自动构建并发布多平台产物。

### 自动构建目标
- Windows x64（Win7 兼容向）独立可执行文件（`.exe`）
- Linux x64 独立可执行文件（ELF 可执行文件）

> 独立二进制由 PyInstaller `--onefile` 构建，目标机器无需预装 Python。

### 推荐 tag 规范
- `v0.1.0`
- `v1.0.0`
- `v1.0.0-rc.1`

### 触发后的流水线行为
1. 校验 tag 格式（`vMAJOR.MINOR.PATCH`，可带后缀）
2. 分平台构建独立二进制（Windows / Linux）
3. 组装发布包（包含：
   - 二进制文件
   - `config.ini.example`
   - `README.md`
   - `LICENSE`（若存在））
4. 输出归档文件：
   - `py-s3server-vX.Y.Z-windows-x64-win7.zip`
   - `py-s3server-vX.Y.Z-linux-x64.tar.gz`
5. 生成校验文件 `SHA256SUMS.txt`
6. 自动创建/更新 GitHub Release 并上传全部产物

> 当前工作流文件：`.github/workflows/release-package.yml`  
> 触发条件：`push tags: v*`

#### 一键发布常用命令（快速参考）
```py-s3server/README.md#L1-999
# 1) 同步主分支代码
git checkout main
git pull

# 2) 提交改动
git add .
git commit -m "release: v0.1.0"

# 3) 打 tag 并推送
git tag v0.1.0
git push origin main
git push origin v0.1.0
```

#### 常用版本 tag 示例
```py-s3server/README.md#L1-999
v0.1.0
v0.2.3
v1.0.0
v1.0.0-rc.1
```

#### 发布后你会在 Release 中看到
- `py-s3server-vX.Y.Z-windows-x64-win7.zip`
- `py-s3server-vX.Y.Z-linux-x64.tar.gz`
- `SHA256SUMS.txt`

#### 发布包内容（精简）
- 可执行文件（Windows 为 `.exe`，Linux 为无扩展名二进制）
- `config.ini.example`
- `README.md`
- `LICENSE`（如果仓库中存在）
- 不包含 `tests/` 目录与测试脚本

---

## 🧱 已知限制

本项目是轻量实现，暂不覆盖完整 S3：
- 未实现 Multipart Upload
- 未实现 ACL/Policy/Versioning
- 未实现完整对象标签/存储类型体系
- 未实现跨区域/高可用能力
- 元数据采用 sidecar 文件（本地实现方案）

---

## 🤝 适用场景

- 本地开发替代真实 S3
- SDK 接口联调
- CI 集成测试
- 功能原型验证
- 教学/学习 S3 协议流程

---

## 📄 许可证（MIT，宽松授权）

本项目采用 **MIT License**（最宽松、最常用的开源许可证之一）。

你可以在满足 MIT 许可证声明保留要求的前提下，自由进行以下行为：

- 商业使用
- 修改源码
- 再发布
- 私有化使用
- 与其他项目集成

你需要履行的主要义务：

- 在源码或发布包中保留原始版权声明与 MIT 许可文本

责任与担保说明：

- 本项目按“现状（AS IS）”提供，不提供任何明示或暗示担保
- 作者/贡献者不对使用本项目产生的任何损失承担责任

完整条款请查看仓库根目录下的 `LICENSE` 文件。
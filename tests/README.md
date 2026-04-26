# 集成冒烟测试说明

本目录提供一个最小可用的端到端冒烟测试，用于验证你的 S3 兼容服务核心能力是否可用。

- 测试脚本：`tests/integration_smoke.py`
- 依赖文件：`tests/requirements.txt`

脚本通过 `boto3` 访问本地服务端点，覆盖你当前已实现的关键兼容路径。

---

## 1）前置条件

- Python 3.9+（建议）
- 本地可运行的服务端代码
- 正确配置的 `config.ini`（尤其是监听端口、鉴权信息）

安装测试依赖：

```/dev/null/cmd.txt#L1-1
pip install -r tests/requirements.txt
```

---

## 2）启动服务端

在项目根目录（`py-s3server`）执行：

```/dev/null/cmd.txt#L1-1
python app.py
```

请保持该终端窗口持续运行。

---

## 3）执行集成冒烟测试

另开一个终端，进入项目根目录后执行：

```/dev/null/cmd.txt#L1-1
python tests/integration_smoke.py
```

通过时会看到：

- 分步骤的 `[OK]` 日志
- 最终 `✅ Integration smoke test passed.`

失败时脚本会以非零退出码结束，并输出 `❌ ... failed`。

---

## 4）可用环境变量

你可以通过环境变量覆盖默认参数：

- `S3_ENDPOINT_URL`  
  默认：`http://127.0.0.1:4431`
- `S3_ACCESS_KEY`  
  默认：`s3admin`
- `S3_SECRET_KEY`  
  默认：`12345678`
- `S3_REGION`  
  默认：`us-east-1`
- `S3_ADDRESSING_STYLE`  
  默认：`path`（自定义 S3 服务建议使用 path-style）
- `S3_VERIFY_TLS`  
  默认：`false`（当值为 `true/1/yes/on` 时启用证书校验）

### PowerShell 示例

```/dev/null/cmd.ps1#L1-7
$env:S3_ENDPOINT_URL="http://127.0.0.1:4431"
$env:S3_ACCESS_KEY="s3admin"
$env:S3_SECRET_KEY="12345678"
$env:S3_REGION="us-east-1"
$env:S3_ADDRESSING_STYLE="path"
$env:S3_VERIFY_TLS="false"
python tests/integration_smoke.py
```

### CMD 示例

```/dev/null/cmd.bat#L1-7
set S3_ENDPOINT_URL=http://127.0.0.1:4431
set S3_ACCESS_KEY=s3admin
set S3_SECRET_KEY=12345678
set S3_REGION=us-east-1
set S3_ADDRESSING_STYLE=path
set S3_VERIFY_TLS=false
python tests/integration_smoke.py
```

---

## 5）当前覆盖场景

当前冒烟测试会验证以下能力：

1. `CreateBucket`（创建两个 bucket）
2. `PutObject`（带 `x-amz-meta-*` 元数据）
3. `HeadObject` 元数据回读
4. 条件请求 `If-None-Match`（期望 `304`）
5. 普通 `GetObject`
6. Range 下载（`bytes=0-4`）
7. `ListObjectsV2` 分页（`MaxKeys`、`ContinuationToken`）
8. `CopyObject` + `MetadataDirective=COPY`
9. `CopyObject` + `MetadataDirective=REPLACE`
10. `DeleteObject` 幂等性（删除不存在对象）
11. `DeleteBucket` 非空失败（`BucketNotEmpty`）
12. 清理测试对象和 bucket

---

## 6）故障排查指南

### A）`403 AccessDenied`

常见原因：

- Access Key / Secret Key 不一致
- SigV4 验签失败
- `x-amz-date` 与服务端时间偏差过大
- 端点地址或端口错误

排查建议：

- 检查 `config.ini` 的 `[AUTH]` 与 `[SECURITY]` 配置
- 确认机器系统时间准确
- 确认测试环境变量与服务端配置一致
- 查看服务端日志中的 `AUTH fail ... reason=...`

---

### B）连接被拒绝 / 超时

常见原因：

- 服务端未启动
- 端点地址错误
- 端口不匹配

排查建议：

- 确认 `python app.py` 正在运行
- 确认 `S3_ENDPOINT_URL` 与 `config.ini` 端口一致

---

### C）TLS/SSL 错误

如果使用 HTTP，请确保：

- `S3_VERIFY_TLS=false`
- 端点是 `http://...`

如果使用 HTTPS + 自签证书：

- 本地测试可临时关闭校验，或
- 将证书加入信任链（更推荐）

---

### D）`304` 断言失败

脚本会刻意验证 `IfNoneMatch` 触发 `304`。  
若失败，通常要检查：

- `ETag` 格式是否正确（是否带引号）
- 条件请求逻辑是否按预期生效

---

### E）分页断言失败

检查以下实现点：

- 列表按 key 字典序排序
- `NextContinuationToken` 生成与消费逻辑
- `MaxKeys` 的边界处理（如 0、超限）

---

### F）Copy 元数据断言失败

对于 `MetadataDirective=COPY`：

- 应保留源对象元数据

对于 `MetadataDirective=REPLACE`：

- 应仅保留新元数据，不保留旧字段

请同时检查 sidecar 元数据文件（`.meta.json`）是否正确写入/读取。

---

### G）清理失败：`BucketNotEmpty`

可能是中途中断后遗留了对象或目录。  
建议：

- 手动清理 `s3data/` 下对应测试 bucket
- 或重新运行测试让 cleanup 逻辑执行完整

---

## 7）说明

- 这是 **冒烟/集成测试**，不是完整的 S3 兼容性认证套件。
- 目标是快速发现回归问题，适合日常开发验证。
- 推荐流程：
  1. 启动服务
  2. 执行冒烟测试
  3. 失败时结合服务日志定位问题
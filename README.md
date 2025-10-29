# Shipyard

> Very early technical preview, lack of manpower, time :(

✨ 一个轻量级 Agent Sandbox 运行环境，支持多 Session 的 Sandbox 复用。支持 Python Interpreter、Shell、File System 等功能。✨ 

## 快速开始

Docker 镜像已发布到 Docker Hub：
- Bay: `soulter/shipyard-bay:latest`
- Ship: `soulter/shipyard-ship:latest` 

## 架构

User <-> Bay <-> Ship

- Bay 是一个中台，起到管理和调度 Ship 的作用。
- Ship 是一个隔离的容器化执行环境，运行 Python Interpreter、Shell、File System 等功能。

## Environment

- MAX_SHIP_NUM: 最大允许的 Ship 数量，默认 10
- BEHAVIOR_AFTER_MAX_SHIP: 达到最大 Ship 数量后的行为，默认 "reject"。可选值：
  - "reject": 拒绝新的 Ship 创建请求
  - "wait": 等待直到有 Ship 被释放
- ACCESS_TOKEN: 访问令牌，用于操作 Ship，默认为 `secret-token`

## Packages

### Bay

包含一个基于 FastAPI 的 API 服务。

#### 接口定义

- `POST /ship` - 创建一个新的 Session，这会启动一个新的 Ship 环境。
- `GET /ship/{ship_id}` - 获取指定 Ship 环境的信息。
- `DELETE /ship/{ship_id}` - 删除指定的 Ship 环境。
- `POST /ship/{ship_id}/exec/{oper_endpoint}` - 在指定的 Ship 环境中执行操作。
- `GET /ship/logs/{ship_id}` - 获取指定 Ship 环境的日志。
- `POST /ship/{ship_id}/extend-ttl` - 延长指定 Ship 环境的生命周期。
- `POST /ship/{ship_id}/upload` - 上传文件到指定 Ship 环境的工作目录。

上述所有接口都需要请求头：

- `Authorization`: Bearer token
- `X-SESSION-ID`: Session ID - 为了了追踪请求来源，实现 Ship 复用

#### Ship Entity

- `id` - 唯一标识符
- `status` - 当前状态。1: running, 0: stopped
- `created_at` - 创建时间
- `updated_at` - 最后更新时间
- `container_id` - 关联的 Docker 容器 ID
- `ip_address` - 容器的 IP 地址
- `ttl` - 生命周期，单位为秒

#### POST /ship

创建一个新的 Ship 环境。

请求体参数：

- `ttl` (int, 必填) - 生命周期，单位为秒。
- `spec` (dict, 可选) - 规格，包含以下可选字段：
  - `cpus` (float, 可选) - 分配的 CPU 数量。
  - `memory` (str, 可选) - 分配的内存大小，例如 "512m"。
- `max_session_num` (int, 可选) - 最大 Session 数量，默认 1。这个值决定了 Ship 可以被多少不同的 Session ID 复用。请注意，Ship 虽然最大程度保证了 Session 之间的隔离，但不能完全杜绝隔离。

返回 Ship 实体。

#### POST /ship/{ship_id}/exec

在指定的 Ship 环境中执行操作。

请求体参数：

- `type` (str, 必填) - 操作的端点，决定执行的具体功能。
- `payload` (dict, 可选) - 传递给具体操作的参数（以 POST 请求体的方式）。

截至目前，`type` 可以是以下值：

- `fs/create_file` - 创建文件
- `fs/read_file` - 读取文件
- `fs/write_file` - 写入文件
- `fs/delete_file` - 删除文件
- `fs/list_dir` - 列出目录内容
- `ipython/exec` - 执行 IPython 代码
- `shell/exec` - 执行 Shell 命令
- `shell/processes` - 获取当前运行的进程列表
- `shell/cwd` - 获取当前工作目录

中台会根据 `oper_endpoint` 将请求体转发到对应的 Ship 容器内的 API，并带上请求头 `X-SESSION-ID`。

需要请求头：

- `X-Ship-ID` - Ship 的 ID
- `X-SESSION-ID` - Session ID - 为了了追踪请求来源，实现 Ship 复用。

#### POST /ship/{ship_id}/upload

上传文件到指定 Ship 环境的指定 Session 的工作目录。

```bash
curl -X POST "http://localhost:8123/upload" \
  -H "X-SESSION-ID: my-session" \
  -F "file=@local_file.txt" \
  -F "file_path=documents/uploaded_file.txt" # or absolute path like /workspace/my-session/documents/uploaded_file.txt
```

当路径是绝对路径时，必须以 `/workspace/{session_id}/` 开头，否则会被拒绝。

### Ship

包含一个基于 FastAPI 的 API 服务，运行在容器中。

#### FS

提供文件系统操作。

- `POST /fs/create_file` - 创建文件

```
{
  "path": "string",
  "content": "",
  "mode": 420
}
```

- `POST /fs/read_file` - 读取文件

```
{
  "path": "string",
  "encoding": "utf-8"
}
```

- `POST /fs/write_file` - 写入文件

```
{
  "path": "string",
  "content": "string",
  "mode": "w",
  "encoding": "utf-8"
}
```

- `POST /fs/delete_file` - 删除文件

```
{
  "path": "string"
}
```

- `POST /fs/list_dir` - 列出目录内容

```
{
  "path": ".",
  "show_hidden": false
}
```

#### Python Interpreter

提供 Python Interpreter 代码执行功能。

- `POST /ipython/exec` - 执行 Python 代码

```
{
  "code": "string",
  "timeout": 30,
  "silent": false
}
```

#### Shell

提供 Shell 命令执行功能。

- `POST /shell/exec` - 执行 Shell 命令

```
{
  "command": "string",
  "cwd": "string",
  "env": {
    "additionalProp1": "string",
    "additionalProp2": "string",
    "additionalProp3": "string"
  },
  "timeout": 30,
  "shell": true,
  "background": false
}
```

- `GET /shell/processes` - 获取当前运行的进程列表
- `GET /shell/cwd` - 获取当前工作目录

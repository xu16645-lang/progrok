# ProGrok 浏览器注册工具

ProGrok 是面向 Windows 的本地 Web 工具，用于执行 Grok（xAI）浏览器注册、微软/自定义邮箱接码、账号凭证保存、账号探活以及 CPA/Sub2API 导入。服务默认只监听 `127.0.0.1`，不依赖 PostgreSQL 或 Redis。

> 仅可在你有权使用的邮箱、代理、账号和站点上运行，并遵守相关服务条款。账号文件、Token、Cookie、邮箱凭据和管理密钥均属于敏感信息，请勿公开上传。

## 当前版本说明

- 当前注册目标：Grok（xAI）。
- 当前注册方式：浏览器注册。
- ChatGPT 注册、半协议注册和 ChatGPT Agent Identity 下载入口暂时停用，相关代码保留以便后续恢复。
- 邮箱入口仅保留“自定义”和“微软邮箱账户池（本地助手）”。
- 支持 CPA JSON、Sub2API JSON 生成和站点导入。
- 支持注册后探活、账号轮询、批量探活和自动半小时轮询。

## 系统要求

- Windows 10/11 64 位。
- Python 3.10 及以上，推荐 Python 3.12。
- 可以访问 Python 软件源、Camoufox 下载源和注册目标站点。
- 首次运行需要下载依赖和浏览器环境，需预留数百 MB 磁盘空间。

默认本地端口：

| 服务 | 地址 |
| --- | --- |
| Web 管理页 | `http://127.0.0.1:3080` |
| Turnstile Solver | `http://127.0.0.1:5072` |
| 微软邮箱助手 | `http://127.0.0.1:17373` |

## 下载与安装

从 GitHub Releases 下载最新的 `progrok-windows-*.zip`，解压到独立目录。不要直接覆盖仍在运行的旧目录；需要回滚时可重新下载旧版本附件。

首次使用双击：

```text
install_and_start.cmd
```

脚本会自动检测或安装 Python、创建虚拟环境、安装依赖、下载 Camoufox，并启动 Web 服务、Solver 和微软邮箱助手。

启动完成后访问：

<http://127.0.0.1:3080>

若 PowerShell 策略阻止脚本，可执行：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 日常启动与停止

```text
start.cmd
stop.cmd
```

或在 PowerShell 中运行：

```powershell
.\start.ps1
.\stop.ps1
```

`start.ps1` 会关联启动三个本地服务，并避免重复占用端口。日志保存在 `runtime/logs`。

## 注册配置

### 注册数量与并发

- 注册数量：单批 `1-10000`。
- 并发数：下拉选择 `1-8`，且不能大于注册数量。
- 错峰毫秒：控制各注册任务的启动间隔。
- 显示注册浏览器：开启后显示固定尺寸的 Camoufox 注册窗口，适合观察失败节点。
- 导入前测活：默认开启，账号取得本地凭证后先探活，再进入导入流程。

并发任务各自领取邮箱并独立执行浏览器流程。并发越高，对 CPU、内存、邮箱和代理稳定性的要求越高。

### 邮箱配置

邮箱类型只提供两种：

1. 自定义：填写兼容邮箱服务的 API 地址、API Key 和邮箱域名。
2. 微软邮箱账户池（本地助手）：导入 Outlook/Hotmail 账号，由本地助手读取验证码。

微软邮箱导入格式：

```text
email----password----client-id----refresh-token
```

添加微软邮箱后会自动测活。每个邮箱支持本体加 `+数字` 别名复用，默认总计最多 10 次；可用注册数量按剩余可用槽位计算。邮箱列表支持验证码实时显示、状态筛选、模糊搜索、全选、删除所选、删除已用和重新测活。

自定义邮箱接口需兼容当前项目的地址创建、邮件列表和验证码读取协议。项目不会内置或上传开发者的域名、API 地址和密钥。

### 代理配置

代理池每行一个，支持：

```text
http://127.0.0.1:7890
http://user:password@host:port
socks5://host:port
host:port
```

支持轮询、随机和固定策略。保存配置或开始注册时，代理会同步给浏览器注册链路和本地 Solver。留空表示直连。

## 注册、探活与导入流程

开启“导入前测活”时：

```text
浏览器注册 → 获取本地凭证 → 生成目标 JSON → 请求上游探活 → 导入 CPA/Sub2API
```

关闭“导入前测活”时，凭证转换完成后直接进入导入阶段。自动导入关闭时，已转换账号会进入“待手动导入”列表，可逐个点击导入。

监控面板按账号显示实时步骤，日志包含步骤时间、验证码、上游探活请求与响应摘要、JSON 转换和站点导入结果。日志只在产生新步骤时滚动到最新位置，用户手动查看历史日志时不会被持续拉回底部。

### 自动导入配置

支持两种目标：

- CPA：填写站点地址和管理密钥，生成 CPA JSON 后导入。
- Sub2API：填写站点地址，选择管理员邮箱密码或 API Key 认证，获取现有分组后选择目标分组。

注册配置中的 JSON 格式决定生成和导入格式。CPA 与 Sub2API 使用不同的导入请求和字段映射，不应混用。

Sub2API 导入账号容量默认使用 `3`。导入任务在单个账号准备完成后即可开始，不等待整批凑满。

### 手动导入 JSON

在自动导入配置模块中可选择一个或多个 JSON 文件手动导入。导入前请确认当前选择的目标站点和 JSON 格式一致。

## 账号轮询

“账号轮询”页面记录注册并生成本地凭证的账号，主要展示：

- 账号信息和类型。
- 注册时间、导入状态和导入时间。
- 账号状态、探活状态和上游错误信息。
- 从注册成功到最后一次探活的存活时间。

支持状态筛选、关键词模糊搜索、分页、单账号探活/删除、全选探活/删除和全部探活。后台默认每 30 分钟执行一次全账号轮询。注册流程中的探活结果会同步到轮询列表。

## 账号文件与下载

本地文件默认保存在：

| 内容 | 路径 |
| --- | --- |
| 独立账号 JSON | `runtime/data/accounts` |
| 合并认证数据 | `runtime/data/auth.json` |
| 原始 SSO/会话诊断 | `runtime/data` 下对应目录 |
| 注册、探活、导入记录 | `runtime/data` 下对应记录文件 |

页面支持下载：

- 纯 SSO。
- `sso=...` Cookie。
- 邮箱 + SSO。
- 邮箱:密码:SSO。
- 普通 JSON（邮箱 + 密码）。
- CPA JSON。
- Sub2API JSON。

ChatGPT Agent Identity auth.json 下载当前不可选。所有下载内容都可能包含敏感凭据。

## 配置与数据安全

页面配置保存到 `config/config.json`，包括邮箱密钥、代理密码、CPA/Sub2API 管理凭据等。该文件已被 Git 忽略。

以下内容不会进入源码仓库或发布包：

- `config/config.json`、`.env`。
- `runtime/`、`data/`、`output/`、`artifacts/`。
- 浏览器缓存、虚拟环境和测试输出。
- Token、Cookie、SSO、账号文件、邮箱凭据、API Key 和管理密钥。

## 测试

在项目根目录执行：

```powershell
python -m pytest tests -q
```

构建 Windows 发布包：

```powershell
.\tools\build_release.ps1 -Version v1.1.0
```

发布包输出到 `artifacts/release`，构建过程会检查禁止文件，并扫描本地配置中的敏感值是否误入发布文件。

## 常见问题

### 页面无法打开

确认 `3080` 端口和服务状态：

```powershell
Get-NetTCPConnection -LocalPort 3080 -ErrorAction SilentlyContinue
Get-Content .\runtime\logs\app.err.log -Tail 100
```

### Solver 未就绪

首次启动需要下载 Camoufox。确认网络可用，等待下载完成后点击页面中的重新检测。也可检查 `5072` 端口和 Solver 进程。

### 微软邮箱测活失败或收不到验证码

确认 Client ID、Refresh Token 与邮箱匹配，本地助手 `17373` 在线，并检查 `runtime/logs/hotmail-helper.err.log`。失效邮箱会返回账户相关错误，不会继续注册流程。

### 注册卡在某个页面

开启“显示注册浏览器”观察当前页面。流程会动态识别邮箱、密码、人机验证、验证码和提交按钮；需要人工处理的人机验证可在浏览器窗口中完成。

### 导入或探活失败

先查看日志中的上游 HTTP 状态和错误信息。`401/403` 通常表示凭证或授权不可用，`429` 表示限流。探活失败与注册失败分别统计，凭证失效不会被计为注册失败。

### 分享日志

分享前必须删除邮箱、密码、代理、Token、Cookie、SSO、API Key 和站点管理凭据。

## 来源与许可

本项目基于 [HM2899/grokcli-2api](https://github.com/HM2899/grokcli-2api) 的注册能力进行二次开发，并内置项目运行所需的第三方组件。第三方组件的许可和 NOTICE 文件随发布包保留。

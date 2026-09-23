# PCC — Plus Codex Collaboration

[简体中文](README.md) | [English](docs/README.en.md)

用当前 ChatGPT 对话规划与审查，通过经过 OAuth 鉴权的本机控制器，让独立登录的 Plus Codex CLI 执行一个受授权任务，并保存结果和用量回执。

**这是可自行部署的工具，不是共享账号或免配置的云服务。**每位使用者配置自己的官方 CLI、独立 ChatGPT 登录、OAuth 和 HTTPS 入口。PCC 不合并额度、不自动换号、不使用模型 API Key。

## 状态与边界

- 参考 Windows 部署已完成一次合成数据的网页派单、独立 Plus 执行、结果与用量读取及负责人审查。该一次验收不代表其他部署自动可用；可分享源码仍需各自部署和验收。
- 当前支持 Windows 11、Python 3.11+（验收使用 3.12）与官方 Codex CLI。本轮模型/档位验收使用 `0.155.0-alpha.9.2`；其他 CLI 版本必须先核对帮助和 `config/read`，配置不匹配会阻止执行。
- `PCC_HOST_TRUSTED` 是本机 Full Access，**没有严格文件或网络隔离**。项目 grant、提示词和 broker 路径检查不是操作系统沙箱。
- 此模式的独立 Plus 子进程默认请求 `gpt-6-sol`／`medium`，由原生 `config/read` 核对后才派发；不更改原 Pro 或 Plus CLI 的全局默认模型。用户的账号若不支持该模型，任务会在模型启动前阻止。
- `PCC_STRICT` 保留失败关闭的门禁；未部署的虚拟机适配不作为可用功能宣传。
- 默认没有项目授权；安装保持派单暂停。完整网页执行、用量回传、独立 REVIEW 必须由部署者自己验收。

## 快速开始（不会自动发起模型请求）

下载或克隆仓库到独立目录，使用 PowerShell：

```powershell
git clone https://github.com/ttacit666-eng/pcc-codex.git
cd pcc-codex
.\install.ps1 -Python 'C:\Tools\Python312\python.exe'
.\.venv\Scripts\python.exe tools\doctor.py
.\.venv\Scripts\python.exe -m pytest tests vendor/tests -q
```

使用你实际安装的 Python 路径。已有 Windows Python Launcher 时可省略 `-Python`。安装只创建仓库内 `.venv`，不安装 Codex、不修改系统 PATH、全局 Profile 或登录。

然后保存三个**非秘密路径**（首次写入，已有文件拒绝覆盖）：

```powershell
.\.venv\Scripts\python.exe tools\configure.py --cli 'C:\Tools\Codex\codex.exe' --plus-home "$env:USERPROFILE\.codex-plus" --controller-home "$env:USERPROFILE\.codex"
.\.venv\Scripts\python.exe tools\doctor.py --native
```

控制器 HOME 必须填实际 Pro 环境路径，不要假定它一定是 `.codex`。Plus 与控制器 HOME 不得重合或嵌套。配置存入忽略上传的 `config/runtime.local.json`；也可使用仅当前控制器进程的 `PCC_CODEX_CLI`、`PCC_PLUS_HOME`、`PCC_CONTROLLER_HOME`。不使用 `setx`。

独立 Plus 登录应由官方 CLI 完成。不要复制现有账号的认证目录；不要上传 `auth.json`。先在专用本机终端确认 `CODEX_HOME`，再按当前 CLI 的 `login --help` 选择设备码/浏览器授权。PCC 的状态采样使用官方 App Server，实际派单前要求 ChatGPT Plus 身份和不同的控制器身份；不能确认则阻止派发。

## 网页接入和项目授权

按 [部署指南](docs/DEPLOYMENT.md) 配置自己的 OAuth/HTTPS，注册 MCP 连接，并保存有限项目 grant。`plugins/pcc` 是标准技能插件包；安装技能不会自动创建 MCP 连接、OAuth 或项目授权，也不需要把别人服务器的地址写进插件。

本人在已接入的网页对话输入：

```text
<进行pcc协作模式>
使用已授权的 smoke 项目，读取合成 CSV 并生成统计摘要。
只执行一次，返回结果、真实用量和独立审查结论。
```

只发送口令时查询就绪状态，不执行。项目未绑定时报告缺项，不要求用户手写内部任务编号。

## 功能

| 功能 | 机制 |
|---|---|
| 任务授权 | 本机保存、版本化和可撤销的项目 grant；网页不能扩权 |
| 单执行器 | SQLite 任务登记、幂等键、冲突检查；状态不明不重派 |
| 工具执行 | 独立 Plus CLI 生成结果；缺文件不能算成功 |
| 受控操作 | broker 发布结果、可恢复删除、哈希固定的离线 wheel 安装、精确目标上传/下载 |
| 用量 | App Server 前后快照和 `exec --json` 原生事件；缓存不重复相加，缺失不填零 |
| 证据 | 每任务输入/输出哈希、结果、事件和持久回执；LOCAL_CHECK 与 REVIEW 分开 |

除离线 wheel 外，现支持本机预先批准的固定版本 DSH/npm 包安装，详见[安装扩展说明（中英文）](docs/PACKAGE_INSTALL.md)。网页只能选择安装 ID，不能传任意命令或包地址；默认禁用生命周期脚本。上传不是通用 SSH/HPC 执行。任何外发目标须由本机 grant 明确批准。模型在 Full Access 下有宿主权限，必须只用于可信任务；参阅 [安全说明](SECURITY.md)。

## 管理与输出

```powershell
.\pcc.cmd grant config\project.json
.\pcc.cmd capabilities '你的OAuth subject'
.\pcc.cmd revoke smoke
.\pcc.cmd pause
.\pcc.cmd resume
.\pcc.cmd serve config\deployment.json
```

`resume` 必须在配置、项目批准和验收准备完成后由本机负责人执行。`serve` 手动监听 `127.0.0.1:8876`，不会注册开机服务。已有服务锁不自动抢占。

输出保存在 `state/runs/<run_id>/`：`result.json`、`usage.json`、`receipt.md`、冻结 `artifacts` 和执行证据；`state/usage-index.json` 为持久索引。输入/输出可能含业务正文，**整个 state/jobs/evidence 不得随仓库分享**。

如需同模型的**执行侧**单次对照，可在全新的 `synthetic/<运行目录>/` 下准备相同合成输入与任务正文，使用 `tools/pro_cli_comparison.py --help` 查看一次性 Pro CLI 采集入口。其回执与 PCC Plus 回执分开保存；Pro 网页规划/审查 token 无官方会话级记录时为“未取得”，因此不能据两次 CLI token 断言完整协作流程的节省率。

撤销及恢复步骤见 [部署指南](docs/DEPLOYMENT.md#撤销与卸载)。GitHub 分享的是源码，不会把作者的 Plus 账号、Auth0 租户或电脑执行权分享给下载者。

## English summary

PCC is a self-hosted, OAuth-authenticated MCP controller for a separately authenticated Plus Codex CLI. The current conversation plans and reviews; a single executor performs the task. It preserves per-task usage receipts, project grants, idempotency, and durable artifacts. Windows host-trusted mode is Full Access, not a security sandbox. Bring your own accounts, CLI, OAuth provider, and HTTPS endpoint. No credentials or personal deployment data are included.

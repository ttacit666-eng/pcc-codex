# 自部署与验收

## 1. 本机准备

先完成 README 的安装、非秘密运行路径配置和非模型测试。不要使用作者的账号、域名或回调；仓库不提供这些配置。不要在业务项目根目录启动控制器。当前必须保持一个控制器服务实例，不能让多个账号/服务同时执行同一项目。

官方 Codex CLI 自行安装并确认来源、版本。Plus 登录由你本人完成；原 Pro 登录不退出、不复制。App Server 的 account/read 和 account/rateLimits/read 只读采样，不充值、不重置。Plus 必须报告 planType=plus，且其身份指纹与控制器不同；不可见时阻止派发。

## 2. OAuth 与 HTTPS

复制 `config/deployment.example.json` 为 `config/deployment.json`，填写自己的 issuer、JWKS、HTTPS resource 和允许的 client_id。`deployment_enabled` 默认 false，准备完成后本机负责人设 true。

OAuth 提供方需要支持授权码、PKCE、刷新登录和 RS256 签名。resource/audience 应与 HTTPS `/mcp` 完全一致，scope 为 `pcc:operate`。MCP 资源服务器不替代 OAuth 授权服务器。客户端密钥只填入 ChatGPT 与身份提供方的官方页面，不写入项目。

若使用 Auth0：API Identifier 使用你的 HTTPS `/mcp`；授权 `pcc:operate`，按需要启用 Offline Access/Refresh Token。将 ChatGPT 注册页显示的**实际回调 URL**加入客户端允许回调，不能复制其他人的回调。严格 `tpc_` 第三方客户端不支持 OIDC ID token/UserInfo；该类型选择 OAuth-only、关闭 OIDC，基础 scope 可保留 offline_access。其他客户端类型按其官方能力配置。

默认 `jwt_clock_skew_seconds=0`。若取得明确时间偏差证据，可由本机负责人配置 0..180 秒；仅容忍未来 iat/nbf，过期 exp 仍按本机当前时间拒绝。该配置不修复系统时钟，也不能代替合理的时间同步。

以 `pcc.cmd serve config/deployment.json` 启动，仅监听 loopback；由自己批准的反向代理提供 HTTPS。不要关闭 JWT 验证，不给控制器任意公网免认证入口。若用 ngrok，authtoken 配置保留在本机专用目录且不入库；PCC 不启动或修改现有隧道。

在 ChatGPT 的自定义 MCP/插件创建界面，填自己的 HTTPS 地址并选择 OAuth。当前界面名称可能变化。确认无认证访问得到 401、合法账户可发现 7 个工具、写操作标为写入。完成一次 `pcc_capabilities` 只读调用后，再进行项目授权。

## 3. 一次性有限项目授权

复制 `config/project.example.json` 为 `config/project.json`。必须明确 project、本人已认证 OAuth subject、root、actions、读写子目录；Full Access 要求 execution_mode=PCC_HOST_TRUSTED 和 host_trusted_ack=true。external_idle_ack 表示负责人确认项目没有冲突的外部执行器，PCC 不能阻止其他进程写文件。

首次使用独立合成项目：输入 `step,value` 的 CSV 四行（1,10 到 4,40），读权限仅 input，发布权限仅 results，不配置安装、删除或外部上传目标。设置 synthetic=true 不会自动授权网络；它只允许显式声明的 loopback 合成目标。

本机执行 `pcc.cmd grant config/project.json`。该命令不通过 MCP 暴露；保存的授权有版本。模板不是批准，网页传 approved=true 或修改 mode 不生效。不要给整个用户目录、控制器状态或认证目录授权。

## 4. 口令与独立审查

`plugins/pcc/skills/pcc/SKILL.md` 可以作为当前对话的技能/指令，或通过支持标准 `.codex-plugin/plugin.json` 的客户端安装。没有实际注册的 PCC 工具时必须报告 NOT_CONNECTED；技能文件本身不能安装网页登录连接。

在已接入对话中：先 pcc_capabilities，再 pcc_project；根据已有 grant 组织 goal/plan，然后 pcc_submit。同一超时任务用 pcc_status 查询，不改 request_label 重新提交。成功后 pcc_result 获取用量和清单，pcc_artifact 读取实际文件并独立核对数值、输入哈希和执行归属。

参考合成验收：count=4、sum=100、min=10、max=40、mean=25、实际输入 SHA256。本机 LOCAL_CHECK 不等于独立 REVIEW；token 数不等于套餐百分比，前后额度差不等于精确任务扣费。

## 撤销与卸载

1. `pcc.cmd pause` 停止新派单；这不终止已有子进程。先查任务状态，必要时使用原任务 cancel；UNKNOWN/RECOVERY_REQUIRED 不盲目重派或删锁。
2. `pcc.cmd revoke <project>` 撤销项目；保留运行证据和用量。
3. 核对 state/service.lock 对应的 PCC 进程和无在途任务后，仅停止本次 PCC 服务；停止自己的专用反向代理。
4. 从 ChatGPT 移除自己新增的 PCC 连接，按身份提供方流程撤销对应客户端。
5. 导出所需证据后，可移走本仓库及其虚拟环境。不要删除 Pro/Plus HOME、旧 CWC 或其他项目。回收删除文件可用 `pcc.cmd restore <subject> <task> <path>`，仅限终态及存在对应记录时。

## 官方参考

- https://developers.openai.com/codex/auth
- https://developers.openai.com/codex/permissions
- https://developers.openai.com/codex/noninteractive/
- https://developers.openai.com/plugins/build/auth
- https://auth0.com/docs/get-started/applications/third-party-applications/security-controls
- https://pyjwt.readthedocs.io/en/stable/usage.html

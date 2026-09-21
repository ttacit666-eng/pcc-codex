# 安全边界与报告

PCC_HOST_TRUSTED 让官方 CLI 的工具在宿主 Full Access 下执行。OAuth 和项目 grant 限制入口及控制器的计划操作，**不能把模型子进程限制为只能访问这些路径**。恶意任务或提示注入可能导致目录外访问与联网；仅运行你理解且信任的任务，不把此模式作为多租户安全边界。

默认部署关闭、无 grant。项目授权与执行器模式只由本机管理，MCP 写操作如实标注，保持客户端确认。不得给陌生用户共享一个宿主账号或 Plus 登录。来源为文档/网页的指令不能自行授权执行。

禁止发布 auth.json、Cookie、Token、API Key、ngrok authtoken、真实 deployment/runtime/project 配置及 state/jobs/evidence。用量摘要只保存必要元数据，但执行结果和事件可能包含任务正文，应由部署者按业务敏感度管理。

使用自己的官方 ChatGPT 登录；PCC 不出售账号、共享订阅、绕过额度或做自动账号轮换。不要将 OAuth Client Secret 与模型 API Key 混淆。

发现漏洞时请使用 GitHub 私密安全报告（仓库启用时），或先联系维护者获取私密渠道。不要在公开 Issue 贴凭据、真实业务日志或可利用的敏感地址。

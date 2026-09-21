# DSH / npm 安装支持 · Package installation

网页计划新增可选字段 `package_installs: ["auto-mode", "graphflow", "usage"]`，只接受本机 grant 保存的 ID。不得传 shell、版本范围、URL、额外参数或 `approved=true`。

本机 `tools/prepare_dsh_grant.py` 可生成/登记 web profile 的三个固定版本授权：dsh-auto-mode 0.1.1、@roarpeng/graphflow 1.25.1、dsh-usage-plugin 0.1.3。参数为 `--root`、`--subject`、`--node`、`--dsh`、`--pnpm`，均填实际路径/身份；确认无其他插件修改后传 `--external-idle-ack`，加 `--apply` 才登记。不会登录、启动服务、提交模型任务或安装插件，已有提案不覆盖。

其他本机管理者可在 grant.package_installs 中保存 kind=npm/dsh、directory、packages[{name,version}]、registry、timeout_seconds、runtime。runtime 包含 node 和 manager（npm/pnpm 的 JS 入口），DSH 另需 dsh JS 入口；各项均为 {path,sha256}。DSH directory 必须等于 profiles/<profile>。必须授权 install/write/run，并明确承认 HOST_TRUSTED；目录须落入 write_roots。

执行顺序：身份检查 → 单次 Plus 生成计划要求的结果 → 结果验收 → broker 安装 → 原用量回执和独立 REVIEW。Plus 不重复运行包管理器。安装使用精确版本、公有 npm registry、禁用生命周期脚本；DSH 额外禁用 pnpmfile hooks。需要构建脚本的插件可能尚不能使用，应另行审查具体脚本；不自动放行。不修改自动模式默认值或重启 DSH。

安装前仅备份 package.json、package-lock.json、pnpm-lock.yaml、pnpm-workspace.yaml 到本任务 package-installs/<ID>/before，并记录哈希；不读取凭据。项目 .npmrc 存在时阻止操作，不能静默继承认证/端点。运行环境移除包管理器/Node 注入配置及常见秘密变量，不记录环境值或原始进程日志。顶层包版本固定，但传递依赖仍由包管理器解析，生成锁文件需独立审查；registry 参数和提示词不是网络硬隔离。

安装失败保留备份、退出码及已观察用量，不自动覆盖回滚或重新安装。超时/取消若进程仍活跃，记录 RECOVERY_REQUIRED，可能存在子进程，阻止新派单直到本机核查。恢复应在确认没有在途进程后人工比较 before/after 和当前文件；恢复清单不等于回滚 node_modules，不能盲目复制清单后宣称完整回滚。

`VERIFIED_INSTALLED` 仅表示项目清单和实际包元数据版本匹配，`activation=NOT_VERIFIED`；它不表示 UI、索引、默认权限或重启已验收。应用/配置/服务和既有补丁回归由后续限定任务完成。Full Access 不提供目录或网络沙箱。

## English

Plans can request `package_installs: ["saved-ID"]`. The web caller cannot supply commands, package specifications, URLs or permission overrides. Local grants pin package names/versions, destination, runtime entry paths and SHA-256 hashes. Both installation and write/run permissions in acknowledged host-trusted mode are required.

Use the local `tools/prepare_dsh_grant.py` helper with your actual root, OAuth subject, Node, DSH and pnpm paths. `--external-idle-ack` records the owner's concurrency confirmation; `--apply` saves the grant. It does not start a service, install a package, or dispatch a model task. The bundled proposal pins exactly the three versions listed above.

The existing single Plus task produces its results first. After validation, the broker executes installation once, with lifecycle scripts disabled, and then returns action evidence alongside the original usage receipt. DSH additionally disables pnpmfile hooks. No automatic service restart or change to default permission mode occurs.

Selected package manifests are backed up and hashed before mutation. A project .npmrc blocks execution without being read. Runtime environment injection is stripped; no raw credentials or subprocess output is recorded. Public npm registry selection is not a firewall, and transitive dependency resolution still needs lockfile review.

Failures retain backups, exit status and observed usage. An uncertain live process means RECOVERY_REQUIRED, never automatic retry. Review child processes and file changes before any manual restore; restoring a manifest alone does not restore node_modules. VERIFIED_INSTALLED means matching package metadata, not activation or independent REVIEW. The adapter is tested with real synthetic local processes; production plugin installation remains a separate task.

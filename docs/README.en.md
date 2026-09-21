# PCC — Plus Codex Collaboration

[简体中文](../README.md) | **English**

PCC lets the current ChatGPT conversation plan and review a task while a local, OAuth-authenticated controller dispatches a separately authenticated Plus Codex CLI. The controller preserves project permissions, prevents duplicate dispatch, and returns durable results and usage receipts.

**This is a self-hosted tool, not a shared account or a hosted execution service.** Bring your own official CLI, ChatGPT accounts, OAuth provider, and HTTPS endpoint. PCC does not pool quotas, rotate accounts automatically, or use a model API key.

## Status and security boundary

- Windows 11 and Python 3.12 were used for validation. Python 3.11+ is the intended runtime.
- The reference CLI was version 0.153.4, using `default_permissions=":danger-full-access"`. Other versions must pass the native help/configuration checks before execution.
- `PCC_HOST_TRUSTED` means Windows host execution with Full Access. **It does not provide strict filesystem or network isolation.** Project grants and broker validation are application controls, not an operating-system sandbox.
- The legacy strict mode retains its fail-closed checks. The undeployed virtual-machine adapter is not advertised as operational.
- Installation starts with dispatch paused and no project grants.
- The reference deployment demonstrated a local synthetic Plus run, OAuth connection, discovery of seven MCP tools, and a real read-only ChatGPT capability call. A complete web dispatch/result/usage/independent-review loop is **not yet verified**.
- This portable snapshot passed **131 non-model tests**, with **one skipped** due to unavailable Windows symbolic-link privileges. Junction tests ran separately. No Plus model request was made to validate this release.

## Install without starting a model task

Clone into a separate tool directory and run PowerShell:

```powershell
git clone https://github.com/ttacit666-eng/pcc-codex.git
cd pcc-codex
.\install.ps1 -Python 'C:\Tools\Python312\python.exe'
.\.venv\Scripts\python.exe tools\doctor.py
.\.venv\Scripts\python.exe -m pytest tests vendor/tests -q
```

Replace the Python path with your actual installation; omit `-Python` if the Windows Python Launcher is available. Installation creates only the repository's virtual environment and paused state. It does not install Codex, change global PATH/Profile, or modify any login.

Configure three non-secret paths:

```powershell
.\.venv\Scripts\python.exe tools\configure.py --cli 'C:\Tools\Codex\codex.exe' --plus-home "$env:USERPROFILE\.codex-plus" --controller-home "$env:USERPROFILE\.codex"
.\.venv\Scripts\python.exe tools\doctor.py --native
```

Use the **actual controller CODEX_HOME**, which may differ from `.codex`. The two homes must be separate and non-nested. Configuration is written once to ignored `config/runtime.local.json`; an existing file is not overwritten. Process-local `PCC_CODEX_CLI`, `PCC_PLUS_HOME`, and `PCC_CONTROLLER_HOME` overrides are also supported. Do not use `setx`.

Complete Plus authentication yourself using the official CLI in a dedicated terminal whose CODEX_HOME points to the Plus directory. Follow the installed version's `login --help`. Never copy credentials between homes. PCC uses official App Server account snapshots and requires visible Plus identity distinct from the controller identity before dispatch.

## Connect ChatGPT and approve a project

Follow the [English deployment guide](DEPLOYMENT.en.md) for OAuth, HTTPS, project grants, acceptance checks, and rollback. The [Chinese guide](DEPLOYMENT.md) covers the same workflow.

`plugins/pcc` contains a standard skill plugin. Installing the skill does **not** register an MCP server or approve a project. Missing real PCC tools must be reported as `NOT_CONNECTED`.

In a connected conversation, enter:

```text
<进行pcc协作模式>
Use the authorized smoke project. Read the synthetic CSV and generate its summary.
Execute once and return the result, measured usage, and an independent review.
```

The phrase alone checks readiness without dispatch. The controller creates task/run IDs. After a timeout, query the existing task instead of submitting it again.

## Features

| Feature | Implementation |
|---|---|
| Saved project permission | Locally approved, versioned, revocable grants; web callers cannot expand them |
| One executor per task | SQLite registry, idempotency keys, directory conflict checks |
| Real execution evidence | Plus-generated files, native events, output and frozen-input hashes |
| Broker operations | Result publishing, recoverable deletion, hash-pinned offline wheel installation, exact-target upload/download |
| Usage receipts | App Server snapshots and `codex exec --json` events; cached input is not counted twice |
| Review | LOCAL_CHECK remains separate from independent conversation REVIEW |

Token totals are not quota percentages. Rate-limit deltas are before/after observations, not exact task billing. Missing data stays missing, and parallel activity/reset windows are flagged. Failed tasks retain their usage; sampling failures never trigger model retries.

Dependency support is an approved offline wheel workflow, not unrestricted online package installation. Upload support is not a general SSH/HPC executor. Full Access must only be used for trusted, explicitly authorized tasks. Read [SECURITY.md](../SECURITY.md).

## Administration and outputs

```powershell
.\pcc.cmd grant config\project.json
.\pcc.cmd capabilities 'your-OAuth-subject'
.\pcc.cmd revoke smoke
.\pcc.cmd pause
.\pcc.cmd resume
.\pcc.cmd serve config\deployment.json
```

Resume only after local approval and configuration checks. The server listens on `127.0.0.1:8876`; it does not register a startup service. Do not steal an existing service lock.

Durable outputs live under `state/runs/<run_id>/`: results, native evidence, frozen artifacts, `usage.json`, and `receipt.md`; `state/usage-index.json` indexes usage history. These may contain task content. **Never publish state, jobs, evidence, credentials, or real deployment configuration.**

Run `python tools/release_check.py` and `python tools/build_release.py` to create a source-only ZIP, manifest, and SHA-256 checksum under `dist/`. GitHub Actions repeats non-model checks and packages the source. Packaging is not deployment acceptance.

## License

MIT. Sharing this repository shares source code, not the author's accounts, quotas, tenant, or computer access.

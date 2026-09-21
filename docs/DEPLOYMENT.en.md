# Self-hosted deployment and acceptance

[简体中文](DEPLOYMENT.md) | **English**

## 1. Prepare the local controller

Complete installation, non-secret path configuration, and non-model tests from the [English README](README.en.md). Use your own accounts and endpoints. Run the controller from a dedicated tool directory, not a business project root. Keep one controller instance and avoid overlapping external execution on the same project.

Install the official CLI separately and verify its source/version. Complete the independent Plus login personally without signing out the controller or copying authentication files. Official App Server `account/read` and `account/rateLimits/read` calls collect status only. Dispatch requires a visible Plus plan and an account identity distinct from the controller; missing identity blocks dispatch.

## 2. Configure OAuth and HTTPS

Copy `config/deployment.example.json` to ignored `config/deployment.json`. Set your issuer, JWKS URL, HTTPS resource and allowed client IDs. Leave `deployment_enabled=false` until the local owner has approved the deployment.

Use an OAuth provider supporting authorization code, PKCE, refresh credentials and RS256 signatures. The resource/audience must exactly match your HTTPS `/mcp` URL. The operation scope is `pcc:operate`. The MCP resource server is not itself an authorization server. Enter client secrets only in the provider's and ChatGPT's official configuration interfaces, never in source control.

For Auth0, use your HTTPS `/mcp` as the API Identifier, authorize `pcc:operate`, and enable Offline Access/Refresh Token when needed. Add the **actual callback URL displayed by ChatGPT** to the application's allowed callbacks. Strict `tpc_` third-party clients do not provide OIDC ID tokens/UserInfo: use OAuth-only for that client type, with OIDC disabled and offline_access as needed. Match other client types to their documented capabilities.

`jwt_clock_skew_seconds` defaults to zero. With measured clock-skew evidence, the local owner may explicitly configure 0–180 seconds for future iat/nbf values. Expired exp is still rejected against the current local clock. This does not synchronize the computer's clock.

Start `pcc.cmd serve config/deployment.json` on loopback, then use your approved HTTPS reverse proxy. Keep JWT verification enabled; do not expose an unauthenticated execution endpoint. Keep any proxy credentials outside the repository. PCC does not start or modify existing tunnels.

In ChatGPT's custom MCP/app interface, enter your HTTPS endpoint and select OAuth. UI labels may change. Verify unauthenticated access returns 401, an authorized account discovers seven tools, and write operations are labeled accurately. Perform a real read-only `pcc_capabilities` call before granting a project.

## 3. Approve one limited synthetic project

Copy `config/project.example.json` to ignored `config/project.json`. Specify the project ID, authenticated OAuth subject, root, actions, and read/write subdirectories. Host-trusted mode requires both `execution_mode=PCC_HOST_TRUSTED` and `host_trusted_ack=true`. `external_idle_ack` records the owner's confirmation that no conflicting external executor is using the project; it cannot stop other processes.

Start with a new synthetic CSV containing `step,value` and four rows from `1,10` to `4,40`. Grant read access only to input and publishing only to results. Initially omit dependency installation, deletion and external upload targets. `synthetic=true` is not general network authorization: it permits only explicitly declared synthetic loopback targets.

Run `pcc.cmd grant config/project.json` locally. This command is not exposed through MCP. Grants are versioned; an example file is not approval. Web callers cannot override execution mode or send `approved=true` to gain permission. Never grant the user home, controller state, or authentication directories.

## 4. Verify the conversation loop

The skill at `plugins/pcc/skills/pcc/SKILL.md` can be supplied to a compatible client or used as conversation instructions. It cannot register a web connection by itself. With no actual PCC tools, report `NOT_CONNECTED`.

The conversation calls `pcc_capabilities`, then `pcc_project`, and constructs a bounded goal/plan for `pcc_submit`. Preserve the request label and query `pcc_status` after timeout; never change labels to retry a possibly running task. Retrieve `pcc_result`, then read necessary frozen files with `pcc_artifact` for independent review.

Expected CSV results: count 4, sum 100, min 10, max 40, mean 25, plus the actual input SHA-256. Verify input preservation, execution ownership, and output hashes. LOCAL_CHECK is not independent REVIEW. Token counts do not map to fixed subscription percentages, and quota deltas are observations rather than exact billing.

## 5. Revoke and uninstall

1. `pcc.cmd pause` prevents new dispatch; it does not terminate existing child processes. Query the original task and use its cancellation mechanism when needed. Never blindly retry UNKNOWN/RECOVERY_REQUIRED tasks or delete their locks.
2. `pcc.cmd revoke <project>` revokes the project while preserving evidence and usage.
3. Confirm the process associated with `state/service.lock` and that no tasks remain active. Stop only this PCC server and its dedicated proxy.
4. Remove only the added PCC connection in ChatGPT and revoke its corresponding OAuth client through the provider.
5. Export evidence you need, then move/remove this repository and its virtual environment. Do not delete Pro/Plus homes, existing CWC, or other projects. Recoverable broker deletion can be restored with `pcc.cmd restore <subject> <task> <path>` only when a terminal task and its matching record exist.

## References

- [Codex authentication](https://developers.openai.com/codex/auth)
- [Codex permissions](https://developers.openai.com/codex/permissions)
- [Non-interactive Codex](https://developers.openai.com/codex/noninteractive/)
- [Plugin authentication](https://developers.openai.com/plugins/build/auth)
- [Auth0 third-party application security controls](https://auth0.com/docs/get-started/applications/third-party-applications/security-controls)
- [PyJWT usage](https://pyjwt.readthedocs.io/en/stable/usage.html)

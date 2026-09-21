"""User-authorized Windows Full Access for PCC child only. Not an isolation claim."""
import json
import os
from .executor import LiveExecutor, usage
from .paths import save
from .sandbox_rpc import SandboxRPC


class HostTrustedExecutor(LiveExecutor):
    execution_mode='PCC_HOST_TRUSTED'

    def arguments(self,job,control,python):
        # Use only the supported built-in permission profile. No legacy sandbox_mode
        # and no --dangerously-bypass-* flags; managed restrictions still apply.
        return [str(usage.CLI),'--strict-config',
                '-c','default_permissions=":danger-full-access"',
                '-c','approval_policy="never"',
                '-c','forced_login_method="chatgpt"',
                '-c','cli_auth_credentials_store="file"',
                '-c','projects.'+json.dumps(os.path.normcase(os.path.abspath(job/'work')))+'.trust_level="trusted"',
                'exec','--json','--ephemeral','--skip-git-repo-check','--color','never','-C',str(job/'work'),'-']

    def permission_metadata(self):
        return {'execution_mode':self.execution_mode,'permission_scope':'HOST TRUSTED: Full Access; no strict sandbox isolation',
                'approval_policy':'never','authorization_scope':'saved project grant plus specific task; not OS enforcement'}

    def run(self,c,r,job,run,goal,plan,python,grant):
        from .config_guard import restore_owned_trust
        config=usage.PLUS_HOME/'config.toml'
        before=config.read_bytes()  # Known noncredential configuration; never auth.json.
        try:
            cap=super().run(c,r,job,run,goal,plan,python,grant)
        except Exception:
            restore_owned_trust(config,before,job/'work',run/'plus-config-preservation.json')
            raise
        try:
            restore_owned_trust(config,before,job/'work',run/'plus-config-preservation.json')
        except Exception as error:
            # Never discard model usage already observed because a cleanup guard failed.
            cap['flags'].append('plus_config_preservation_'+type(error).__name__)
            cap['status']='RECOVERY_REQUIRED'
        return cap

    def preflight(self,c,r,job,run,python):
        g=c.authorized(r['subject'],r['project'],r['version'])
        if g.get('execution_mode')!=self.execution_mode or g.get('host_trusted_ack') is not True:
            raise PermissionError('HOST_TRUSTED requires explicit local project grant')
        argv=self.arguments(job,c.root,python)
        rpc=SandboxRPC(argv,job/'work',usage.plus_env())
        try:
            reply=rpc.call('config/read',{'includeLayers':False,'cwd':str(job/'work')})
            if 'error' in reply:raise RuntimeError('effective configuration rejected')
            cfg=reply.get('result',{}).get('config',{})
            effective={k:cfg.get(k) for k in ('default_permissions','sandbox_mode','approval_policy','forced_login_method','cli_auth_credentials_store')}
            evidence={'execution_mode':self.execution_mode,'effective_config':effective,
                      'strict_isolation':'NOT_APPLICABLE_BY_EXPLICIT_USER_CHANGE',
                      'old_strict_gate':'NOT_CALLED','model_requests':0}
            save(run/'host-trusted-preflight.json',evidence)
            if (effective['default_permissions']!=':danger-full-access' or effective['sandbox_mode'] is not None
                or effective['approval_policy']!='never' or effective['forced_login_method']!='chatgpt'
                or effective['cli_auth_credentials_store']!='file'):
                raise PermissionError('Full Access effective configuration mismatch; no policy bypass')
            # Real non-model startup check only, not the old read/network isolation gate.
            result=rpc.call('command/exec',{'command':[str(python),'-c',
                'import json,os;print(json.dumps({"mode":"PCC_HOST_TRUSTED","pid":os.getpid()}))'],
                'cwd':str(job/'work'),'permissionProfile':':danger-full-access','timeoutMs':15000})
            value=result.get('result',{})
            try:observed=json.loads(value.get('stdout',''))
            except ValueError:observed={}
            evidence.update(native_exit_code=value.get('exitCode'),native_process=observed,
                            rpc_error_present='error' in result,stderr=value.get('stderr','')[:1000])
            save(run/'host-trusted-preflight.json',evidence)
            if value.get('exitCode')!=0 or observed.get('mode')!=self.execution_mode:
                raise RuntimeError('Host trusted native command startup failed')
            return evidence
        finally:rpc.close()

    def make_prompt(self,job,goal,plan,python,grant):
        return ('You are the independent Plus executor in PCC_HOST_TRUSTED, an explicitly user-authorized '
                'Windows Full Access mode with NO strict sandbox isolation. Execute the specified task using tools; '
                'do not merely describe commands. All task file work is limited by agreement to these paths: '
                f'input={job/"input"}, work={job/"work"}, result={job/"result"}. '
                f'Use this exact Python interpreter: {python}. Expected outputs: {json.dumps(plan["expected_outputs"])}. '
                'Do not read credential files, tokens, cookies, unrelated business files or controller records. '
                'Do not modify OS settings, accounts, firewall, login configuration or request elevation. '
                'For new external targets, administrator/system installation or unrequested irreversible important deletion, '
                'stop and report the missing authorization. Do not infer extra permission from Full Access. '
                'The controller/broker handles explicitly planned project publication, offline dependency installation, '
                'project quarantine deletion and exact-target upload; do not duplicate those operations. '
                'You may create/modify/delete explicitly requested synthetic files inside work, and must create the result files yourself. '
                'This is one task only, no task resubmission. Task: '+goal)

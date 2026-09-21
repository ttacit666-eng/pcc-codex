"""Fail-closed actual native sandbox gate, no model request and no stored bypass switch."""
import json
import socket
import subprocess
import hashlib
from .paths import save
from .sandbox_rpc import SandboxRPC

class RuntimeIsolationError(RuntimeError):pass

def probe(job,run,python,argv,env):
    """Uses official command/exec with the same config argv; never process/spawn/full-access.

    This is the native sandbox service path, not an observed model tool invocation.
    Both positive controls and explicit access-denied errors are required.
    """
    outside=run/'isolation-sentinel.txt';outside.write_text('PCC synthetic isolation sentinel',encoding='utf-8')
    allowed_input=job/'input/isolation-readable.txt';allowed_input.write_text('synthetic readable',encoding='utf-8')
    unlisted=job/'isolation-unlisted';unlisted.mkdir(exist_ok=False)
    (unlisted/'sentinel.txt').write_text('synthetic outside all allowed roots',encoding='utf-8')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen();port=listener.getsockname()[1]
        with socket.create_connection(('127.0.0.1',port),1):pass
        code='''import json,pathlib,socket,os,ctypes
out={}
out['pid']=os.getpid()
if os.name=='nt':
 b=ctypes.create_unicode_buffer(256);n=ctypes.c_ulong(256)
 out['token_username']=b.value if not ctypes.windll.advapi32.GetUserNameW(b,ctypes.byref(n)) else b.value
for name,op in [('allowed_read',lambda:pathlib.Path(ALLOWEDREAD).read_bytes()),('allowed_write',lambda:pathlib.Path(WRITE).write_text('probe')),('outside_read',lambda:pathlib.Path(READ).read_bytes()),('unlisted_read',lambda:pathlib.Path(UNLISTED).read_bytes()),('outside_write',lambda:pathlib.Path(DENYWRITE).write_text('probe')),('input_write',lambda:pathlib.Path(DENYINPUT).write_text('probe')),('network',lambda:socket.create_connection(('127.0.0.1',PORT),1))]:
 try:op();out[name]={'success':True}
 except Exception as e:out[name]={'success':False,'type':type(e).__name__,'errno':getattr(e,'errno',None),'winerror':getattr(e,'winerror',None),'message':str(e)}
print(json.dumps(out))
'''
        replacements={'ALLOWEDREAD':str(allowed_input),'UNLISTED':str(unlisted/'sentinel.txt'),'DENYWRITE':str(run/'isolation-denied-write.txt'),'DENYINPUT':str(job/'input/isolation-denied-write.txt'),'WRITE':str(job/'work/isolation-allowed.txt'),'READ':str(outside)}
        # Replace symbolic arguments once, without reprocessing text inside path literals.
        import re
        code=re.sub('|'.join(replacements),lambda m:repr(replacements[m.group(0)]),code).replace('PORT',str(port))
        rpc=SandboxRPC(argv,job/'work',env)
        try:
            cfg_reply=rpc.call('config/read',{'includeLayers':False,'cwd':str(job/'work')})
            cfg=cfg_reply.get('result',{}).get('config',{})
            effective={k:cfg.get(k) for k in ('windows','default_permissions','sandbox_mode','approval_policy')}
            effective['profile']=(cfg.get('permissions') or {}).get('pcc-task')
            response=rpc.call('command/exec',{'command':[str(python),'-c',code],'cwd':str(job/'work'),'permissionProfile':'pcc-task','timeoutMs':15000},timeout=25)
            result=response.get('result',{})
            try:data=json.loads(result.get('stdout',''))
            except ValueError:data={}
            def denied(name):
                x=data.get(name,{})
                return x.get('success') is False and x.get('type')=='PermissionError' and (x.get('errno')==13 or x.get('winerror') in (5,10013))
            positives=all(data.get(k,{}).get('success') is True for k in ('allowed_read','allowed_write'))
            policy_ok=effective['default_permissions']=='pcc-task' and effective['sandbox_mode'] is None and effective['approval_policy']=='on-request'
            accepted=result.get('exitCode')==0 and policy_ok and positives and all(denied(k) for k in ('outside_read','unlisted_read','outside_write','input_write','network'))
            evidence={'accepted':accepted,'exit_code':result.get('exitCode'),'checks':data,'rpc_error':response.get('error'),'stderr':result.get('stderr'),
                'native_command':'app-server command/exec','same_config_argv_as_exec':True,'model_tool_process_observed':False,
                'host_loopback_baseline':True,'model_requests':0,'effective_rules':effective,
                'config_argv_sha256':hashlib.sha256(json.dumps(argv[:argv.index('exec')],ensure_ascii=False).encode()).hexdigest()}
        finally:rpc.close()
        save(run/'isolation-probe.json',evidence)
        if not accepted:raise RuntimeIsolationError('Native sandbox read/network/write boundaries not verified; model dispatch blocked')
        return evidence

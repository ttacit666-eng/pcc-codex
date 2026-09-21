"""Non-model native command probe transport. No login/setup/thread/turn/process-spawn API."""
import json,queue,subprocess,threading,time
from .execution_evidence import scrub

ALLOWED={'initialize','config/read','permissionProfile/list','command/exec'}
class SandboxRPC:
    def __init__(self,argv,cwd,env):
        prefix=argv[:argv.index('exec')]
        self.argv=prefix+['app-server','--stdio'];self.q=queue.Queue();self.errors=[];self.seq=0
        self.p=subprocess.Popen(self.argv,cwd=cwd,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace')
        def read():
            for line in self.p.stdout:
                try:self.q.put(json.loads(line))
                except ValueError:continue
            self.q.put(None)
        def err():
            for line in self.p.stderr:
                # Only known sandbox/config diagnostics, bounded. Never raw authentication output.
                if any(s in line.lower() for s in ('sandbox','permission profile','config')):
                    self.errors.append(scrub(line)[:500])
                    self.errors=self.errors[-20:]
        threading.Thread(target=read,daemon=True).start();threading.Thread(target=err,daemon=True).start()
        self.call('initialize',{'clientInfo':{'name':'pcc_permission_probe','version':'0.2'},'capabilities':{'experimentalApi':True}})
        self.p.stdin.write('{"method":"initialized","params":{}}\n');self.p.stdin.flush()
    def call(self,method,params,timeout=40):
        if method not in ALLOWED:raise ValueError('RPC not permitted')
        self.seq+=1;i=self.seq;self.p.stdin.write(json.dumps({'id':i,'method':method,'params':params})+'\n');self.p.stdin.flush();deadline=time.monotonic()+timeout
        while True:
            msg=self.q.get(timeout=max(.01,deadline-time.monotonic()))
            if msg is None:raise RuntimeError('native server exited')
            if msg.get('id')==i and ('result' in msg or 'error' in msg):return msg
            if 'id' in msg and 'method' in msg:
                self.p.stdin.write(json.dumps({'id':msg['id'],'error':{'code':-32601,'message':'PCC probe does not approve escalation'}})+'\n');self.p.stdin.flush()
            if time.monotonic()>=deadline:raise TimeoutError(method)
    def close(self):
        self.p.stdin.close()
        try:self.p.wait(timeout=3)
        except subprocess.TimeoutExpired:self.p.terminate();self.p.wait(timeout=5)

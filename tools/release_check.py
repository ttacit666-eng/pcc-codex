"""Check only allowlisted source; never open local credentials or deployment state."""
from pathlib import Path
import json
import re
import subprocess
import os

ROOT=Path(__file__).resolve().parents[1]
TOP={'.gitignore','README.md','LICENSE','SECURITY.md','CONTRIBUTING.md','requirements.lock.txt','install.ps1','pcc.cmd','pytest.ini'}
DIRS={'pcc','tests','tools','plugins','vendor','docs','.github','isolation'}
EXT={'.py','.md','.json','.yml','.yaml'}
RULES={
    'personal_windows_home':re.compile(r'[A-Za-z]:[/\\]Users[/\\](?!example(?:[/\\]|[\"\']))[^/\\\s\"\']+[/\\]',re.I),
    'personal_auth0_tenant':re.compile(r'dev-[a-z0-9]{8,}\.(?:us|eu|au)\.auth0\.com'),
    'personal_ngrok_endpoint':re.compile(r'https://[a-z0-9-]+\.ngrok-free\.(?:dev|app)'),
    'private_key_pem':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github_token':re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})'),
    'jwt_literal':re.compile(r'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'),
}

def source_files(root=ROOT):
    files=[]
    candidates=[]
    for directory, dirs, names in os.walk(root, followlinks=False):
        base=Path(directory)
        dirs[:]=[name for name in dirs if name not in ('__pycache__','state','jobs','evidence','verification','dist','node_modules') and (not name.startswith('.') or name in ('.github','.codex-plugin')) and (base!=root or name in DIRS|{'config'})]
        for name in dirs:
            child=base/name
            if child.is_symlink() or getattr(child.lstat(),'st_file_attributes',0)&0x400:
                raise ValueError('Source contains a reparse point')
        candidates.extend(base/name for name in names)
    for p in candidates:
        rel=p.relative_to(root)
        if any(x.startswith('.') and x not in ('.github','.codex-plugin','.gitignore') for x in rel.parts):continue
        if any(x in ('__pycache__','state','jobs','evidence','verification','dist','node_modules') for x in rel.parts):continue
        allowed=(len(rel.parts)==1 and p.name in TOP) or (rel.parts[0] in DIRS and p.suffix in EXT) or (rel.parts[0]=='config' and p.name.endswith('.example.json'))
        if not allowed:continue
        if p.is_symlink() or getattr(p.lstat(),'st_file_attributes',0)&0x400:raise ValueError('Source contains a reparse point')
        if p.is_file():files.append(p)
    return sorted(files)

def violations(text):
    return [name for name,pattern in RULES.items() if pattern.search(text)]

def check(root=ROOT):
    files=source_files(root);errors=[]
    for p in files:
        for name in violations(p.read_text(encoding='utf-8')):errors.append({'file':p.relative_to(root).as_posix(),'rule':name})
    if (root/'.git').exists():
        result=subprocess.run(['git','ls-files','-z'],cwd=root,capture_output=True,check=True)
        allowed={p.relative_to(root).as_posix() for p in files}
        for path in result.stdout.decode('utf-8').split('\0'):
            if path and path not in allowed:errors.append({'file':path,'rule':'tracked_file_outside_release_allowlist'})
    return {'source_file_count':len(files),'errors':errors,'credential_files_read':False}

if __name__=='__main__':
    report=check();print(json.dumps(report,indent=2));raise SystemExit(bool(report['errors']))

import hashlib
import json
import os
import stat
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]

def plain(path):
    p=Path(os.path.abspath(path))
    for part in (p,*p.parents):
        if part.exists() or part.is_symlink():
            s=part.lstat()
            if stat.S_ISLNK(s.st_mode) or getattr(s,'st_file_attributes',0)&0x400:
                raise ValueError('symlink/reparse path rejected')
            if stat.S_ISREG(s.st_mode) and s.st_nlink>1:
                raise ValueError('hard-linked file rejected')
    return p

def inside(root,rel):
    root=plain(root)
    if not isinstance(rel,str) or not rel or '\\' in rel or ':' in rel:
        raise ValueError('relative slash-separated path required')
    q=Path(rel)
    if q.is_absolute() or any(x in ('..','.') for x in rel.split('/')) or any(not x or x.endswith((' ','.')) for x in rel.split('/')):
        raise ValueError('unsafe relative path')
    p=plain(root/q)
    if root not in p.parents:raise ValueError('path outside root')
    return p

def allowed(rel,roots):
    x=Path(rel)
    return any(x==Path(a) or Path(a) in x.parents for a in roots)

def digest(path):
    return hashlib.sha256(plain(path).read_bytes()).hexdigest()

def save(path,value):
    p=plain(path);p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_name(p.name+'.tmp-'+os.urandom(8).hex())
    with tmp.open('x',encoding='utf-8',newline='\n') as f:
        json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(tmp,p)

def load(path):return json.loads(plain(path).read_text(encoding='utf-8'))

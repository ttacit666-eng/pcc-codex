"""Build a source-only ZIP using the same reviewed allowlist as release checks."""
import hashlib
import json
from pathlib import Path
import zipfile
from release_check import ROOT,check,source_files

def build():
    report=check()
    if report['errors']:raise RuntimeError('Release check failed; inspect filenames/rules with release_check.py')
    out=ROOT/'dist';out.mkdir(exist_ok=True)
    version=json.loads((ROOT/'plugins/pcc/.codex-plugin/plugin.json').read_text(encoding='utf-8'))['version']
    archive=out/('pcc-codex-'+version+'.zip')
    manifest=[]
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for p in source_files():
            rel=p.relative_to(ROOT).as_posix();data=p.read_bytes()
            info=zipfile.ZipInfo('pcc-codex/'+rel,date_time=(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            z.writestr(info,data);manifest.append({'path':rel,'sha256':hashlib.sha256(data).hexdigest()})
    (out/'source-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    (out/'SHA256SUMS').write_text(digest+'  '+archive.name+'\n',encoding='utf-8')
    print(json.dumps({'archive':str(archive),'sha256':digest,'files':len(manifest)}))

if __name__=='__main__':build()

import concurrent.futures
import hashlib
import http.server
import json
from pathlib import Path
import threading
import zipfile
import pytest
from pcc.controller import Controller
from pcc.broker import Broker,validate_plan
from pcc.paths import inside,plain,load

def wheel(root):
    p=root/'wheels/pcc_fixture-1.0-py3-none-any.whl';p.parent.mkdir(parents=True)
    with zipfile.ZipFile(p,'w') as z:
        z.writestr('pcc_fixture/__init__.py','VALUE = 7\n')
        z.writestr('pcc_fixture-1.0.dist-info/METADATA','Metadata-Version: 2.1\nName: pcc-fixture\nVersion: 1.0\n')
        z.writestr('pcc_fixture-1.0.dist-info/WHEEL','Wheel-Version: 1.0\nGenerator: PCC synthetic fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        z.writestr('pcc_fixture-1.0.dist-info/RECORD','')
    return {'name':'pcc-fixture','path':p.relative_to(root).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}

def grant(root):
    root.mkdir(parents=True,exist_ok=True)
    (root/'input').mkdir(exist_ok=True);(root/'input/data.csv').write_text('step,value\n1,10\n2,20\n3,30\n4,40\n',encoding='utf-8')
    return {'project':'synthetic','subject':'owner','root':str(root),'actions':['read','write','run','delete','install','upload'],
            'read_roots':['input'],'write_roots':['results'],'delete_roots':['temp'],'wheels':[],'upload_targets':[],
            'external_idle_ack':True,'synthetic':True,'timeout_seconds':120}

def plan():return {'read_files':['input/data.csv'],'expected_outputs':['summary.json'],'publish':[{'artifact':'summary.json','destination':'results/summary.json'}]}

def sample():
    return {role:{'account':{'type':'chatgpt','planType':'plus' if role=='plus_executor' else 'prolite','identity_sha256':role},
            'account_status':'ok','rate_limits_status':'unavailable'} for role in ('plus_executor','pro_controller')}

class Fake:
    calls=0
    def version(self):return 'synthetic'
    def sample(self,cwd):return sample()
    def run(self,c,r,job,run,goal,p,python,g):
        self.calls+=1;c.update(r['id'],'RUNNING',999999)
        (job/'result/summary.json').write_text('{"count":4,"sum":100,"min":10,"max":40,"mean":25}',encoding='utf-8')
        return {'source_kind':'synthetic','status':'completed','pid':999999,'exit_code':0,'flags':[],
            'events':[{'source_seq':1,'type':'thread.started','session_id':'synthetic-session'},
                      {'source_seq':2,'type':'turn.started'},
                      {'source_seq':3,'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':80,'output_tokens':20}}]}

@pytest.fixture
def setup(tmp_path):
    c=Controller(tmp_path/'control');g=grant(tmp_path/'project');v=c.grant(g)['version'];return c,g,v

def test_duplicate_concurrent_submit_and_claim(setup):
    c,g,v=setup
    def submit(_):return c.submit('owner','synthetic',v,'sum',plan())['id']
    with concurrent.futures.ThreadPoolExecutor(2) as pool:ids=list(pool.map(submit,range(2)))
    assert ids[0]==ids[1]
    fake=Fake();c.execute(ids[0],fake)
    with pytest.raises(RuntimeError):c.execute(ids[0],fake)
    assert fake.calls==1
    assert c.submit('owner','synthetic',v,'sum',plan())['id']==ids[0]

def test_marker_only_no_task(setup):
    c,g,v=setup;assert c.submit('owner','synthetic',v,'<进行pcc协作模式>',{})['dispatched'] is False

def test_account_serial_and_unknown_never_reclaim(setup):
    c,g,v=setup;r=c.submit('owner','synthetic',v,'first',plan());c.claim(r['id']);c.reconcile()
    assert c.status('owner',r['id'])['status']=='RECOVERY_REQUIRED'
    with pytest.raises(RuntimeError):c.submit('owner','synthetic',v,'second',plan())

def test_subject_revoke_and_version(setup):
    c,g,v=setup;r=c.submit('owner','synthetic',v,'first',plan())
    with pytest.raises(PermissionError):c.status('other',r['id'])
    c.revoke('synthetic')
    with pytest.raises(PermissionError):c.result('owner',r['id'])
    v2=c.grant(g)['version'];assert v2==v+1
    with pytest.raises(PermissionError):c.submit('owner','synthetic',v,'x',plan())

@pytest.mark.parametrize('rel',['../escape','input/../../escape','C:/Users/example','/outside','input\\foo','input/a:stream','input/a.'])
def test_path_attacks(setup,rel):
    c,g,v=setup
    with pytest.raises(ValueError):inside(g['root'],rel)

def test_policy_denials(setup):
    c,g,v=setup
    for p in ({'read_files':['outside']},{'delete':['input/data.csv']},{'publish':[{'artifact':'x','destination':'input/data.csv'}]},
              {'dependencies':['unapproved']},{'uploads':[{'artifact':'x','target':'other'}]},{'allow_all':True}):
        with pytest.raises((ValueError,PermissionError)):validate_plan(g,p)

def test_external_write_conflict(setup):
    c,g,v=setup;r=c.submit('owner','synthetic',v,'x',plan())
    class External(Fake):
        def run(self,*a):
            out=super().run(*a);p=Path(g['root'])/'results';p.mkdir();(p/'summary.json').write_text('external');return out
    c.execute(r['id'],External());assert (Path(g['root'])/'results/summary.json').read_text()=='external'
    assert c.status('owner',r['id'])['status']=='FAILED'

def test_synthetic_brokers_and_restore(setup):
    c,g,v=setup;root=Path(g['root']);(root/'temp').mkdir();(root/'temp/old.txt').write_text('recoverable')
    g['wheels']=[wheel(root)];received=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_PUT(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])));self.send_response(201);self.end_headers()
        def log_message(self,*a):pass
    server=http.server.HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        g['upload_targets']=[{'id':'local','url':f'http://127.0.0.1:{server.server_port}/synthetic','artifact_patterns':['summary.json'],'max_bytes':2048}]
        v=c.grant(g)['version'];p={**plan(),'delete':['temp/old.txt'],'dependencies':['pcc-fixture'],'uploads':[{'artifact':'summary.json','target':'local'}]}
        r=c.submit('owner','synthetic',v,'sum and operations',p);out=c.execute(r['id'],Fake())
        assert out['status']=='LOCAL_CHECK';assert len(received)==1;assert json.loads(received[0])['sum']==100
        assert not (root/'temp/old.txt').exists()
        row=c.row('owner',r['id']);b=Broker(c,row,c.authorized('owner','synthetic'),c.root/'runs'/row['run_id']);b.restore('temp/old.txt')
        assert (root/'temp/old.txt').read_text()=='recoverable'
        result=c.result('owner',r['id']);assert result['usage']['roles']['plus_executor']['tokens']['totals']['input_plus_output_tokens']==120
        assert result['usage']['source_kind']=='synthetic';assert result['independent_review']=='REVIEW_REQUIRED'
        assert c.artifact('owner',r['id'],'summary.json',0,4)['truncated']
        with pytest.raises(PermissionError):c.artifact('owner',r['id'],'../auth.json')
    finally:server.shutdown();server.server_close();thread.join()

def test_receipt_after_failed_sample_no_dispatch(setup):
    c,g,v=setup;r=c.submit('owner','synthetic',v,'x',plan())
    class Missing(Fake):
        def sample(self,cwd):return {}
    fake=Missing();c.execute(r['id'],fake);assert fake.calls==0
    assert c.result('owner',r['id'])['usage']['roles']['plus_executor']['tokens']['totals']['input_tokens'] is None

def test_queued_cancel_and_symlink(setup):
    c,g,v=setup;r=c.submit('owner','synthetic',v,'x',plan());assert c.cancel('owner',r['id'])['status']=='CANCELLED'
    with pytest.raises(RuntimeError):c.execute(r['id'],Fake())
    link=Path(g['root'])/'input/link'
    try:link.symlink_to(Path(g['root'])/'input/data.csv')
    except OSError:pytest.skip('Windows symlink privilege unavailable; junction checked in integration probe')
    with pytest.raises(ValueError):plain(link)

"""Real loopback synthetic downloads; no model requests."""
import hashlib
import http.server
import threading
from pathlib import Path
import pytest
from pcc.controller import Controller
from pcc.broker import Broker,validate_plan
from test_controller import grant,plan


@pytest.mark.parametrize('case',['ok','redirect','size','hash','unapproved'])
def test_exact_download_target_and_failures(tmp_path,case):
    calls=[];body=b'synthetic download\n'
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.send_response(302 if case=='redirect' else 200)
            self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=http.server.HTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        c=Controller(tmp_path/'control');g=grant(tmp_path/'project');g['actions'].append('download')
        g['download_targets']=[{'id':'fixture','url':f'http://127.0.0.1:{server.server_port}/fixture',
          'max_bytes':1 if case=='size' else 1024,'sha256':'0'*64 if case=='hash' else hashlib.sha256(body).hexdigest()}]
        v=c.grant(g)['version'];g=c.authorized('owner','synthetic',v)
        p={**plan(),'downloads':[{'target':'other' if case=='unapproved' else 'fixture','destination':'downloads/fixture.txt'}]}
        if case=='unapproved':
            with pytest.raises(PermissionError):validate_plan(g,p)
            assert calls==[];return
        p=validate_plan(g,p);r=c.submit('owner','synthetic',v,'synthetic download',p);row=c.row('owner',r['id'])
        run=tmp_path/'run';run.mkdir();job=tmp_path/'job';(job/'input').mkdir(parents=True)
        broker=Broker(c,row,g,run)
        if case=='ok':
            inputs=broker.freeze_inputs(job,p)
            assert (job/'input/downloads/fixture.txt').read_bytes()==body
            assert inputs[-1]['sha256']==hashlib.sha256(body).hexdigest()
        else:
            with pytest.raises((RuntimeError,ValueError)):broker.freeze_inputs(job,p)
            assert not (job/'input/downloads/fixture.txt').exists()
        assert calls==['/fixture']
    finally:server.shutdown();server.server_close();thread.join()


def test_empty_download_extension_preserves_original_idempotency(tmp_path):
    c=Controller(tmp_path/'control');v=c.grant(grant(tmp_path/'project'))['version']
    first=c.submit('owner','synthetic',v,'same',plan())
    second=c.submit('owner','synthetic',v,'same',{**plan(),'downloads':[]})
    assert first['id']==second['id']

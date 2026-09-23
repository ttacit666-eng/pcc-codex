"""Lifecycle regressions using synthetic records and a local Python child only."""
import json
import os
import sqlite3
import sys
import pytest
from pcc.executor import LiveExecutor, usage
from test_controller import setup, plan, Fake, sample


def events():
    return [
        {'source_seq':1,'type':'thread.started','session_id':'synthetic-capture'},
        {'source_seq':2,'type':'turn.started'},
        {'source_seq':3,'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':80,'output_tokens':20}},
    ]


def test_cancel_rechecks_queued_snapshot_after_concurrent_claim(setup,monkeypatch):
    c,g,v=setup;r=c.submit('owner','synthetic',v,'first',plan())
    original=c.row;observed=False
    def stale(subject,task):
        nonlocal observed
        row=original(subject,task)
        if not observed:
            observed=True;c.claim(task);c.update(task,'RUNNING',999999)
        return row
    monkeypatch.setattr(c,'row',stale)
    result=c.cancel('owner',r['id'])
    assert result['status']=='RUNNING' and result['cancel']==1
    with pytest.raises(RuntimeError,match='occupied'):
        c.submit('owner','synthetic',v,'distinct second task',plan())


@pytest.mark.parametrize('broken_tail',[False,True])
def test_capture_exception_preserves_durable_usage_without_retry(setup,broken_tail):
    c,g,v=setup;r=c.submit('owner','synthetic',v,'sum',plan())
    class Interrupted(Fake):
        source_kind='synthetic'
        def run(self,c,r,job,run,*args):
            self.calls+=1;c.update(r['id'],'RUNNING',999999)
            text=''.join(json.dumps(e)+'\n' for e in events())
            (run/'usage-events.jsonl').write_text(text+('{"type":' if broken_tail else ''),encoding='utf-8')
            raise TimeoutError('synthetic post-event capture failure')
    executor=Interrupted();c.execute(r['id'],executor)
    result=c.result('owner',r['id']);tokens=result['usage']['roles']['plus_executor']['tokens']
    assert result['status']=='RECOVERY_REQUIRED' and executor.calls==1
    assert tokens['session_id']=='synthetic-capture'
    assert tokens['observed_partial']=={'input_tokens':100,'cached_input_tokens':80,'output_tokens':20,'input_plus_output_tokens':120}
    assert all(value is None for value in tokens['totals'].values())
    assert 'statistics_incomplete' in tokens['flags']
    if broken_tail:assert 'incomplete_event_tail' in result['flags']
    with pytest.raises(RuntimeError):c.execute(r['id'],executor)
    with pytest.raises(RuntimeError,match='occupied'):c.submit('owner','synthetic',v,'different',plan())


def test_database_context_closes_and_rolls_back(setup):
    c,g,v=setup
    with c.db() as db:assert db.execute('SELECT 1').fetchone()[0]==1
    with pytest.raises(sqlite3.ProgrammingError):db.execute('SELECT 1')
    with pytest.raises(RuntimeError):
        with c.db() as db:
            db.execute("INSERT INTO audit(time,task,event,body) VALUES('synthetic',NULL,'rollback-test','{}')")
            raise RuntimeError('rollback')
    with c.db() as db:assert db.execute("SELECT 1 FROM audit WHERE event='rollback-test'").fetchone() is None


@pytest.mark.parametrize('operation',['timeout','cancel'])
def test_real_nonmodel_child_keeps_unknown_tree_occupied(setup,monkeypatch,operation):
    c,g,v=setup;r=c.submit('owner','synthetic',v,'nonmodel lifecycle probe',plan())
    script=(
        'import json,sys,time\n'
        'sys.stdin.read()\n'
        'print(json.dumps({"type":"thread.started","thread_id":"synthetic-child"}),flush=True)\n'
        'print(json.dumps({"type":"turn.started"}),flush=True)\n'
        'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":80,"output_tokens":20}}),flush=True)\n'
        'time.sleep(30)\n'
    )
    class Child(LiveExecutor):
        source_kind='synthetic'
        execution_mode='synthetic_nonmodel_child'
        preflight=None
        def version(self):return 'synthetic-python-child'
        def sample(self,cwd):return sample()
        def arguments(self,*args):return [sys.executable,'-u','-c',script]
        def run(self,c,r,job,run,goal,p,python,grant):
            return super().run(c,r,job,run,goal,p,python,{**grant,'timeout_seconds':1})
    # No CLI/App Server or account reads occur in this test.
    monkeypatch.setattr(usage,'plus_env',lambda:os.environ.copy())
    original=c.update
    def update(task,status,pid=None):
        original(task,status,pid)
        if operation=='cancel' and status=='RUNNING':c.cancel('owner',task)
    monkeypatch.setattr(c,'update',update)
    child=Child()
    # Existing execute() uses hasattr for optional preflight; supply a no-op.
    child.preflight=lambda *args:None
    result=c.execute(r['id'],child)
    assert result['status']=='RECOVERY_REQUIRED'
    expected='cancel_or_revocation' if operation=='cancel' else 'timeout_process_tree_unknown'
    assert expected in result['flags']
    assert c.status('owner',r['id'])['pid'] is not None
    with pytest.raises(RuntimeError,match='occupied'):c.submit('owner','synthetic',v,'another',plan())
    if operation=='timeout':
        tokens=c.result('owner',r['id'])['usage']['roles']['plus_executor']['tokens']
        assert tokens['observed_partial']['input_tokens']==100
        assert tokens['totals']['input_tokens'] is None


def test_restart_never_requeues_an_uncertain_process(setup):
    c,g,v=setup;r=c.submit('owner','synthetic',v,'sum',plan());c.claim(r['id']);c.update(r['id'],'RUNNING',999999)
    c.reconcile();c.reconcile()
    assert c.status('owner',r['id'])['status']=='RECOVERY_REQUIRED'
    with pytest.raises(RuntimeError,match='already claimed'):c.claim(r['id'])


@pytest.mark.parametrize('failure_point',['registry_update','pid_evidence_write'])
def test_spawned_child_never_becomes_prestart_block_after_journal_failure(setup,monkeypatch,failure_point):
    import pcc.executor as module
    c,g,v=setup;r=c.submit('owner','synthetic',v,'nonmodel spawn fault probe',plan())
    children=[];popen=module.subprocess.Popen
    def spawn(*args,**kwargs):
        child=popen(*args,**kwargs);children.append(child);return child
    monkeypatch.setattr(module.subprocess,'Popen',spawn)
    monkeypatch.setattr(usage,'plus_env',lambda:os.environ.copy())
    class Child(LiveExecutor):
        source_kind='synthetic'
        def version(self):return 'synthetic-python-child'
        def sample(self,cwd):return sample()
        def preflight(self,*args):pass
        def arguments(self,*args):return [sys.executable,'-c','import time;time.sleep(0.2)']
    update=c.update;save=module.save
    def fail_update(task,status,pid=None):
        if status=='RUNNING':raise RuntimeError('synthetic DB update failure after Popen')
        update(task,status,pid)
    def fail_save(path,value):
        if path.name=='process-launch.json' and value.get('state')=='spawned':
            raise OSError('synthetic PID evidence write failure after Popen')
        save(path,value)
    if failure_point=='registry_update':monkeypatch.setattr(c,'update',fail_update)
    else:monkeypatch.setattr(module,'save',fail_save)
    try:
        result=c.execute(r['id'],Child())
        assert len(children)==1 and result['status']=='RECOVERY_REQUIRED'
        status=c.status('owner',r['id'])
        if failure_point=='registry_update':assert status['pid']==children[0].pid
        else:assert status['pid'] is None and status['process_observation']=='unknown'
        with pytest.raises(RuntimeError,match='occupied'):c.submit('owner','synthetic',v,'another',plan())
    finally:
        for child in children:
            child.wait(timeout=10)
            for stream in (child.stdin,child.stdout,child.stderr):
                if stream:stream.close()

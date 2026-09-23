import json
import pytest
from test_controller import setup, plan

def blocked(setup):
    c,g,v=setup
    row=c.submit('owner','synthetic',v,'install',plan())
    c.update(row['id'],'BLOCKED')
    run=c.root/'runs'/row['run_id'];run.mkdir(parents=True)
    (run/'failure.json').write_text(json.dumps({'error_type':'FileNotFoundError','stage':'pre_execution_or_capture'}))
    (run/'result.json').write_text(json.dumps({'actions':[],'artifacts':[]}))
    return c,row,run

def test_preserves_and_deduplicates(setup):
    c,row,run=blocked(setup)
    new=c.retry_prestart(row['id'],'owner authorized after runtime repair')
    assert new['id']!=row['id']
    assert c.status('owner',row['id'])['status']=='BLOCKED'
    assert c.retry_prestart(row['id'],'same recovery')['id']==new['id']
    assert (run/'failure.json').exists()

@pytest.mark.parametrize('hazard',['pid','actions','preflight','unknown','launch_intent','launch_spawned'])
def test_refuses_uncertainty(setup,hazard):
    c,row,run=blocked(setup)
    if hazard=='pid':c.update(row['id'],'BLOCKED',123)
    if hazard=='actions':(run/'actions.json').write_text('[]')
    if hazard=='preflight':(run/'host-trusted-preflight.json').write_text('{}')
    if hazard=='unknown':c.update(row['id'],'RECOVERY_REQUIRED')
    if hazard.startswith('launch_'):(run/'process-launch.json').write_text(json.dumps({'state':hazard.removeprefix('launch_')}))
    with pytest.raises(PermissionError):c.retry_prestart(row['id'],'repair')

def test_explicit_not_started_launch_proof_preserves_recovery_gate(setup):
    c,row,run=blocked(setup)
    (run/'process-launch.json').write_text(json.dumps({'state':'not_started','error_type':'FileNotFoundError'}))
    assert c.retry_prestart(row['id'],'owner authorized verified prestart recovery')['original_task']==row['id']

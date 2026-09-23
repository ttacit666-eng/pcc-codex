"""Read-only frozen-input evidence, without exposing arbitrary file contents."""
import hashlib
import json
from pathlib import Path
import pytest
from test_controller import setup,plan,Fake


def test_result_exposes_actual_frozen_input_digest(setup):
    c,g,v=setup;r=c.submit('owner','synthetic',v,'sum',plan());c.execute(r['id'],Fake())
    original=(Path(g['root'])/'input/data.csv').read_bytes()
    # Later source edits must not replace the controller's frozen-input hash.
    (Path(g['root'])/'input/data.csv').write_text('changed source\n',encoding='utf-8')
    result=c.result('owner',r['id'])
    assert result['inputs']==[{'path':'input/data.csv','sha256':hashlib.sha256(original).hexdigest(),'bytes':len(original)}]
    assert result['input_hash_scope'].startswith('controller-frozen')
    assert result['execution_observation'] is None
    assert result['independent_review']=='REVIEW_REQUIRED'


def test_result_projects_only_allowed_input_and_execution_metadata(setup):
    c,g,v=setup;r=c.submit('owner','synthetic',v,'sum',plan());c.execute(r['id'],Fake())
    run=c.root/'runs'/r['run_id'];inputs=json.loads((run/'inputs.json').read_text())
    inputs[0].update(body='SYNTHETIC_PRIVATE_BODY',source_absolute_path='SYNTHETIC_PRIVATE_PATH')
    (run/'inputs.json').write_text(json.dumps(inputs))
    (run/'execution-summary.json').write_text(json.dumps({'command_items_started':2,'command_items_completed':2,
        'agent_messages_completed':1,'errors':['SYNTHETIC_PRIVATE_ERROR'],'command':'SYNTHETIC_PRIVATE_COMMAND',
        'scope':'SYNTHETIC_PRIVATE_SCOPE','tool_item_types':['SYNTHETIC_PRIVATE_TYPE']}))
    result=c.result('owner',r['id']);observed=result['execution_observation']
    assert observed['command_items_started']==2 and observed['error_count']==1
    assert set(result['inputs'][0])=={'path','sha256','bytes'}
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(result)
    with pytest.raises(PermissionError):c.result('different-subject',r['id'])
    c.revoke('synthetic')
    with pytest.raises(PermissionError):c.result('owner',r['id'])


def test_missing_metadata_stays_unknown_without_reading_project(setup):
    c,g,v=setup;r=c.submit('owner','synthetic',v,'sum',plan())
    result=c.result('owner',r['id'])
    assert result['inputs'] is None and result['execution_observation'] is None
    run=c.root/'runs'/r['run_id'];run.mkdir(parents=True)
    (run/'inputs.json').write_text('[]')
    (run/'execution-summary.json').write_text(json.dumps({'command_items_started':True,'command_items_completed':-1}))
    result=c.result('owner',r['id'])
    assert result['inputs']==[]
    assert all(result['execution_observation'][key] is None for key in
               ('command_items_started','command_items_completed','agent_messages_completed','error_count'))

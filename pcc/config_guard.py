"""Restore only an official CLI trust stanza owned by this task, never arbitrary changes."""
import hashlib
import os
from .paths import save


def restore_owned_trust(path, before, cwd, evidence):
    after=path.read_bytes()
    record={'path':str(path),'before_sha256':hashlib.sha256(before).hexdigest(),
            'observed_after_sha256':hashlib.sha256(after).hexdigest(),'only_owned_stanza_removed':False}
    if after!=before:
        own=os.path.normcase(os.path.abspath(cwd))
        stanza=('\n[projects.\''+own+'\']\ntrust_level = "trusted"\n').encode()
        candidates=[stanza,stanza.replace(b'\n',b'\r\n')]
        valid=any(after.count(s)==1 and after.replace(s,b'')==before for s in candidates)
        if not valid:
            record['status']='CONCURRENT_OR_UNRECOGNIZED_CHANGE_NOT_OVERWRITTEN';save(evidence,record)
            raise RuntimeError('Plus config changed beyond exact task-owned trust stanza; no overwrite')
        if path.read_bytes()!=after:raise RuntimeError('Plus config changed during restoration check')
        path.write_bytes(before)
        record['only_owned_stanza_removed']=True
    record['restored_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    record['original_hash_match']=record['restored_sha256']==record['before_sha256']
    save(evidence,record)
    if not record['original_hash_match']:raise RuntimeError('Plus config restoration verification failed')
    return record

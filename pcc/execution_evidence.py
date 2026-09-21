"""Task execution diagnostics, kept separate from usage receipts. Never a credential collector."""
import hashlib,json,re

def scrub(text):
    text=str(text)
    text=re.sub(r'(?i)(bearer\s+|(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*[=:]\s*)[^\s,;]+',r'\1[REDACTED]',text)
    text=re.sub(r'\bsk-[A-Za-z0-9_-]{10,}\b','[REDACTED]',text)
    return text[:8000]

class ExecutionEvidence:
    def __init__(self,run):
        self.run=run;self.commands_started=set();self.commands_completed=set();self.tool_types=set();self.agent_messages=0;self.errors=[]
    def record(self,native,seq):
        typ=native.get('type');item=native.get('item') or {};row={'source_seq':seq,'type':typ}
        if typ in ('item.started','item.updated','item.completed'):
            kind=item.get('type');ident=item.get('id');row.update(item_type=kind,item_id=ident,status=item.get('status'))
            if kind=='command_execution':
                self.tool_types.add(kind)
                if typ=='item.started':self.commands_started.add(ident)
                if typ=='item.completed':self.commands_completed.add(ident)
                row['exit_code']=item.get('exit_code')
                row['command_sha256']=hashlib.sha256(str(item.get('command','')).encode()).hexdigest()
                if typ=='item.completed':
                    row['output']=scrub(item.get('aggregated_output',''))
                    row['output_truncated']=len(str(item.get('aggregated_output','')))>8000
            elif kind=='agent_message' and typ=='item.completed':
                self.agent_messages+=1
                with (self.run/'executor-response.md').open('a',encoding='utf-8') as f:f.write(scrub(item.get('text',''))+'\n')
            elif kind not in ('agent_message','reasoning'):self.tool_types.add(str(kind))
        elif typ in ('error','turn.failed'):
            row['error']=scrub(native.get('message') or native.get('error') or '')
            self.errors.append(row['error'])
        with (self.run/'execution-events.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    def summary(self):
        return {'command_items_started':len(self.commands_started),'command_items_completed':len(self.commands_completed),
                'tool_item_types':sorted(self.tool_types),'agent_messages_completed':self.agent_messages,'errors':self.errors,
                'scope':'CLI event observations; command item start is not independent OS process creation proof'}

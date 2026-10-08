"""Persist provider-reported token usage across interrupted/resumed requests."""
import json
import os
import time
from pathlib import Path
from langchain.callbacks.openai_info import OpenAICallbackHandler

from skillexpand.reliability.errors import LedgerCorrupt

class PersistentUsage(OpenAICallbackHandler):
    raise_error=True
    fields=('prompt_tokens','completion_tokens','total_tokens','successful_requests')
    def __init__(self,path):
        super().__init__()
        self.path=Path(path)
        self.previous=json.loads(self.path.read_text()) if self.path.exists() else {}
        request_path = self.path.with_suffix('.requests.jsonl')
        if request_path.exists():
            from skillexpand.persistence.io import read_jsonl
            rows = read_jsonl(request_path)
            pending, finished = {}, set()
            totals = dict.fromkeys((*self.fields, 'started_requests', 'failed_requests'), 0)
            for row in rows:
                if row['event'] == 'start':
                    if not row['run_id'] or row['run_id'] in pending or row['run_id'] in finished:
                        raise LedgerCorrupt('Duplicate or missing request ID in usage log')
                    pending[row['run_id']] = row
                    totals['started_requests'] += 1
                else:
                    if row['event'] not in ('end', 'error', 'abandoned') or row['run_id'] not in pending:
                        raise LedgerCorrupt('Unmatched terminal event in usage log')
                    pending.pop(row['run_id'])
                    finished.add(row['run_id'])
                    if row['event'] in ('error', 'abandoned'):
                        totals['failed_requests'] += 1
                    elif row['event'] == 'end':
                        usage = (row.get('provider') or {}).get('token_usage')
                        if usage:
                            totals['successful_requests'] += 1
                            for field in self.fields[:-1]:
                                totals[field] += usage.get(field, 0)
            # Called only by the owner of a task/campaign lock. A start with no
            # terminal event belongs to the previous process, not a live request.
            for rid in pending:
                self.audit('abandoned', run_id=rid, error_type='InterruptedProcess', tokens_unknown=True)
                totals['failed_requests'] += 1
            self.previous = totals
        self.started=self.previous.get('started_requests',0)
        self.failed=self.previous.get('failed_requests',0)
        if request_path.exists():
            self.persist()

    def persist(self):
        from skillexpand.persistence.io import save
        values={k:self.previous.get(k,0)+getattr(self,k) for k in self.fields}
        values.update(started_requests=self.started,failed_requests=self.failed,
            usage_note='Provider-reported successful-response usage; failed or in-flight tokens may be unknown')
        save(self.path,values)

    def audit(self, event, **payload):
        # Kept beside usage, outside every L2 evidence projection. Never serialize
        # model/client objects: they may contain credentials.
        path = self.path.with_suffix('.requests.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            stream.write(json.dumps(dict(event=event, time=time.time(), **payload), ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def on_llm_start(self,*args,**kwargs):
        self.started+=1
        prompts = kwargs.get('prompts', args[1] if len(args) > 1 else None)
        if prompts is not None:
            self.audit('start', run_id=str(kwargs.get('run_id','')), prompts=prompts)
        self.persist()

    def on_llm_end(self,response,**kwargs):
        self.audit('end', run_id=str(kwargs.get('run_id','')),
                   generations=[[dict(text=g.text, info=g.generation_info) for g in group]
                                for group in response.generations],
                   provider=response.llm_output)
        super().on_llm_end(response,**kwargs);self.persist()

    def on_llm_error(self,*args,**kwargs):
        self.failed+=1
        error = args[0] if args else kwargs.get('error')
        self.audit('error', run_id=str(kwargs.get('run_id','')), error_type=type(error).__name__)
        self.persist()


def attach_usage(wrappers,path):
    tracker=PersistentUsage(path)
    seen=set()
    for wrapper in wrappers:
        llm=getattr(wrapper,'llm',None)
        if llm is not None and id(llm) not in seen:
            seen.add(id(llm))
            llm.callbacks=[c for c in (llm.callbacks or []) if not isinstance(c,PersistentUsage)]+[tracker]
    return tracker

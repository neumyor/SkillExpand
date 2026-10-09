"""Persist provider-reported token usage across interrupted/resumed requests."""
import json
import os
import time
from pathlib import Path
from langchain.callbacks.openai_info import OpenAICallbackHandler

from skillexpand.persistence.io import read_jsonl, save
from skillexpand.reliability.errors import LedgerCorrupt

TOKEN_FIELDS = ('prompt_tokens', 'completion_tokens', 'total_tokens')


def replay_ledger(rows):
    """Fold request events into counts and token totals: the one ledger state machine.

    An ``end`` without provider token usage is corrupt (the strictest handling): its
    tokens cannot be verified, and a total that skipped it would understate cost.
    ``pending`` lists requests with no terminal event; the caller decides what that means.
    """
    pending, finished = {}, set()
    out = {'started': 0, 'successful': 0, 'failed': 0, 'abandoned': 0, 'error_types': [],
           'tokens': dict.fromkeys(TOKEN_FIELDS, 0)}
    for row in rows:
        rid, event = row.get('run_id'), row.get('event')
        if event == 'start':
            if not rid or rid in pending or rid in finished:
                raise LedgerCorrupt('Duplicate or missing request ID in usage log')
            pending[rid] = row
            out['started'] += 1
            continue
        if event not in ('end', 'error', 'abandoned') or rid not in pending:
            raise LedgerCorrupt('Unmatched terminal event in usage log')
        del pending[rid]
        finished.add(rid)
        if event == 'end':
            usage = (row.get('provider') or {}).get('token_usage')
            if not usage or any(field not in usage for field in TOKEN_FIELDS):
                raise LedgerCorrupt('Provider token usage missing; cannot verify totals')
            out['successful'] += 1
            for field in TOKEN_FIELDS:
                out['tokens'][field] += int(usage[field])
        else:
            out['failed'] += 1
            if event == 'abandoned':
                out['abandoned'] += 1
            else:
                out['error_types'].append(row.get('error_type'))
    return dict(out, pending=list(pending))


class PersistentUsage(OpenAICallbackHandler):
    raise_error=True
    fields=('prompt_tokens','completion_tokens','total_tokens','successful_requests')
    def __init__(self,path):
        super().__init__()
        self.path=Path(path)
        self.previous=json.loads(self.path.read_text()) if self.path.exists() else {}
        request_path = self.path.with_suffix('.requests.jsonl')
        if request_path.exists():
            ledger = replay_ledger(read_jsonl(request_path))
            # Called only by the owner of a task/campaign lock. A start with no
            # terminal event belongs to the previous process, not a live request.
            for rid in ledger['pending']:
                self.audit('abandoned', run_id=rid, error_type='InterruptedProcess', tokens_unknown=True)
            self.previous = {**ledger['tokens'], 'successful_requests': ledger['successful'],
                             'started_requests': ledger['started'],
                             'failed_requests': ledger['failed'] + len(ledger['pending'])}
        self.started=self.previous.get('started_requests',0)
        self.failed=self.previous.get('failed_requests',0)
        if request_path.exists():
            self.persist()

    def persist(self):
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

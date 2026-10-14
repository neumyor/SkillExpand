"""One native generic proposal per predeclared candidate slot."""
import os
import time
from types import SimpleNamespace
from pathlib import Path

from state import ROOT,read,write,freeze_bytes,verify_freeze,reserve,arm_root,task_root
from skillexpand import schema as S
from skillexpand.l2.editor import SkillEditor
from skillexpand.runtime.llm_relay import relay_from_env,coalesce_sse,iter_sse_events

def inputs(task,arm,number):
    folder=arm_root(task,arm)
    base=read(task_root(task)/'frozen/seed.json')
    feedback=[];feedback_paths=[]
    previous=folder/'candidate-001'
    if number==2 and (previous/'skill.json').exists():
        base=read(previous/'skill.json')
        feedback_paths=[previous/'dev'/f'{i:02d}'/'feedback.json' for i in range(1,4)]
        feedback=[read(p) for p in feedback_paths]
    evidence=read(task_root(task)/f'frozen/evidence-{arm}.json')
    return base,evidence,feedback,feedback_paths

def generate(task,arm,number):
    if number not in (1,2):raise ValueError('Candidate cap')
    verify_freeze()
    folder=arm_root(task,arm)/f'candidate-{number:03d}'
    if not reserve(folder,'generation'):
        if not (folder/'generation_status.json').exists():
            write(folder/'generation_status.json',{'status':'interrupted_unresolved','no_retry':True})
        return (folder/'skill.json').exists()
    relay=None
    started=time.time()
    try:
        base,evidence,feedback,paths=inputs(task,arm,number)
        write(folder/'generation_input.json',{'base_skill':base,'evidence':evidence,'feedback':feedback,
                                            'feedback_paths':[str(p.relative_to(ROOT)) for p in paths]})
        editor=SkillEditor(SimpleNamespace(llm=None),SimpleNamespace(version=0))
        planned=editor.build_package_prompt(S.from_dict(S.Skill,base),evidence,feedback)
        planned_payload={**read(ROOT/'frozen/generation_settings.json'),
                         'messages':[{'role':'system' if m.type=='system' else 'user','content':m.content} for m in planned]}
        freeze_bytes(folder/'request.json',(__import__('json').dumps(planned_payload,ensure_ascii=False,indent=2)+'\n').encode())
        os.environ['OPENAI_API_KEY']=os.environ['TB21_METHOD_API_KEY']
        relay=relay_from_env()
        def llm(messages,**kwargs):
            payload={**read(ROOT/'frozen/generation_settings.json'),
                     'messages':[{'role':'system' if m.type=='system' else 'user','content':m.content} for m in messages]}
            freeze_bytes(folder/'request.json',( __import__('json').dumps(payload,ensure_ascii=False,indent=2)+'\n').encode())
            relay.start()
            with (folder/'response.sse').open('wb') as stream:
                def chunks():
                    for c in relay.transport.stream(payload):
                        c=c.encode() if isinstance(c,str) else c
                        stream.write(c);stream.flush();yield c
                result=coalesce_sse(chunks())
            write(folder/'completion.json',result)
            usage=[e['usage'] for e in iter_sse_events((folder/'response.sse').read_bytes().splitlines(keepends=True)) if e.get('usage')]
            write(folder/'generation_metrics.json',{'seconds':time.time()-started,
                'usage':usage[-1] if usage else result.get('usage'),'response_model':result.get('model')})
            return result['choices'][0]['message']['content']
        editor=SkillEditor(SimpleNamespace(llm=llm),SimpleNamespace(version=0))
        outcome=editor.propose_package(S.from_dict(S.Skill,base),evidence,feedback)
        write(folder/'outcome.json',S.to_dict(outcome))
        if outcome.proposed:write(folder/'skill.json',S.to_dict(outcome.candidate.skill))
        write(folder/'generation_status.json',{'status':'generated' if outcome.proposed else outcome.reason,
                                              'seconds':time.time()-started,'no_retry':True})
    except Exception as exc:
        write(folder/'generation_status.json',{'status':'failed','error_type':type(exc).__name__,
                                               'seconds':time.time()-started,'no_retry':True})
    finally:
        if relay is not None:
            try:relay.close()
            except Exception as exc:write(folder/'cleanup_error.json',{'error_type':type(exc).__name__})
    verify_freeze()
    print(__import__('json').dumps({'task':task,'arm':arm,'candidate':number,
                                    **read(folder/'generation_status.json')}),flush=True)
    return (folder/'skill.json').exists()

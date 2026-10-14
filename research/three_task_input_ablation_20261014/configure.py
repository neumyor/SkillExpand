"""Create fixed inputs/configs before semantic audit and manifest freezing."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from state import ROOT,TASKS,ARMS,read,write
sys.path.insert(0,str(ROOT/'code'))
from input_filter import project_history,sanitize_seed
from skillexpand import schema as S
from skillexpand.l2.editor import SkillEditor
REMOTE='/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/three-task-input-ablation-20261014'

def main():
    if (ROOT/'manifest.json').exists():raise ValueError('Already configured')
    write(ROOT/'frozen/generation_settings.json',{'model':'DEEPSEEK_up5zdj','stream':True,
         'max_tokens':65536,'enable_thinking':True})
    ids=ROOT/'historical_sandbox_ids.json'
    if ids.exists():ids.replace(ROOT/'frozen/historical_sandbox_ids.json')
    editor=SkillEditor(SimpleNamespace(llm=None),SimpleNamespace(version=0))
    systems=[]
    for task in TASKS:
        folder=ROOT/'tasks'/task/'frozen'
        raw=read(folder/'input.json')
        seed=S.to_dict(S.from_dict(S.Skill,sanitize_seed(raw['original_skill'])))
        write(folder/'seed.json',seed)
        runtime=folder/'runtime'
        if not runtime.exists():runtime.symlink_to('../../../frozen/runtime',target_is_directory=True)
        protocol={'task_name':task,'model_name':'qwen3.6-flash-distill',
                  'rollout':{'runner_script':REMOTE+'/frozen/runtime/tencent/scripts/run_tencent_tb21_smoke.sh',
                             'llm_transport':'tencent_e2b_relay','provider_base_url':'https://llm-center.modelbest.co/v1'},
                  'excluded_sandbox_ids':read(ROOT/'frozen/historical_sandbox_ids.json')}
        write(folder/'protocol.json',protocol)
        for arm in ARMS:
            evidence=project_history(raw,arm)
            write(folder/f'evidence-{arm}.json',evidence)
            messages=editor.build_package_prompt(S.from_dict(S.Skill,seed),evidence)
            preview={'messages':[{'role':'system' if m.type=='system' else 'user','content':m.content} for m in messages],
                     **read(ROOT/'frozen/generation_settings.json')}
            write(folder/f'initial_request-{arm}.json',preview)
            systems.append(messages[0].content)
    if len(set(systems))!=1:raise ValueError('Task/arm-specific system drift')
    (ROOT/'frozen/system_prompt.txt').write_text(systems[0])
    write(ROOT/'manifest.json',{'format':'three-task-joint-history-ablation-v1','status':'prepared_pending_audit',
         'tasks':list(TASKS),'arms':list(ARMS),'baseline':'shared sanitized-original, five per task',
         'budget':{'baseline_per_task':5,'candidates_per_arm':2,'dev_per_candidate':3,'final_per_arm':5,'max_executor_trials':81,'generation_slots':12},
         'generation_module':'SkillEditor.propose_package','native_source_commit':'255a67c4b71c24767265a142757443ec0b91dcda',
         'preaudit_passed':False,'executor_model':'openai/qwen3.6-flash-distill','generation_model':'DEEPSEEK_up5zdj',
         'codex_requested_model':'gpt-6.1-sol','codex_requested_reasoning':'high','codex_root_switch_verified':False,
         'concurrency':{'drivers':9,'max_simultaneous_executor_slots':45,'max_simultaneous_generator_slots':6},
         'retry_policy':'one executor call per fixed slot; Harbor retries0; frozen provider HTTP retries retained',
         'no_final_feedback':True,'original_experiments_preserved':True})
if __name__=='__main__':main()

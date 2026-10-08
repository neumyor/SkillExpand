#!/usr/bin/env python3
"""Import one Harbor/Tencent TerminalBench trial into a SkillExpand smoke run.

This keeps the real rollout boundary intact: SkillExpand receives the verifier
outcome and ATIF trajectory as evidence, while the Harbor runner remains the
owner of shell execution, timeouts, and infrastructure errors.
"""
import argparse, json, shutil
from pathlib import Path
from skillexpand import schema as S
from skillexpand.l1 import learning as L
from skillexpand.l1 import protocol as P
from skillexpand.l1.family_discovery import TaskTag, FamilyProposal, FamilyAssignment, FamilyPlan
import hashlib


def _events(traj):
    events=[]
    for step in traj.get('steps', []):
        msg = step.get('message') or step.get('observation') or ''
        if not msg:
            continue
        events.append({'ref': f'e{len(events)+1}', 'action': 'TerminalBatch',
                       'observation': str(msg), 'environment': {'success': False}})
    if not events:
        events=[{'ref':'e1','action':'TerminalBatch','observation':'No trajectory steps recorded.',
                 'environment': {'success': False}}]
    return events

def freeze(path, value):
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    payload=json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)+'\n'
    if path.exists() and path.read_text() != payload:
        raise ValueError('frozen artifact changed: '+str(path))
    if not path.exists(): path.write_text(payload)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--trial-dir', type=Path, required=True)
    ap.add_argument('--run-dir', type=Path, required=True)
    args=ap.parse_args()
    trial=args.trial_dir.resolve(); root=args.run_dir.resolve(); root.mkdir(parents=True, exist_ok=True)
    result=json.loads((trial/'result.json').read_text())
    traj=json.loads((trial/'agent'/'trajectory.json').read_text())
    reward=bool(result.get('reward', result.get('success',
                    (result.get('verifier_result') or {}).get('rewards', {}).get('reward', 0))))
    instruction='Repair the repository according to the task files and make the provided verifier pass.'
    task_file=(Path(__file__).resolve().parents[1]/'src/skillexpand/data/terminalbench/fix-git.json').resolve()
    raw_tasks=json.loads(task_file.read_text())
    tasks=[{'task': row['instruction'],
            'env_kwargs': {'instruction': row['instruction'], 'task_name': row['task_name']},
            'env_name': 'terminalbench'} for row in raw_tasks]
    config={'benchmark': {'name':'terminalbench','task_prefix':'','task_file':str(task_file),
                          'max_steps':1,'num_fewshots':0,'ai_name':'terminal agent',
                          'env':{'show_admissible_commands':False},
                          'l1':{'adapter':'skillexpand.l1.adapters:TerminalBenchAdapter'}},
            'agent': {'llm':'openai/qwen3.6-flash-distill'},
            'models': {k:'openai/qwen3.6-flash-distill' for k in ('l1_executor','cold_start','l2_planner','l2_editor','l2_reviewer','selector')}}
    assignment={0:S.SPLIT_TRAIN,1:S.SPLIT_VAL,2:S.SPLIT_TEST}
    split=S.SplitPlan.make(assignment,'terminalbench',42)
    family='terminalbench.fix-git'
    tag=TaskTag(0, ('shell-repair','git','verifier'), 'Repair a git repository and satisfy a verifier.')
    proposal=FamilyProposal(family,'fix-git repository repair','TerminalBench repository repair tasks',('git repair','verifier pass'))
    fa=FamilyAssignment(0,family,'direct','Single-task smoke family.')
    clusters=FamilyPlan('terminalbench','forced_choice_assignment',{0:family},{family:{'name':'fix-git repository repair','definition':proposal.definition,'trigger_conditions':list(proposal.trigger_conditions),'task_ids':[0]}},(tag,),(proposal,),(fa,))
    events=_events(traj)
    trial_record={'index':1,'phase':'autonomous','status':'completed','success':reward,'termination':'verifier',
                  'trajectory':str(trial/'agent'/'trajectory.json'),'events':events}
    evidence=P.evidence(instruction,[trial_record])
    synthesis={'status':'valid','claims':[]}
    card=L.card(0,instruction,[trial_record],synthesis,'external_harbor',
                'tb21-e1-fix-git',len,evidence=evidence,card_id='discovery:terminalbench.fix-git:0',
                benchmark='terminalbench',family_id=family)
    exp=S.TaskExperience('discovery:terminalbench.fix-git:0','terminalbench',0,instruction,family,S.SPLIT_TRAIN,
                         reward,1,failed_trajectories=() if reward else (str(trial/'agent'/'trajectory.json'),),
                         final_trajectory=str(trial/'agent'/'trajectory.json') if reward else None,
                         selection_source=S.SELECTION_UNSKILLED,trial_rewards=(reward,),trial_phases=('autonomous',),
                         experience_card=card,l1_trials=(trial_record,))
    initial=S.Skill('terminalbench.'+family,family,0,proposal.name,
                    proposal.definition,'1. Inspect the repository state and task files.\n2. Make the smallest verified repair.\n3. Run the provided verifier before finishing.',
                    S.Provenance(rationale='Imported from one real TB2.1 Harbor trajectory',source_experience_ids=(exp.experience_id,),source_task_ids=(0,)))
    freeze(root/'config.json',config); freeze(root/'split.json',S.to_dict(split))
    manifest={'protocol':'experience-first','split':S.to_dict(split),'config':config,
              'task_table_hash':S.content_hash(tasks),'prompts':{},'k':1,'supervised':False,
              'supervised_attempts':0,'card_batch_size':12,'skill_edit_mode':'rewrite','code':{'importer':'tb21-e1-smoke-v1'},
              'trajectory_import':{'source_trial':str(trial),'source_model':traj.get('agent',{}).get('model_name')}}
    freeze(root/'manifest.json',manifest)
    freeze(root/'clusters.json',clusters.to_dict()); freeze(root/'task_skill_map.json',{'0':initial.skill_id})
    freeze(root/'initial_skills.json',[S.to_dict(initial)])
    freeze(root/'cold_start_complete.json',{'protocol':'experience-first','train_count':1,
          'mapping_hash':S.content_hash({'0':initial.skill_id}),'initial_skills_hash':S.content_hash([S.to_dict(initial)])})
    freeze(root/'discovery/card_hashes.json',{'0':S.content_hash(card)})
    freeze(root/'discovery/results/0.json',S.to_dict(exp)); freeze(root/'discovery/initial_skills'/ (family+'-0-patterns.json'),
          {'card_hashes':{'0':S.content_hash(card)},'raw':None,'patterns':[],'status':'insufficient_cards'})
    freeze(root/'discovery/initial_skills'/ (family+'-0.json'),S.to_dict(initial))
    freeze(root/'skills.jsonl', '') if False else None
    (root/'skills.jsonl').write_text(json.dumps(S.to_dict(initial),sort_keys=True)+'\n')
    (root/'trajectory_import.json').write_text(json.dumps({'trial_dir':str(trial),'result':result,'trajectory':str(trial/'agent/trajectory.json'),'source_model':traj.get('agent',{}).get('model_name'),'reward':reward},indent=2)+'\n')
    summary={'status':'cold_start_imported','benchmark':'terminalbench','task':'fix-git','reward':int(reward),
             'source_model':traj.get('agent',{}).get('model_name'),'trajectory_path':str(trial/'agent/trajectory.json'),
             'acceptance_rule':'candidate_mean > base_mean on the same smoke panel; ties reject',
             'next':'run Harbor-backed SkillExpand L2 when API credentials are available'}
    (root/'smoke_summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))

if __name__=='__main__': main()

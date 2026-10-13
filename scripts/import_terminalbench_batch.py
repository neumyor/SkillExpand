#!/usr/bin/env python3
"""Build a complete external-trajectory cold-start ledger from Harbor outputs."""
import argparse, json, hashlib
from pathlib import Path
from collections import defaultdict
from skillexpand import schema as S
from skillexpand.l1 import learning as L
from skillexpand.l1 import protocol as P

def freeze(path, value):
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    payload=json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)+'\n'
    if path.exists() and path.read_text()!=payload: raise ValueError('frozen artifact changed: '+str(path))
    if not path.exists(): path.write_text(payload)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--source-root',type=Path,required=True); ap.add_argument('--task-file',type=Path,required=True); ap.add_argument('--run-dir',type=Path,required=True); ap.add_argument('--source-model',default='openai/qwen3.6-flash-distill'); ap.add_argument('--method-model',default=None); args=ap.parse_args()
    source=args.source_root.resolve(); root=args.run_dir.resolve(); rows=json.loads(args.task_file.read_text())
    by_name={r['task_name']:r for r in rows}; grouped=defaultdict(list)
    for rp in sorted(source.glob('*/result.json')):
        d=json.loads(rp.read_text());
        if d.get('task_name') in by_name: grouped[d['task_name']].append((rp.parent,d))
    names=sorted(by_name); assert len(names)==89 and all(len(grouped[n])==3 for n in names), {n:len(grouped[n]) for n in names if len(grouped[n])!=3}
    # Progressive E1 routes all tasks through the Skill catalog at execution time.
    source_model = args.source_model
    assignment={i: S.SPLIT_TRAIN for i in range(89)}
    family='terminalbench.general'; train_ids=[i for i in range(89) if assignment[i]==S.SPLIT_TRAIN]
    task_rows=[{'task':by_name[n]['instruction'],'env_kwargs':{'instruction':by_name[n]['instruction'],'task_name':n},'env_name':'terminalbench'} for n in names]
    split=S.SplitPlan.make(assignment,'terminalbench',42)
    # Progressive cold start intentionally has no operational task -> family map.
    # The bootstrap Skill keeps the imported run executable; formal family/Skill
    # proposals are produced by the model-driven discovery stage before E1.
    proposal_name = 'TerminalBench repository repair bootstrap'
    proposal_definition = 'Reusable command-line inspection, repair, and verifier workflow.'
    cards={}; exps={}
    trial_manifest=[]
    for i,n in enumerate(names):
        trials=[]; rewards=[]; traj_paths=[]
        for idx,(td,d) in enumerate(sorted(grouped[n],key=lambda x:x[0].name),1):
            reward=bool((d.get('verifier_result') or {}).get('rewards',{}).get('reward',0))
            rewards.append(reward); remote=str(d.get('trial_uri','')).removeprefix('file://')
            local=str(td/'agent'/'trajectory.json'); traj_paths.append(remote or local)
            trial_manifest.append({'task_id':i,'task_name':n,'attempt_index':idx,'reward':int(reward),
                                  'source_model':source_model,'trial_dir':remote or str(td),
                                  'result_path':remote+'/result.json' if remote else str(td/'result.json'),
                                  'trajectory_path':remote+'/agent/trajectory.json' if remote else local})
            tpath=td/'agent'/'trajectory.json'; steps=json.loads(tpath.read_text()).get('steps',[]) if tpath.exists() else []
            events=[{'ref':f'e{j+1}','action':'TerminalBatch','observation':str(s.get('message') or s.get('observation') or ''),'environment':{'success':False}} for j,s in enumerate(steps) if s.get('message') or s.get('observation')]
            if not events: events=[{'ref':'e1','action':'TerminalBatch','observation':'No trajectory steps recorded.','environment':{'success':False}}]
            trials.append({'index':idx,'phase':'autonomous','status':'completed','success':reward,'termination':'verifier','trajectory':remote or local,'events':events})
        instruction=by_name[n]['instruction']; evidence=P.evidence(instruction,trials); solved=any(rewards)
        card=L.card(i,instruction,trials,{'status':'valid','claims':[]},'external_harbor','tb21-e1-batch',len,evidence=evidence,card_id=f'discovery:terminalbench.general:{i}',benchmark='terminalbench',family_id=family)
        eid=f'discovery:terminalbench.general:{i}'; exp=S.TaskExperience(eid,'terminalbench',i,instruction,family,S.SPLIT_TRAIN,solved,3,failed_trajectories=tuple(t['trajectory'] for t in trials if not t['success']),final_trajectory=trials[-1]['trajectory'] if solved else None,selection_source=S.SELECTION_UNSKILLED,trial_rewards=tuple(rewards),trial_phases=('autonomous',)*3,experience_card=card,l1_trials=tuple(trials)); exps[i]=exp; cards[i]=card
    initial=S.Skill('terminalbench.'+family,family,0,proposal_name,proposal_definition,'1. Inspect the repository and task files.\n2. Make the smallest repair supported by observed evidence.\n3. Run the provided verifier before finishing.',S.Provenance(rationale='Bootstrap only; replace with model-generated proposals before formal E1',source_experience_ids=tuple(exps[i].experience_id for i in train_ids),source_task_ids=tuple(train_ids)))
    method_model = args.method_model or args.source_model
    config={'benchmark':{'name':'terminalbench','task_prefix':'','task_file':str(args.task_file.resolve()),'max_steps':1,'num_fewshots':0,'ai_name':'terminal agent','env':{'show_admissible_commands':False},'l1':{'adapter':'skillexpand.l1.adapters:TerminalBenchAdapter'},'progressive_library':True,'rollout':{'mode':'harbor_rollout','runner_script':'/data2/liyishan/tb21-tencent-skill/scripts/run_tencent_tb21_smoke.sh'}},'agent':{'llm':method_model},'models':{k:method_model for k in ('l1_executor','cold_start','l2_planner','l2_editor','l2_reviewer','selector')}}
    freeze(root/'config.json',config); freeze(root/'split.json',S.to_dict(split)); freeze(root/'initial_skills.json',[S.to_dict(initial)]); freeze(root/'cold_start_complete.json',{'protocol':'progressive-library','train_count':len(train_ids),'initial_skills_hash':S.content_hash([S.to_dict(initial)])})
    manifest={'protocol':'progressive-library-experience-first','split':S.to_dict(split),'config':config,'task_table_hash':S.content_hash(task_rows),'prompts':{},'k':3,'supervised':False,'supervised_attempts':0,'card_batch_size':12,'skill_edit_mode':'rewrite','routing_mode':'progressive_library','acceptance_panel':'all_train','code':{'importer':'tb21-parallel-stage-v2'},'trajectory_import':{'source_root':str(source),'source_model':source_model,'trial_count':267,'valid_rollout_count':None,'coverage':'89 tasks x 3 attempts raw; validity recorded by input_coverage.json'}}
    from skillexpand.l1.protocol import projection
    freeze(root/'manifest.json',manifest); freeze(root/'discovery/card_hashes.json',{str(i):S.content_hash(projection(cards[i])) for i in train_ids})
    for i,e in exps.items(): freeze(root/'discovery/results'/f'{i}.json',S.to_dict(e))
    freeze(root/'discovery/initial_skills'/ (family+'-0-patterns.json'),{'card_hashes':{str(i):S.content_hash(projection(cards[i])) for i in train_ids},'raw':None,'patterns':[],'status':'bootstrap'})
    freeze(root/'discovery/initial_skills'/ (family+'-0.json'),S.to_dict(initial))
    skills_path = root / 'skills.jsonl'
    skill_line = json.dumps(S.to_dict(initial), ensure_ascii=False, sort_keys=True) + '\n'
    if skills_path.exists() and skills_path.read_text() != skill_line:
        raise ValueError('frozen artifact changed: ' + str(skills_path))
    if not skills_path.exists():
        skills_path.write_text(skill_line)
    (root/'trial_manifest.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False,sort_keys=True)+'\n' for x in sorted(trial_manifest,key=lambda x:(x['task_id'],x['attempt_index']))) )
    summary={'status':'cold_start_imported','benchmark':'terminalbench','tasks':89,'trials':267,'valid_rollout_count':None,'coverage':'89 tasks x 3 attempts raw; validity recorded by input_coverage.json','train_tasks':len(train_ids),'reward_1':sum(bool((json.loads((td/'result.json').read_text()).get('verifier_result') or {}).get('rewards',{}).get('reward',0)) for v in grouped.values() for td,_ in v),'source_model':source_model,'next':'run Harbor-backed L2 on jinan40'}
    freeze(root/'batch_import_summary.json',summary); print(json.dumps(summary,indent=2))
if __name__=='__main__': main()

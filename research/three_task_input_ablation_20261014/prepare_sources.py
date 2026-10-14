"""Collect preselected original historical sources, never prior research outputs."""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand')
TASKS = ('cancel-async-tasks', 'multi-source-data-merger', 'log-summary-date-ranges')
PANELS = {'E1': 'tb21-e1-empirical-final-library-20261008',
          'E4': 'tb21-e4-e3-final-library-empirical-20261010',
          'E6': 'tb21-e6-final-library-empirical-20261010'}
MEMORY = {'E1': PANELS['E1'], 'E4': 'tb21-e3-e1aligned-free-library-20261009',
          'E6': 'tb21-e6-e1aligned-qwen-experience-deepseek-evolver-20261009'}

def read(p):
    return json.loads(p.read_text())

def write(p,x):
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')

def main():
    if (ROOT/'sources_complete.json').exists():
        raise ValueError('Source collection already completed; no overwrite')
    table=read(REPO/'runs'/PANELS['E6']/'tasks.json')
    library=read(REPO/'runs'/PANELS['E6']/'library.json')
    ids=[]
    for name in TASKS:
        idx=next(i for i,t in enumerate(table) if t['task_name']==name)
        folder=ROOT/'tasks'/name/'frozen'
        rows=[]; source_manifest=[]
        for label,panel in PANELS.items():
            for attempt in range(1,4):
                slot=REPO/'runs'/panel/f'slots/{idx:02d}-{attempt}'
                rec=read(slot/'record.json')
                if rec['task_name']!=name or rec['task_id']!=idx:
                    raise ValueError('Original source task mismatch')
                result=read(Path(rec['result_path']))
                reward=(result.get('verifier_result') or {}).get('rewards',{}).get('reward')
                if rec['reward']!=reward: raise ValueError('Original reward mismatch')
                trace=read(Path(rec['trajectory_path']))
                skill_body=Path(rec['skill_file']).read_text()
                rid=f'{len(rows):02d}-{label}-{attempt}'
                archive=folder/'sources'/rid
                for src,filename in [(slot/'record.json','record.json'),(Path(rec['result_path']),'result.json'),
                                     (Path(rec['trajectory_path']),'trajectory.json'),(Path(rec['skill_file']),'SKILL.md')]:
                    archive.mkdir(parents=True,exist_ok=True);shutil.copy2(src,archive/filename)
                snapshot=Path(rec['result_path']).parent/'snapshot.json'
                sandbox=read(snapshot).get('source_sandbox_id') if snapshot.exists() else None
                if sandbox: ids.append(sandbox)
                write(archive/'source.json',{'panel':panel,'slot':str(slot),'result':rec['result_path'],
                                            'trajectory':rec['trajectory_path'],'skill':rec['skill_file'],'sandbox_id':sandbox})
                rows.append({'id':rid,'reward':reward,'skill_body':skill_body,'steps':trace['steps'],
                             'metrics':trace.get('final_metrics')})
                source_manifest.append({'id':rid,'panel':panel,'slot':str(slot),'reward':reward,'sandbox_id':sandbox})
        first=read(REPO/'runs'/PANELS['E6']/f'slots/{idx:02d}-1/record.json')
        seed=next(s for s in library if s['skill_id']==first['selection']['loaded_skill_id'])
        memory={}; memory_manifest={}
        for label,panel in MEMORY.items():
            p=REPO/'runs'/panel/f'discovery/results/{idx}.json'
            memory_manifest[label]={'path':str(p),'exists':p.exists()}
            if p.exists():
                memory[label]=read(p);write(folder/f'memory-{label}.json',memory[label])
        source_task=Path(result['config']['task']['path'])
        if source_task.name!=name: raise ValueError('Task path mismatch')
        shutil.copytree(source_task,folder/'tasks'/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        public=(source_task/'instruction.md').read_text()
        write(folder/'input.json',{'method_model':'DEEPSEEK_up5zdj','public_contract':public,
                                  'original_skill':seed,'memory':memory,'evidence':rows})
        write(folder/'task_table.json',[{'task_name':name,'instruction':public}])
        write(folder/'executor.json',result['config'])
        write(folder/'source_manifest.json',{'task_id':idx,'task_path':str(source_task),
                                            'seed_panel':PANELS['E6'],'seed_selected_slot':str(REPO/'runs'/PANELS['E6']/f'slots/{idx:02d}-1'),
                                            'history':source_manifest,'memory':memory_manifest})
    old=REPO/'runs/generic-skill-evolution-20261014'
    for p in old.rglob('record.json'):
        if 'frozen' in p.relative_to(old).parts:continue
        rec=read(p)
        if rec.get('sandbox_id'):ids.append(rec['sandbox_id'])
    ids+=read(old/'frozen/protocol.json')['excluded_sandbox_ids']
    write(ROOT/'frozen/historical_sandbox_ids.json',sorted(set(ids)))
    write(ROOT/'sources_complete.json',{'tasks':list(TASKS),'historical_ids':len(set(ids)),
                                       'source_policy':'preselected original E1/E4/E6 panels and existing memory only'})
if __name__=='__main__':main()

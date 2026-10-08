"""Bounded task-local repair with atomic trial checkpoints and an exclusive lock."""
import copy
import json
from contextlib import contextmanager
from pathlib import Path
from langchain.schema import HumanMessage
from omegaconf import OmegaConf
from skillexpand.persistence.io import exclusive_lock, save
from skillexpand.reliability.errors import FrozenProtocolChanged, InvalidInput, JournalConflict, classify
from skillexpand.l1.adapters import resolve
from skillexpand.l1.adapters import PROMPT_FIELDS
from skillexpand.l1 import protocol as P
from skillexpand.l1 import learning as L
from skillexpand import schema as S

@contextmanager
def checkpoint(path):
    if path is None:
        yield None
        return
    path = Path(path).resolve()
    with exclusive_lock(path.with_suffix('.lock')):
        yield path


def _error_entry(exc, stage):
    # Type name only: the message may echo provider payloads into the checkpoint.
    return {'stage': stage, 'error': type(exc).__name__, 'category': classify(exc).value}


def run(agent, cfg, task_id, family_id, split, skill, selected_skill_id,
        selection_source, selection_reason, selection_raw, k=4, supervised=True,
        supervised_attempts=1, checkpoint_path=None, evolution_round=0):
    if k < 1:
        raise InvalidInput('autonomous attempts must be >= 1')
    if supervised_attempts < 0:
        raise InvalidInput('supervised attempts must be >= 0')
    if split != S.SPLIT_TRAIN:
        raise InvalidInput('L1 repair is train-only')
    adapter = resolve(cfg)
    adapter.configure(agent)
    settings = cfg.benchmark.get('l1', {})
    identity = {'protocol': P.VERSION, 'task': agent.task,
                'env': agent.tasks[task_id]['env_kwargs'],
                'adapter': type(adapter).__module__ + ':' + type(adapter).__name__,
                'revision': adapter.revision, 'prompts': {key: getattr(adapter,key) for key in PROMPT_FIELDS},
                'k': k, 'supervised': supervised, 'supervised_attempts': supervised_attempts,
                'model': cfg.agent.llm,
                'skill': {'key': skill.key, 'body': skill.body} if skill else None,
                'evolution_round': evolution_round,
                'selected_skill_id': selected_skill_id, 'settings': dict(settings),
                'max_steps': agent.max_steps,
                'action_budget': [adapter.action_format_attempts, adapter.action_only_attempts]}
    identity = OmegaConf.to_container(OmegaConf.create(identity), resolve=True)
    signature = S.content_hash(identity)
    with checkpoint(checkpoint_path) as path:
        data = json.loads(path.read_text()) if path and path.exists() else {
            'protocol': P.VERSION, 'signature': signature, 'trials': [], 'state': {},
            'guidance': None, 'guidance_checked': False, 'reflections': [], 'errors': []}
        if data['signature'] != signature:
            raise FrozenProtocolChanged('L1 checkpoint configuration/task/skill mismatch; use a new run directory')
        data['identity'] = identity
        if data.get('experience'):
            return S.from_dict(S.TaskExperience, data['experience']), agent
        if path is not None:
            from skillexpand.persistence.usage import attach_usage
            attach_usage([agent.llm],path.with_suffix('.usage.json'))
        trials = data['trials']
        for trial in trials:
            if trial['status'] == 'running':
                trial.update(status='interrupted', success=False, termination='interrupted',
                             trajectory='\n'.join(e.get('model_text','') + '\n' +
                                 e.get('observation','') for e in trial['events']))
                data['state'] = P.refresh(data['state'],trials)
                save(path,data)

        def reflect(phase):
            key = phase + ':' + str(len(trials))
            previous = next((r for r in data['reflections'] if r['key']==key),None)
            if previous is not None:
                return previous['state']
            guided = phase == 'supervised'
            guidance = adapter.render_guidance(data['guidance']) if guided else None
            payload = P.context(agent.task,trials,data['state'],guidance,agent.token_counter, adapter=adapter)
            try:
                raw = agent.llm([HumanMessage(content=adapter.reflection_prompt(guided)),
                                HumanMessage(content=json.dumps(payload,ensure_ascii=False))],
                               stop=[],replace_newline=False)
            except Exception as exc:
                data['errors'].append(dict(_error_entry(exc, 'reflection'), key=key))
                save(path, data)
                raise
            parsed = P.parse(raw,payload['evidence'],guided)
            # Program-owned outcome/feedback always takes precedence over model fields.
            state = P.refresh(parsed,trials)
            data['reflections'].append({'key':key,'phase':phase,'purpose': 'repair','input':payload,'raw':raw,'state':state})
            data['state'] = state
            save(path,data)
            return state

        while not any(t['success'] for t in trials):
            # Infrastructure interruptions are retained for audit but do not
            # consume the model's autonomous repair budget. A timeout/reconnect
            # must be retryable on resume without being mislabeled as a hard task
            # failure.
            n = sum(t['phase'] == 'autonomous' and t['status'] == 'completed'
                    for t in trials)
            phase = 'autonomous' if n < k else 'supervised'
            if phase == 'supervised':
                if (not supervised or supervised_attempts == 0 or
                        sum(t['phase'] == 'supervised' and t['status'] == 'completed'
                            for t in trials) >= supervised_attempts):
                    break
                if not data['guidance_checked']:
                    data['guidance'] = adapter.prepare_guidance(agent,trials)
                    data['guidance_checked'] = True
                    save(path,data)
                if data['guidance'] is None:
                    break
                if not (trials and trials[-1]['status'] == 'interrupted' and trials[-1]['phase'] == phase):
                    data['state'] = reflect(phase)
            elif trials and trials[-1]['status']=='completed':
                data['state'] = reflect(phase)
            trial = {'index':len(trials)+1,'phase':phase,'status':'running',
                     'success':False,'events':[], 'repair_state':copy.deepcopy(data['state'])}
            trials.append(trial)
            save(path,data)
            def on_event(events):
                trial['events'] = events
                save(path,data)
            guidance = adapter.render_guidance(data['guidance']) if phase=='supervised' else ''
            if guidance:
                guidance += '\nUse the completed repair state; now execute. Do not repeat the diagnosis.'
            try:
                result = agent.execute_trial(adapter,trial['repair_state'],guidance,on_event)
            except Exception as exc:
                # Interrupted trials never consume the autonomous budget; the
                # category tells a resumed run whether retrying can help.
                trial.update(status='interrupted',success=False,termination='execution_error',
                             error=type(exc).__name__+': '+str(exc),failure_category=classify(exc).value,
                             trajectory='\n'.join(e.get('model_text','') for e in trial['events']))
                data['state'] = P.refresh(data['state'],trials)
                save(path,data)
                raise
            trial.update(result,status='completed')
            trial['feedback'] = adapter.build_feedback(agent,trial)
            data['state'] = P.refresh(data['state'],trials)
            save(path,data)

        completed = [t for t in trials if t['status']=='completed']
        if not completed or trials[-1]['status']=='interrupted':
            raise JournalConflict('L1 budget ended on an interrupted trial; inspect the checkpoint')
        # Final extraction uses completed episodes without promoting retry hypotheses.
        if 'synthesis' not in data:
            payload = P.context(agent.task, completed, {}, None, agent.token_counter, adapter=adapter)
            try:
                raw = agent.llm([HumanMessage(content=adapter.reflection_prompt(extraction=True)),
                                HumanMessage(content=json.dumps(payload, ensure_ascii=False))],
                               stop=[], replace_newline=False)
            except Exception as exc:
                data['errors'].append(_error_entry(exc, 'extraction'))
                save(path, data)
                raise
            data['synthesis'] = {'input': payload, 'raw': raw,
                'initial_result': L.parse(raw, payload['evidence'],
                                          any(t['success'] for t in completed))}
            save(path, data)
        synthesis = data['synthesis']
        if 'result' not in synthesis:
            initial = synthesis['initial_result']
            if initial['rejected'] and len(initial['claims']) < 2:
                if 'repair' not in synthesis:
                    repair_payload = L.repair_input(synthesis['input'], initial)
                    try:
                        repair_raw = agent.llm([
                            HumanMessage(content=adapter.reflection_prompt(extraction=True)),
                            HumanMessage(content=L.REPAIR_INSTRUCTION),
                            HumanMessage(content=json.dumps(repair_payload, ensure_ascii=False))],
                            stop=[], replace_newline=False)
                    except Exception as exc:
                        data['errors'].append(_error_entry(exc, 'extraction_repair'))
                        save(path, data)
                        raise
                    synthesis['repair'] = {'input': repair_payload, 'raw': repair_raw,
                        'parsed': L.parse(repair_raw, synthesis['input']['evidence'],
                                          any(t['success'] for t in completed),
                                          max_claims=repair_payload['open_slots'])}
                    save(path, data)
                synthesis['result'] = L.finish(initial, synthesis['repair']['parsed'])
            else:
                synthesis['result'] = L.finish(initial)
            save(path, data)
        experience_id = (('discovery' if skill is None else 'evolution') + ':' +
                         (str(evolution_round) if skill is not None else '0') + ':' +
                         S.TaskExperience.make_id(cfg.benchmark.name,family_id,task_id))
        card = L.card(task_id, agent.task, trials, synthesis['result'],
                      adapter.guidance_source(data['guidance']) if data['guidance'] is not None else None,
                      f'task-{task_id}-round-{evolution_round}', agent.token_counter,
                      target=int(settings.get('card_target_tokens', 400)),
                      limit=int(settings.get('card_limit_tokens', 800)),
                      evidence=P.evidence(agent.task, completed, adapter=adapter),
                      card_id=experience_id, benchmark=cfg.benchmark.name,
                      family_id=family_id, evolution_round=evolution_round,
                      skill_key=skill.key if skill else None)
        solved = [t for t in completed if t['success']]
        exp = S.TaskExperience(
            experience_id=experience_id,
            benchmark=cfg.benchmark.name,task_id=task_id,task=agent.task,
            family_id=family_id,split=split,reward=bool(solved),num_trials=len(completed),
            evolution_round=evolution_round,
            initial_skill_key=skill.key if skill else None,
            selected_skill_id=selected_skill_id,selection_source=selection_source,
            selection_reason=selection_reason,selection_raw=selection_raw,
            failed_trajectories=tuple(t['trajectory'] for t in completed if not t['success']),
            final_trajectory=solved[-1]['trajectory'] if solved else None,
            reflections=tuple(json.dumps(r['state'],ensure_ascii=False) for r in data['reflections']),
            trial_rewards=tuple(t['success'] for t in completed),
            trial_phases=tuple(t['phase'] for t in completed),experience_card=card,
            l1_audit_path=str(path) if path else None,l1_trials=tuple(trials) if path is None else ())
        data['experience'] = S.to_dict(exp)
        save(path,data)
        return exp,agent

"""Run the native generic L2 package editor on fixed authorized evidence."""
import argparse
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from skillexpand import schema as S
from skillexpand.l2.editor import SkillEditor
from skillexpand.runtime.llm_relay import relay_from_env, coalesce_sse, iter_sse_events
from evaluate import project_tool_results


ROOT = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def project(frozen):
    rows = []
    for row in frozen['evidence']:
        steps = project_tool_results({'steps': row['steps']})
        rows.append({'id': row['id'], 'official_reward': row['reward'],
                     'loaded_skill': row['skill_body'], 'steps': steps})
    return {'public_contract': frozen['public_contract'],
            'frozen_memory': {key: value['experience_card'] for key, value in frozen['memory'].items()},
            'historical_evidence': rows,
            'evidence_status': 'Historical tool results may echo source. Legacy memory claims remain unverified claims. No restarted trial is appended to original evidence.'}


def generate(number):
    frozen = read(ROOT / 'frozen/input.json')
    evidence = read(ROOT / 'frozen/projected_input.json')
    if evidence != project(frozen):
        raise ValueError('Fixed input projection drift')
    base = S.from_dict(S.Skill, frozen['original_skill'])
    feedback = []
    if number > 1:
        previous = ROOT / f'development/candidate-{number-1:03d}'
        base = S.from_dict(S.Skill, read(previous / 'skill.json'))
        feedback = [read(path) for path in sorted(previous.glob('dev-batch-*/trial-*/feedback.json'))]
    out = ROOT / f'development/candidate-{number:03d}'
    if (out / 'skill.json').exists():
        return
    attempts = out / 'generation_attempts'
    attempt = attempts / f'{len(list(attempts.glob("*")))+1:03d}'
    attempt.mkdir(parents=True, exist_ok=False)
    os.environ['OPENAI_API_KEY'] = os.environ['TB21_METHOD_API_KEY']
    relay = relay_from_env()

    def llm(messages, **kwargs):
        payload = {'model': frozen['method_model'], 'stream': True,
                   'max_tokens': 65536, 'enable_thinking': True,
                   'messages': [{'role': 'system' if message.type == 'system' else 'user',
                                 'content': message.content} for message in messages]}
        write(attempt / 'request.json', payload)
        started = time.time()
        try:
            relay.start()
            with (attempt / 'response.sse').open('wb') as stream:
                def chunks():
                    for chunk in relay.transport.stream(payload):
                        chunk = chunk.encode() if isinstance(chunk, str) else chunk
                        stream.write(chunk)
                        stream.flush()
                        yield chunk
                result = coalesce_sse(chunks())
            write(attempt / 'completion.json', result)
            usage = [event['usage'] for event in iter_sse_events(
                (attempt / 'response.sse').read_bytes().splitlines(keepends=True)) if event.get('usage')]
            write(attempt / 'generation.json', {'seconds': time.time()-started,
                'usage': usage[-1] if usage else result.get('usage'), 'response_model': result.get('model')})
            return result['choices'][0]['message']['content']
        except Exception as exc:
            write(attempt / 'error.json', {'error_type': type(exc).__name__,
                'error_class': getattr(exc, 'error_class', None), 'seconds': time.time()-started})
            raise
        finally:
            relay.close()

    editor = SkillEditor(SimpleNamespace(llm=llm), SimpleNamespace(version=0))
    outcome = editor.propose_package(base, evidence, feedback)
    write(attempt / 'outcome.json', S.to_dict(outcome))
    if not outcome.proposed:
        raise ValueError('No valid package proposed: ' + outcome.reason)
    write(out / 'skill.json', S.to_dict(outcome.candidate.skill))
    write(out / 'request.json', read(attempt / 'request.json'))
    write(out / 'provenance.json', {'native_method': 'SkillEditor.propose_package',
        'generation_attempt': str(attempt), 'input': 'frozen/projected_input.json',
        'previous_candidate': str(previous) if number > 1 else None,
        'feedback_paths': [str(path) for path in sorted(previous.glob('dev-batch-*/trial-*/feedback.json'))] if number > 1 else []})
    print(json.dumps({'candidate': number, 'status': 'generated', 'skill_key': outcome.candidate.skill.key}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', type=int, default=1)
    args = parser.parse_args()
    generate(args.candidate)

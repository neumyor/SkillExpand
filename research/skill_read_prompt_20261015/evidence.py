"""Conservative offline proof of model-visible Skill body before task operations."""
import json
import shlex


def browsing(command):
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return True
    if any(token in words for token in (';', '&&', '||', '|', '>', '>>', '<', '&')):
        return False
    if any('$' in token or '`' in token or '\n' in token for token in words):
        return False
    if words[0] in ('pwd', 'cd', 'ls'):
        return True
    if words[0] not in ('cat', 'head', 'tail', 'sed'):
        return False
    paths = [word for word in words[1:] if word.startswith('/')]
    return bool(paths) and all(path.startswith('/opt/openclaw-skills/') for path in paths)


def evaluate(trace, requests, library):
    bodies = {}
    for skill in library:
        bodies['/opt/openclaw-skills/' + skill['skill_id'] + '/SKILL.md'] = [
            line.strip() for line in skill['body'].splitlines() if len(line.strip()) >= 30]
    first_task, candidates, all_commands = None, [], []
    for index, step in enumerate(trace.get('steps', [])):
        commands = [call.get('arguments', {}).get('keystrokes', '')
                    for call in step.get('tool_calls') or []
                    if call.get('function_name') != 'mark_task_complete']
        all_commands.append({'step_id': step.get('step_id'), 'commands': commands})
        pure = all(browsing(command) for command in commands)
        if commands and not pure and first_task is None:
            first_task = {'step_index': index, 'step_id': step.get('step_id'), 'commands': commands}
        observation = '\n'.join(row.get('content', '') for row in (step.get('observation') or {}).get('results', []))
        for path, lines in bodies.items():
            read_calls = [command for command in commands if path in command and
                          command.lstrip().split(' ', 1)[0] in ('cat', 'head', 'tail', 'sed')]
            matches = [line for line in lines if line in observation]
            if not read_calls or len(matches) < 2 or sum(map(len, matches)) < 120:
                continue
            returned = []
            for request_index, request in enumerate(requests):
                messages = request.get('messages', [])
                for message_index, message in enumerate(messages):
                    text = message.get('content', '')
                    if message_index and message.get('role') == 'user' and isinstance(text, str) and all(line in text for line in matches):
                        returned.append({'request_index': request_index, 'message_index': message_index})
                        break
            candidates.append({'step_index': index, 'step_id': step.get('step_id'), 'path': path,
                'commands': read_calls, 'body_lines': matches, 'observation': observation,
                'returned_to_model': returned, 'pure_read_batch': pure})
    early = [row for row in candidates if row['pure_read_batch'] and row['returned_to_model'] and
             (first_task is None or row['step_index'] < first_task['step_index'])]
    return {'reading_order_pass': bool(early), 'qualifying_reads': early,
            'all_substantive_reads': candidates, 'first_task_operation': first_task,
            'commands_by_step': all_commands}

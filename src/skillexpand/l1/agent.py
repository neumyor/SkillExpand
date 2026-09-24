"""Shared bounded execution loop; only source L1 orchestrates retries and guidance."""
import json
from langchain.schema import HumanMessage
from skillexpand.runtime.agent.expel import ExpelAgent
from skillexpand.runtime.agent.react import ReactAgent


class RepairAgent(ExpelAgent):
    def insert_before_task_prompt(self):
        if getattr(self, 'rules', '') and not getattr(self, 'no_rules', True):
            self.prompt_history.extend(self.rule_template.format_messages(rules=self.rules))
        if getattr(self, 'repair_state', None):
            self.prompt_history.append(HumanMessage(content='Current task repair state (hypotheses '
                'are unverified):\n' + json.dumps(self.repair_state, ensure_ascii=False)))
        if getattr(self, 'supervision_text', ''):
            self.prompt_history.append(HumanMessage(content=self.supervision_text))

    def execute_trial(self, adapter, state, supervision_text='', on_event=None):
        self.repair_state = state
        self.supervision_text = supervision_text
        self.env.reset()
        if hasattr(getattr(self.env, 'explorer', None), 'reset'):
            self.env.explorer.reset()
        self.reward = self.terminated = self.truncated = False
        ReactAgent.reset(self)
        events = []
        termination = 'step_budget'
        for step in range(1, self.max_steps + 1):
            self.curr_step = step
            if self.is_truncated():
                termination = 'context_or_step_budget'
                break
            action = None
            # Bounded formatting recovery; no placeholder is sent to the environment.
            normal = adapter.action_format_attempts
            for request in range(normal + adapter.action_only_attempts):
                action_only = request >= normal
                if action_only:
                    # Bypass upstream's dynamic Thought suffix and newline stops:
                    # a valid action must be emitted, never inferred from prose.
                    prompt = list(self.prompt_history) + [HumanMessage(content=
                        f'Action-only recovery at step {step}. ' +
                        adapter.action_recovery_instructions)]
                    raw = self.llm(prompt, stop=[], replace_newline=False)
                else:
                    raw = self.prompt_agent()
                message, kind, extra = self.llm_parser(raw, step, False)
                self.prompt_history.append(message)
                self.print_message(message)
                event = {'ref': f'e{len(events)+1}', 'model_text': message.content,
                         'request_mode': 'action_only' if action_only else 'normal'}
                events.append(event)
                if on_event:
                    on_event(events)
                if kind == 'action' and extra.get('action') and extra['action'] != 'N/A':
                    action = extra['action'].strip()
                    if adapter.repeated_action_is_stalled(events[:-1], action):
                        self.prompt_history.append(HumanMessage(content='That local query was already '
                            'executed in this trial and cannot produce new evidence. Choose another '
                            'document, inspect a keyword, or submit an answer.'))
                        event['blocked_action'] = action
                        action = None
                        continue
                    break
                self.prompt_history.append(HumanMessage(content='Take one valid Action now using '
                    'the benchmark action syntax. Do not emit another Thought.'))
            if action is None:
                termination = 'no_action_progress'
                break
            event['action'] = action
            if on_event:
                on_event(events)
            observation, self.reward, self.terminated, self.truncated, _ = self.env.step(action)
            event['observation'] = str(observation)
            event['environment'] = {'success': bool(self.env.success_fn()),
                                    'terminated': bool(self.terminated), 'truncated': bool(self.truncated)}
            # Both supported L1 environments use append observations. Other adapters may
            # format their observation text, but each event remains independently auditable.
            formatted, _ = self.observation_formatter(observation, step=step)
            self.prompt_history.append(formatted)
            self.prompt_history = self.collapse_prompts(self.prompt_history)
            if on_event:
                on_event(events)
            if self.env.success_fn():
                termination = 'success'
                break
            if self.env.is_terminated():
                termination = getattr(self.env, 'termination_reason', None) or 'environment_terminated'
                break
        return {'success': bool(self.env.success_fn()), 'termination': termination,
                'events': events, 'trajectory': '\n'.join(
                    e['model_text'] + ('\nObservation: ' + e['observation']
                                       if 'observation' in e else '') for e in events)}

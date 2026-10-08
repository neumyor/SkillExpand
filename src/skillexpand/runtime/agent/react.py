"""ExpeL's ReAct executor: prompt construction around one benchmark environment.

Trajectory retrieval, critique/rule learning and ExpeL's own reflection loop were
removed; the L1 repair loop (:mod:`skillexpand.l1.agent`) drives execution.
"""
from typing import List, Callable, Dict, Any, Union
from functools import partial

from langchain.prompts import PromptTemplate
from langchain.schema import ChatMessage

from skillexpand.benchmarks import BaseEnv
from skillexpand.runtime.agent.base import BaseAgent
from skillexpand.runtime.prompts.templates.human import human_instruction_fewshot_message_prompt
from skillexpand.runtime.prompts.templates.human import human_task_message_prompt
from skillexpand.runtime.utils import print_message
from skillexpand.runtime.utils import token_counter


class ReactAgent(BaseAgent):
    """A ReAct executor with an optional Skill injected as rules."""
    def __init__(self,
                 name: str,
                 system_instruction: Union[str, Dict[str, str]],
                 human_instruction: Callable,
                 fewshots: Union[List[str], Dict[str, List[str]]],
                 system_prompt: Callable,
                 env: BaseEnv,
                 llm: str,
                 llm_builder: Callable,
                 openai_api_key: str,
                 tasks: List[Dict[str, Any]],
                 max_steps: int,
                 llm_parser: Callable,
                 observation_formatter: Callable,
                 rule_template: PromptTemplate,
                 task_idx: int = 0,
                 benchmark_name=None,
                 ) -> None:
        self.benchmark_name = benchmark_name
        self.name = name
        self.tasks = tasks
        self.task_idx = task_idx
        self.all_system_instruction = system_instruction
        self.human_instruction = human_instruction
        self.human_instruction_kwargs = {'max_steps': max_steps}
        self.all_fewshots = fewshots
        self.system_prompt = system_prompt
        self.rule_template = rule_template
        self.prompt_history = []
        self.max_steps = max_steps
        self.llm_parser = llm_parser
        self.observation_formatter = observation_formatter

        self.env = env(**self.tasks[self.task_idx]['env_kwargs'], max_steps=self.max_steps)
        self.env.reset()
        self.task = self.tasks[self.task_idx]['task']
        self.reset()
        self.truncated, self.reward, self.terminated = False, False, False
        self.print_message = print_message

        self.llm = llm_builder(llm_name=llm, openai_api_key=openai_api_key)
        del openai_api_key
        self.token_counter = partial(token_counter, llm=llm, tokenizer=getattr(self.llm, 'tokenizer', None))

    def prompt_agent(self) -> str:
        self.prompt_history = self.collapse_prompts(self.prompt_history)
        return self.llm(self.prompt_history, stop=['\n', '\n\n'])

    def _build_fewshot_prompt(
        self,
        fewshots: List[str],
        prompt_history: List[ChatMessage],
        instruction_prompt: PromptTemplate,
        instruction_prompt_kwargs: Dict[str, Any],
    ) -> None:
        if human_instruction_fewshot_message_prompt is not None and instruction_prompt is not None:
            prompt_history.append(
                human_instruction_fewshot_message_prompt('message_style_kwargs').format_messages(
                    instruction=instruction_prompt.format_messages(
                        **instruction_prompt_kwargs)[0].content,
                    fewshots='\n\n'.join(fewshots)
                )[0]
            )

    def _build_agent_prompt(self) -> None:
        system_prompt = self.system_prompt.format_messages(
            instruction=self.system_instruction, ai_name=self.name
        )
        self.prompt_history.extend(system_prompt)
        self._build_fewshot_prompt(
            fewshots=self.fewshots, prompt_history=self.prompt_history,
            instruction_prompt=self.human_instruction,
            instruction_prompt_kwargs=self.human_instruction_kwargs,
        )
        self.prompt_history = self.collapse_prompts(self.prompt_history)
        self.log_idx = len(self.prompt_history)
        self.insert_before_task_prompt()

        self.prompt_history.append(human_task_message_prompt.format_messages(task=self.remove_task_suffix(self.task))[0])
        self.prompt_history = self.collapse_prompts(self.prompt_history)
        self.pretask_idx = len(self.prompt_history)
        return self.prompt_history

    def reset(self) -> None:
        self.prompt_history = []
        self.update_prompt_components()
        self.curr_step = 1
        self._build_agent_prompt()

    def insert_before_task_prompt(self) -> None:
        return

    def collapse_prompts(self, prompt_history: List[ChatMessage]) -> List[ChatMessage]:
        """Courtesy of GPT4"""
        if not prompt_history:
            return []

        new_prompt_history = []
        scratch_pad = prompt_history[0].content
        last_message_type = type(prompt_history[0])

        for message in prompt_history[1:]:
            current_message_type = type(message)
            if current_message_type == last_message_type:
                scratch_pad += '\n' + message.content
            else:
                new_prompt_history.append(last_message_type(content=scratch_pad))
                scratch_pad = message.content
                last_message_type = current_message_type

        # Handle the last accumulated message
        new_prompt_history.append(last_message_type(content=scratch_pad))

        return new_prompt_history

    def update_prompt_components(self) -> None:
        #####################
        # Updating fewshots #
        #####################
        if isinstance(self.all_fewshots, dict):
            self.fewshots = self.all_fewshots[self.env.env_name]
        elif isinstance(self.all_fewshots, list):
            self.fewshots = self.all_fewshots

        #########################
        # Updating instructions #
        #########################
        if isinstance(self.all_system_instruction, str):
            self.system_instruction = self.all_system_instruction
        elif isinstance(self.all_system_instruction, dict):
            self.system_instruction = self.all_system_instruction[self.env.env_name]
        # if system gives instruction, then human instruction is empty
        self.human_instruction_kwargs['instruction'] = ''
        self.num_fewshots = len(self.fewshots)

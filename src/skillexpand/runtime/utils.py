"""Shared helpers used by the current executor and benchmark adapters."""

import random
import string
from typing import List, Callable, Any, Union

import tiktoken
from langchain.schema import (
    ChatMessage
)

from skillexpand.runtime.prompts import FEWSHOTS

import math
import pickle
import re


ENV_NAMES = [
            'pick_and_place',
            'pick_clean_then_place',
            'pick_heat_then_place',
            'pick_cool_then_place',
            'look_at_obj',
            'pick_two_obj'
        ]

TASK_ENV_NAMES = [
    ('clean', 'pick_clean_then_place'),
    ('hot', 'pick_heat_then_place'),
    ('heat', 'pick_heat_then_place'),
    ('cool', 'pick_cool_then_place'),
    ('look', 'look_at_obj'),
    ('examine', 'look_at_obj'),
    ('two', 'pick_two_obj'),
    ('put', 'pick_and_place') # last one must be at last position
]

class Count:
    """
    Class for reflection counting.
    """
    def __init__(self, maximum):
        self.count = 0
        self.maximum = maximum

    def increment(self):
        self.count += 1
        if self.count > self.maximum:
            self.count = self.maximum

    def is_beginning(self):
        return self.count == 1

    def reset(self):
        self.count = 0

    def is_maximum(self):
        return self.count == self.maximum

def random_divide_list(lst: List[Any], k: int):
    """
    Divides the list into chunks, each with maximum length k.

    Args:
        lst: The list to be divided.
        k: The maximum length of each chunk.

    Returns:
        A list of chunks.
    """
    random.shuffle(lst)
    if len(lst) <= k:
        return [lst]
    else:
        num_chunks = math.ceil(len(lst) / k)
        chunk_size = math.ceil(len(lst) / num_chunks)
        return [lst[i*chunk_size:(i+1)*chunk_size] for i in range(num_chunks)]


#: Tiktoken encoding used to approximate token counts for models that are not in
#: tiktoken's OpenAI model registry (e.g. a locally served 7B model).
FALLBACK_TOKEN_ENCODING = 'cl100k_base'


def token_counter(text: str, llm: str = 'gpt-3.5-turbo', tokenizer: Callable = None) -> int:
    """
    Counts the number of tokens in the text.

    Upstream raised NotImplementedError for any model name without 'gpt' in it.
    That makes ExpeL unrunnable against a local OpenAI-compatible server: this
    function is called on every step (``print_message``), on every truncation
    budget, and by ``get_fewshot_max_tokens``, so an unknown name aborts the run
    before the first action rather than degrading gracefully.

    Token counts here only ever drive budgeting and truncation -- never
    correctness -- so an approximation is strictly better than a crash. Callers
    that have the real tokenizer should pass it.

    Args:
        text: The text to be counted.
        llm: The language model name.
        tokenizer: The tokenizer to be used.
    """
    if tokenizer is not None:
        return len(tokenizer.encode(text))

    if 'gpt' in llm:
        return len(tiktoken.encoding_for_model(llm).encode(text))

    return len(tiktoken.get_encoding(FALLBACK_TOKEN_ENCODING).encode(text))

def print_message(message: ChatMessage, token_counter: Callable = None, testing: bool = True, extra_text: str = '') -> None:
    """
    Prints the formatted message.
\x20\x20\x20\x20
    Args:
        message: The message to be printed.
        token_counter: A function that takes in a string and returns the number of tokens in the string.
        testing: Add message type and token count in testing mode.
        extra_text: Extra text to be printed after the message in testing mode.
    """
    if testing:
        message = f"$$${message.type}$$$\t{message.content}\t$$${message.type}$$$"
        if token_counter is not None:
            message += f"\t***{token_counter(message)} tokens***"
            message += extra_text
        print(message)
    else:
        print(message.content)

def parse_action(string: str):
    """
    Parse action string into action type and argument for HotpotQA and Fever.
\x20\x20\x20\x20
    Args:
        string: action string
\x20\x20\x20\x20
    Returns:
        action_type: action type
        argument: argument
    """
    pattern = r'^(\w+)\[(.+)\]$'
    match = re.match(pattern, string)

    if match:
        action_type = match.group(1)
        argument = match.group(2)
        return action_type, argument

    else:
        return None, None

def normalize_answer(s: str):
    """
    Lower text and remove punctuation, articles and extra whitespace.
\x20\x20\x20\x20
    Args:
        s: string to normalize

    Returns:
        normalized string
    """
    def remove_articles(text):
        return re.sub(r"\b(a|an|the)\b", " ", text)

    def white_space_fix(text):
        return " ".join(text.split())

    def remove_punc(text):
        exclude = set(string.punctuation)
        return "".join(ch for ch in text if ch not in exclude)

    def lower(text):
        return text.lower()

    return white_space_fix(remove_articles(remove_punc(lower(s))))

def EM(answer, key) -> bool:
    """
    Exact match between answer and key.

    Args:
        answer: answer
        key: key
\x20\x20\x20\x20
    Returns:
        True if exact match, else False
    """
    return normalize_answer(answer) == normalize_answer(key)


def save_trajectories_log(path: str, log: str = None, dicts: list = None, true_log: str = None, save_log: bool = True, save_dict: bool = True, save_true_log: bool = True, run_name: str = 'run') -> None:
    """
    Saves the log and the dict to the path.
\x20\x20\x20\x20
    Args:
        path: The path to save the log and the dictionaries.
        log: The log to be saved.
        dicts: The dict to be saved.
        true_log: The true log to be saved.
        save_log: Whether to save the log.
        save_dict: Whether to save the dictionaries.
        save_true_log: Whether to save the true log.
        run_name: The name of the run.
    """
    if save_log:
        with open(f'{path}/{run_name}.txt', 'w') as f:
            f.write(log)
    if save_dict:
        with open(f'{path}/{run_name}.pkl', 'wb') as f:
            pickle.dump(dicts, f)
    if save_true_log:
        with open(f'{path}/{run_name}_true.txt', 'w') as f:
            f.write(true_log)


def get_env_name_from_gamefile(gamefile: str) -> Union[str, None]:
    """
    Gets the environment name from the gamefile for ALFWorld.

    Args:
        gamefile: The gamefile.

    Returns:
        The environment name.
    """
    for k in ENV_NAMES:
        if k in gamefile:
            return k

def get_env_name_from_task(task: str, benchmark: str) -> Union[str, None]:
    """
    Gets the environment name from the task instruction for ALFWorld.

    Args:
        task: The task.
        benchmark: The benchmark name.

    Returns:
        The environment name.
    """
    if benchmark == 'alfworld':
        for k, v in TASK_ENV_NAMES:
            if k in task:
                return v
    else:
        return benchmark


def get_fewshot_max_tokens(benchmark: str) -> int:
    """
    Gets the maximum number of tokens in the fewshot tasks for the given benchmark.
\x20\x20\x20\x20
    Args:
        benchmark: The benchmark name.
\x20\x20\x20\x20
    Returns:
        The maximum number of tokens.
    """
    fewshots = FEWSHOTS[benchmark]
    if isinstance(fewshots, dict):
        return max([max([token_counter(f) for f in fs]) for fs in fewshots.values()])
    elif isinstance(fewshots, list):
        return max([token_counter(f) for f in fewshots])

import re
import string
from typing import Tuple
from typing import Any

from langchain import Wikipedia
from langchain.agents.react.base import DocstoreExplorer

from skillexpand.benchmarks.base import BaseEnv


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


class ContextExplorer:
    """Small deterministic docstore used by SearchQA rows with supplied context."""

    def __init__(self, context: Any):
        if isinstance(context, str):
            self.pages = [context]
        elif isinstance(context, (list, tuple)):
            self.pages = []
            for item in context:
                if isinstance(item, dict):
                    self.pages.append(str(item.get('text') or item.get('context') or item))
                else:
                    self.pages.append(str(item))
        else:
            self.pages = [str(context)]
        self.last = ''

    def reset(self):
        self.last = ''

    def search(self, query: str) -> str:
        terms = [term.lower() for term in str(query).split() if term]
        ranked = sorted(self.pages,
                        key=lambda page: sum(term in page.lower() for term in terms),
                        reverse=True)
        self.last = ranked[0] if ranked else ''
        return self.last

    def lookup(self, keyword: str) -> str:
        if not self.last:
            raise ValueError('no page has been searched')
        needle = str(keyword).lower()
        sentences = [part.strip() for part in self.last.replace('\n', ' ').split('.')]
        matches = [sentence for sentence in sentences if needle in sentence.lower()]
        return '. '.join(matches) if matches else self.last

class QAEnv(BaseEnv):
    def __init__(self,
                 question: str,
                 key: str,
                 context: Any = None,
                 max_steps: int = 6,
                 explorer: DocstoreExplorer = DocstoreExplorer(Wikipedia())):

        self.question = question
        self.key = key
        self.max_steps = max_steps
        self.explorer = ContextExplorer(context) if context is not None else explorer
        self.task = """multi-hop QA. The agent was given access to a Docstore API environment and a question to answer. The agent can search for pages related to the question, lookup keywords in the pages, and finish with an answer."""
        self.env_name = 'hotpotqa'

        self.reset()

    def reset(self):
        self.curr_step = 1
        self.answer = ''
        self.terminated = False
        # A retry is a fresh episode.  ContextExplorer keeps the last page as
        # mutable state, so retaining it would let Lookup in the next trial read
        # evidence that was never searched after reset.
        if hasattr(self.explorer, 'reset'):
            self.explorer.reset()

    def step(self, action: str) -> Tuple[str, bool, bool, bool, bool]:
        action_type, argument = parse_action(action)

        if action_type == 'Finish':
            self.answer = argument
            if self.success_fn():
                observation = 'Answer is CORRECT'
            else:
                observation = 'Answer is INCORRECT'
            self.terminated = True
        elif action_type == 'Search':
            # Provider/tool failure is an interrupted unit, never task evidence.
            observation = self.explorer.search(argument).strip('\n').strip()
        elif action_type == 'Lookup':
            try:
                observation = self.explorer.lookup(argument).strip('\n').strip()
            except ValueError:
                observation = 'The last page Searched was not found, so you cannot Lookup a keyword in it. Please try one of the similar pages given.'
        else:
            observation = 'Invalid Action. Valid Actions are Lookup[<topic>] Search[<topic>] and Finish[<answer>].'

        self.curr_step += 1
        self.reward = self.success_fn()
        self.terminated = self.is_terminated()
        self.truncated = self.is_truncated()

        return observation, self.reward, self.terminated, self.truncated, self.curr_step

    def success_fn(self) -> bool:
        return EM(self.answer, self.key)

"""Token counting and step printing for the inherited executor."""
from typing import Callable

import tiktoken
from langchain.schema import ChatMessage

#: Tiktoken encoding used to approximate token counts for models that are not in
#: tiktoken's OpenAI model registry (e.g. a locally served 7B model).
FALLBACK_TOKEN_ENCODING = 'cl100k_base'


def token_counter(text: str, llm: str = 'gpt-3.5-turbo', tokenizer: Callable = None) -> int:
    """Count tokens for budgeting and truncation, never for correctness.

    Model names unknown to tiktoken (e.g. a local OpenAI-compatible server) fall
    back to ``cl100k_base``: an approximation is strictly better than aborting the
    run before the first action.  Callers that have the real tokenizer pass it.
    """
    if tokenizer is not None:
        return len(tokenizer.encode(text))

    if 'gpt' in llm:
        return len(tiktoken.encoding_for_model(llm).encode(text))

    return len(tiktoken.get_encoding(FALLBACK_TOKEN_ENCODING).encode(text))


def print_message(message: ChatMessage) -> None:
    """Echo one model or environment message to the worker log."""
    print(message.content)

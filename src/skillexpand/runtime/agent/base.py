"""State shared by every executor: environment status and the prompt transcript."""


class BaseAgent:
    def is_terminated(self) -> bool:
        return self.env.is_terminated()

    def is_truncated(self) -> bool:
        return self.env.is_truncated() or (self.token_counter(self.log_history()) > 15800)

    def log_history(self) -> str:
        return '\n'.join(prompt.content for prompt in self.prompt_history)

    def remove_task_suffix(self, task: str) -> str:
        if self.benchmark_name == 'alfworld':
            return task.split('___')[0]
        return task

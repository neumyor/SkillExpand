"""Disable redundant terminal artifacts in the selected Harbor installation."""
import ast
import argparse
import importlib.util
from pathlib import Path


def replace_once(text, old, new):
    if new in text:
        return text
    if text.count(old) != 1:
        raise RuntimeError(f"Unsupported Harbor source: {old!r}")
    return text.replace(old, new, 1)


def patch_sources(agent, tmux):
    agent = replace_once(agent, "record_terminal_session: bool = True,",
                         "record_terminal_session: bool = False,\n        record_terminal_output: bool = False,")
    agent = replace_once(agent, "self._record_terminal_session = record_terminal_session",
                         "self._record_terminal_session = record_terminal_session\n        self._record_terminal_output = record_terminal_output")
    agent = replace_once(agent, 'logging_path=EnvironmentPaths.agent_dir / "terminus_2.pane",',
                         'logging_path=(EnvironmentPaths.agent_dir / "terminus_2.pane"\n                          if self._record_terminal_output else None),')
    tmux = replace_once(tmux, "logging_path: Path | PurePosixPath,",
                        "logging_path: Path | PurePosixPath | None,")
    start = tmux.index("    @property\n    def _tmux_start_session")
    end = tmux.index("    def _tmux_send_keys", start)
    block = tmux[start:end]
    if "if self._logging_path is not None" not in block:
        old = next(line for line in block.splitlines() if line.lstrip().startswith('f"tmux new-session'))
        pipe = '            f"pipe-pane -t {self._session_name} "\n            f"\'cat > {self._logging_path}\'"'
        if pipe not in block or "\\\\;" not in old:
            raise RuntimeError("Unsupported tmux startup")
        new = old.replace(" \\\\; ", " ")
        block = block.replace(old, new).replace(pipe,
            '            + (f"\\\\; pipe-pane -t {self._session_name} "\n'
            '               f"\'cat > {self._logging_path}\'"\n'
            '               if self._logging_path is not None else "")\n'
            '            +')
        tmux = tmux[:start] + block + tmux[end:]
    ast.parse(agent)
    ast.parse(tmux)
    return agent, tmux


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--adapter', action='append', default=[])
    args = parser.parse_args()
    spec = importlib.util.find_spec("harbor")
    root = Path(next(iter(spec.submodule_search_locations))) / "agents/terminus_2"
    paths = [root / "terminus_2.py", root / "tmux_session.py"]
    original = [p.read_text() for p in paths]
    updated = patch_sources(*original)
    for directory in args.adapter:
        adapter = Path(directory)
        path = adapter / 'tencent_sandbox_terminus.py'
        text = path.read_text()
        new = replace_once(text, '        self._tencent_llm_call_kwargs = llm_call_kwargs or {}',
                           '        self._tencent_llm_call_kwargs = llm_call_kwargs or {}\n        kwargs.update(record_terminal_session=False, record_terminal_output=False)')
        ast.parse(new)
        paths.append(path)
        original.append(text)
        updated += (new,)
        path = adapter / 'run_tencent_job.py'
        text = path.read_text()
        new = replace_once(text, '    config = JobConfig.model_validate(yaml.safe_load(config_path.read_text()))',
                           '    config = JobConfig.model_validate(yaml.safe_load(config_path.read_text()))\n    for agent in config.agents:\n        agent.kwargs.update(record_terminal_session=False, record_terminal_output=False)')
        ast.parse(new)
        paths.append(path)
        original.append(text)
        updated += (new,)
    for path, old, new in zip(paths, original, updated):
        if old != new:
            path.write_text(new)
        print(f"Patched: {path}")


if __name__ == "__main__":
    main()

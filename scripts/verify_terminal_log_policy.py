"""Focused checks and reader reference snapshot for terminal artifact cleanup."""
import argparse
import asyncio
import inspect
import json
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock


def policy():
    from harbor.agents.terminus_2.terminus_2 import Terminus2
    from harbor.agents.terminus_2.tmux_session import TmuxSession
    sig = inspect.signature(Terminus2.__init__)
    assert sig.parameters['record_terminal_session'].default is False
    assert sig.parameters['record_terminal_output'].default is False
    session = TmuxSession('test', AsyncMock(), None, None, None)
    assert 'pipe-pane' not in session._tmux_start_session
    enabled = TmuxSession('test', AsyncMock(), Path('/tmp/test.pane'), None, None)
    assert 'pipe-pane' in enabled._tmux_start_session
    assert 'capture-pane' in inspect.getsource(TmuxSession)
    session._session_name = f'terminal-policy-check-{__import__("os").getpid()}'
    try:
        subprocess.run(session._tmux_start_session, shell=True, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        subprocess.run(['tmux', 'send-keys', '-t', session._session_name,
                        'echo TERMINAL_POLICY_OBSERVATION_OK', 'Enter'], check=True)
        time.sleep(0.3)
        observed = subprocess.check_output(session._tmux_capture_pane(), shell=True, text=True)
        assert 'TERMINAL_POLICY_OBSERVATION_OK' in observed
    finally:
        subprocess.run(['tmux', 'kill-session', '-t', session._session_name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with TemporaryDirectory() as directory:
        agent = Terminus2(model_name='openai/gpt-4o', logs_dir=Path(directory))
        environment = AsyncMock()
        environment.trial_paths.agent_dir = Path(directory)
        original = TmuxSession.start
        try:
            TmuxSession.start = AsyncMock()
            asyncio.run(agent.setup(environment))
        finally:
            TmuxSession.start = original
        assert agent._session._logging_path is None
        assert agent._session._remote_asciinema_recording_path is None
        assert agent._session._local_asciinema_recording_path is None
    print('Terminal policy checks passed')


def reader_snapshot(destination, compare):
    from skillexpand.reader.data import Reader
    reader = Reader(Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs'))
    reader.refresh()
    paths, loaded = {}, False
    cards = 0
    for key, run in reader.runs.items():
        cards += len(run.cards)
        for tid in run.tasks:
            for trace in reader.traces(key, tid):
                for p in (trace.path, trace.result_path):
                    if p:
                        paths[str(p)] = p.exists()
                if not loaded and trace.path.exists() and trace.result_path and trace.result_path.exists():
                    detail = reader.trace_detail(trace)
                    if not detail.get('error') and not detail.get('result_error') and detail.get('steps') and run.cards:
                        assert run.cards[0].path.exists()
                        loaded = True
    assert loaded, 'No complete trajectory/result/card could be loaded'
    if compare:
        previous = json.loads(Path(compare).read_text())
        lost = [p for p, exists in previous['paths'].items() if exists and not Path(p).exists()]
        assert not lost, lost[:10]
    Path(destination).write_text(json.dumps(dict(paths=paths, cards=cards)))
    print(json.dumps(dict(references=len(paths), existing=sum(paths.values()), cards=cards, loaded=loaded)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot')
    parser.add_argument('--compare')
    args = parser.parse_args()
    if args.snapshot:
        reader_snapshot(args.snapshot, args.compare)
    else:
        policy()

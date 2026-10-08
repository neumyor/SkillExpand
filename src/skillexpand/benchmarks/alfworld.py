import re
from typing import Dict, Any, Tuple, Union
from skillexpand.benchmarks.base import BaseEnv
from skillexpand.benchmarks.base import environment_call


ENV_NAMES = [
            'pick_and_place',
            'pick_clean_then_place',
            'pick_heat_then_place',
            'pick_cool_then_place',
            'look_at_obj',
            'pick_two_obj'
        ]


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


def resolve_alfworld_env_cls(env_type: str):
    """Resolve an ALFWorld environment class from its config name.

    ExpeL was written against alfworld <= 0.3.x, whose
    ``alfworld/agents/environment/__init__.py`` re-exported the env classes::

        from alfworld.agents.environment.alfred_tw_env import AlfredTWEnv

    alfworld 0.4.x dropped those module-level re-exports and exposes only
    ``get_environment(env_type)``. Both 0.3.5 and 0.4.2 define an identical
    ``AlfredTWEnv(config, train_eval=...)`` plus ``init_env``, so resolving
    through ``get_environment`` is behaviour-preserving.

    We intentionally do NOT pin ``alfworld==0.3.5`` instead: 0.3.5 makes
    ``ai2thor==2.1.0``, ``opencv-python``, ``torchvision`` and
    ``werkzeug==2.0.3`` hard dependencies, and declares
    ``textworld[pddl]>=1.6.1`` with no upper bound -- which resolves to
    textworld 1.7.0, a release with no CPython 3.9 wheel whose sdist
    ``./setup.sh`` build fails.
    """
    import alfworld.agents.environment

    if hasattr(alfworld.agents.environment, env_type):
        # alfworld <= 0.3.x path; kept so this file still works after a downgrade.
        return getattr(alfworld.agents.environment, env_type)
    return alfworld.agents.environment.get_environment(env_type)


#: The placeholder the agent loop substitutes when the model emits three
#: consecutive thoughts (``agent/react.py:104-112``).  It is not an action.
NO_ACTION_PLACEHOLDER = 'N/A'


def show_admissible_commands_enabled(config: Dict[str, Any]) -> bool:
    """Whether to append the ALFWorld engine's admissible action set to the
    observation.

    Context -- this is a measured executor-competence fix, not a method change.
    The served model is a 7B ALFWorld checkpoint, not gpt-3.5-turbo. Measured on
    18 tasks it solved 22.2%, and **72.2% of all episodes died the same way**: the
    model emitted an invalid action, received "Nothing happens.", then repeated
    that action -- and ``step`` terminates the episode on any repeated action.

    The engine computes the valid action set already: ``init_env`` registers
    ``textworld.EnvInfos(admissible_commands=True)``, and upstream simply never
    shows it. Verified directly: while standing at ``desk 2``, ``go to desk 2`` is
    **not** in the admissible set, whereas ``take bowl 1 from desk 2`` is. So
    showing the set removes the trap that causes most failures.

    Note the goal action is *not* directly admissible either --
    ``look at bowl under the desklamp`` is absent from the reset state's set; the
    agent must ``take bowl 1 from desk 2`` and then ``use desklamp 1``. The engine
    therefore supplies real guidance, not the answer.

    Defaults to False so upstream configs behave exactly as before.
    """
    try:
        env_cfg = config['env']
    except (KeyError, TypeError):
        return False
    try:
        return bool(env_cfg['show_admissible_commands'])
    except (KeyError, TypeError):
        return False


class AlfworldEnv(BaseEnv):
    def __init__(self,
                gamefile: str,
                config: Dict[str, Any],
                max_steps: int = 50,
                ):
        self.max_steps = max_steps
        self.gamefile = gamefile
        self.config = config
        # Opt-in: absent from upstream configs, so default False keeps ExpeL's
        # original observations byte-identical.
        self.show_admissible_commands = show_admissible_commands_enabled(config)
        self._admissible_commands = []
        self.main_env = resolve_alfworld_env_cls(self.config.env.type)(self.config, train_eval=self.config.split)
        self.main_env.game_files = [self.gamefile]
        self.task = "housekeeper robot. The agent was placed in a household environment and a task to complete."
        self.env_name = get_env_name_from_gamefile(gamefile)

        self.reset()

    @environment_call
    def reset(self):
        previous = getattr(self, "env", None)
        if previous is not None:
            previous.close()
        self.curr_step = 1
        self.answer = ''
        self.terminated = False
        self.reward = False
        self.is_exhausted = False
        self.env = self.main_env.init_env(batch_size=1)
        self.env.reset()
        self.last_action = None
        self.truncated = False
        self.termination_reason = None
        self._admissible_commands = []

    @environment_call
    def step(self, action: str) -> Tuple[str, bool, bool, bool, int]:
        if action == NO_ACTION_PLACEHOLDER:
            return self._step_without_action()

        if action.startswith('put'):
            pattern = r'put (\w+\s*\d+) (?:in|on) (\w+\s*\d+)'
            match = re.match(pattern, action)
            if match is not None:
                action = 'put ' + match.group(1) + ' in/on ' + match.group(2)

        observation, reward, done = self.alfworld_run(action)
        self.terminated = self.terminated or bool(done)
        if self.last_action == action:
            self.termination_reason = 'repeated_action'
            self.truncated = True
            self.terminated = True

        self.last_action = action

        if reward:
                observation = 'Task is SOLVED.'
                self.terminated = True
        else:
            if self.is_truncated():
                observation = 'Max steps reached.'
            pass

        self.curr_step += 1
        self.terminated = self.is_terminated()
        self.truncated = self.truncated or self.is_truncated()
        self.reward = reward

        # Surface the engine's admissible action set, when enabled.  Done after
        # the terminal checks so a solved or exhausted episode is not padded, and
        # computed from the info block of THIS step so it describes the state the
        # agent is now in.
        if self.show_admissible_commands and not self.terminated and not self.reward:
            commands = self._admissible_commands
            if commands:
                observation = f'{observation}\nAdmissible actions: ' +\
                              ', '.join(commands)

        return observation, self.reward, self.terminated, self.truncated, self.curr_step

    def _step_without_action(self) -> Tuple[str, bool, bool, bool, int]:
        """Advance the step budget without executing anything.

        The agent loop already knows the model produced no action: it sets
        ``others['action'] = 'N/A'`` and then immediately *replaces* the
        environment's observation with "You are thinking too many times without
        taking action" (``agent/react.py:104-112``). The engine's reply is
        discarded, so the call has no purpose other than side effects -- and those
        side effects are fatal:

        *   it consumes an engine step, which the step budget counts, and
        *   it sets ``last_action = 'N/A'``, so a *second* stall satisfies
            ``self.last_action == action`` below and terminates the episode.

        Measured on 12 tasks, 6 episodes were killed by a repeated action and
        **5 of those 6 were two consecutive ``'N/A'`` steps** -- the model had
        concluded (rightly or wrongly) that the task was finished and stopped
        acting, and the harness turned that stall into a hard failure. Two of the
        recorded episodes had in fact completed the required sub-goal before the
        stall.

        This does not touch the task's success criterion, which stays exactly as
        ExpeL defines it: no action reaches the engine, so nothing is scored. Only
        the leak of a loop-internal placeholder into the environment is removed.
        """
        self.curr_step += 1
        self.truncated = self.is_truncated()
        observation = 'You are thinking too many times without taking action.'
        return (observation, self.reward, self.terminated, self.truncated,
                self.curr_step)

    def success_fn(self) -> bool:
        return self.reward

    @environment_call
    def close(self):
        env = getattr(self, 'env', None)
        if env is not None:
            env.close()

    def alfworld_run(self, action):
        observation, reward, done, info = self.env.step([action])
        observation, reward, done = process_observation(observation[0]), info['won'][0], done[0]
        # `init_env` already requests EnvInfos(admissible_commands=True) for this
        # engine, but upstream dropped the info block here.  Keep it so `step` can
        # optionally show it.
        raw = info.get('admissible_commands')
        self._admissible_commands = list(raw[0]) if raw else []

        return observation, reward, done

def process_observation(obs):
    if obs.startswith('You arrive at loc '):
        obs = obs[obs.find('. ')+2:]
    return obs

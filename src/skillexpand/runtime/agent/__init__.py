from skillexpand.runtime.agent.base import BaseAgent
from skillexpand.runtime.agent.react import ReactAgent
from skillexpand.runtime.agent.reflect import ReflectAgent
from skillexpand.runtime.agent.expel import ExpelAgent


AGENT = dict(reflection=ReflectAgent, react=ReactAgent, expel=ExpelAgent)

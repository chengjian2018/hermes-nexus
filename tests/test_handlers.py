"""handlers / agents 离线单测（不依赖 LLM）。

AgentHandler 与 handlers 包拆分落地前，先钉住 AgentRunner 接口的可注入性
与默认实现的委托行为。
"""

from src.chat.agents import AgentRunner, LoopAgentRunner
from src.chat.loop import TurnResult


class _RecordingRunner:
    """记录调用参数的 stub runner，证明可注入。"""

    def __init__(self, reply="stub回复"):
        self.reply = reply
        self.calls = []

    def run(self, session, module, llm_config, force_close=False):
        self.calls.append({
            "module": module,
            "llm_config": llm_config,
            "force_close": force_close,
        })
        return TurnResult(reply=self.reply)


class TestAgentRunnerProtocol:
    def test_recording_runner_satisfies_protocol(self):
        assert isinstance(_RecordingRunner(), AgentRunner)

    def test_loop_agent_runner_satisfies_protocol(self):
        assert isinstance(LoopAgentRunner(), AgentRunner)


class TestLoopAgentRunner:
    def test_delegates_to_run_agent(self, monkeypatch):
        """默认实现委托 loop.run_agent 且透传 force_close。"""
        recorded = {}

        def fake_run_agent(session, module, llm_config, force_close=False):
            recorded["args"] = (session, module, llm_config, force_close)
            return TurnResult(reply="来自loop")

        import src.chat.agents as agents_mod
        monkeypatch.setattr(agents_mod, "run_agent", fake_run_agent)

        session = object()
        module = object()
        result = LoopAgentRunner().run(session, module, {"model": "m"}, force_close=True)

        assert result.reply == "来自loop"
        assert recorded["args"] == (session, module, {"model": "m"}, True)

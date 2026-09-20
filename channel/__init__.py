"""Channel adapter layer — the endpoint collection through which external message sources reach the engine.

One module per channel (e.g. ``xianyu.py``), implementing a ChannelSpec
declaration (payload schema, session derivation, task_info mapping, success
response contract) with module-level ``registry.register()`` self-registration;
AST auto-discovery (register.py), with the common flow in the webhooks.py
generic handler (token validation / staleness filtering / get-or-create /
error codes — structurally impossible to bypass). Engine operations are
injected by main.py via EngineOps; channel modules know nothing about session
governance or the LLM and can be tested offline in isolation.

Adding a channel: implement ChannelSpec + registry.register() in
``channel/<name>.py``; main.py needs no changes (auto-discovery + auto router
generation).
"""

from typing import Optional, Any


class Pattern:
    def __init__(self,
                 code,
                 name: str,
                 description: str,
                 entry_module_code,
                 modules: Optional[list[Any]] = None,
                 stages: Optional[list[Any]] = None,
                 generate: Optional[Any] = None,
                 pre_recall: Optional[Any] = None,
                 query: Optional[Any] = None,
                 post_recall: Optional[Any] = None,
                 agent_hooks: Optional[dict] = None,
                 **kwargs):
        self.code = code
        self.name = name
        self.description = description
        self.modules = modules
        self.stages = stages
        self.entry_module_code = entry_module_code

        # agent loop hooks（pattern 级声明，module 层 agent_hooks 可整体替换；
        # 形态 {点位: [hook,...]}，消费方见 src/chat/agent_hooks.py）
        self.agent_hooks = agent_hooks

        # 管线槽位三层默认（node > module > pattern 中的 pattern 层）
        self.generate = generate
        self.pre_recall = pre_recall
        self.query = query
        self.post_recall = post_recall

        self.node_map = dict()
        self.module_map = dict()

        # ------------------------------------------------------------------
        # 模块拓扑注册 + 注册期 fail fast（悬空/自环/越权配置，spec §2.4）
        # ------------------------------------------------------------------
        self.max_hops = int(kwargs.pop("max_hops", 2))

        if self.modules is not None:
            for module in self.modules:
                self.module_map[module.module_code] = module
                # AgentModule has no node_code; only FSM/Route modules do
                if hasattr(module, "node_code") and module.node_code:
                    self.node_map[module.node_code] = module

                for node in module.module_nodes:
                    self.node_map[node.node_code] = node

            for module in self.modules:
                # 1) sub_modules 邻接边校验（transfer 工具 / 借出工具配置）
                for link in module.sub_modules:
                    if link.target not in self.module_map:
                        raise ValueError(
                            f"悬空转移边: {module.module_code} → {link.target}"
                            f"（目标不在 module_map 中）"
                        )
                    if link.target == module.module_code:
                        raise ValueError(
                            f"自环转移边: {module.module_code} → {link.target}"
                        )
                    target = self.module_map[link.target]
                    unauthorized = set(link.lend_tools) - set(target.use_tools or [])
                    if unauthorized:
                        raise ValueError(
                            f"越权借出: {module.module_code} 借出配置无效: "
                            f"{sorted(unauthorized)} 不在 {link.target}.use_tools 中"
                        )
                # 2) 节点 jump_module 配置校验（跳转目标 fail fast；
                #    运行期由 chat 层 _detect_jump_after_stage 消费，无邻接图）
                for node in module.module_nodes:
                    jump_target = getattr(node, "jump_module", None)
                    if jump_target:
                        if jump_target not in self.module_map:
                            raise ValueError(
                                f"悬空转移边: 节点 {node.node_code}.jump_module "
                                f"→ {jump_target} 不存在"
                            )
                        if jump_target == module.module_code:
                            raise ValueError(
                                f"自环转移边: 节点 {node.node_code}.jump_module "
                                f"→ {jump_target}（模块自环）"
                            )

            for key, value in kwargs.items():
                setattr(self, key, value)



from typing import Optional, Any, Dict, Set


class Pattern:
    def __init__(self,
                 code,
                 name: str,
                 description: str,
                 entry_module_code,
                 modules: Optional[list[Any]] = None,
                 stages: Optional[list[Any]] = None,
                 **kwargs):
        self.code = code
        self.name = name
        self.description = description
        self.modules = modules
        self.stages = stages
        self.entry_module_code = entry_module_code

        self.node_map = dict()
        self.module_map = dict()

        # ------------------------------------------------------------------
        # 转移图构建 + 注册期 fail fast（spec §2.4）
        # ------------------------------------------------------------------
        self.max_hops = int(kwargs.pop("max_hops", 2))
        self.dispatch_graph: Dict[str, Set[str]] = {}

        if self.modules is not None:
            for module in self.modules:
                self.module_map[module.module_code] = module
                # AgentModule has no node_code; only FSM/Route modules do
                if hasattr(module, "node_code") and module.node_code:
                    self.node_map[module.node_code] = module

                for node in module.module_nodes:
                    self.node_map[node.node_code] = node

            for module in self.modules:
                edges: Set[str] = set()
                # 1) sub_modules 邻接边
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
                    edges.add(link.target)
                # 2) ROUTE 菜单节点 jump_module 推导
                for node in module.module_nodes:
                    jump_target = getattr(node, "jump_module", None)
                    if jump_target:
                        if jump_target not in self.module_map:
                            raise ValueError(
                                f"悬空转移边: 节点 {node.node_code}.jump_module "
                                f"→ {jump_target} 不存在"
                            )
                        edges.add(jump_target)
                if edges:
                    self.dispatch_graph[module.module_code] = edges

            for key, value in kwargs.items():
                setattr(self, key, value)



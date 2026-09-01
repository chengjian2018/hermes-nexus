from typing import Optional, Any


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

        if self.modules is not None:
            for module in self.modules:
                self.module_map[module.module_code] = module
                # AgentModule has no node_code; only FSM/Route modules do
                if hasattr(module, "node_code") and module.node_code:
                    self.node_map[module.node_code] = module

                for node in module.module_nodes:
                    self.node_map[node.node_code] = node

            for key, value in kwargs.items():
                setattr(self, key, value)



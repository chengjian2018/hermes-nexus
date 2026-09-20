"""stages — pipeline stage implementations.

Concrete stage implementations for the four slots (pre_recall / query / post_recall / generate) live in this package:
- ``nlu/`` ``nlg/``: two-phase intent recognition and reply generation
- ``unified.py``: single-call NLU+NLG combined form (generate as a single stage)
- ``clarify/``: FSM off-topic clarification (dual-track)
- ``query/``: query rewrite slot
- ``recaller/``: recall / rerank slot

The pipeline contract (``PipelineStage``, three-layer slot resolution) lives in ``dialogue/``;
business patterns reference the stages here to assemble their modules.
"""

"""Dialogue system package — Pipeline stages, context and modules.

Exports base types for use by src/chat and all stages.
"""

from src.dialogue.base import DialogueContext, PipelineStage, SessionMessage

__all__ = ["DialogueContext", "PipelineStage", "SessionMessage"]

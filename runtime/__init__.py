"""Presence Runtime Package
Foundational Headless Runtime and Backend Intelligence Engine for the Intelligent Presence.
"""

__version__ = "0.2.0"
__author__ = "Antigravity Engineering"

from runtime.conversation import ConversationMetadata
from runtime.trajectory import TrajectoryStore, Step, StepType, StepStatus
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.reconstruction import ContextAssembler, ReconstructedContext


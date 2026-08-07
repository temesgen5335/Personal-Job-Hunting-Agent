"""The assistant adapter: this system's half of the agentkit contract.

`agentkit` supplies the harness; this package supplies the domain — tools, knowledge,
prompt and policy. Nothing here is imported by agentkit, and a test enforces that.
"""

from jobagent.assistant.manifest import ASSISTANT_NAME, Assistant, build_assistant
from jobagent.assistant.tools import EXCLUDED

__all__ = ["ASSISTANT_NAME", "Assistant", "build_assistant", "EXCLUDED"]

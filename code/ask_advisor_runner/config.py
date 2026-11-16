from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Group = Literal["A", "C"]


@dataclass(frozen=True)
class ExperimentConfig:
    """Frozen inputs required by the offline state machine.

    Transport details are deliberately absent. A later reviewed version may add
    adapters, but this stage-0 skeleton cannot contact vLLM, DSH, or Advisor.
    """

    schema_version: str = "0.1-draft"
    experiment_version: str = "stage0-draft"
    checkpoint_repository: str = "nvidia/Qwen3.8-27B-NVFP4"
    checkpoint_revision: str = "482ca0f3832238542f8f5295dde86b5f22711d80"
    vllm_candidate_version: str = "0.32.0"
    advisor_call_limit: int = 3
    detection_start_tool_step: int = 3

    def validate(self) -> None:
        if self.advisor_call_limit != 3:
            raise ValueError("experiment Advisor call limit must remain 3")
        if self.detection_start_tool_step != 3:
            raise ValueError("detection must start after tool step 3")
        if len(self.checkpoint_revision) != 40:
            raise ValueError("checkpoint revision must be a full 40-character commit")

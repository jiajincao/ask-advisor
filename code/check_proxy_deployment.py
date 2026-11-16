from __future__ import annotations

import json
import os

from ask_advisor_runner import check_deployment_environment


def main() -> None:
    result = check_deployment_environment(os.environ)
    print(
        json.dumps(
            {
                "ok": result.ok,
                "required_inputs_present": result.required_inputs_present,
                "activation_disabled": result.activation_disabled,
                "threshold": result.threshold,
                "advisor_call_limit": result.advisor_call_limit,
                "detection_start_tool_step": result.detection_start_tool_step,
                "network_opened": False,
                "services_started": False,
                "values_printed": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

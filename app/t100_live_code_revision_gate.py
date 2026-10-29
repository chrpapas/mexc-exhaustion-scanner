"""Exact code-revision pin for a T100 tiny pilot.

A reviewed live pilot must run exactly the commit that passed review/CI.
Auto-deploy must be disabled so the executable revision cannot drift during
the authorization window.
"""
from __future__ import annotations

import re
from typing import Any


SHA40 = re.compile(r"^[0-9a-f]{40}$")


def assess_code_revision_pin(
    *,
    reviewed_commit: Any,
    runtime_commit: Any,
    ci_green_for_reviewed_commit: bool,
    auto_deploy_disabled: bool,
) -> dict[str, Any]:
    result = {
        "code_revision_verified": False,
        "commit_match": False,
        "ci_green_for_reviewed_commit": False,
        "auto_deploy_disabled": False,
        "live_authorization": False,
    }
    if (
        not isinstance(reviewed_commit, str)
        or not isinstance(runtime_commit, str)
        or SHA40.fullmatch(reviewed_commit) is None
        or SHA40.fullmatch(runtime_commit) is None
        or type(ci_green_for_reviewed_commit) is not bool
        or type(auto_deploy_disabled) is not bool
    ):
        result["status"] = "CODE_REVISION_EVIDENCE_INVALID"
        return result

    result["commit_match"] = reviewed_commit == runtime_commit
    result["ci_green_for_reviewed_commit"] = (
        ci_green_for_reviewed_commit
    )
    result["auto_deploy_disabled"] = auto_deploy_disabled
    result["code_revision_verified"] = all((
        result["commit_match"],
        ci_green_for_reviewed_commit,
        auto_deploy_disabled,
    ))
    result["status"] = (
        "EXACT_REVIEWED_CODE_REVISION_PINNED"
        if result["code_revision_verified"]
        else "CODE_REVISION_PIN_BLOCKED"
    )
    return result

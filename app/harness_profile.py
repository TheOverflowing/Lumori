"""Restricted ACP composition verified against Harness runtime 0.1.5rc1.

The caller must give the subprocess an explicit DSH_HOME and writable
PKG_NATIVE_CACHE_PATH, and a sanitized environment containing only a local
proxy token under FYP_HARNESS_PROXY_TOKEN. No provider key belongs here.
This list is a version-specific overlay, not a sandbox or a forward-compatible
allowlist: pin the runtime and re-audit its composed profile when upgrading.
"""

from pathlib import Path
from urllib.parse import urlparse


HARNESS_RUNTIME_VERSION = "0.1.5rc1"

# IDs from the actual 0.1.5rc1 macOS arm64 ACP --dump-config. These remove
# general-purpose tools, ambient instructions/settings/credentials, autonomous
# child work, auxiliary model calls, provider extensions, and remote telemetry.
DISABLED_PLUGIN_IDS = (
    "hmr", "deepseek-llm-api-extensions", "session-log-deepseek",
    "typert", "typert-loader", "typert-gateway", "session-title-llm",
    "user-questions", "plugin-package-inventory-deepseek", "agent-default-model",
    "jobs", "llm-retry", "settings", "credentials", "llm-pi-ai",
    "attachment-local", "session-query-sqlite", "session-telemetry-otel",
    "subprocess", "sandbox", "sandbox-policy", "bash-sandbox", "pwsh-sandbox",
    "approval", "permission", "shell-env", "tool-bash", "tool-pwsh", "tool-jobs",
    "fs-observation-policy", "tool-fs", "tool-fs-search", "agent-instructions",
    "skill", "skill-filesystem", "skill-badge", "tool-skill", "commands",
    "command-feedback", "goal", "goal-round-driver", "command-goal", "plan-mode",
    "token-meter", "compaction-basic", "command-compact", "subagent",
    "subagent-spawn-in-process", "subagent-fork-in-process",
    "tool-subagent-control", "tool-subagent-list-agents", "tool-subagent",
    "tool-subagent-fork", "workflow-worker-thread", "tool-workflow",
    "timeout-policy", "spill-local", "spill-policy", "tool-result-pruner",
    "tool-todo", "tool-goal", "tool-ralph", "repeat-tool-reminder", "web",
    "web-search-deepseek", "web-fetch-http", "tool-web", "fs-sandbox",
)


def profile_patch(
    model: str, base_url: str, max_tokens: int, session_root: Path, *,
    reasoning_effort: str = "off",
) -> list[dict]:
    """Produce a JSON/YAML overlay for a fresh ACP-derived ``education`` profile.

    Only per-session ACP MCP declarations may introduce model-facing tools.
    The route is advertised exactly as supplied, without a V4 name substitution.
    Call/token limits and endpoint authentication remain the local proxy's job.
    """
    if not isinstance(model, str) or not model.strip() or model != model.strip():
        raise ValueError("Harness model must be a nonempty exact model ID")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise ValueError("Harness max_tokens must be a positive integer")
    if reasoning_effort not in ("off", "high"):
        raise ValueError("Harness reasoning_effort must be off or high")
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Harness must use an explicit HTTP loopback proxy URL")
    root = Path(session_root)
    if not root.is_absolute():
        raise ValueError("Harness session_root must be absolute")
    return [
        *({"id": plugin_id, "disabled": True} for plugin_id in DISABLED_PLUGIN_IDS),
        {"id": "acp", "config": {"provider": "deepseek-official", "model": model}},
        {
            "id": "llm-deepseek",
            "config": {
                "protocol": "chat-completions",
                "baseURL": base_url,
                "apiKeyEnv": "FYP_HARNESS_PROXY_TOKEN",
                "thinking": "enabled" if reasoning_effort == "high" else "disabled",
                "reasoningEffort": reasoning_effort,
                "maxTokens": max_tokens,
                "models": [{"id": model, "contextWindow": 131072}],
            },
        },
        {
            "id": "system-prompt",
            "config": {
                "personaPrefix": (
                    "You are an educational assessment assistant. "
                    "Use only the configured educational tools."
                ),
                "personaSuffix": "",
            },
        },
        {
            "id": "session-persistence-jsonl",
            "config": {"root": str(root), "compression": "none"},
        },
        # Host-owned serial dispatch simplifies per-job verification and budgets.
        {"id": "agent-loop", "config": {"agents": [], "maxParallelToolCalls": 1}},
    ]

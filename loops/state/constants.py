"""Global constants shared across Loops runtime modules."""

INNER_LOOP_RUNS_DIR_NAME = "jobs"
ARCHIVE_DIR_NAME = ".archive"
RUN_RECORD_FILE_NAME = "run.json"
RUN_LOG_FILE_NAME = "run.log"
AGENT_LOG_FILE_NAME = "agent.log"
OUTER_STATE_FILE_NAME = "outer_state.json"
OUTER_LOG_FILE_NAME = "oloops.log"
INNER_LOOP_RUNTIME_CONFIG_FILE = "inner_loop_runtime_config.json"
SIGNAL_OFFSET_FILE = "state_signals.offset"
PUSH_PR_URL_FILE = "push-pr.url"
STATE_HOOKS_LEDGER_FILE = "state_hooks.json"

LATEST_LOOPS_CONFIG_VERSION = 4
CHECKOUT_MODE_BRANCH = "branch"
CHECKOUT_MODE_WORKTREE = "worktree"
VALID_CHECKOUT_MODES = frozenset({CHECKOUT_MODE_BRANCH, CHECKOUT_MODE_WORKTREE})

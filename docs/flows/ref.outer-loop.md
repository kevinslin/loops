# Outer Loop Flow

Last updated: 2026-02-16

## Overview

This document describes how Loops discovers ready tasks, de-duplicates them, materializes per-task run directories, and launches inner-loop processes.
It is intended as a fast context-recapture artifact for humans and LLM agents changing outer-loop behavior.

**Related Documents:**
- `DESIGN.md`
- `README.md`
- `LAYOUT.md`
- `docs/flows/ref.inner-loop.md`
- `docs/specs/active/2026-02-05-implement-outer-loop-runner.md`
- `docs/specs/active/2026-02-03-implement-github-projects-v2-task-provider.md`

## Terminology

- `Outer loop`: Poll-and-dispatch runtime that turns provider tasks into run directories.
- `Outer state`: Dedup ledger in `.loops/outer_state.json` with `initialized`, `tasks`, `updated_at`.
- `Run dir`: Per-task folder under `.loops/jobs/` created by `create_run_dir`.
- `Launcher`: Callable that starts the configured inner-loop command with task/run env vars.
- `Ready task`: Provider task whose status matches `loop_config.task_ready_status` (case-insensitive).

## Config

### Statsig

None identified.

### Environment Variables

| Name | Where Read | Default | Effect on Flow |
|---|---|---|---|
| `GITHUB_TOKEN` / `GH_TOKEN` | `loops/providers/github_projects_v2.py:220` | none | Required by GitHub provider polling when `provider_config.github_token` is not set. |
| Process env passthrough (`os.environ.copy()`) | `loops/outer_loop.py:298`, `loops/providers/github_projects_v2.py:236` | N/A | Baseline environment passed to inner-loop child and `gh api graphql` subprocesses. |

### Other User-Settable Inputs

| Name | Type | Where Read | Effect on Flow |
|---|---|---|---|
| `--config` | CLI option | `loops/cli.py:38`, consumed in `loops/cli.py:196` | Selects config file for provider/loop/inner-loop settings and loops root resolution. |
| `--run-once/--run-forever` | CLI option | `loops/cli.py:46`, dispatched at `loops/cli.py:217` | Controls single-cycle execution vs continuous polling loop. |
| `--limit` | CLI option | `loops/cli.py:51`, forwarded to provider poll | Caps tasks returned/considered in a cycle. |
| `--force` | CLI option | `loops/cli.py:57`, override at `loops/cli.py:198` | Reprocesses tasks even if previously seen in outer state. |
| `provider_id` / `provider_config.*` | Config file fields | `loops/outer_loop.py:243`, `loops/outer_loop.py:274` | Chooses task provider and provider-specific polling behavior. |
| `loop_config.*` | Config file fields | `loops/outer_loop.py:394` | Controls poll interval, ready filter, sync mode, emit-on-first-run, force, and parallel launch behavior. |
| `inner_loop.*` | Config file fields | `loops/outer_loop.py:44`, `loops/outer_loop.py:283` | Defines launch command, cwd, env injection, and URL appending for child processes. |

## Flow

### Entry assumptions and boundaries

- Operator enters via `python -m loops run ...`, handled by CLI (`loops/cli.py:36`).
- `_run_outer_loop` assembles a runner from config + provider + launcher (`loops/cli.py:187`).
- Outer loop creates and updates `.loops/outer_state.json`, `.loops/oloops.log`, and `.loops/jobs/<run>/...`.
- Inner-loop execution boundary begins at launcher invocation (`loops/outer_loop.py:293`).

### State Timeline Table

| value | write step | snapshot step | read step | ordering valid? |
|---|---|---|---|---|
| `outer_state.initialized` | Set true in `run_once` finally block (`loops/outer_loop.py:203`) and persisted (`loops/outer_loop.py:205`) | Loaded at cycle start (`loops/outer_loop.py:163`) | Used to compute first-run emission policy (`loops/outer_loop.py:169`) | Yes |
| `outer_state.tasks` ledger | Updated per ready task via `record_task` (`loops/outer_loop.py:174`) then persisted (`loops/outer_loop.py:205`) | Loaded at cycle start (`loops/outer_loop.py:163`) | Used by `has_task` dedupe gate (`loops/outer_loop.py:173`) | Yes |
| `ready_tasks` | Created from provider poll filtered by `_is_ready` (`loops/outer_loop.py:164`) | Snapshot per cycle in memory | Used to build emit set and log counts (`loops/outer_loop.py:172`, `loops/outer_loop.py:206`) | Yes |
| `emit_tasks` | Built in cycle loop (`loops/outer_loop.py:168`, `loops/outer_loop.py:179`) | Snapshot before launch (`loops/outer_loop.py:183`) | Drives run-dir creation + launcher dispatch (`loops/outer_loop.py:184`, `loops/outer_loop.py:199`) | Yes |
| `run.json` initial state | Written by `write_run_record` (`loops/outer_loop.py:194`) | Materialized before launcher call | Consumed by inner loop as authoritative starting state | Yes |
| `oloops.log` cycle summary | Appended in finally block (`loops/outer_loop.py:206`, formatter at `loops/outer_loop.py:493`) | N/A | Used for operational summaries (`ready`/`processed`) | Yes |

### Outer-loop runtime invocation

- `loops/cli.py:187` + `loops/outer_loop.py:158`
```ts
function runOuterLoop(configPath: Path, runOnce: boolean, limit?: number, forceOverride?: boolean): void {
  config = loadConfig(configPath)

  loopConfig = config.loopConfig
  if (forceOverride is boolean) {
    loopConfig = { ...loopConfig, force: forceOverride }
  }

  if (config.innerLoop is null) {
    config.innerLoop = {
      command: [python, "-m", "loops.inner_loop"],
      appendTaskUrl: false,
    }
  }

  provider = buildProvider(config)
  launcher = buildInnerLoopLauncher(config)
  loopsRoot = resolveLoopsRoot(configPath)
  runner = new OuterLoopRunner(provider, loopConfig, loopsRoot, launcher)

  if (runOnce) {
    runner.runOnce(limit)
  } else {
    runner.runForever(limit)
  }
}

class OuterLoopRunner {
  runOnce(limit?: number): Path[] {
    ensureLoopsRootAndJobsDir()

    state = readOuterState(outerStatePath)
    readyTasks = provider.poll(limit).filter(task => isReady(task, config.taskReadyStatus))

    nowIso = now()
    firstRun = !state.initialized
    shouldEmit = config.emitOnFirstRun || config.force || !firstRun

    emitTasks = []
    for (task of readyTasks) {
      alreadySeen = state.hasTask(task)
      state.recordTask(task, nowIso)

      if (!shouldEmit) continue
      if (alreadySeen && !config.force) continue
      emitTasks.push(task)
    }

    if (emitTasks.length > 0 && launcher missing) {
      throw Error("inner_loop_launcher is required to launch tasks")
    }

    toLaunch = []
    for (task of emitTasks) {
      runDir = createRunDir(task, loopsRoot)
      writeRunRecord(runDir/run.json, {
        task,
        pr: null,
        codexSession: null,
        needsUserInput: false,
        lastState: "RUNNING",
        updatedAt: nowIso,
      })
      touch(runDir/run.log)
      touch(runDir/agent.log)
      toLaunch.push([runDir, task])
    }

    try {
      if (toLaunch.length > 0) {
        launchTasks(toLaunch, config)
      }
    } finally {
      state.initialized = true
      state.updatedAt = nowIso
      writeOuterState(outerStatePath, state)
      appendOuterLog(`ready=${readyTasks.length} processed=${toLaunch.length}`)
    }

    return toLaunch.map(([runDir]) => runDir)
  }

  runForever(limit?: number): void {
    while (true) {
      runOnce(limit)
      sleep(config.pollIntervalSeconds)
    }
  }

  launchTasks(tasks): void {
    if (config.syncMode) {
      // serial foreground launch
      for each task -> launcher(runDir, task)
      return
    }

    if (!config.parallelTasks || tasks.length <= 1) {
      for each task -> launcher(runDir, task)
      return
    }

    // bounded thread pool for concurrent launches
    run launch tasks with maxWorkers = min(config.parallelTasksLimit, tasks.length)
  }
}
```

### Child-launch behavior and handoff contract

- `build_inner_loop_launcher` builds a closure that:
  - Injects run/task metadata env vars (`LOOPS_RUN_DIR`, `LOOPS_TASK_ID`, `LOOPS_TASK_TITLE`, `LOOPS_TASK_URL`, `LOOPS_TASK_PROVIDER`) (`loops/outer_loop.py:299`).
  - Merges configured `inner_loop.env` (`loops/outer_loop.py:304`).
  - Appends task URL to command when configured (`loops/outer_loop.py:307`).
  - Uses `subprocess.run` in `sync_mode=true` (`loops/outer_loop.py:310`) or detached `subprocess.Popen` writing to `run.log` (`loops/outer_loop.py:319`).

### Provider polling behavior (GitHub Projects V2)

- `build_provider` currently supports only `github_projects_v2` (`loops/outer_loop.py:274`).
- Provider flow (`loops/providers/github_projects_v2.py:164`):
  - Resolve token (config override, else env) (`loops/providers/github_projects_v2.py:220`).
  - Parse project URL (`loops/providers/github_projects_v2.py:32`).
  - Poll GraphQL pages via `gh api graphql`, map items to `Task`, stop at limit or pagination end (`loops/providers/github_projects_v2.py:172`).

**File(s)**: `loops/cli.py`, `loops/outer_loop.py`, `loops/providers/github_projects_v2.py`, `loops/run_record.py`

## Architecture Diagram

```text
+-------------------------+
| CLI: loops run          |
| (config/run-once/limit) |
+-----------+-------------+
            |
            v
+-------------------------+
| _run_outer_loop         |
| load config             |
| build provider          |
| build launcher          |
+-----------+-------------+
            |
            v
+-------------------------+
| OuterLoopRunner.run_once|
| - read outer_state      |
| - provider.poll         |
| - ready filter          |
| - dedupe/force gating   |
| - create run dirs/files |
| - launch inner loops    |
| - write outer_state/log |
+-------+-----------+-----+
        |           |
        |           v
        |   +----------------------+
        |   | .loops/oloops.log    |
        |   | .loops/outer_state   |
        |   +----------------------+
        v
+-------------------------------+
| inner-loop launcher closure   |
| sets LOOPS_TASK_* + RUN_DIR   |
| run(sync) or popen(detached)  |
+-------------------------------+
        |
        v
+-------------------------------+
| .loops/jobs/<run>/            |
| run.json, run.log, agent.log  |
+-------------------------------+
```

## Metrics

No dedicated metrics emitter exists in code today.

Useful derived metrics:

- Ready count per poll cycle (`ready` from `oloops.log` entries).
- Processed/launched count per poll cycle (`processed` from `oloops.log`).
- Launch throughput (`processed` over time).
- Dedupe rate (`ready - processed` in steady state without force).
- First-run suppression effect (`emit_on_first_run=false` yields `processed=0` on first cycle).

## Logs

Key outer-loop logs and emit sites:

- Per-cycle summary log: `_log(self.log_path, _format_log_line(...))` (`loops/outer_loop.py:206`).
- Log format payload: `ready=<n> processed=<m>` (`loops/outer_loop.py:493`).
- Log sink file: `.loops/oloops.log` (`loops/outer_loop.py:156`).

Related launch output behavior:

- Detached mode routes child stdout/stderr to per-run `run.log` (`loops/outer_loop.py:319`).
- Sync mode uses foreground `subprocess.run` and does not detach (`loops/outer_loop.py:310`).

## FAQ

Q: Why can `run_once` find ready tasks but launch none?
A: On first run with `emit_on_first_run=false`, tasks are recorded in outer state but intentionally not emitted (`loops/outer_loop.py:170`, `loops/outer_loop.py:175`).

Q: What is the difference between dedupe and force?
A: Dedupe skips already-seen tasks (`has_task`), while `force=true` bypasses that check and re-launches (`loops/outer_loop.py:173`, `loops/outer_loop.py:177`).

Q: When does outer state persist if launch fails?
A: State write happens in `finally`, so initialization and task ledger updates persist even when launcher raises (`loops/outer_loop.py:199`, `loops/outer_loop.py:205`).

Q: How is `loops_root` chosen?
A: If config is inside `.loops/`, that directory is used; otherwise `.loops/` is created adjacent to config (`loops/cli.py:275`).

## Manual Notes 

[keep this for the user to add notes. do not change between edits]

## Changelog
- 2026-02-16: Created outer-loop flow doc covering poll, dedupe, run materialization, and launch semantics. (019c6863-d581-7f83-9809-fabbefa042e8)

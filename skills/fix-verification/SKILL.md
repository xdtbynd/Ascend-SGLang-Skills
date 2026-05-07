---
name: fix-verification
description: >
    After a regression fix has been applied (e.g. following analysis from test-regression-pr-locator),
    this skill verifies the fix by spinning up a fresh Docker container, running the previously-failing
    test, and confirming the fix works as expected. All commands are echoed for auditability.
    Designed as the final quality gate in the regression-fix pipeline.
---

# Fix Verification — Docker‑Based Regression Validation

## Reference Index

| Item                   | Path (relative to SGLang repo root)               | Description                                                                               |
| ---------------------- | ------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| SGLang Test Utilities  | `python/sglang/test/test_utils.py`                | Official SGLang test framework — provides `run_test`, `check_health`, etc. Use first.     |
| SGLang Test Script     | `python/sglang/test/run_tests.py`                 | Automated smoke-test script (equivalent to the one in `sglang-npu-adapter`).              |
| NPU Adapter Test Config| `../sglang-npu-adapter/templates/test_config.json`| Reference test configuration template.                                                    |
| NPU Test Validator     | `../sglang-npu-adapter/prompts/test_validator.md` | Sub-agent prompt for SGLang validation — reusable test-case generation logic.             |

> **Path Resolution Rule:** The SGLang repository root is detected dynamically in Step 0 (`git rev-parse --show-toplevel` or by walking up to find `python/sglang/test/test_utils.py`). It is never hard-coded. Each developer's local path will differ; the detected result is authoritative.
>
> **Priority Rule:** When `repo_url` points to an SGLang project, prefer SGLang's own `test_utils.py` test framework over generic `pytest` / `jest` commands. Use the pre-built SGLang Docker image (`lmsysorg/sglang:latest`) which already contains all dependencies — no in-container install needed.

## Trigger Conditions

- A regression fix has been applied and needs to be verified in a clean environment.
- `test-regression-pr-locator` (or similar) has identified a problematic PR and a fix has been produced.
- User explicitly requests: "verify this fix in Docker," "validate the regression fix," or "run verification."
- A previously-failing test now needs proof that it passes with the fix applied.

## Role in the Pipeline

```
test-regression-pr-locator  →  finds the root-cause PR
        ↓
  (other skills fix it)      →  code-review-and-quality / debugging-and-error-recovery / incremental-implementation
        ↓
  fix-verification (this)    →  Docker-based clean-room validation ← YOU ARE HERE
```

## Input Parameters

| Parameter           | Type    | Required | Description                                                                                 |
| ------------------- | ------- | -------- | ------------------------------------------------------------------------------------------- |
| `repo_url`          | string  | yes      | Full repository URL (GitHub/GitLab), HTTPS or SSH                                           |
| `branch_or_ref`     | string  | yes      | Branch/commit/tag containing the fix (e.g., `fix/login-regression`, `abc1234`)              |
| `test_case`         | string  | yes      | Test case name/path that was previously failing (e.g., `tests/test_auth.py::test_login`)    |
| `expected_behavior` | string  | yes      | What the test should output when passing (e.g., "PASSED", "OK", "2 passed")                 |
| `docker_image`      | string  | no       | Pre-built Docker image to pull and use. For SGLang, defaults to `lmsysorg/sglang:latest`.   |
| `build_command`     | string  | no       | Test command to run inside the container (default: auto-detect from project structure). No setup steps — the image has all deps. |
| `failure_log`       | string  | no       | The original failure log (for comparison — to confirm the old failure is gone)              |
| `timeout_seconds`   | integer | no       | Max runtime for the container verification (default 600)                                    |
| `gpu_required`      | boolean | no       | Whether the test needs GPU/NPU access (default false); if true, use `--gpus all`             |
| `env_vars`          | object  | no       | Additional environment variables for the container (key-value pairs)                        |

## Execution Flow

### Step 0: Pre‑validation & Context Gathering

**Goal:** Ensure all required inputs are available and consistent before any container work begins.

1. **Verify the fix source:**
    - Confirm `repo_url` is reachable (e.g., `git ls-remote <repo_url>`).
    - Confirm `branch_or_ref` exists and contains the fix.
    - If neither `repo_url` nor `branch_or_ref` can be verified, ask the user.

2. **Verify the test case:**
    - Confirm the test path exists in the repository at `branch_or_ref`.
    - If the test file cannot be found, warn the user and ask for a corrected path.

3. **Capture the baseline (the "before" state):**
    - If `failure_log` is provided, record it as the baseline failure.
    - If not provided, attempt to infer from CI logs or ask the user to supply it.

4. **Resolve the Docker image** (when `docker_image` is not provided):
    - **SGLang projects** → `lmsysorg/sglang:latest` (official pre-built image, all dependencies included).
    - If the repo has a `Dockerfile` → infer the image name from it.
    - Other ecosystems → check `.devcontainer.json` then fallback to `python:3.10-slim` / `node:18-alpine` etc.
    - Record the resolved image in `verification_config.json`.

5. **Auto‑detect build command** (when `build_command` is not provided):
    - The image already has all dependencies — the command is **just the test invocation**, no setup steps:

| Ecosystem  | Default Test Command (no install, image has deps)                                              |
| ---------- | ---------------------------------------------------------------------------------------------- |
| **SGLang** | `python -m pytest python/sglang/test/<test_file>::<test_name> -v`                              |
| Python     | `python -m pytest <test_case> -v`                                                              |
| Node.js    | `npx jest <test_case> --verbose`                                                               |
| Go         | `go test -v -run <TestName> <package>`                                                         |
| Rust       | `cargo test <test_name> -- --nocapture`                                                        |
| C/C++      | `ctest --output-on-failure -R test_name`                                                       |
| Java/Maven | `mvn test -Dtest=<TestClass>`                                                                  |

6. **Load the Docker verifier prompt** — read `prompts/docker_verifier.md` and keep it ready for sub‑agent dispatch.

**Output of Step 0:** A filled `verification_config.json` written to the workspace.

---

### Step 1: Pull Image & Prepare Docker Command

**Goal:** Pull the pre-built image, construct the run command, and print it for auditability.

1. **Pull the image explicitly:**

    ```bash
    docker pull ${docker_image}
    ```

    - This is a separate, visible step. If the pull fails (network, auth), the user sees it immediately.
    - On retry, the cached layers are reused — no redundant downloads.
    - If the pull fails, try the ecosystem fallback image and warn the user.

2. **Construct the Docker run command:**

    ```bash
    # Template (filled with actual values):
    docker run --rm \
        ${gpu_required:+--gpus all} \
        ${env_vars_as_flags} \
        -v /path/to/repo:/workspace \
        -w /workspace \
        ${docker_image} \
        bash -c "
            git fetch origin ${branch_or_ref} && \
            git checkout ${branch_or_ref} && \
            ${build_command}
        "
    ```

    **No `pip install`, no `apt-get`** — the image already has everything. The container only needs to checkout the fix branch and run the test.

3. **SGLang project — concrete example:**

    ```bash
    docker pull lmsysorg/sglang:latest
    docker run --rm \
        -v ${SGLANG_ROOT}:/workspace \
        -w /workspace \
        -e PYTHONPATH=/workspace/python \
        lmsysorg/sglang:latest \
        bash -c "
            git fetch origin ${branch_or_ref} && \
            git checkout ${branch_or_ref} && \
            python -m pytest python/sglang/test/test_xxx.py::test_case -v
        "
    ```

    - `PYTHONPATH=/workspace/python` ensures the mounted local code overrides the image's installed SGLang.
    - If NPU inference validation is involved, reference `sglang-npu-adapter` Step 5 for the two‑stage model (Dummy → Real weights).

4. **Print the full command** (auditability requirement):

    ```
    === VERIFICATION DOCKER COMMAND ===
    docker pull lmsysorg/sglang:latest
    docker run --rm \
        -v /home/user/sglang:/workspace \
        -w /workspace \
        -e PYTHONPATH=/workspace/python \
        lmsysorg/sglang:latest \
        bash -c "git checkout fix/my-fix && python -m pytest python/sglang/test/test_xxx.py::test_case -v"
    === END COMMAND ===
    ```

5. **If GPU/NPU is needed:**
    - Verify with: `docker run --rm --gpus all ${docker_image} nvidia-smi`
    - If unavailable, warn and ask whether to proceed without GPU.

**Output of Step 1:** Image pulled, resolved Docker command printed and logged.

---

### Step 2: Execute the Verification in Docker

**Goal:** Run the command, capture all output, and determine pass/fail.

1. **Run the Docker command** (do NOT just describe it — actually execute it).

2. **Capture:**
    - `stdout` + `stderr` → write to `output/docker_run.log`
    - Exit code → write to `output/exit_code.txt`
    - Wall‑clock duration → write to `output/duration.txt`

3. **Health checks (during container run):**
    - Did the container start successfully?
    - Did the test command actually execute (not timeout before reaching it)?

4. **Timeouts:** If the container does not finish within `timeout_seconds`, kill it (`docker kill <name>`) and mark as `TIMEOUT`.

**Output of Step 2:** Raw outputs (`docker_run.log`, `exit_code.txt`, `duration.txt`).

---

### Step 3: Analyze Results

**Goal:** Compare actual output against `expected_behavior` and determine the verdict.

1. **Parse the test output** — extract pass/fail counts, match against `expected_behavior`.
2. **Compare to the original failure** (if `failure_log` provided) — confirm old error is absent.
3. **Verdict matrix:**

| Exit Code | Output Matches Expected | Old Error Gone | Verdict            |
| --------- | ----------------------- | -------------- | ------------------ |
| 0         | Yes                     | Yes            | **PASSED**         |
| 0         | Yes                     | —              | **PASSED**         |
| 0         | No                      | —              | **UNEXPECTED**     |
| ≠0        | —                       | No             | **FIX_INCOMPLETE** |
| ≠0        | —                       | Yes            | **PARTIAL**        |
| timeout   | —                       | —              | **TIMEOUT**        |

4. **Fill `verification_result.json`**.

**Output of Step 3:** Structured result in `verification_result.json`.

---

### Step 4: Generate Verification Report

**Goal:** Produce a human‑readable report for the PR/issue.

Use `templates/verification_report.md` and fill in: header, environment, exact Docker command, verdict, evidence, and next steps.

**Output of Step 4:** `output/verification_report.md`.

---

### Step 5: Cleanup & Handoff

1. Stop/remove the container if still running.
2. Summarize the result to the user (the `=== FIX VERIFICATION SUMMARY ===` block).
3. Handoff to `shipping-and-launch` if verified, or back to `debugging-and-error-recovery` if not.

---

## Per‑Step Checklist (Must Follow Every Step)

- [ ] Step 0: All parameters verified, image resolved, `verification_config.json` written
- [ ] Step 1: Image `docker pull` executed, command constructed and printed (`=== VERIFICATION DOCKER COMMAND ===`)
- [ ] Step 2: Docker container actually executed (not "could run" — really ran)
- [ ] Step 3: Output parsed, `verification_result.json` populated
- [ ] Step 4: `verification_report.md` generated with full Docker command
- [ ] Step 5: Container cleaned up, result summarized

## Hard Constraints

1. **Must pull the image explicitly** — `docker pull` as a visible step before `docker run`.
2. **Must print the full Docker command** — copy‑paste‑ready, no vague descriptions.
3. **Must actually execute** — no theoretical analysis that skips the real container run.
4. **Must run in a clean container** — fresh `docker run --rm` every time.
5. **No in‑container installs** — the image has all dependencies; `build_command` is test-only.
6. **Kill on timeout** — after `timeout_seconds`, immediately `docker kill`; no indefinite waiting.

## File Structure

```
skills/fix-verification/
├── SKILL.md                          # This file — main skill definition
├── prompts/
│   ├── docker_verifier.md            # Sub-agent prompt for the Docker verifier
│   └── result_analyzer.md            # Sub-agent prompt for the result analyzer
├── templates/
│   ├── verification_config.json      # Verification config template
│   ├── verification_result.json      # Verification result template
│   └── verification_report.md        # Verification report template
└── scripts/
    ├── run_verification.sh           # Automated verification execution script
    └── parse_test_output.py          # Test output parsing script
```

## Quality Gates

- [ ] Image pulled successfully before `docker run`
- [ ] `verification_config.json` — all required fields are non-empty
- [ ] Docker command in the report is complete, copyable, and executable
- [ ] `verification_result.json.verdict` is a valid enum value
- [ ] Report includes exit code, duration, and key output lines
- [ ] If verdict ≠ `PASSED`, report includes actionable next steps
- [ ] Container is cleaned up (`docker rm` or already used `--rm`)

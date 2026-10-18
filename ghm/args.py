import argparse
import getpass
import hashlib
import json
import re
import subprocess
import timeago
import datetime
import time
import os
import sys
from prettytable import PrettyTable
from .runner import GhRunner, GitRunner
from .utils import load_repos, REPO_CONFIG_LOCATION, fetch_buildpack_toml
from .cache import get_cache

NOT_RUNNABLE = (
    "could not create workflow dispatch event: HTTP 422:"
    " Workflow does not have 'workflow_dispatch' trigger"
)
NOT_FOUND_WORKFLOW = "could not find any workflows named"


def check_run_ok(s):
    return (
        s["__typename"] == "CheckRun"
        and s["status"] == "COMPLETED"
        and s["conclusion"] == "SUCCESS"
    )


def check_status_context(s):
    return s["__typename"] == "StatusContext" and s["state"] == "SUCCESS"


def check_status_ok(s):
    return check_run_ok(s) or check_status_context(s)


def pr_actions_ok(pr):
    status = pr["statusCheckRollup"] and pr["statusCheckRollup"] or []
    return str(all(map(check_status_ok, status)))


def filter_repos(repos, repo, filter=None):
    if filter is None:
        return [r for r in repos if repo is None or r == repo]

    pattern = re.compile(filter)
    result = []
    for r in repos:
        m = pattern.match(r)
        if m:
            result.append(r)
    return result


def handle_repos_local(args):
    print(f"Repos configured in [{REPO_CONFIG_LOCATION}]")
    print("\t" + "\n\t".join(load_repos()))


def handle_repos_remote(args):
    repos = filter_repos(
        load_repos(remote_repos=True, org=args.org), args.repo, args.repo_filter
    )
    if not args.json:
        print("Repos available remotely")
        print("\t" + "\n\t".join(repos))
    else:
        print(json.dumps(repos, indent=2))


def handle_repos_toggle_branch_enforce_admin(args):
    runner = GhRunner()
    repos = filter_repos(
        load_repos(pr_list=args.pr_list), args.repo, args.repo_filter
    )
    branch = args.branch or "main"
    for repo in repos:
        print(f"Processing repository: {repo}")
        print("----------------------------------------")
        try:
            enabled = runner.branch_enforce_admins_enabled(repo, branch)
        except Exception:
            print(f"    Error: Could not fetch branch protection for {repo}/{branch}")
            print()
            continue

        if enabled:
            print(
                "    Current status: Bypass prevention ENABLED"
                " (admins cannot bypass)"
            )
            print(
                "    Action: DISABLING bypass prevention"
                " (allowing admins to bypass)"
            )
            try:
                runner.branch_enforce_admins_disable(repo, branch)
                print("    Successfully disabled bypass prevention")
            except Exception:
                print("    Error: Could not disable bypass prevention")
        else:
            print(
                "    Current status: Bypass prevention DISABLED"
                " (admins can bypass)"
            )
            print(
                "    Action: ENABLING bypass prevention"
                " (preventing admin bypass)"
            )
            try:
                runner.branch_enforce_admins_enable(repo, branch)
                print("    Successfully enabled bypass prevention")
            except Exception:
                print("    Error: Could not enable bypass prevention")
        print()
    print("All repositories processed!")


def handle_repos_settings_pull(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(), args.repo, args.repo_filter)
    branch = args.branch or "main"
    for repo in repos:
        # Check if we should resume and file already exists
        if args.resume and args.output:
            import os
            safe_name = repo.replace("/", "_")
            filepath = os.path.join(args.output, f"{safe_name}.json")
            if os.path.exists(filepath):
                print(f"Skipping {repo} (already exists: {filepath})", file=sys.stderr)
                continue

        print(f"Fetching settings for {repo}...", file=sys.stderr)
        try:
            settings = runner.fetch_all_settings(repo, branch)
        except Exception as ex:
            print(f"Error fetching settings for {repo}: {ex}", file=sys.stderr)
            continue

        output = json.dumps(settings, indent=2)
        if args.output:
            import os
            os.makedirs(args.output, exist_ok=True)
            safe_name = repo.replace("/", "_")
            filepath = os.path.join(args.output, f"{safe_name}.json")
            with open(filepath, "w") as f:
                f.write(output)
            print(f"Settings written to {filepath}", file=sys.stderr)
        else:
            print(output)


def format_settings_change(ch):
    """Format a settings diff entry into a readable string."""
    section = ch.get("section")
    field = ch.get("field")
    name = ch.get("name")
    current = ch.get("current")
    desired = ch.get("desired")
    where = f"{section}.{field}" + (f" ({name})" if name else "")
    if ch.get("action") == "add":
        return f"{where}: ADD {desired}"
    if ch.get("action") == "remove":
        return f"{where}: REMOVE {current}"
    if current is None:
        return f"{where}: (none) -> {desired}"
    if isinstance(current, list) or isinstance(desired, list):
        return f"{where}: {current} -> {desired}"
    return f"{where}: {current} -> {desired}"


def format_push_results(results):
    """Format push results into readable lines, highlighting failures."""
    lines = []
    for section, result in results.items():
        if isinstance(result, dict) and result.get("status") == "error":
            lines.append(f"    {section}: ERROR: {result.get('error', '').strip()}")
            continue
        if isinstance(result, dict) and result.get("status") == "partial":
            lines.append(f"    {section}: PARTIAL")
            for f in result.get("results", {}).get("failed", []):
                lines.append(f"      {f.get('field')}: ERROR: {f.get('error', '').strip()}")
            continue
        if isinstance(result, list):
            errors = [r for r in result if r.get("status") == "error"]
            done = [r for r in result if r.get("status") != "error"]
            summary = ", ".join(
                sorted({f"{r.get('status')}" for r in done})
            ) or "no changes"
            lines.append(f"    {section}: {summary} ({len(done)} ok, {len(errors)} failed)")
            for r in errors:
                who = r.get("name") or r.get("slug") or r.get("login") or "?"
                lines.append(f"      {who}: ERROR: {r.get('error', '').strip()}")
            continue
        status = result.get("status") if isinstance(result, dict) else result
        detail = ""
        if isinstance(result, dict) and result.get("reason"):
            detail = f" ({result['reason']})"
        lines.append(f"    {section}: {status}{detail}")
    return lines


def handle_repos_settings_push(args):
    runner = GhRunner()

    # Load settings from JSON file
    with open(args.input, "r") as f:
        settings = json.load(f)

    # Determine target repos
    if args.repo:
        repos = [args.repo]
    elif "repository" in settings and "full_name" in settings["repository"]:
        repos = [settings["repository"]["full_name"]]
    elif args.repo_filter:
        repos = filter_repos(load_repos(), None, args.repo_filter)
    else:
        print(
            "Error: No target repo specified. Use --repo, --repo-filter,"
            " or include repository.full_name in the JSON",
            file=sys.stderr,
        )
        return

    branch = args.branch or "main"
    dry_run = args.dry_run or False
    complete = args.complete or False

    if not complete:
        print(
            "Per-repo settings (topics, collaborators, teams) will be skipped."
            " Pass --complete to apply them.",
            file=sys.stderr,
        )

    # Skip the repo the settings came from - pushing to it is a no-op
    source_repo = settings.get("repository", {}).get("full_name")
    if source_repo:
        skipped = [r for r in repos if r == source_repo]
        repos = [r for r in repos if r != source_repo]
        for r in skipped:
            print(f"Skipping {r} (settings originated from this repo)", file=sys.stderr)

    for repo in repos:
        print(f"Pushing settings to {repo}...", file=sys.stderr)
        if dry_run:
            try:
                current = runner.fetch_all_settings(repo, branch)
                changes = runner.settings_diff(settings, current, complete=complete)
            except Exception as ex:
                print(f"  Error fetching settings for {repo}: {ex}", file=sys.stderr)
                continue
            if not changes:
                print(f"  [DRY RUN] No changes needed for {repo}", file=sys.stderr)
                continue
            print(f"  [DRY RUN] Changes that would be applied to {repo}:", file=sys.stderr)
            for ch in changes:
                print(f"    {format_settings_change(ch)}", file=sys.stderr)
            continue

        try:
            results = runner.push_all_settings(repo, settings, branch, dry_run, complete)
        except Exception as ex:
            print(f"  Error pushing settings to {repo}: {ex}", file=sys.stderr)
            continue

        failed = any(
            (isinstance(r, dict) and r.get("status") in ("error", "partial"))
            or (isinstance(r, list) and any(x.get("status") == "error" for x in r))
            for r in results.values()
        )
        header = "  Settings pushed to"
        print(f"{header} {repo}:", file=sys.stderr)
        for line in format_push_results(results):
            print(line, file=sys.stderr)
        if failed:
            print(f"  Done with errors for {repo}", file=sys.stderr)
        else:
            print(f"  Done for {repo}", file=sys.stderr)


def read_secret_value(what):
    """Read a value securely, from STDIN or an interactive prompt.

    When input is piped in, the value is read from STDIN so the command can be
    scripted. Otherwise the value is read from the terminal without echoing.
    `what` describes the value being read, e.g. `secret 'FOO'`.
    """
    if not sys.stdin.isatty():
        return sys.stdin.read().rstrip("\r\n")

    value = getpass.getpass(f"Enter value for {what}: ")
    confirm = getpass.getpass(f"Confirm value for {what}: ")
    if value != confirm:
        print("Error: values do not match", file=sys.stderr)
        sys.exit(1)
    return value


def _handle_repos_secrets(args, must_exist):
    runner = GhRunner()
    repo = args.repo
    name = args.name
    secret_type = args.type

    try:
        exists = runner.secret_exists(repo, name, secret_type)
    except Exception as ex:
        print(f"Error: {ex}", file=sys.stderr)
        sys.exit(1)

    if must_exist and not exists:
        print(
            f"Error: secret '{name}' does not exist in {repo} ({secret_type})."
            " Use `repos secrets create` to create it.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not must_exist and exists:
        print(
            f"Error: secret '{name}' already exists in {repo} ({secret_type})."
            " Use `repos secrets update` to change it.",
            file=sys.stderr,
        )
        sys.exit(1)

    value = read_secret_value(f"secret '{name}'")
    if not value:
        print(
            "Error: no secret value provided. Pipe the value on STDIN"
            " or enter it at the prompt.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        runner.secret_set(repo, name, value, secret_type)
    except Exception as ex:
        print(f"Error: {ex}", file=sys.stderr)
        sys.exit(1)

    verb = "Updated" if must_exist else "Created"
    print(f"{verb} {secret_type} secret '{name}' in {repo}", file=sys.stderr)


def handle_repos_secrets_create(args):
    _handle_repos_secrets(args, must_exist=False)


def handle_repos_secrets_update(args):
    _handle_repos_secrets(args, must_exist=True)


SECRET_TYPES = ["actions", "agents", "codespaces", "dependabot"]


def _parse_gh_timestamp(value):
    if not value:
        return None
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _format_secret_time(value):
    parsed = _parse_gh_timestamp(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else "-"


def _format_secret_age(value):
    parsed = _parse_gh_timestamp(value)
    if not parsed:
        return "-"
    return timeago.format(parsed, datetime.datetime.utcnow())


def handle_repos_secrets_list(args):
    runner = GhRunner()
    repo = args.repo
    types = SECRET_TYPES if args.type == "all" else [args.type]

    pt = PrettyTable()
    pt.field_names = ["NAME", "TYPE", "CREATED", "UPDATED", "AGE"]
    for column in ("NAME", "TYPE", "CREATED", "UPDATED", "AGE"):
        pt.align[column] = "l"
    pt.sortby = "UPDATED"

    rows = 0
    for secret_type in types:
        try:
            secrets = runner.fetch_secrets(repo, secret_type)
        except Exception as ex:
            print(f"Warning: {ex}", file=sys.stderr)
            continue
        for secret in secrets or []:
            updated = secret.get("updated_at")
            pt.add_row(
                [
                    secret.get("name", ""),
                    secret_type,
                    _format_secret_time(secret.get("created_at")),
                    _format_secret_time(updated),
                    _format_secret_age(updated),
                ]
            )
            rows += 1

    if rows == 0:
        print(f"No secrets found in {repo}", file=sys.stderr)
        return
    print(pt)


def handle_repos_list(args):
    parser = argparse.ArgumentParser(prog="manage.py repos list")
    parser.add_argument(
        "--repo", help="repo name"
    )
    parser.add_argument(
        "--repo-filter", help="filter on repo name"
    )
    parser.add_argument(
        "--branch",
        help="branch to check (default: main)",
        default="main",
    )
    parser.add_argument(
        "--pr-list",
        help="path to a file containing PR URLs to act on",
        type=path_exists,
    )
    parser.add_argument(
        "--json", help="output in json", action=argparse.BooleanOptionalAction
    )
    parser.print_help()


def handle_repos_list_branch_enforce_admin(args):
    runner = GhRunner()
    repos = filter_repos(
        load_repos(pr_list=args.pr_list), args.repo, args.repo_filter
    )
    branch = args.branch or "main"
    print(f"{'REPO':<45} {'BRANCH':<15} {'ENFORCE_ADMINS':<15}")
    for repo in repos:
        try:
            enabled = runner.branch_enforce_admins_enabled(repo, branch)
            print(f"{repo:<45} {branch:<15} {str(enabled):<15}")
        except Exception:
            print(f"{repo:<45} {branch:<15} {'ERROR':<15}")


def handle_pr_list(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(pr_list=args.pr_list), args.repo, args.repo_filter)
    cols = "{:<45} {:^6} {:^10} {:^15} {:^10} {:^18} {:^10} {:^20} {}"
    print(
        cols.format(
            "REPO",
            "NUMBER",
            "STATE",
            "MERGE?",
            "MERGST",
            "REVIEW",
            "CHECK?",
            "AUTHOR",
            "TITLE",
        )
    )
    prs_by_repo = runner.pr_list_many(
        repos,
        filter=args.filter,
        merge_state=args.merge_state,
        review_decision=args.review_decision,
        author=args.author,
    )
    for repo, prs in prs_by_repo.items():
        for pr in prs:
            print(
                cols.format(
                    repo,
                    str(pr["number"]),
                    pr["state"],
                    pr["mergeable"],
                    pr["mergeStateStatus"],
                    pr["reviewDecision"],
                    pr_actions_ok(pr),
                    pr.get("author", {}).get("login", "n/a"),
                    pr["title"][:75],
                )
            )


def handle_pr_approve(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(pr_list=args.pr_list), args.repo, args.repo_filter)
    prs_by_repo = runner.pr_list_many(
        repos,
        filter=args.filter,
        merge_state=args.merge_state,
        review_decision=args.review_decision,
        author=args.author,
    )
    for repo, prs in prs_by_repo.items():
        for pr in prs:
            print(f"    Approving {repo} -> {pr['number']} [{pr['title']}]")
            stdout, stderr = runner.pr_approve(repo, pr["number"])
            if stdout:
                print(stdout)
            if stderr:
                print(stderr)


def handle_open(args):
    GhRunner().pr_open(args.repo, args.number)


def handle_pr_create(args):
    gr = GitRunner()
    ghr = GhRunner()

    print("Creating PRs...")
    repos = filter_repos(load_repos(), args.repo, args.repo_filter)
    for num_run, repo in enumerate(repos):
        print(f"  {repo}")
        repo_path = os.path.join(args.workdir, repo)

        if os.path.exists(repo_path):
            # repo exists, clean & update it
            gr.cwd(repo_path)
            gr.clean()
            gr.checkout_branch("main")
            gr.reset_hard("origin/main")
            gr.pull()
            if not _is_branch_clean(gr):
                raise RuntimeError(f"branch at {repo_path} has unclean working tree")
        else:
            # repo doesn't exist, clone it
            repo_parent = os.path.dirname(repo_path)
            os.makedirs(repo_parent, exist_ok=True)
            gr.cwd(repo_parent)
            repo_url = f"git@github.com:{repo}.git"
            gr.clone(repo_url)
            gr.cwd(repo_path)

        # check out branch & make changes
        branch = _branch_name(args.script)
        gr.checkout_new_branch(branch)
        _run_script(repo_path, args.script)
        if args.title is not None or args.body is not None:
            if _is_branch_clean(gr):
                print(
                    f"    Skipping {repo} which was not" f" modified by {args.script}"
                )
                continue  # nothing to commit

            # add & commit any changes
            gr.add(".")
            gr.commit(args.title, args.body)
            gr.push(branch)
        else:
            if not _was_commit_made(gr, branch):
                print(
                    f"    Skipping {repo} which was not" f" modified by {args.script}"
                )
                continue  # nothing was committed
            gr.push(branch)

        # create a pull request
        ghr.pr_create(repo_path, args.label)

        if args.batch_size is not None and (num_run + 1) % args.batch_size == 0:
            print("    *** Batch Submitted - Pausing ***")
            time.sleep(args.batch_pause)
            num_run = 0


def _is_branch_clean(gr):
    stdout = gr.status()[0].decode("utf-8").strip()
    return stdout.endswith("nothing to commit, working tree clean")


def _was_commit_made(gr, branch):
    head = gr.rev_parse("main")
    br = gr.rev_parse(branch)
    return head != br


def _branch_name(script):
    h = hashlib.sha256(open(script).read().encode("utf-8"))
    return f"ghm-pr-{h.hexdigest()[0:8]}"


def _run_script(cwd, script):
    try:
        proc = subprocess.run(
            os.path.realpath(script),
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
        )
        if len(proc.stdout) > 0:
            print("STDOUT:")
            print(proc.stdout)
        if len(proc.stderr) > 0:
            print("STDERR:")
            print(proc.stderr)
    except subprocess.CalledProcessError as ex:
        print(f"Error running script: {ex.cmd}")
        print(f"Exit Code: {ex.returncode}")
        print()
        print("STDOUT:")
        print(ex.stdout)
        print()
        print("STDERR:")
        print(ex.stderr)
        raise RuntimeError("script execution failed, see errors above")


def _run_workflow(runner, repo, filter, batch_size, batch_pause):
    pattern = re.compile(filter)

    num_run = 0
    workflows = runner.workflow_list(repo)
    for workflow in workflows:
        m = pattern.match(workflow)
        if filter is None or m:
            num_run += 1
            print(f"    Running {repo} -> {workflow}")
            try:
                stdout, stderr = runner.workflow_run(repo, workflow)
                if stdout:
                    print(f"        {stdout.decode('UTF-8').strip()}")
                if stderr:
                    print(f"        {stderr.decode('UTF-8').strip()}")
            except Exception as ex:
                errMsg = ex.stderr.decode("UTF-8").strip()
                if not errMsg.startswith(NOT_RUNNABLE):
                    raise ex
                print(f"        Skipped {repo}/{workflow}, not runnable")
            if batch_size is not None and num_run % batch_size == 0:
                print("    *** Batch Submitted - Pausing ***")
                time.sleep(batch_pause)
                num_run = 0
    return num_run


def handle_action_run(args):
    _run_workflow(GhRunner(), args.repo, args.filter)


def handle_action_run_matching(args):
    runner = GhRunner()
    num_run = 0
    repos = filter_repos(load_repos(), args.repo, args.repo_filter)
    for repo in repos:
        num_run += _run_workflow(
            runner, repo, args.filter, args.batch_size, args.batch_pause
        )
        if args.batch_size is not None and num_run % args.batch_size == 0:
            print("    *** Batch Submitted - Pausing ***")
            time.sleep(args.batch_pause)


def _rerun_failed(runner, pr, repo):
    rollup = pr.get("statusCheckRollup") or []
    failed = [s for s in rollup if s.get("conclusion") == "FAILURE"]
    for fail in failed:
        print(f"    Rerunning {repo} -> {fail['name']} ({fail['detailsUrl']}")
        run_num = fail["detailsUrl"].split("/")[-1]
        stdout, stderr = runner.action_run_rerun(repo, run_num)
        if stdout:
            print(stdout)
        if stderr:
            print(stderr)


def handle_action_rerun(args):
    runner = GhRunner()
    pr = runner.pr_get(args.repo, args.number)
    _rerun_failed(runner, pr, args.repo)


def handle_action_rerun_matching(args):
    runner = GhRunner()
    repos = load_repos(pr_list=args.pr_list)
    for repo in repos:
        prs = runner.pr_list(
            repo,
            filter=args.filter,
            merge_state=args.merge_state,
            author=args.author,
            pr_list=args.pr_list,
        )
        if args.failed:
            prs = [
                pr
                for pr in prs
                if any(
                    map(
                        lambda pr: not check_status_ok(pr),
                        pr.get("statusCheckRollup") or [],
                    )
                )
            ]
        for pr in prs:
            _rerun_failed(runner, pr, repo)


def handle_action_enable_matching(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(), args.repo, args.repo_filter)
    pattern = re.compile(args.filter is None and ".*" or args.filter)

    for repo in repos:
        workflows = runner.workflow_list(repo)
        for workflow in workflows:
            m = pattern.match(workflow)
            if args.filter is None or m:
                print(f"    Enabling {repo} -> {workflow}")
                try:
                    stdout, stderr = runner.workflow_enable(repo, workflow)
                    if stdout:
                        print(stdout)
                    if stderr:
                        print(stderr)
                except Exception as ex:
                    errMsg = ex.stderr.decode("UTF-8").strip()
                    if not errMsg.startswith(NOT_FOUND_WORKFLOW):
                        raise ex
                    print("        Skipped. Already enabled?")


def handle_action_disable_matching(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(), args.repo, args.repo_filter)
    pattern = re.compile(args.filter is None and ".*" or args.filter)

    for repo in repos:
        workflows = runner.workflow_list(repo)
        for workflow in workflows:
            m = pattern.match(workflow)
            if args.filter is None or m:
                print(f"    Disabling {repo} -> {workflow}")
                try:
                    stdout, stderr = runner.workflow_disable(repo, workflow)
                    if stdout:
                        print(stdout)
                    if stderr:
                        print(stderr)
                except Exception as ex:
                    errMsg = ex.stderr.decode("UTF-8").strip()
                    if not errMsg.startswith(NOT_FOUND_WORKFLOW):
                        raise ex
                    print("        Skipped. Already disabled?")


def handle_action_run_active_list(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(args.all_repos), args.repo, args.repo_filter)

    pt = PrettyTable()
    pt.field_names = [
        "REPO",
        "ID",
        "STATUS",
        "EVENT",
        "CREATED AT",
        "RUN STARTED AT",
        "DURATION",
        "RUN ATTEMPT",
        "NAME",
    ]
    pt.align["REPO"] = "l"
    pt.align["NAME"] = "l"
    pt.sortby = "CREATED AT"

    for repo in repos:
        data = runner.run_list_active(repo, args.status).get("workflow_runs", {})
        for wf_run in data:
            created_at = datetime.datetime.strptime(
                wf_run.get("created_at", "0000-00-00T00:00:00Z"), "%Y-%m-%dT%H:%M:%SZ"
            )
            pt.add_row(
                [
                    wf_run.get("repository", {}).get("full_name", "<not found>"),
                    wf_run.get("id", "<not found>"),
                    wf_run.get("status", "<not found>"),
                    wf_run.get("event", "<not found>"),
                    wf_run.get("created_at", "<not found>"),
                    wf_run.get("run_started_at", "<not found>"),
                    timeago.format(created_at, datetime.datetime.utcnow()),
                    wf_run.get("run_attempt", "<not found>"),
                    wf_run.get("name", "<not found>"),
                ]
            )

    print(pt)


def handle_action_run_complete_list(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(args.all_repos), args.repo, args.repo_filter)
    data = []
    for repo in repos:
        repo_data = runner.run_list_complete(repo, args.limit)
        data.extend(repo_data)

    print(
        ",".join(
            [
                "repo",
                "status",
                "event",
                "created_at",
                "run_started_at",
                "updated_at",
                "queue_duration",
                "run_duration",
                "total_duration",
                "run_attempt",
                "name",
            ]
        )
    )
    for row in data:
        created_at = datetime.datetime.strptime(
            row.get("created_at", "0000-00-00T00:00:00Z"), "%Y-%m-%dT%H:%M:%SZ"
        )
        run_started_at = datetime.datetime.strptime(
            row.get("run_started_at", "0000-00-00T00:00:00Z"), "%Y-%m-%dT%H:%M:%SZ"
        )
        updated_at = datetime.datetime.strptime(
            row.get("updated_at", "0000-00-00T00:00:00Z"), "%Y-%m-%dT%H:%M:%SZ"
        )
        print(
            ",".join(
                [
                    row.get("repository", {}).get("full_name", "<not found>"),
                    row.get("status", "<not found>"),
                    row.get("event", "<not found>"),
                    row.get("created_at", "0000-00-00T00:00:00Z"),
                    row.get("run_started_at", "0000-00-00T00:00:00Z"),
                    row.get("updated_at", "0000-00-00T00:00:00Z"),
                    str((run_started_at - created_at).seconds),
                    str((updated_at - run_started_at).seconds),
                    str((updated_at - created_at).seconds),
                    str(row.get("run_attempt", "<not found>")),
                    row.get("name", "<not found>"),
                ]
            )
        )


def _merge_repo_prs(runner, repo, prs, args, break_merging):
    for pr in prs:
        if break_merging:
            return True
        if args.with_approve:
            print(
                f"    Approving & Merging {repo} -> "
                f"{pr['number']} [{pr['title']}]"
            )
            runner.pr_approve(repo, pr["number"])
        else:
            print(f"    Merging {repo} -> {pr['number']} [{pr['title']}]")
        try:
            stdout, stderr = runner.pr_merge(
                repo, pr["number"], args.admin, args.merge_type
            )
            if stderr:
                print(stderr)
            if stdout:
                print(stdout)
        except Exception as ex:
            if ex.stderr:
                print("An error occurred while attempting to merge:")
                print((ex.stderr).decode())
            if ex.returncode != 0:
                if args.skip_failing:
                    continue
                if single_yes_or_no_question(
                    "Do you wish to continue merging?", True
                ):
                    continue
                else:
                    return True
    return break_merging


def _merge_repo_prs_with_admin(runner, repo, prs, args, break_merging):
    base_branch = "main"
    try:
        pr = runner.pr_get(repo, prs[0]["number"])
        base_branch = pr.get("baseRefName", "main")
        if runner.branch_enforce_admins_enabled(repo, base_branch):
            runner.branch_enforce_admins_disable(repo, base_branch)
    except Exception:
        pass

    try:
        return _merge_repo_prs(runner, repo, prs, args, break_merging)
    finally:
        try:
            runner.branch_enforce_admins_enable(repo, base_branch)
        except Exception:
            print(
                f"    Warning: Could not re-enforce branch protection"
                f" on {repo}/{base_branch}"
            )


def handle_pr_merge(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(pr_list=args.pr_list), args.repo, args.repo_filter)
    break_merging = False

    prs_by_repo = runner.pr_list_many(
        repos,
        filter=args.filter,
        merge_state=args.merge_state,
        review_decision=args.review_decision,
        author=args.author,
    )
    for repo, prs in prs_by_repo.items():
        if break_merging:
            break
        if not prs:
            continue

        merge_fn = _merge_repo_prs_with_admin if args.admin else _merge_repo_prs
        break_merging = merge_fn(runner, repo, prs, args, break_merging) or break_merging


def handle_pr_branch_update(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(pr_list=args.pr_list), args.repo, args.repo_filter)
    for repo in repos:
        prs = runner.pr_list(
            repo,
            filter=args.filter,
            merge_state=args.merge_state,
            author=args.author,
            pr_list=args.pr_list,
        )
        for pr in prs:
            if pr["mergeStateStatus"] == "BEHIND" or args.force:
                print(
                    f"    Updating branch {repo} -> " f"{pr['number']} [{pr['title']}]"
                )
                resp = runner.pr_update_branch(repo, pr["number"])
                if (
                    "message" not in resp.keys()
                    or resp["message"] != "Updating pull request branch."
                ):
                    print("Unexpected response:")
                    print(f"    {resp}")


def handle_pr_apply_label(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(pr_list=args.pr_list), args.repo, args.repo_filter)
    for repo in repos:
        prs = runner.pr_list(
            repo,
            filter=args.filter,
            merge_state=args.merge_state,
            author=args.author,
            pr_list=args.pr_list,
        )
        for pr in prs:
            print(f"    Applying tag to {repo} -> {pr['number']} [{pr['title']}]")
            runner.pr_apply_label(repo, pr["number"], args.label)


def handle_pr_remove_label(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(pr_list=args.pr_list), args.repo, args.repo_filter)
    for repo in repos:
        prs = runner.pr_list(
            repo, filter=args.filter, merge_state=args.merge_state, author=args.author, pr_list=args.pr_list,
        )
        for pr in prs:
            print(f"    Removing label from {repo} -> {pr['number']} [{pr['title']}]")
            runner.pr_remove_label(repo, pr["number"], args.label)


def handle_release_list(args):
    runner = GhRunner()

    repos = []
    if args.composite:
        bp_toml = fetch_buildpack_toml(args.composite)
        for order in bp_toml["order"]:
            for group in order["group"]:
                repos.append(group["id"])
    else:
        repos = load_repos()

    repos = filter_repos(repos, args.repo, filter=args.filter)

    if args.summary:
        pt = PrettyTable()
        pt.field_names = [
            "REPO",
            "LATEST VERSION",
            "DRAFT AVAILABLE",
            "LAST RELEASE DATE",
            "SINCE LAST RELEASE",
        ]
        pt.align["REPO"] = "l"
        # separate list for drafts as these aren't sortable by date
        table, drafts = [], []
        for repo in repos:
            r = runner.fetch_latest_release(repo)

            # 2 latest releases are returned per repo
            if not r:
                print(f"Skipping repo {repo}, no release found")
                print()
                continue
            # only 1 release exists, determine if it is a Draft
            if len(r) == 1:
                if r[0][1] == "Draft":
                    drafts.append([repo, "Draft", "YES", "N/A", "N/A"])
                    continue
                else:
                    draft = "NO"
                    r = r[0]
            # If the top release is a Draft, note this for the Available?
            # column and use the second release for the row
            else:
                if r[0][1] == "Draft":
                    draft = "YES"
                    r = r[1]
                else:
                    draft = "NO"
                    r = r[0]
            # removed the timestamp since we don't need this granularity
            # and also there are issues with TZ
            rDate = datetime.datetime.strptime(r[3], "%Y-%m-%dT%H:%M:%S%z").date()
            # Used timeago library to format readable duration since last
            # release
            table.append(
                [
                    repo,
                    r[0].strip().split()[-1:][0],
                    draft,
                    rDate,
                    timeago.format(rDate, datetime.datetime.now()),
                ]
            )
        sorted_table = drafts
        # sort by last-release-date column
        sorted_table.extend(sorted(table, key=lambda row: row[-2]))
        pt.add_rows(sorted_table)
        print(pt)
    else:
        for repo in repos:
            r = runner.fetch_draft_release(repo)
            if not r:
                print(f"Skipping repo {repo}, no release found")
                print()
                continue
            print(f"Release [{r['name'].strip()}]")
            print(f"    Author : {r['author']['login']}")
            print(f"    URL    : {r['url']}")
            print(f"    Tag    : {r['tag_name']}")
            print(f"    Draft  : {r['draft']}")
            print(f"    Pre    : {r['prerelease']}")
            print(f"    Version: {r['name'].strip().split()[-1:][0]}")
            print("")
            print(r["body"])
            print()
            print(124 * "-")
            print()


def handle_release_publish(args):
    runner = GhRunner()

    repos = []
    if args.composite:
        bp_toml = fetch_buildpack_toml(args.composite)
        for order in bp_toml["order"]:
            for group in order["group"]:
                repos.append(group["id"])
    else:
        repos = load_repos()

    repos = filter_repos(repos, args.repo, filter=args.filter)

    if not args.publish:
        print("**DRY RUN** - add the `--publish` flag to actually publish")
        print()

    for repo in repos:
        r = runner.fetch_draft_release(repo)
        if not r:
            print(f"    ** Skipping repo {repo}, no release found")
            continue
        name = " ".join(r["name"].strip().split()[:-1])
        version = r["name"].strip().split()[-1:][0]
        print(f"    Publishing release for {repo} -> [{name}/{version} ]")
        if args.publish:
            runner.release_publish(repo, r["id"], version)


def clear_cache(args):
    get_cache().clear()


def single_yes_or_no_question(question, default_no=True):
    choices = " [y/N]: " if default_no else " [Y/n]: "
    default_answer = "n" if default_no else "y"
    reply = str(input(question + choices)).lower().strip() or default_answer
    if reply[0] == "y":
        return True
    if reply[0] == "n":
        return False
    else:
        return False if default_no else True


def path_exists(p):
    if os.path.isfile(p):
        return p
    else:
        raise argparse.ArgumentTypeError(f"{p} must exist")


def label_valid(label):
    if label is not None and (label.startswith("semver:") or label.startswith("type:")):
        return label
    else:
        raise argparse.ArgumentTypeError(
            f"{label} must start with 'semver:' or 'type:'"
        )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Manage many Github repos in an efficient way"
    )
    parser.add_argument(
        "--skip-cache",
        help="bypass cache for this run",
        action=argparse.BooleanOptionalAction,
    )

    subparsers = parser.add_subparsers()

    subparser_repos = subparsers.add_parser(
        "repos", help="manage repos"
    ).add_subparsers()

    subparser_repos.add_parser(
        "list-local", help="list configured local repos"
    ).set_defaults(func=handle_repos_local)

    subparser_repos_list_remote = subparser_repos.add_parser(
        "list-remote", help="list all remote repos"
    )
    subparser_repos_list_remote.add_argument("--org", help="Github org to search")
    subparser_repos_list_remote.add_argument(
        "--json", help="output in json", action=argparse.BooleanOptionalAction
    )
    subparser_repos_list_remote.add_argument("--repo", help="repo name")
    subparser_repos_list_remote.add_argument(
        "--repo-filter", help="filter on repo name"
    )
    subparser_repos_list_remote.set_defaults(func=handle_repos_remote)

    subparser_repos_list = subparser_repos.add_parser(
        "list", help="list repo settings"
    )
    subparser_repos_list.set_defaults(func=handle_repos_list)
    subparser_repos_list_sub = subparser_repos_list.add_subparsers()

    subparser_repos_list_branch_enforce_admin = subparser_repos_list_sub.add_parser(
        "branch-enforce-admin",
        help="list enforce_admin setting for branch protection",
    )
    subparser_repos_list_branch_enforce_admin.add_argument(
        "--branch",
        help="branch to check (default: main)",
        default="main",
    )
    subparser_repos_list_branch_enforce_admin.add_argument(
        "--repo", help="repo name"
    )
    subparser_repos_list_branch_enforce_admin.add_argument(
        "--repo-filter", help="filter on repo name"
    )
    subparser_repos_list_branch_enforce_admin.add_argument(
        "--pr-list",
        help="path to a file containing PR URLs to act on",
        type=path_exists,
    )
    subparser_repos_list_branch_enforce_admin.set_defaults(
        func=handle_repos_list_branch_enforce_admin
    )

    subparser_repos_toggle = subparser_repos.add_parser(
        "toggle", help="toggle repo settings"
    ).add_subparsers()

    subparser_repos_toggle_branch_enforce_admin = subparser_repos_toggle.add_parser(
        "branch-enforce-admin",
        help="toggle ability to bypass branch protection rules for admin",
    )
    subparser_repos_toggle_branch_enforce_admin.add_argument(
        "--branch",
        help="branch to toggle (default: main)",
        default="main",
    )
    subparser_repos_toggle_branch_enforce_admin.add_argument(
        "--repo", help="repo name"
    )
    subparser_repos_toggle_branch_enforce_admin.add_argument(
        "--repo-filter", help="filter on repo name"
    )
    subparser_repos_toggle_branch_enforce_admin.add_argument(
        "--pr-list",
        help="path to a file containing PR URLs to act on",
        type=path_exists,
    )
    subparser_repos_toggle_branch_enforce_admin.set_defaults(
        func=handle_repos_toggle_branch_enforce_admin
    )

    subparser_repos_settings = subparser_repos.add_parser(
        "settings", help="manage repository settings"
    ).add_subparsers()

    subparser_repos_settings_pull = subparser_repos_settings.add_parser(
        "pull", help="pull repository settings to JSON"
    )
    subparser_repos_settings_pull.add_argument(
        "--branch",
        help="branch to fetch branch protection for (default: main)",
        default="main",
    )
    subparser_repos_settings_pull.add_argument(
        "--repo", help="repo name"
    )
    subparser_repos_settings_pull.add_argument(
        "--repo-filter", help="filter on repo name"
    )
    subparser_repos_settings_pull.add_argument(
        "-o", "--output", help="output directory (default: stdout). Writes one file per repo."
    )
    subparser_repos_settings_pull.add_argument(
        "--resume", help="skip repos that already have output files", action=argparse.BooleanOptionalAction
    )
    subparser_repos_settings_pull.set_defaults(func=handle_repos_settings_pull)

    subparser_repos_settings_push = subparser_repos_settings.add_parser(
        "push", help="push repository settings from JSON file"
    )
    subparser_repos_settings_push.add_argument(
        "input", help="input JSON file with settings"
    )
    subparser_repos_settings_push.add_argument(
        "--branch",
        help="branch to apply branch protection to (default: main)",
        default="main",
    )
    subparser_repos_settings_push.add_argument(
        "--repo", help="repo name (required if not in JSON)"
    )
    subparser_repos_settings_push.add_argument(
        "--repo-filter", help="filter on repo name"
    )
    subparser_repos_settings_push.add_argument(
        "--dry-run", help="show what would be changed without applying", action=argparse.BooleanOptionalAction
    )
    subparser_repos_settings_push.add_argument(
        "--complete",
        help="also apply per-repo settings (topics, collaborators, teams)",
        action=argparse.BooleanOptionalAction,
    )
    subparser_repos_settings_push.set_defaults(func=handle_repos_settings_push)

    subparser_repos_secrets = subparser_repos.add_parser(
        "secrets", help="manage repository secrets"
    ).add_subparsers()

    subparser_repos_secrets_create = subparser_repos_secrets.add_parser(
        "create", help="create a repository secret"
    )
    subparser_repos_secrets_create.add_argument("name", help="secret name")
    subparser_repos_secrets_create.add_argument(
        "--repo", help="repo name (owner/repo)", required=True
    )
    subparser_repos_secrets_create.add_argument(
        "--type",
        help="secret type (default: actions)",
        choices=["actions", "agents", "codespaces", "dependabot"],
        default="actions",
    )
    subparser_repos_secrets_create.set_defaults(func=handle_repos_secrets_create)

    subparser_repos_secrets_update = subparser_repos_secrets.add_parser(
        "update", help="update a repository secret"
    )
    subparser_repos_secrets_update.add_argument("name", help="secret name")
    subparser_repos_secrets_update.add_argument(
        "--repo", help="repo name (owner/repo)", required=True
    )
    subparser_repos_secrets_update.add_argument(
        "--type",
        help="secret type (default: actions)",
        choices=["actions", "agents", "codespaces", "dependabot"],
        default="actions",
    )
    subparser_repos_secrets_update.set_defaults(func=handle_repos_secrets_update)

    subparser_repos_secrets_list = subparser_repos_secrets.add_parser(
        "list", help="list repository secrets"
    )
    subparser_repos_secrets_list.add_argument(
        "--repo", help="repo name (owner/repo)", required=True
    )
    subparser_repos_secrets_list.add_argument(
        "--type",
        help="secret type (default: all)",
        choices=["all", "actions", "agents", "codespaces", "dependabot"],
        default="all",
    )
    subparser_repos_secrets_list.set_defaults(func=handle_repos_secrets_list)

    subparser_cache = subparsers.add_parser("cache", help="manage cache")

    subparser_clear = subparser_cache.add_subparsers()
    subparser_clear.add_parser("clear", help="clear cache").set_defaults(
        func=clear_cache
    )

    subparser_pr = subparsers.add_parser("pr", help="manage PRs").add_subparsers()

    subparser_release = subparsers.add_parser(
        "release", help="manage releases"
    ).add_subparsers()

    subparser_action = subparsers.add_parser(
        "action", help="manage actions"
    ).add_subparsers()

    list_parser = subparser_pr.add_parser("list", help="list open PRs")
    list_parser.add_argument("--filter", help="keyword or Github filter")
    list_parser.add_argument(
        "--merge-state",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=["blocked", "!blocked", "clean", "!clean", "draft", "!draft"],
    )
    list_parser.add_argument(
        "--review-decision",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=[
            "commented",
            "!commented",
            "changes_requested",
            "!changes_requested",
            "approved",
            "!approved",
        ],
    )
    list_parser.add_argument("--repo", help="repo name")
    list_parser.add_argument("--repo-filter", help="filter on repo name")
    list_parser.add_argument("--author", help="filter on author")
    list_parser.add_argument(
        "--pr-list",
        help="path to a file containing PR URLs to act on",
        type=path_exists,
    )
    list_parser.set_defaults(func=handle_pr_list)

    approve_parser = subparser_pr.add_parser("approve", help="approve matching PRs")
    approve_parser.add_argument("--filter", help="keyword or Github filter")
    approve_parser.add_argument(
        "--merge-state",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=["blocked", "!blocked", "clean", "!clean", "draft", "!draft"],
    )
    approve_parser.add_argument(
        "--review-decision",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=[
            "commented",
            "!commented",
            "changes_requested",
            "!changes_requested",
            "approved",
            "!approved",
        ],
    )
    approve_parser.add_argument("--repo", help="repo name")
    approve_parser.add_argument("--repo-filter", help="filter on repo name")
    approve_parser.add_argument("--author", help="filter on author")
    approve_parser.add_argument(
        "--pr-list",
        help="path to a file containing PR URLs to act on",
        type=path_exists,
    )
    approve_parser.set_defaults(func=handle_pr_approve)

    merge_parser = subparser_pr.add_parser("merge", help="merge matching PRs")
    merge_parser.add_argument("--filter", help="keyword or Github filter")
    merge_parser.add_argument(
        "--merge-state",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=["blocked", "!blocked", "clean", "!clean", "draft", "!draft"],
    )
    merge_parser.add_argument(
        "--review-decision",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=[
            "commented",
            "!commented",
            "changes_requested",
            "!changes_requested",
            "approved",
            "!approved",
        ],
    )
    merge_parser.add_argument("--repo", help="repo name")
    merge_parser.add_argument("--repo-filter", help="filter on repo name")
    merge_parser.add_argument(
        "--admin",
        help="use admin privileges to merge",
        action=argparse.BooleanOptionalAction,
    )
    merge_parser.add_argument(
        "--skip-failing",
        help="skip past any merges that fail",
        action=argparse.BooleanOptionalAction,
    )
    merge_parser.add_argument(
        "--with-approve",
        help="approve PR before merging",
        action=argparse.BooleanOptionalAction,
    )
    merge_parser.add_argument(
        "--merge-type",
        help="Defaults to merge, others options are squash or rebase",
        choices=["merge", "squash", "rebase"],
        default="merge",
    )
    merge_parser.add_argument("--author", help="filter on author")
    merge_parser.add_argument(
        "--pr-list",
        help="path to a file containing PR URLs to act on",
        type=path_exists,
    )
    merge_parser.set_defaults(func=handle_pr_merge)

    update_br_parser = subparser_pr.add_parser(
        "update-branch", help="update the PR's branch"
    )
    update_br_parser.add_argument("--filter", help="keyword or Github filter")
    update_br_parser.add_argument(
        "--merge-state",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=["blocked", "!blocked", "clean", "!clean", "draft", "!draft"],
    )
    update_br_parser.add_argument("--repo", help="repo name")
    update_br_parser.add_argument("--repo-filter", help="filter on repo name")
    update_br_parser.add_argument("--author", help="filter on author")
    update_br_parser.add_argument(
        "--force",
        help="force update despite merge status",
        action=argparse.BooleanOptionalAction,
    )
    update_br_parser.add_argument(
        "--pr-list",
        help="path to a file containing PR URLs to act on",
        type=path_exists,
    )
    update_br_parser.set_defaults(func=handle_pr_branch_update)

    apply_label_parser = subparser_pr.add_parser(
        "apply-label", help="apply a label to the PR"
    )
    apply_label_parser.add_argument("--filter", help="keyword or Github filter")
    apply_label_parser.add_argument(
        "--merge-state",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=["blocked", "!blocked", "clean", "!clean", "draft", "!draft"],
    )
    apply_label_parser.add_argument("--repo", help="repo name")
    apply_label_parser.add_argument("--repo-filter", help="filter on repo name")
    apply_label_parser.add_argument("--author", help="filter on author")
    apply_label_parser.add_argument(
        "--label",
        help="label to apply",
        required=True,
    )
    apply_label_parser.add_argument(
        "--pr-list",
        help="path to a file containing PR URLs to act on",
        type=path_exists,
    )
    apply_label_parser.set_defaults(func=handle_pr_apply_label)

    remove_label_parser = subparser_pr.add_parser(
        "remove-label", help="remove a label from the PR"
    )
    remove_label_parser.add_argument("--filter", help="keyword or Github filter")
    remove_label_parser.add_argument(
        "--merge-state",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=["blocked", "!blocked", "clean", "!clean", "draft", "!draft"],
    )
    remove_label_parser.add_argument("--repo", help="repo name")
    remove_label_parser.add_argument("--repo-filter", help="filter on repo name")
    remove_label_parser.add_argument("--author", help="filter on author")
    remove_label_parser.add_argument(
        "--label",
        help="label to remove",
        required=True,
    )
    remove_label_parser.add_argument(
        "--pr-list",
        help="path to a file containing PR URLs to act on",
        type=path_exists,
    )
    remove_label_parser.set_defaults(func=handle_pr_remove_label)

    open_parser = subparser_pr.add_parser("open", help="open the PR in a browser")
    open_parser.add_argument("repo", help="repo where issue exists", type=str)
    open_parser.add_argument("number", help="PR number", type=int)
    open_parser.set_defaults(func=handle_open)

    create_parser = subparser_pr.add_parser(
        "create", help="create a PR across all of the repos"
    )
    create_parser.add_argument("--repo", help="repo name")
    create_parser.add_argument("--repo-filter", help="filter on repo name")
    create_parser.add_argument("--title", help="PR title")
    create_parser.add_argument("--body", help="PR body")
    create_parser.add_argument(
        "--workdir", help="location to do temporary work", default=".ghm-work"
    )
    create_parser.add_argument(
        "--script", help="script to run against each repo", type=path_exists
    )
    create_parser.add_argument(
        "--label",
        help="labels to apply (space separated list)",
        nargs="*",
        type=label_valid,
    )
    create_parser.add_argument(
        "--batch-size", help="Size of batch to process before pausing", type=int
    )
    create_parser.add_argument(
        "--batch-pause",
        help="Amount of time in seconds to pause between batches",
        type=float,
    )
    create_parser.set_defaults(func=handle_pr_create)

    run_parser = subparser_action.add_parser("run", help="Run actions for a repo")
    run_parser.add_argument("repo", help="filter by repo name")
    run_parser.add_argument("--filter", help="regex filter for workflow name")
    run_parser.set_defaults(func=handle_action_run)

    run_matching_parser = subparser_action.add_parser(
        "run-matching", help="Run actions matching filter"
    )
    run_matching_parser.add_argument("--filter", help="regex filter for workflow name")
    run_matching_parser.add_argument("--repo", help="repo name")
    run_matching_parser.add_argument("--repo-filter", help="filter on repo name")
    run_matching_parser.add_argument(
        "--batch-size", help="Size of batch to process before pausing", type=int
    )
    run_matching_parser.add_argument(
        "--batch-pause",
        help="Amount of time in seconds to pause between batches",
        type=float,
    )
    run_matching_parser.set_defaults(func=handle_action_run_matching)

    rerun_parser = subparser_action.add_parser(
        "rerun", help="Rerun failed actions for a PR"
    )
    rerun_parser.add_argument("repo", help="repo where action exists", type=str)
    rerun_parser.add_argument("number", help="PR number where action failed", type=int)
    rerun_parser.set_defaults(func=handle_action_rerun)

    rerun_matching_parser = subparser_action.add_parser(
        "rerun-matching", help="Rerun failed actions matching filter"
    )
    rerun_matching_parser.add_argument("--filter", help="keyword or Github filter")
    rerun_matching_parser.add_argument("--author", help="filter on author")
    rerun_matching_parser.add_argument(
        "--merge-state",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=["blocked", "!blocked", "clean", "!clean", "draft", "!draft"],
    )
    rerun_matching_parser.add_argument(
        "--failed", help="only failed", action=argparse.BooleanOptionalAction
    )
    rerun_matching_parser.add_argument(
        "--pr-list",
        help="path to a file containing PR URLs to act on",
        type=path_exists,
    )
    rerun_matching_parser.set_defaults(func=handle_action_rerun_matching)

    enable_matching_parser = subparser_action.add_parser(
        "enable-matching", help="Enable actions matching filter"
    )
    enable_matching_parser.add_argument(
        "--filter", help="regex filter for workflow name"
    )
    enable_matching_parser.add_argument("--repo", help="repo name")
    enable_matching_parser.add_argument("--repo-filter", help="filter on repo name")
    enable_matching_parser.set_defaults(func=handle_action_enable_matching)

    disable_matching_parser = subparser_action.add_parser(
        "disable-matching", help="Disable actions matching filter"
    )
    disable_matching_parser.add_argument(
        "--filter", help="regex filter for workflow name"
    )
    disable_matching_parser.add_argument("--repo", help="repo name")
    disable_matching_parser.add_argument("--repo-filter", help="filter on repo name")
    disable_matching_parser.set_defaults(func=handle_action_disable_matching)

    run_list_active_parser = subparser_action.add_parser(
        "run-list-active", help="List active workflow runs"
    )
    run_list_active_parser.add_argument(
        "--filter", help="regex filter for workflow name"
    )
    run_list_active_parser.add_argument("--repo", help="repo name")
    run_list_active_parser.add_argument("--repo-filter", help="filter on repo name")
    run_list_active_parser.add_argument(
        "--all-repos",
        help="all repos in the org",
        action=argparse.BooleanOptionalAction,
    )
    run_list_active_parser.add_argument(
        "--status",
        help="Status to check, for completed use `run-list-complet`",
        choices=["queued", "in_progress", "failed"],
        default="queued",
    )
    run_list_active_parser.set_defaults(func=handle_action_run_active_list)

    run_list_complete_parser = subparser_action.add_parser(
        "run-list-complete", help="List complete workflow runs"
    )
    run_list_complete_parser.add_argument(
        "--filter", help="regex filter for workflow name"
    )
    run_list_complete_parser.add_argument("--repo", help="repo name")
    run_list_complete_parser.add_argument("--repo-filter", help="filter on repo name")
    run_list_complete_parser.add_argument(
        "--limit", help="result set limit", type=int, default="500"
    )
    run_list_complete_parser.add_argument(
        "--all-repos",
        help="all repos in the org",
        action=argparse.BooleanOptionalAction,
    )
    run_list_complete_parser.set_defaults(func=handle_action_run_complete_list)

    list_parser = subparser_release.add_parser(
        "list", help="list releases and their notes"
    )
    list_parser.add_argument(
        "--composite",
        help="Target composite buildpack (release all dependency buildpacks)",
    )
    list_parser.add_argument(
        "--summary",
        nargs="?",
        const=True,
        default=False,
        help="Show latest release for repo (summary only)",
    )
    list_parser.add_argument("--repo", help="a specific repo to release")
    list_parser.add_argument("--filter", help="regex to refine repos")

    list_parser.set_defaults(func=handle_release_list)

    publish_parser = subparser_release.add_parser("publish", help="publish a release")
    publish_parser.add_argument(
        "--composite",
        help="Target composite buildpack (release all dependency buildpacks)",
    )
    publish_parser.add_argument("--repo", help="a specific repo to release")
    publish_parser.add_argument("--filter", help="regex to refine repos")
    publish_parser.add_argument(
        "--publish",
        help="defaults to a dry-run, this flag actually publishes",
        action=argparse.BooleanOptionalAction,
    )
    publish_parser.set_defaults(func=handle_release_publish)

    return parser

import argparse
import hashlib
import re
import subprocess
import timeago
import datetime
import time
import os
from prettytable import PrettyTable
from .runner import GhRunner, GitRunner
from .utils import load_repos, REPO_CONFIG_LOCATION, fetch_buildpack_toml
from .cache import Cache

NOT_RUNNABLE = "could not create workflow dispatch event: HTTP 422:" \
    " Workflow does not have 'workflow_dispatch' trigger"
NOT_FOUND_WORKFLOW = 'could not find any workflows named'


def check_run_ok(s):
    return s['__typename'] == 'CheckRun' and s['status'] == "COMPLETED" and \
        s['conclusion'] == "SUCCESS"


def check_status_context(s):
    return s['__typename'] == 'StatusContext' and s['state'] == "SUCCESS"


def check_status_ok(s):
    return check_run_ok(s) or check_status_context(s)


def pr_actions_ok(pr):
    status = pr['statusCheckRollup'] and pr['statusCheckRollup'] or []
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


def handle_repos(args):
    print(f"Repos configured in [{REPO_CONFIG_LOCATION}]")
    print("\t" + "\n\t".join(load_repos()))


def handle_pr_list(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(), args.repo, args.repo_filter)
    cols = "{:<45} {:^6} {:^10} {:^15} {:^10} {:^18} {:^10} {:^20} {}"
    print(cols.format(
        "REPO",
        "NUMBER",
        "STATE",
        "MERGE?",
        "MERGST",
        "REVIEW",
        "CHECK?",
        "AUTHOR",
        "TITLE"))
    for repo in repos:
        prs = runner.pr_list(
            repo, args.filter, args.merge_state, args.review_decision)
        for pr in prs:
            print(cols.format(
                repo,
                str(pr['number']),
                pr['state'],
                pr['mergeable'],
                pr['mergeStateStatus'],
                pr['reviewDecision'],
                pr_actions_ok(pr),
                pr.get('author', {}).get('login', 'n/a'),
                pr['title'][:75]
            ))


def handle_pr_approve(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(), args.repo, args.repo_filter)
    for repo in repos:
        prs = runner.pr_list(repo, args.filter, args.merge_state)
        for pr in prs:
            print(f"    Approving {repo} -> {pr['number']} [{pr['title']}]")
            stdout, stderr = runner.pr_approve(repo, pr['number'])
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
    for repo in repos:
        print(f"  {repo}")
        repo_path = os.path.join(args.workdir, repo)

        if os.path.exists(repo_path):
            # repo exists, clean & update it
            gr.cwd(repo_path)
            gr.clean()
            gr.checkout_branch('main')
            gr.reset_hard("origin/main")
            gr.pull()
            if not _is_branch_clean(gr):
                raise RuntimeError(
                    f"branch at {repo_path} has unclean working tree")
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
                print(f"    Skipping {repo} which was not"
                      f" modified by {args.script}")
                continue  # nothing to commit

            # add & commit any changes
            gr.add(".")
            gr.commit(args.title, args.body)
        else:
            if _is_branch_clean(gr):
                print(f"    Skipping {repo} which was not"
                      f" modified by {args.script}")
                continue  # nothing to commit

        gr.push(branch)

        # create a pull request
        ghr.pr_create(repo_path, args.label)


def _is_branch_clean(gr):
    stdout = gr.status()[0].decode('utf-8').strip()
    return stdout.endswith('nothing to commit, working tree clean')


def _branch_name(script):
    h = hashlib.sha256(open(script).read().encode('utf-8'))
    return f"ghm-pr-{h.hexdigest()[0:8]}"


def _run_script(cwd, script):
    try:
        subprocess.run(
            os.path.realpath(script),
            capture_output=True,
            check=True,
            cwd=cwd)
    except subprocess.CalledProcessError as ex:
        print(f"Error running script: {ex.cmd}")
        print(f"Exit Code: {ex.returncode}")
        print()
        print("STDOUT:")
        print(ex.stdout.decode('utf-8'))
        print()
        print("STDERR:")
        print(ex.stderr.decode('utf-8'))
        raise RuntimeError('script execution failed, see errors above')


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
                    print(stdout)
                if stderr:
                    print(stderr)
            except Exception as ex:
                errMsg = ex.stderr.decode('UTF-8').strip()
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
        num_run += _run_workflow(runner, repo, args.filter,
                                 args.batch_size, args.batch_pause)
        if args.batch_size is not None and num_run % args.batch_size == 0:
            print("    *** Batch Submitted - Pausing ***")
            time.sleep(args.batch_pause)


def _rerun_failed(runner, pr, repo):
    failed = [s for s in pr['statusCheckRollup']
              if s['conclusion'] == 'FAILURE']
    for fail in failed:
        print(f"    Rerunning {repo} -> {fail['name']} ({fail['detailsUrl']}")
        run_num = fail['detailsUrl'].split('/')[-1]
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
    repos = load_repos()
    for repo in repos:
        prs = runner.pr_list(repo, args.filter, args.merge_state)
        if args.failed:
            prs = [pr for pr in prs if any(
                map(lambda pr: not check_status_ok(pr),
                    pr['statusCheckRollup']))]
        for pr in prs:
            _rerun_failed(runner, pr, repo)


def handle_action_enable_matching(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(), args.repo, args.repo_filter)
    pattern = re.compile(args.filter is None and '.*' or args.filter)

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
                    errMsg = ex.stderr.decode('UTF-8').strip()
                    if not errMsg.startswith(NOT_FOUND_WORKFLOW):
                        raise ex
                    print("        Skipped. Already enabled?")


def handle_action_disable_matching(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(), args.repo, args.repo_filter)
    pattern = re.compile(args.filter is None and '.*' or args.filter)

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
                    errMsg = ex.stderr.decode('UTF-8').strip()
                    if not errMsg.startswith(NOT_FOUND_WORKFLOW):
                        raise ex
                    print("        Skipped. Already disabled?")


def handle_action_run_active_list(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(args.all_repos),
                         args.repo, args.repo_filter)

    pt = PrettyTable()
    pt.field_names = ["REPO", "ID", "STATUS", "EVENT", "CREATED AT",
                      "RUN STARTED AT", "DURATION", "RUN ATTEMPT", "NAME"]
    pt.align["REPO"] = 'l'
    pt.align["NAME"] = 'l'
    pt.sortby = "CREATED AT"

    for repo in repos:
        data = runner.run_list_active(
            repo, args.status).get('workflow_runs', {})
        for wf_run in data:
            created_at = datetime.datetime.strptime(
                wf_run.get('created_at', '0000-00-00T00:00:00Z'),
                '%Y-%m-%dT%H:%M:%SZ')
            pt.add_row([
                wf_run.get('repository', {}).get('full_name', '<not found>'),
                wf_run.get('id', '<not found>'),
                wf_run.get('status', '<not found>'),
                wf_run.get('event', '<not found>'),
                wf_run.get('created_at', '<not found>'),
                wf_run.get('run_started_at', '<not found>'),
                timeago.format(created_at, datetime.datetime.utcnow()),
                wf_run.get('run_attempt', '<not found>'),
                wf_run.get('name', '<not found>')])

    print(pt)


def handle_action_run_complete_list(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(args.all_repos),
                         args.repo, args.repo_filter)
    data = []
    for repo in repos:
        repo_data = runner.run_list_complete(repo, args.limit)
        data.extend(repo_data)

    print(",".join([
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
        "name"]))
    for row in data:
        created_at = datetime.datetime.strptime(
            row.get('created_at', '0000-00-00T00:00:00Z'),
            '%Y-%m-%dT%H:%M:%SZ')
        run_started_at = datetime.datetime.strptime(
            row.get('run_started_at', '0000-00-00T00:00:00Z'),
            '%Y-%m-%dT%H:%M:%SZ')
        updated_at = datetime.datetime.strptime(
            row.get('updated_at', '0000-00-00T00:00:00Z'),
            '%Y-%m-%dT%H:%M:%SZ')
        print(",".join([
            row.get('repository', {}).get('full_name', '<not found>'),
            row.get('status', '<not found>'),
            row.get('event', '<not found>'),
            row.get('created_at', '0000-00-00T00:00:00Z'),
            row.get('run_started_at', '0000-00-00T00:00:00Z'),
            row.get('updated_at', '0000-00-00T00:00:00Z'),
            str((run_started_at - created_at).seconds),
            str((updated_at - run_started_at).seconds),
            str((updated_at - created_at).seconds),
            str(row.get('run_attempt', '<not found>')),
            row.get('name', '<not found>')]))


def handle_pr_merge(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(), args.repo, args.repo_filter)
    break_merging = False
    for repo in repos:
        if break_merging:
            break
        prs = runner.pr_list(repo, args.filter, args.merge_state)
        for pr in prs:
            if args.with_approve:
                print(
                    f"    Approving & Merging {repo} -> "
                    f"{pr['number']} [{pr['title']}]")
                runner.pr_approve(repo, pr['number'])
            else:
                print(f"    Merging {repo} -> {pr['number']} [{pr['title']}]")
            try:
                stdout, stderr = runner.pr_merge(
                    repo, pr['number'], args.admin)
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
                            "Do you wish to continue merging?", True):
                        continue
                    else:
                        break_merging = True
                        break


def handle_pr_branch_update(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(), args.repo, args.repo_filter)
    for repo in repos:
        prs = runner.pr_list(repo, args.filter, args.merge_state)
        for pr in prs:
            if pr['mergeStateStatus'] == 'BEHIND' or args.force:
                print(f"    Updating branch {repo} -> "
                      f"{pr['number']} [{pr['title']}]")
                resp = runner.pr_update_branch(repo, pr['number'])
                if 'message' not in resp.keys() or \
                        resp['message'] != 'Updating pull request branch.':
                    print("Unexpected response:")
                    print(f"    {resp}")


def handle_release_list(args):
    runner = GhRunner()

    repos = []
    if args.composite:
        bp_toml = fetch_buildpack_toml(args.composite)
        for order in bp_toml['order']:
            for group in order['group']:
                repos.append(group['id'])
    else:
        repos = load_repos()

    repos = filter_repos(repos, args.repo, filter=args.filter)

    if args.summary:
        pt = PrettyTable()
        pt.field_names = ["REPO", "LATEST VERSION", "DRAFT AVAILABLE",
                          "LAST RELEASE DATE", "SINCE LAST RELEASE"]
        pt.align["REPO"] = 'l'
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
                if r[0][1] == 'Draft':
                    drafts.append([repo, "Draft", "YES", "N/A", "N/A"])
                    continue
                else:
                    draft = "NO"
                    r = r[0]
            # If the top release is a Draft, note this for the Available?
            # column and use the second release for the row
            else:
                if r[0][1] == 'Draft':
                    draft = "YES"
                    r = r[1]
                else:
                    draft = "NO"
                    r = r[0]
            # removed the timestamp since we don't need this granularity
            # and also there are issues with TZ
            rDate = datetime.datetime.strptime(
                r[3], "%Y-%m-%dT%H:%M:%S%z").date()
            # Used timeago library to format readable duration since last
            # release
            table.append([repo, r[0].strip().split()[-1:][0], draft,
                         rDate, timeago.format(rDate,
                         datetime.datetime.now())])
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
            print(r['body'])
            print()
            print(124 * '-')
            print()


def handle_release_publish(args):
    runner = GhRunner()

    repos = []
    if args.composite:
        bp_toml = fetch_buildpack_toml(args.composite)
        for order in bp_toml['order']:
            for group in order['group']:
                repos.append(group['id'])
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
        name = " ".join(r['name'].strip().split()[:-1])
        version = r['name'].strip().split()[-1:][0]
        print(f"    Publishing release for {repo} -> [{name}/{version} ]")
        if args.publish:
            runner.release_publish(repo, r['id'], version)


def clear_cache(args):
    Cache().clear()


def single_yes_or_no_question(question, default_no=True):
    choices = ' [y/N]: ' if default_no else ' [Y/n]: '
    default_answer = 'n' if default_no else 'y'
    reply = str(input(question + choices)).lower().strip() or default_answer
    if reply[0] == 'y':
        return True
    if reply[0] == 'n':
        return False
    else:
        return False if default_no else True


def path_exists(p):
    if os.path.isfile(p):
        return p
    else:
        raise argparse.ArgumentTypeError(f"{p} must exist")


def label_valid(label):
    if label is not None and \
            (label.startswith('semver:') or label.startswith('type:')):
        return label
    else:
        raise argparse.ArgumentTypeError(
            f"{label} must start with 'semver:' or 'type:'")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Manage many Github repos in an efficient way")

    subparsers = parser.add_subparsers()
    subparsers.add_parser(
        "repos", help="list configured repos").set_defaults(func=handle_repos)

    subparser_cache = subparsers.add_parser(
        "cache", help="manage cache")

    subparser_clear = subparser_cache.add_subparsers()
    subparser_clear.add_parser(
        "clear", help="clear cache").set_defaults(func=clear_cache)

    subparser_pr = subparsers.add_parser(
        "pr", help="manage PRs").add_subparsers()

    subparser_release = subparsers.add_parser(
        "release", help="manage releases").add_subparsers()

    subparser_action = subparsers.add_parser(
        "action", help="manage actions").add_subparsers()

    list_parser = subparser_pr.add_parser("list", help="list open PRs")
    list_parser.add_argument("--filter", help="keyword or Github filter")
    list_parser.add_argument(
        "--merge-state",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=['blocked', '!blocked', 'clean', '!clean', 'draft', '!draft'])
    list_parser.add_argument(
        "--review-decision",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=['commented', '!commented', 'changes_requested',
                 '!changes_requested', 'approved', '!approved'])
    list_parser.add_argument('--repo', help="repo name")
    list_parser.add_argument('--repo-filter', help="filter on repo name")
    list_parser.set_defaults(func=handle_pr_list)

    approve_parser = subparser_pr.add_parser(
        "approve", help="approve matching PRs")
    approve_parser.add_argument("--filter", help="keyword or Github filter")
    approve_parser.add_argument(
        "--merge-state",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=['blocked', '!blocked', 'clean', '!clean', 'draft', '!draft'])
    approve_parser.add_argument('--repo', help="repo name")
    approve_parser.add_argument('--repo-filter', help="filter on repo name")
    approve_parser.set_defaults(func=handle_pr_approve)

    merge_parser = subparser_pr.add_parser("merge", help="merge matching PRs")
    merge_parser.add_argument("--filter", help="keyword or Github filter")
    merge_parser.add_argument(
        "--merge-state",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=['blocked', '!blocked', 'clean', '!clean', 'draft', '!draft'])
    merge_parser.add_argument("--repo", help="repo name")
    merge_parser.add_argument('--repo-filter', help="filter on repo name")
    merge_parser.add_argument('--admin', help="use admin privileges to merge",
                              action=argparse.BooleanOptionalAction)
    merge_parser.add_argument("--skip-failing",
                              help="skip past any merges that fail",
                              action=argparse.BooleanOptionalAction)
    merge_parser.add_argument("--with-approve",
                              help="approve PR before merging",
                              action=argparse.BooleanOptionalAction)
    merge_parser.set_defaults(func=handle_pr_merge)

    update_br_parser = subparser_pr.add_parser(
        "update-branch", help="update the PR's branch")
    update_br_parser.add_argument("--filter", help="keyword or Github filter")
    update_br_parser.add_argument(
        "--merge-state",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=['blocked', '!blocked', 'clean', '!clean', 'draft', '!draft'])
    update_br_parser.add_argument("--repo", help="repo name")
    update_br_parser.add_argument('--repo-filter', help="filter on repo name")
    update_br_parser.add_argument(
        '--force',
        help="force update despite merge status",
        action=argparse.BooleanOptionalAction)
    update_br_parser.set_defaults(func=handle_pr_branch_update)

    open_parser = subparser_pr.add_parser(
        "open", help="open the PR in a browser")
    open_parser.add_argument(
        "repo", help="repo where issue exists", type=str)
    open_parser.add_argument("number", help="PR number", type=int)
    open_parser.set_defaults(func=handle_open)

    create_parser = subparser_pr.add_parser(
        "create", help="create a PR across all of the repos")
    create_parser.add_argument('--repo', help="repo name")
    create_parser.add_argument('--repo-filter', help="filter on repo name")
    create_parser.add_argument('--title', help="PR title")
    create_parser.add_argument('--body', help="PR body")
    create_parser.add_argument(
        '--workdir', help='location to do temporary work', default='.ghm-work')
    create_parser.add_argument('--script',
                               help="script to run against each repo",
                               type=path_exists)
    create_parser.add_argument('--label',
                               help="labels to apply (space separated list)",
                               nargs='*',
                               type=label_valid)
    create_parser.set_defaults(func=handle_pr_create)

    run_parser = subparser_action.add_parser(
        "run", help="Run actions for a repo")
    run_parser.add_argument(
        "repo", help="filter by repo name")
    run_parser.add_argument(
        "--filter", help="regex filter for workflow name")
    run_parser.set_defaults(func=handle_action_run)

    run_matching_parser = subparser_action.add_parser(
        "run-matching", help="Run actions matching filter")
    run_matching_parser.add_argument(
        "--filter", help="regex filter for workflow name")
    run_matching_parser.add_argument("--repo", help="repo name")
    run_matching_parser.add_argument(
        '--repo-filter', help="filter on repo name")
    run_matching_parser.add_argument(
        '--batch-size', help="Size of batch to process before pausing",
        type=int)
    run_matching_parser.add_argument(
        '--batch-pause',
        help="Amount of time in seconds to pause between batches",
        type=float)
    run_matching_parser.set_defaults(func=handle_action_run_matching)

    rerun_parser = subparser_action.add_parser(
        "rerun", help="Rerun failed actions for a PR")
    rerun_parser.add_argument(
        "repo", help="repo where action exists", type=str)
    rerun_parser.add_argument(
        "number", help="PR number where action failed", type=int)
    rerun_parser.set_defaults(func=handle_action_rerun)

    rerun_matching_parser = subparser_action.add_parser(
        "rerun-matching", help="Rerun failed actions matching filter")
    rerun_matching_parser.add_argument(
        "--filter", help="keyword or Github filter")
    rerun_matching_parser.add_argument(
        "--merge-state",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=['blocked', '!blocked', 'clean', '!clean', 'draft', '!draft'])
    rerun_matching_parser.add_argument(
        "--failed", help="only failed", action=argparse.BooleanOptionalAction)
    rerun_matching_parser.set_defaults(func=handle_action_rerun_matching)

    enable_matching_parser = subparser_action.add_parser(
        "enable-matching", help="Enable actions matching filter")
    enable_matching_parser.add_argument(
        "--filter", help="regex filter for workflow name")
    enable_matching_parser.add_argument("--repo", help="repo name")
    enable_matching_parser.add_argument(
        '--repo-filter', help="filter on repo name")
    enable_matching_parser.set_defaults(func=handle_action_enable_matching)

    disable_matching_parser = subparser_action.add_parser(
        "disable-matching", help="Disable actions matching filter")
    disable_matching_parser.add_argument(
        "--filter", help="regex filter for workflow name")
    disable_matching_parser.add_argument("--repo", help="repo name")
    disable_matching_parser.add_argument(
        '--repo-filter', help="filter on repo name")
    disable_matching_parser.set_defaults(func=handle_action_disable_matching)

    run_list_active_parser = subparser_action.add_parser(
        "run-list-active", help="List active workflow runs")
    run_list_active_parser.add_argument(
        "--filter", help="regex filter for workflow name")
    run_list_active_parser.add_argument("--repo", help="repo name")
    run_list_active_parser.add_argument(
        '--repo-filter', help="filter on repo name")
    run_list_active_parser.add_argument(
        '--all-repos', help="all repos in the org",
        action=argparse.BooleanOptionalAction)
    run_list_active_parser.add_argument(
        "--status",
        help="Status to check, for completed use `run-list-complet`",
        choices=['queued', 'in_progress'],
        default="queued")
    run_list_active_parser.set_defaults(func=handle_action_run_active_list)

    run_list_complete_parser = subparser_action.add_parser(
        "run-list-complete", help="List complete workflow runs")
    run_list_complete_parser.add_argument(
        "--filter", help="regex filter for workflow name")
    run_list_complete_parser.add_argument("--repo", help="repo name")
    run_list_complete_parser.add_argument(
        '--repo-filter', help="filter on repo name")
    run_list_complete_parser.add_argument('--limit', help="result set limit",
                                          type=int, default="500")
    run_list_complete_parser.add_argument(
        '--all-repos', help="all repos in the org",
        action=argparse.BooleanOptionalAction)
    run_list_complete_parser.set_defaults(func=handle_action_run_complete_list)

    list_parser = subparser_release.add_parser(
        "list", help="list releases and their notes")
    list_parser.add_argument(
        "--composite",
        help="Target composite buildpack (release all dependency buildpacks)")
    list_parser.add_argument(
        "--summary", nargs='?', const=True, default=False,
        help="Show latest release for repo (summary only)")
    list_parser.add_argument("--repo", help="a specific repo to release")
    list_parser.add_argument('--filter', help="regex to refine repos")

    list_parser.set_defaults(func=handle_release_list)

    publish_parser = subparser_release.add_parser(
        "publish", help="publish a release")
    publish_parser.add_argument(
        "--composite",
        help="Target composite buildpack (release all dependency buildpacks)")
    publish_parser.add_argument("--repo", help="a specific repo to release")
    publish_parser.add_argument('--filter', help="regex to refine repos")
    publish_parser.add_argument(
        '--publish',
        help="defaults to a dry-run, this flag actually publishes",
        action=argparse.BooleanOptionalAction)
    publish_parser.set_defaults(func=handle_release_publish)

    return parser

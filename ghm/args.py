import argparse
import re
from .runner import GhRunner
from .utils import load_repos, REPO_CONFIG_LOCATION, fetch_buildpack_toml
from .cache import Cache

NOT_RUNNABLE = "could not create workflow dispatch event: HTTP 422:" \
    " Workflow does not have 'workflow_dispatch' trigger"


def check_run_ok(s):
    return s['__typename'] == 'CheckRun' and s['status'] == "COMPLETED" and \
        s['conclusion'] == "SUCCESS"


def check_status_context(s):
    return s['__typename'] == 'StatusContext' and s['status'] == "" and \
        s['conclusion'] == "" and s['state'] == "SUCCESS"


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
        prs = runner.pr_list(repo, args.filter, args.merge_state)
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


def _run_workflow(runner, repo, filter):
    pattern = re.compile(filter)

    workflows = runner.workflow_list(repo)
    for workflow in workflows:
        m = pattern.match(workflow)
        if filter is None or m:
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


def handle_action_run(args):
    _run_workflow(GhRunner(), args.repo, args.filter)


def handle_action_run_matching(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(), args.repo, args.repo_filter)
    for repo in repos:
        _run_workflow(runner, repo, args.filter)


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


def handle_pr_merge(args):
    runner = GhRunner()
    repos = filter_repos(load_repos(), args.repo, args.repo_filter)
    for repo in repos:
        prs = runner.pr_list(repo, args.filter, args.merge_state)
        for pr in prs:
            print(f"    Merging {repo} -> {pr['number']} [{pr['title']}]")
            stdout, stderr = runner.pr_merge(repo, pr['number'], args.admin)
            if stdout:
                print(stdout)
            if stderr:
                print(stderr)


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
        print("--------------------------------------------------------------"
              "--------------------------------------------------------------")
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

    list_parser = subparser_pr.add_parser("list", help="list open PRs")
    list_parser.add_argument("--filter", help="keyword or Github filter")
    list_parser.add_argument(
        "--merge-state",
        help="blocked, clean or draft. Prefix with `!` to negate.",
        choices=['blocked', '!blocked', 'clean', '!clean', 'draft', '!draft'])
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

    subparser_action = subparsers.add_parser(
        "action", help="manage actions").add_subparsers()

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

    list_parser = subparser_release.add_parser(
        "list", help="list releases and their notes")
    list_parser.add_argument(
        "--composite",
        help="Target composite buildpack (release all dependency buildpacks)")
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

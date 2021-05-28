import argparse
from .runner import GhRunner
from .utils import load_repos, REPO_CONFIG_LOCATION
from .cache import Cache


def status_check_ok(s):
    return s['status'] == "COMPLETED" and s['conclusion'] == "SUCCESS"


def handle_repos(args):
    print(f"Repos configured in [{REPO_CONFIG_LOCATION}]")
    print("\t" + "\n\t".join(load_repos()))


def handle_pr_list(args):
    runner = GhRunner()
    repos = load_repos()
    print("NUMBER\t\t\t\tSTATE\tMERGEABLE\tMERGE STATE\tREVIEW DEC"
          "\tCHECK STATUS\tAUTHOR\tTITLE")
    for repo in repos:
        prs = runner.pr_list(repo, args.filter, args.merge_state)
        for pr in prs:
            print("\t".join([
                repo,
                str(pr['number']),
                pr['state'],
                pr['mergeable'],
                pr['mergeStateStatus'],
                pr['reviewDecision'],
                str(all(map(status_check_ok, pr['statusCheckRollup']))),
                pr.get('author', {}).get('login', 'n/a'),
                pr['title']
            ]))


def handle_pr_approve(args):
    runner = GhRunner()
    repos = load_repos()
    for repo in repos:
        prs = runner.pr_list(repo, args.filter)
        for pr in prs:
            print(f"    Approving {repo} -> {pr['number']} [{pr['title']}]")
            stdout, stderr = runner.pr_approve(repo, pr['number'])
            if stdout:
                print(stdout)
            if stderr:
                print(stderr)


def handle_open(args):
    GhRunner().pr_open(args.repo, args.number)


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
        for pr in prs:
            _rerun_failed(runner, pr, repo)


def handle_pr_merge(args):
    runner = GhRunner()
    repos = load_repos()
    for repo in repos:
        prs = runner.pr_list(repo, args.filter, args.merge_state)
        for pr in prs:
            print(f"    Merging {repo} -> {pr['number']} [{pr['title']}]")
            stdout, stderr = runner.pr_merge(repo, pr['number'])
            if stdout:
                print(stdout)
            if stderr:
                print(stderr)


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

    parser_list = subparser_pr.add_parser("list", help="list open PRs")
    parser_list.add_argument("--filter", help="keyword or Github filter")
    parser_list.add_argument(
        "--merge-state", help="blocked or clean", choices=['blocked', 'clean'])
    parser_list.set_defaults(func=handle_pr_list)

    approve_parser = subparser_pr.add_parser(
        "approve", help="approve matching PRs")
    approve_parser.add_argument("--filter", help="keyword or Github filter")
    approve_parser.set_defaults(func=handle_pr_approve)

    merge_parser = subparser_pr.add_parser("merge", help="merge matching PRs")
    merge_parser.add_argument("--filter", help="keyword or Github filter")
    merge_parser.add_argument(
        "--merge-state", help="blocked or clean", choices=['blocked', 'clean'])
    merge_parser.set_defaults(func=handle_pr_merge)

    open_parser = subparser_pr.add_parser(
        "open", help="open the PR in a browser")
    open_parser.add_argument(
        "repo", help="repo where issue exists", type=str)
    open_parser.add_argument("number", help="PR number", type=int)
    open_parser.set_defaults(func=handle_open)

    subparser_action = subparsers.add_parser(
        "action", help="manage actions").add_subparsers()

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
        "--merge-state", help="blocked or clean", choices=['blocked', 'clean'])
    rerun_matching_parser.set_defaults(func=handle_action_rerun_matching)

    return parser

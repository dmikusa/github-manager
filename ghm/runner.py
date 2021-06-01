import subprocess
import json
from .cache import Cache, cache, invalidate


class GhRunner:
    def __init__(self):
        self._cache = Cache()
        self._cache.load()

    def __del__(self):
        self._cache.store()

    def help(self):
        """Print help for `gh` command"""
        cmd = ["gh", "help"]
        out = subprocess.run(cmd, capture_output=True, check=True)
        return out.stdout

    def _list_json_fields(self):
        return ",".join(
            ["author", "number", "state", "title", "url", "reviewDecision",
                "statusCheckRollup", "mergeable", "mergeStateStatus"])

    @cache
    def pr_get(self, repo, number):
        """Get a PR in a given repo by its number"""
        cmd = ["gh", "pr", "view", "--json",
               self._list_json_fields(), "-R", repo, str(number)]
        out = subprocess.run(cmd, capture_output=True, check=True)
        return json.loads(out.stdout)

    @cache
    def pr_list(self, repo, filter=None, merge_state=None):
        """List PRs for a repo

        Given a repo and an optional filter, return the list of current PRs.

        Filter can be any valid search string like a keyword or Github keys.
        """
        cmd = ["gh", "pr", "list", "-R", repo,
               "--json", self._list_json_fields()]
        if filter:
            cmd.extend(["--search", filter])
        if merge_state:
            cmd.extend(
                ['-q',
                 """[.[] | select(.mergeStateStatus == "{}") ]""".format(
                     merge_state.upper())])
        out = subprocess.run(cmd, capture_output=True, check=True)
        return json.loads(out.stdout)

    @invalidate
    def pr_approve(self, repo, number):
        """Mark a PR as reviewed

        Given a repo and a PR number, mark the PR reviewed and return the
        status.
        """
        cmd = ["gh", "pr", "review", "-R", repo, str(number), "--approve"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return (res.stdout, res.stderr)

    @cache
    def pr_open(self, repo, number):
        """Open the PR in a browser"""
        cmd = ["gh", "pr", "view", "-R", repo, str(number), "-w"]
        subprocess.run(cmd, capture_output=True, check=True)

    @invalidate
    def pr_merge(self, repo, number):
        """Merge the PR"""
        cmd = ["gh", "pr", "merge", "-R", repo, str(number), "-m"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return (res.stdout, res.stderr)

    @cache
    def _fetch_action_job_by_id(self, repo, id):
        """Fetches a job by its id"""
        cmd = ["gh", "api", f"/repos/{repo}/actions/jobs/{str(id)}"]
        out = subprocess.run(cmd, capture_output=True, check=True)
        return json.loads(out.stdout)

    @invalidate
    def action_run_rerun(self, repo, number):
        """Rerun a failed Github Action"""
        job = self._fetch_action_job_by_id(repo, number)
        cmd = ["gh", "run", "rerun", "-R", repo, str(job['run_id'])]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return (res.stdout, res.stderr)

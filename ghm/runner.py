import subprocess
import json
import re
from .cache import Cache, cache, invalidate

PAGE = re.compile(r'<.*?page=(\d+)&.*?>; rel="(.*?)",')


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
            op = "=="
            if merge_state.startswith('!'):
                op = "!="
                merge_state = merge_state[1:]
            cmd.extend(
                ['-q',
                 """[.[] | select(.mergeStateStatus {} "{}") ]""".format(
                     op, merge_state.upper())])
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
    def pr_merge(self, repo, number, admin):
        """Merge the PR"""
        cmd = ["gh", "pr", "merge", "-R", repo, str(number), "-m"]
        if admin:
            cmd.append("--admin")
        res = subprocess.run(cmd, capture_output=True, check=True)
        return (res.stdout, res.stderr)

    @invalidate
    def pr_update_branch(self, repo, number):
        """Update branch of the PR"""
        cmd = ["gh", "api", "-X", "PUT", "-H",
               "Accept: application/vnd.github.lydian-preview+json",
               f"/repos/{repo}/pulls/{str(number)}/update-branch"]
        out = subprocess.run(cmd, capture_output=True, check=True)
        return json.loads(out.stdout)

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

    @cache
    def workflow_list(self, repo):
        """List all of the workflows"""
        cmd = ["gh", "workflow", "list", "-R", repo]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return [" ".join(line.decode('utf-8').split()[0:-2])
                for line in res.stdout.splitlines()]

    def run_list_active(self, repo):
        """List active workflow runs (in_progress & queued)"""
        cmd = ["gh", "api", "-H",
               "Accept: application/vnd.github.v3+json",
               f"/repos/{repo}/actions/runs?status=in_progress&status=queued"]
        out = subprocess.run(cmd, capture_output=True, check=True)
        return json.loads(out.stdout)

    def run_list_complete(self, repo, limit=100):
        """List completed workflow runs"""
        data = []
        per_page = (limit > 100) and 100 or limit
        (next_page, page_data) = self._fetch_run_list_complete_page(
            repo,
            1,
            per_page)
        data.extend(page_data.get('workflow_runs', []))
        while next_page > 0 and len(data) < limit:
            (next_page, page_data) = self._fetch_run_list_complete_page(
                repo,
                next_page,
                per_page)
            data.extend(page_data.get('workflow_runs', []))
        return data

    def _fetch_run_list_complete_page(self, repo, page, per_page):
        """Fetch a single page of results"""
        cmd = ["gh", "api", "-i", "-H",
               "Accept: application/vnd.github.v3+json",
               f"/repos/{repo}/actions/runs?"
               f"status=completed&page={page}&per_page={per_page}"]
        out = subprocess.run(cmd, capture_output=True, check=True, text=True)
        headers, body = out.stdout.split('\n\n', 1)
        return (self._next_page(headers), json.loads(body))

    def workflow_run(self, repo, name):
        """Run a given workflow"""
        cmd = ["gh", "workflow", "run", "-R", repo, name]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return (res.stdout, res.stderr)

    def workflow_enable(self, repo, name):
        """Enable a given workflow"""
        cmd = ["gh", "workflow", "enable", "-R", repo, name]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return (res.stdout, res.stderr)

    def workflow_disable(self, repo, name):
        """Disable a given workflow"""
        cmd = ["gh", "workflow", "disable", "-R", repo, name]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return (res.stdout, res.stderr)

    @cache
    def fetch_draft_release(self, repo):
        """Fetch the draft release of a repo"""
        cmd = ["gh", "api", f"/repos/{repo}/releases"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        for release in json.loads(res.stdout):
            if release['draft']:
                return release

    def fetch_latest_release(self, repo):
        """Fetch the latest 2 releases of a repo"""
        cmd = ["gh", "release", "list", "-R", repo, "-L", "2"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return [(line.decode('utf-8').split("\t")[0:])
                for line in res.stdout.splitlines()]

    @invalidate
    def release_publish(self, repo, id, tag):
        """Publish a draft release"""
        cmd = ["gh", "api", f"/repos/{repo}/releases/{id}",
               "-X", "PATCH", "-F", "draft=false", "-F", f"tag_name=v{tag}"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return json.loads(res.stdout)

    def list_repos(self):
        """List all the repos in the org"""
        data = []
        (next_page, page_data) = self._fetch_list_repos_page(1)
        data.extend(page_data)
        while next_page > 0:
            (next_page, page_data) = self._fetch_list_repos_page(next_page)
            data.extend(page_data)
        return data

    def _fetch_list_repos_page(self, page):
        """Fetch a single page of results"""
        cmd = ["gh", "api", "-i", "-H",
               "Accept: application/vnd.github.v3+json",
               f"/orgs/paketo-buildpacks/repos?page={page}&per_page=100"]
        out = subprocess.run(cmd, capture_output=True, check=True, text=True)
        headers, body = out.stdout.split('\n\n', 1)
        return (self._next_page(headers), json.loads(body))

    def _next_page(self, headers):
        for i, header in enumerate(headers.split('\n')):
            if i == 0:
                continue  # skip first line
            key, value = header.split(':', 1)
            if key.strip().lower() == "link":
                matches = PAGE.findall(value)
                next_link = [int(item[0])
                             for item in matches if item[1] == 'next']
                return len(next_link) == 1 and int(next_link[0]) or -1
        return -1

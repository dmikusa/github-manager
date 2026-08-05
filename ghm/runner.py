from itertools import chain
import subprocess
import json
import re
from .cache import get_cache, cache, invalidate, cache_key, _debug

PR_BATCH_SIZE = 20

PAGE = re.compile(r'<.*?page=(\d+)&.*?>; rel="(.*?)",')


class GhRunner:
    skip_cache = False

    def __init__(self):
        self._cache = get_cache()

    def __del__(self):
        self._cache.store()

    def help(self):
        """Print help for `gh` command"""
        cmd = ["gh", "help"]
        out = subprocess.run(cmd, capture_output=True, check=True)
        return out.stdout

    def _list_json_fields(self):
        return ",".join(
            [
                "author",
                "baseRefName",
                "number",
                "state",
                "title",
                "url",
                "reviewDecision",
                "statusCheckRollup",
                "mergeable",
                "mergeStateStatus",
            ]
        )

    @cache
    def pr_get(self, repo, number):
        """Get a PR in a given repo by its number"""
        cmd = [
            "gh",
            "pr",
            "view",
            "--json",
            self._list_json_fields(),
            "-R",
            repo,
            str(number),
        ]
        out = subprocess.run(cmd, capture_output=True, check=True)
        return json.loads(out.stdout)

    @cache
    def pr_list(
        self, repo, filter=None, merge_state=None, review_decision=None, author=None, pr_list=None
    ):
        """List PRs for a repo

        Given a repo and an optional filter, return the list of current PRs.

        Filter can be any valid search string like a keyword or Github keys.
        """
        if pr_list is not None:
            from urllib.parse import urlparse

            pr_numbers = []
            with open(pr_list) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parsed = urlparse(line)
                    path_parts = parsed.path.strip("/").split("/")
                    if len(path_parts) >= 4 and path_parts[2] == "pull":
                        pr_repo = f"{path_parts[0]}/{path_parts[1]}"
                        if pr_repo == repo:
                            pr_numbers.append(int(path_parts[3]))
            results = []
            for num in pr_numbers:
                try:
                    results.append(self.pr_get(repo, num))
                except subprocess.CalledProcessError:
                    print(f"    Warning: PR #{num} not found in {repo}, skipping")
            if filter:
                results = [
                    pr
                    for pr in results
                    if filter.lower() in pr["title"].lower()
                ]
            if author:
                results = [
                    pr
                    for pr in results
                    if pr.get("author", {}).get("login") == author
                ]
            if review_decision:
                negate = review_decision.startswith("!")
                value = review_decision[1:] if negate else review_decision
                results = [
                    pr
                    for pr in results
                    if (pr.get("reviewDecision") == value.upper()) != negate
                ]
            if merge_state:
                negate = merge_state.startswith("!")
                value = merge_state[1:] if negate else merge_state
                results = [
                    pr
                    for pr in results
                    if (pr.get("mergeStateStatus") == value.upper()) != negate
                ]
            return results

        cmd = ["gh", "pr", "list", "-R", repo, "--json", self._list_json_fields()]
        if filter:
            cmd.extend(["--search", filter])
        if author:
            cmd.extend(["--author", author])
        if review_decision:
            op = "=="
            if review_decision.startswith("!"):
                op = "!="
                review_decision = review_decision[1:]
            cmd.extend(
                [
                    "-q",
                    """[.[] | select(.reviewDecision {} "{}") ]""".format(
                        op, review_decision.upper()
                    ),
                ]
            )
        if merge_state:
            op = "=="
            if merge_state.startswith("!"):
                op = "!="
                merge_state = merge_state[1:]
            cmd.extend(
                [
                    "-q",
                    """[.[] | select(.mergeStateStatus {} "{}") ]""".format(
                        op, merge_state.upper()
                    ),
                ]
            )
        out = subprocess.run(cmd, capture_output=True, check=True)
        return json.loads(out.stdout)

    def pr_list_many(
        self,
        repos,
        filter=None,
        merge_state=None,
        review_decision=None,
        author=None,
        states="OPEN",
        batch_size=PR_BATCH_SIZE,
    ):
        """Fetch PRs for many repos, batching the queries into a single
        GraphQL request per batch. Returns a dict of repo -> list of PRs.

        Each repo's PRs are cached individually under the 'pr_list_many'
        method so invalidating one repo does not discard the rest. The
        filter/merge_state/review_decision/author filters are applied
        client-side after fetching, keeping the cached data reusable
        across different filters.
        """
        result = {}
        missing = []
        for repo in repos:
            if not self.skip_cache:
                entry = self._cache.get("pr_list_many", cache_key((repo,), {}))
                if entry is not None:
                    _debug(f"HIT  pr_list_many({repo})")
                    result[repo] = entry
                    continue
            _debug(f"MISS pr_list_many({repo})")
            missing.append(repo)

        for batch in _chunks(missing, batch_size):
            try:
                data = self._gh_pr_query_many(batch, states)
            except subprocess.CalledProcessError:
                data = {}
                for repo in batch:
                    data[repo] = self.pr_list(repo)
            for repo, prs in data.items():
                if not self.skip_cache:
                    self._cache.save(
                        "pr_list_many", cache_key((repo,), {}), prs, scope=repo
                    )
                    _debug(f"SAVE pr_list_many({repo})")
                result[repo] = prs

        return {
            repo: [pr for pr in prs if _pr_matches(
                pr, filter, merge_state, review_decision, author
            )]
            for repo, prs in result.items()
        }

    def _gh_pr_query_many(self, repos, states="OPEN", first=100):
        """Run a single GraphQL query fetching PRs for the given repos."""
        parts = []
        for i, repo in enumerate(repos):
            owner, name = repo.split("/", 1)
            parts.append(
                f'r{i}: repository(owner: "{owner}", name: "{name}") {{ '
                f'pullRequests(first: {first}, states: {states}) {{ nodes {{ '
                "author { login } baseRefName number state title url "
                "reviewDecision mergeable mergeStateStatus "
                "statusCheckRollup { state contexts(first: 50) { nodes { "
                "__typename "
                "... on CheckRun { name status conclusion detailsUrl } "
                "... on StatusContext { context state targetUrl } "
                "} } } "
                "} } }"
            )
        query = "query { " + " ".join(parts) + " }"
        cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
        out = subprocess.run(cmd, capture_output=True, check=True)
        data = json.loads(out.stdout)["data"]
        result = {}
        for i, repo in enumerate(repos):
            node = data.get(f"r{i}")
            if node is None:
                result[repo] = []
                continue
            prs = []
            for pr in node["pullRequests"]["nodes"]:
                pr = dict(pr)
                rollup = pr.get("statusCheckRollup") or {}
                pr["statusCheckRollup"] = (
                    rollup.get("contexts", {}).get("nodes", []) or []
                )
                prs.append(pr)
            result[repo] = prs
        return result

    @invalidate("pr_list", "pr_get", "pr_list_many")
    def pr_approve(self, repo, number):
        """Mark a PR as reviewed

        Given a repo and a PR number, mark the PR reviewed and return the
        status.
        """
        cmd = ["gh", "pr", "review", "-R", repo, str(number), "--approve"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return (res.stdout, res.stderr)

    def pr_open(self, repo, number):
        """Open the PR in a browser"""
        cmd = ["gh", "pr", "view", "-R", repo, str(number), "-w"]
        subprocess.run(cmd, capture_output=True, check=True)

    @invalidate("pr_list", "pr_list_many", use_scope=False)
    def pr_create(self, repo_path, labels):
        """Create a PR"""
        cmd = ["gh", "pr", "create", "--fill"]
        labels = list(chain(*[("-l", label) for label in labels]))
        if len(labels) > 0:
            cmd.extend(labels)
        subprocess.run(cmd, cwd=repo_path, capture_output=True, check=True)

    @invalidate("pr_list", "pr_get", "pr_list_many")
    def pr_merge(self, repo, number, admin, merge_type):
        """Merge the PR"""
        cmd = ["gh", "pr", "merge", "-R", repo, str(number)]
        if merge_type == "merge":
            cmd.append("-m")
        elif merge_type == "squash":
            cmd.append("-s")
        elif merge_type == "rebase":
            cmd.append("-r")
        if admin:
            cmd.append("--admin")
        res = subprocess.run(cmd, capture_output=True, check=True)
        return (res.stdout.decode("utf-8"), res.stderr.decode("utf-8"))

    @cache
    def branch_enforce_admins_enabled(self, repo, branch):
        """Check if enforce_admins is enabled for a branch"""
        cmd = [
            "gh",
            "api",
            f"/repos/{repo}/branches/{branch}/protection/enforce_admins",
        ]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode != 0:
            return False
        return json.loads(res.stdout).get("enabled", False)

    @invalidate("branch_enforce_admins_enabled")
    def branch_enforce_admins_disable(self, repo, branch):
        """Disable enforce_admins for a branch (allow admin bypass)"""
        cmd = [
            "gh",
            "api",
            "--method",
            "DELETE",
            f"/repos/{repo}/branches/{branch}/protection/enforce_admins",
        ]
        subprocess.run(cmd, capture_output=True, check=True)

    @invalidate("branch_enforce_admins_enabled")
    def branch_enforce_admins_enable(self, repo, branch):
        """Enable enforce_admins for a branch (block admin bypass)"""
        cmd = [
            "gh",
            "api",
            "--method",
            "POST",
            f"/repos/{repo}/branches/{branch}/protection/enforce_admins",
        ]
        subprocess.run(cmd, capture_output=True, check=True)

    @invalidate("pr_list", "pr_get", "pr_list_many")
    def pr_update_branch(self, repo, number):
        """Update branch of the PR"""
        cmd = [
            "gh",
            "api",
            "-X",
            "PUT",
            "-H",
            "Accept: application/vnd.github.lydian-preview+json",
            f"/repos/{repo}/pulls/{str(number)}/update-branch",
        ]
        out = subprocess.run(cmd, capture_output=True, check=True)
        return json.loads(out.stdout)

    @invalidate("pr_list", "pr_get", "pr_list_many")
    def pr_apply_label(self, repo, number, label):
        """Apply a label to the PRs"""
        cmd = [
            "gh",
            "pr",
            "edit",
            str(number),
            "-R",
            repo,
            "--add-label",
            label,
        ]
        out = subprocess.run(cmd, capture_output=True, check=True)
        return out.stdout

    @invalidate("pr_list", "pr_get", "pr_list_many")
    def pr_remove_label(self, repo, number, label):
        """Remove a label from PRs"""
        cmd = [
            "gh",
            "pr",
            "edit",
            str(number),
            "-R",
            repo,
            "--remove-label",
            label,
        ]
        out = subprocess.run(cmd, capture_output=True, check=True)
        return out.stdout

    @cache
    def _fetch_action_job_by_id(self, repo, id):
        """Fetches a job by its id"""
        cmd = ["gh", "api", f"/repos/{repo}/actions/jobs/{str(id)}"]
        out = subprocess.run(cmd, capture_output=True, check=True)
        return json.loads(out.stdout)

    @invalidate("_fetch_action_job_by_id")
    def action_run_rerun(self, repo, number):
        """Rerun a failed Github Action"""
        job = self._fetch_action_job_by_id(repo, number)
        cmd = ["gh", "run", "rerun", "-R", repo, str(job["run_id"])]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return (res.stdout, res.stderr)

    @cache
    def workflow_list(self, repo):
        """List all of the workflows"""
        cmd = ["gh", "workflow", "list", "-R", repo]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return [
            " ".join(line.decode("utf-8").split()[0:-2])
            for line in res.stdout.splitlines()
        ]

    @cache
    def run_list_active(self, repo, status):
        """List active workflow runs (in_progress & queued)"""
        cmd = [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.v3+json",
            f"/repos/{repo}/actions/runs?status={status}",
        ]
        out = subprocess.run(cmd, capture_output=True, check=True)
        return json.loads(out.stdout)

    @cache
    def run_list_complete(self, repo, limit=100):
        """List completed workflow runs"""
        data = []
        per_page = (limit > 100) and 100 or limit
        (next_page, page_data) = self._fetch_run_list_complete_page(repo, 1, per_page)
        data.extend(page_data.get("workflow_runs", []))
        while next_page > 0 and len(data) < limit:
            (next_page, page_data) = self._fetch_run_list_complete_page(
                repo, next_page, per_page
            )
            data.extend(page_data.get("workflow_runs", []))
        return data

    @cache
    def _fetch_run_list_complete_page(self, repo, page, per_page):
        """Fetch a single page of results"""
        cmd = [
            "gh",
            "api",
            "-i",
            "-H",
            "Accept: application/vnd.github.v3+json",
            f"/repos/{repo}/actions/runs?"
            f"status=completed&page={page}&per_page={per_page}",
        ]
        out = subprocess.run(cmd, capture_output=True, check=True, text=True)
        headers, body = out.stdout.split("\n\n", 1)
        return (self._next_page(headers), json.loads(body))

    @invalidate("run_list_active", "run_list_complete")
    def workflow_run(self, repo, name):
        """Run a given workflow"""
        cmd = ["gh", "workflow", "run", "-R", repo, name]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return (res.stdout, res.stderr)

    @invalidate("workflow_list")
    def workflow_enable(self, repo, name):
        """Enable a given workflow"""
        cmd = ["gh", "workflow", "enable", "-R", repo, name]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return (res.stdout, res.stderr)

    @invalidate("workflow_list")
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
            if release["draft"]:
                return release

    @cache
    def fetch_latest_release(self, repo):
        """Fetch the latest 2 releases of a repo"""
        cmd = ["gh", "release", "list", "-R", repo, "-L", "2"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return [
            (line.decode("utf-8").split("\t")[0:]) for line in res.stdout.splitlines()
        ]

    @invalidate("fetch_draft_release", "fetch_latest_release")
    def release_publish(self, repo, id, tag):
        """Publish a draft release"""
        cmd = [
            "gh",
            "api",
            f"/repos/{repo}/releases/{id}",
            "-X",
            "PATCH",
            "-F",
            "draft=false",
            "-F",
            f"tag_name=v{tag}",
        ]
        res = subprocess.run(cmd, capture_output=True, check=True)
        return json.loads(res.stdout)

    @cache
    def list_repos(self, org=None):
        """List all the repos in the org"""
        data = []
        (next_page, page_data) = self._fetch_list_repos_page(1, org=org)
        data.extend(page_data)
        while next_page > 0:
            (next_page, page_data) = self._fetch_list_repos_page(next_page, org=org)
            data.extend(page_data)
        return data

    @cache
    def _fetch_list_repos_page(self, page, org=None):
        """Fetch a single page of results"""
        if org is None:
            org = "paketo-buildpacks"
        cmd = [
            "gh",
            "api",
            "-i",
            "-H",
            "Accept: application/vnd.github.v3+json",
            f"/orgs/{org}/repos?page={page}&per_page=100",
        ]
        out = subprocess.run(cmd, capture_output=True, check=True, text=True)
        headers, body = out.stdout.split("\n\n", 1)
        return (self._next_page(headers), json.loads(body))

    def _next_page(self, headers):
        for i, header in enumerate(headers.split("\n")):
            if i == 0:
                continue  # skip first line
            key, value = header.split(":", 1)
            if key.strip().lower() == "link":
                matches = PAGE.findall(value)
                next_link = [int(item[0]) for item in matches if item[1] == "next"]
                return len(next_link) == 1 and int(next_link[0]) or -1
        return -1


class GitRunner:
    def __init__(self, cwd=None):
        self._cwd = cwd

    def __run(self, cmd):
        full_cmd = ["git", "-C", self._cwd, *cmd]
        res = subprocess.run(full_cmd, capture_output=True, check=True)
        return (res.stdout, res.stderr)

    def cwd(self, cwd):
        self._cwd = cwd
        return self

    def clone(self, repo):
        """Clone a repo"""
        cmd = ["clone", repo]
        return self.__run(cmd)

    def reset_hard(self, branch):
        """Reset hard to the given branch"""
        cmd = ["reset", "--hard", branch]
        return self.__run(cmd)

    def clean(self):
        """Clean up any untracked files"""
        cmd = ["clean", "-df"]
        return self.__run(cmd)

    def pull(self):
        """Pull latest changes"""
        cmd = ["pull"]
        return self.__run(cmd)

    def push(self, branch):
        """Push commits"""
        cmd = ["push", "-u", "origin", branch]
        return self.__run(cmd)

    def status(self):
        """Fetch git status"""
        cmd = ["status"]
        return self.__run(cmd)

    def add(self, paths):
        """Add modified files"""
        cmd = ["add", *paths]
        return self.__run(cmd)

    def commit(self, title, body):
        """Commit staged changes"""
        msg = title
        if body is not None:
            msg += "\n\n" + body
        cmd = ["commit", "-m", msg]
        return self.__run(cmd)

    def checkout_branch(self, branch):
        cmd = ["checkout", branch]
        return self.__run(cmd)

    def checkout_new_branch(self, branch):
        cmd = ["checkout", "-b", branch]
        return self.__run(cmd)

    def rev_parse(self, branch):
        cmd = ["rev-parse", branch]
        return self.__run(cmd)


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _pr_matches(pr, filter=None, merge_state=None, review_decision=None, author=None):
    if filter and filter.lower() not in pr["title"].lower():
        return False
    if author and pr.get("author", {}).get("login") != author:
        return False
    if review_decision:
        negate = review_decision.startswith("!")
        value = review_decision[1:] if negate else review_decision
        if (pr.get("reviewDecision") == value.upper()) == negate:
            return False
    if merge_state:
        negate = merge_state.startswith("!")
        value = merge_state[1:] if negate else merge_state
        if (pr.get("mergeStateStatus") == value.upper()) == negate:
            return False
    return True

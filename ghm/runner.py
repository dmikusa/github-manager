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
    def fetch_repo(self, repo):
        """Fetch repository general settings (adjustable only)"""
        cmd = ["gh", "api", f"/repos/{repo}"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        data = json.loads(res.stdout)
        return {
            "name": data.get("name"),
            "description": data.get("description"),
            "homepage": data.get("homepage"),
            "private": data.get("private"),
            "has_issues": data.get("has_issues"),
            "has_projects": data.get("has_projects"),
            "has_wiki": data.get("has_wiki"),
            "has_pages": data.get("has_pages"),
            "has_discussions": data.get("has_discussions"),
            "allow_squash_merge": data.get("allow_squash_merge"),
            "allow_merge_commit": data.get("allow_merge_commit"),
            "allow_rebase_merge": data.get("allow_rebase_merge"),
            "allow_auto_merge": data.get("allow_auto_merge"),
            "delete_branch_on_merge": data.get("delete_branch_on_merge"),
            "allow_update_branch": data.get("allow_update_branch"),
            "use_squash_pr_title_as_default": data.get("use_squash_pr_title_as_default"),
            "squash_merge_commit_message": data.get("squash_merge_commit_message"),
            "squash_merge_commit_title": data.get("squash_merge_commit_title"),
            "merge_commit_message": data.get("merge_commit_message"),
            "merge_commit_title": data.get("merge_commit_title"),
            "archived": data.get("archived"),
            "allow_forking": data.get("allow_forking"),
            "web_commit_signoff_required": data.get("web_commit_signoff_required"),
            "default_branch": data.get("default_branch"),
            "topics": data.get("topics"),
            "visibility": data.get("visibility"),
            "security_and_analysis": data.get("security_and_analysis"),
        }

    @cache
    def fetch_collaborators(self, repo):
        """Fetch repository collaborators/access settings (adjustable only)"""
        # Get direct collaborators (explicitly added to repo)
        cmd = ["gh", "api", "--paginate", f"/repos/{repo}/collaborators?affiliation=direct"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        direct = json.loads(res.stdout)

        # Get outside collaborators (not org members)
        cmd = ["gh", "api", "--paginate", f"/repos/{repo}/collaborators?affiliation=outside"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        outside = json.loads(res.stdout)

        # Get all collaborators (includes org members via teams)
        cmd = ["gh", "api", "--paginate", f"/repos/{repo}/collaborators?affiliation=all"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        all_collaborators = json.loads(res.stdout)

        # Get teams with access to the repo
        cmd = ["gh", "api", "--paginate", f"/repos/{repo}/teams"]
        res = subprocess.run(cmd, capture_output=True)
        teams = []
        team_slugs = []
        if res.returncode == 0 and res.stdout.strip():
            teams_data = json.loads(res.stdout)
            for t in teams_data:
                slug = t.get("slug")
                team_slugs.append(slug)
                teams.append({
                    "name": t.get("name"),
                    "slug": slug,
                    "description": t.get("description"),
                    "privacy": t.get("privacy"),
                    "permission": t.get("permission"),
                    "permissions": t.get("permissions"),
                    "url": t.get("url"),
                    "html_url": t.get("html_url"),
                })

        # Fetch members for each team to build access source mapping
        team_members = {}
        org = repo.split("/")[0]
        for slug in team_slugs:
            cmd = ["gh", "api", "--paginate", f"/orgs/{org}/teams/{slug}/members"]
            res = subprocess.run(cmd, capture_output=True)
            if res.returncode == 0 and res.stdout.strip():
                members = json.loads(res.stdout)
                for m in members:
                    login = m.get("login")
                    if login not in team_members:
                        team_members[login] = []
                    team_members[login].append(slug)

        # Build all collaborators with access source
        direct_logins = {c.get("login") for c in direct}
        outside_logins = {c.get("login") for c in outside}

        def build_user(c, source):
            return {
                "login": c.get("login"),
                "permissions": c.get("permissions"),
                "role_name": c.get("role_name"),
                "access_source": source,
            }

        def has_elevated_permissions(c):
            """Check if user has write/maintain/admin access (beyond read)"""
            perms = c.get("permissions", {})
            return perms.get("push") or perms.get("triage") or perms.get("maintain") or perms.get("admin")

        all_users = []
        for c in all_collaborators:
            login = c.get("login")
            if login in direct_logins:
                source = "direct"
            elif login in outside_logins:
                source = "outside"
            elif login in team_members:
                source = "team"
            else:
                source = "org_member"

            # Skip org_member with only read access (default org permissions)
            if source == "org_member" and not has_elevated_permissions(c):
                continue

            all_users.append(build_user(c, source))

        return {
            "direct": [build_user(c, "direct") for c in direct],
            "outside": [build_user(c, "outside") for c in outside],
            "all": all_users,
            "teams": teams,
            "team_members": team_members,
        }

    def _clean_required_status_checks(self, data):
        """Clean required_status_checks to only adjustable fields.

        The PUT branch-protection endpoint rejects the newer `checks` array, so
        only `strict` and `contexts` are kept. `contexts` carries the same check
        names and round-trips cleanly between repositories.
        """
        if not data:
            return None
        return {
            "strict": data.get("strict"),
            "contexts": data.get("contexts"),
        }

    def _clean_review_actors(self, data):
        """Normalize dismissal/bypass actor lists to arrays of logins/slugs.

        The API returns full user/team objects on read but expects plain strings
        on write, so reduce each entry to its login (users) or slug (teams/apps).
        """
        if not data:
            return None
        return {
            "users": [self._actor_name(u) for u in data.get("users", [])],
            "teams": [self._actor_name(t) for t in data.get("teams", [])],
            "apps": [self._actor_name(a) for a in data.get("apps", [])],
        }

    def _actor_name(self, actor):
        if isinstance(actor, dict):
            return actor.get("login") or actor.get("slug") or actor.get("name")
        return actor

    def _clean_required_pull_request_reviews(self, data):
        """Clean required_pull_request_reviews to only adjustable fields"""
        if not data:
            return None
        reviews = {
            "dismiss_stale_reviews": data.get("dismiss_stale_reviews"),
            "require_code_owner_reviews": data.get("require_code_owner_reviews"),
            "require_last_push_approval": data.get("require_last_push_approval"),
            "required_approving_review_count": data.get("required_approving_review_count"),
        }
        for key in ("dismissal_restrictions", "bypass_pull_request_allowances"):
            actors = self._clean_review_actors(data.get(key))
            if actors is not None:
                reviews[key] = actors
        return reviews

    def _clean_restrictions(self, data):
        """Clean restrictions to only adjustable fields"""
        if not data:
            return None
        return {
            "users": [self._actor_name(u) for u in data.get("users", [])],
            "teams": [self._actor_name(t) for t in data.get("teams", [])],
            "apps": [self._actor_name(a) for a in data.get("apps", [])],
        }

    def _normalize_branch_protection(self, settings):
        """Normalize a branch protection payload so it can be sent to the API.

        Handles settings produced by older versions of this tool that included
        the `checks` array and full user/team objects.
        """
        if not settings:
            return None
        rsc = settings.get("required_status_checks")
        if rsc is not None:
            rsc = {"strict": rsc.get("strict"), "contexts": rsc.get("contexts")}
        rpr = settings.get("required_pull_request_reviews")
        if rpr is not None:
            rpr = dict(rpr)
            for key in ("dismissal_restrictions", "bypass_pull_request_allowances"):
                if rpr.get(key) is None:
                    rpr.pop(key, None)
                else:
                    rpr[key] = self._clean_review_actors(rpr[key])
        restrictions = settings.get("restrictions")
        if restrictions is not None:
            restrictions = self._clean_restrictions(restrictions)
        normalized = {
            "required_status_checks": rsc,
            "enforce_admins": settings.get("enforce_admins"),
            "required_pull_request_reviews": rpr,
            "restrictions": restrictions,
        }
        for key in (
            "required_linear_history",
            "allow_force_pushes",
            "allow_deletions",
            "block_creations",
            "required_conversation_resolution",
            "lock_branch",
            "allow_fork_syncing",
        ):
            if key in settings and settings.get(key) is not None:
                normalized[key] = settings.get(key)
        return normalized

    @cache
    def fetch_branch_protection(self, repo, branch="main"):
        """Fetch old-style branch protection rules for a branch (adjustable only)"""
        cmd = ["gh", "api", f"/repos/{repo}/branches/{branch}/protection"]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode == 404 or b"404" in res.stderr:
            return None
        if res.returncode != 0:
            raise Exception(f"Failed to fetch branch protection: {res.stderr.decode()}")
        data = json.loads(res.stdout)
        return {
            "branch": branch,
            "required_status_checks": self._clean_required_status_checks(data.get("required_status_checks")),
            "enforce_admins": data.get("enforce_admins", {}).get("enabled"),
            "required_pull_request_reviews": self._clean_required_pull_request_reviews(data.get("required_pull_request_reviews")),
            "restrictions": self._clean_restrictions(data.get("restrictions")),
            "required_linear_history": data.get("required_linear_history", {}).get("enabled"),
            "allow_force_pushes": data.get("allow_force_pushes", {}).get("enabled"),
            "allow_deletions": data.get("allow_deletions", {}).get("enabled"),
            "block_creations": data.get("block_creations", {}).get("enabled"),
            "required_conversation_resolution": data.get("required_conversation_resolution", {}).get("enabled"),
            "lock_branch": data.get("lock_branch", {}).get("enabled"),
            "allow_fork_syncing": data.get("allow_fork_syncing", {}).get("enabled"),
        }

    @cache
    def fetch_rulesets(self, repo):
        """Fetch new-style branch protection rules (rulesets) (adjustable only)"""
        cmd = ["gh", "api", "--paginate", f"/repos/{repo}/rulesets"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        data = json.loads(res.stdout)
        return [
            {
                "name": r.get("name"),
                "target": r.get("target"),
                "enforcement": r.get("enforcement"),
                "bypass_actors": r.get("bypass_actors"),
                "conditions": r.get("conditions"),
                "rules": r.get("rules"),
            }
            for r in data
        ]

    @cache
    def fetch_actions_permissions(self, repo):
        """Fetch GitHub Actions permissions (adjustable only)"""
        cmd = ["gh", "api", f"/repos/{repo}/actions/permissions"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        data = json.loads(res.stdout)
        return {
            "enabled": data.get("enabled"),
            "allowed_actions": data.get("allowed_actions"),
            "sha_pinning_required": data.get("sha_pinning_required"),
        }

    @cache
    def fetch_actions_workflow_permissions(self, repo):
        """Fetch default workflow permissions (adjustable only)"""
        cmd = ["gh", "api", f"/repos/{repo}/actions/permissions/workflow"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        data = json.loads(res.stdout)
        return {
            "default_workflow_permissions": data.get("default_workflow_permissions"),
            "can_approve_pull_request_reviews": data.get("can_approve_pull_request_reviews"),
        }

    @cache
    def fetch_actions_access_level(self, repo):
        """Fetch actions access level (private repos only) (adjustable only)"""
        cmd = ["gh", "api", f"/repos/{repo}/actions/permissions/access"]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode != 0:
            err = res.stderr.decode() if res.stderr else res.stdout.decode()
            if "Access policy only applies to internal and private repositories" in err:
                return None
            raise Exception(f"Failed to fetch actions access: {err}")
        data = json.loads(res.stdout)
        return {
            "access_level": data.get("access_level"),
        }

    @cache
    def fetch_webhooks(self, repo):
        """Fetch repository webhooks (adjustable only)"""
        cmd = ["gh", "api", "--paginate", f"/repos/{repo}/hooks"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        data = json.loads(res.stdout)
        return [
            {
                "name": w.get("name"),
                "active": w.get("active"),
                "events": w.get("events"),
                "config": {
                    "url": w.get("config", {}).get("url"),
                    "content_type": w.get("config", {}).get("content_type"),
                    "insecure_ssl": w.get("config", {}).get("insecure_ssl"),
                },
            }
            for w in data
        ]

    @cache
    def fetch_environments(self, repo):
        """Fetch repository environments (adjustable only)"""
        cmd = ["gh", "api", "--paginate", f"/repos/{repo}/environments"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        data = json.loads(res.stdout)
        environments = data.get("environments", [])
        return [
            {
                "name": e.get("name"),
                "wait_timer": e.get("wait_timer"),
                "prevent_self_review": e.get("prevent_self_review"),
                "reviewers": e.get("reviewers"),
                "deployment_branch_policy": e.get("deployment_branch_policy"),
            }
            for e in environments
        ]

    @cache
    def fetch_code_security_config(self, repo):
        """Fetch code security configuration (secret scanning, dependabot, etc.) (adjustable only)"""
        cmd = ["gh", "api", f"/repos/{repo}/code-security-configuration"]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode == 404 or b"404" in res.stderr:
            return None
        if res.returncode != 0:
            raise Exception(f"Failed to fetch code security config: {res.stderr.decode()}")
        if not res.stdout.strip():
            return None
        data = json.loads(res.stdout)
        config = data.get("configuration", {})
        return {
            "advanced_security": config.get("advanced_security"),
            "dependency_graph": config.get("dependency_graph"),
            "dependency_graph_autosubmit_action": config.get("dependency_graph_autosubmit_action"),
            "dependabot_alerts": config.get("dependabot_alerts"),
            "dependabot_security_updates": config.get("dependabot_security_updates"),
            "secret_scanning": config.get("secret_scanning"),
            "secret_scanning_push_protection": config.get("secret_scanning_push_protection"),
            "secret_scanning_validity_checks": config.get("secret_scanning_validity_checks"),
            "secret_scanning_non_provider_patterns": config.get("secret_scanning_non_provider_patterns"),
            "secret_scanning_generic_secrets": config.get("secret_scanning_generic_secrets"),
            "secret_scanning_extended_metadata": config.get("secret_scanning_extended_metadata"),
            "private_vulnerability_reporting": config.get("private_vulnerability_reporting"),
            "enforcement": config.get("enforcement"),
        }

    @cache
    def fetch_code_scanning_setup(self, repo):
        """Fetch code scanning default setup (adjustable only)"""
        cmd = ["gh", "api", f"/repos/{repo}/code-scanning/default-setup"]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode == 404 or b"404" in res.stderr:
            return None
        if res.returncode != 0:
            raise Exception(f"Failed to fetch code scanning setup: {res.stderr.decode()}")
        if not res.stdout.strip():
            return None
        data = json.loads(res.stdout)
        return {
            "state": data.get("state"),
            "languages": data.get("languages"),
            "runner_type": data.get("runner_type"),
            "runner_label": data.get("runner_label"),
            "query_suite": data.get("query_suite"),
            "threat_model": data.get("threat_model"),
            "schedule": data.get("schedule"),
        }

    @cache
    def fetch_code_quality_setup(self, repo):
        """Fetch code quality setup (adjustable only)"""
        cmd = ["gh", "api", f"/repos/{repo}/code-quality/setup"]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode == 404 or b"404" in res.stderr:
            return None
        if res.returncode != 0:
            raise Exception(f"Failed to fetch code quality setup: {res.stderr.decode()}")
        if not res.stdout.strip():
            return None
        data = json.loads(res.stdout)
        return {
            "state": data.get("state"),
            "languages": data.get("languages"),
            "runner_type": data.get("runner_type"),
            "runner_label": data.get("runner_label"),
            "schedule": data.get("schedule"),
            "ai_findings_option": data.get("ai_findings_option"),
        }

    @cache
    def fetch_actions_secrets(self, repo):
        """Fetch Actions secrets (metadata only, no values)"""
        cmd = ["gh", "api", "--paginate", f"/repos/{repo}/actions/secrets"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        data = json.loads(res.stdout)
        return {
            "total_count": data.get("total_count"),
            "secrets": [
                {
                    "name": s.get("name"),
                    "created_at": s.get("created_at"),
                    "updated_at": s.get("updated_at"),
                    "visibility": s.get("visibility"),
                }
                for s in data.get("secrets", [])
            ],
        }

    @cache
    def fetch_dependabot_secrets(self, repo):
        """Fetch Dependabot secrets (metadata only, no values)"""
        cmd = ["gh", "api", "--paginate", f"/repos/{repo}/dependabot/secrets"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        data = json.loads(res.stdout)
        return {
            "total_count": data.get("total_count"),
            "secrets": [
                {
                    "name": s.get("name"),
                    "created_at": s.get("created_at"),
                    "updated_at": s.get("updated_at"),
                    "visibility": s.get("visibility"),
                }
                for s in data.get("secrets", [])
            ],
        }

    @cache
    def fetch_codespaces_secrets(self, repo):
        """Fetch Codespaces secrets (metadata only, no values)"""
        cmd = ["gh", "api", "--paginate", f"/repos/{repo}/codespaces/secrets"]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode == 404 or b"404" in res.stderr:
            return None
        if res.returncode != 0:
            raise Exception(f"Failed to fetch codespaces secrets: {res.stderr.decode()}")
        if not res.stdout.strip():
            return None
        data = json.loads(res.stdout)
        return {
            "total_count": data.get("total_count"),
            "secrets": [
                {
                    "name": s.get("name"),
                    "created_at": s.get("created_at"),
                    "updated_at": s.get("updated_at"),
                    "visibility": s.get("visibility"),
                }
                for s in data.get("secrets", [])
            ],
        }

    def secret_exists(self, repo, name, secret_type="actions"):
        """Check whether a repository secret of the given type exists

        `secret_type` is one of the `gh secret set --app` applications:
        actions, agents, codespaces or dependabot.
        """
        cmd = ["gh", "api", f"/repos/{repo}/{secret_type}/secrets/{name}"]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode == 0:
            return True
        if res.returncode == 404 or b"404" in res.stderr:
            return False
        err = res.stderr.decode() if res.stderr else res.stdout.decode()
        raise Exception(f"Failed to check secret {name}: {err.strip()}")

    @cache
    def fetch_secrets(self, repo, secret_type="actions"):
        """Fetch repository secrets of a given type (metadata only, no values)

        Returns a list of secret metadata, or None when the type is not
        available for the repo. `secret_type` is one of actions, agents,
        codespaces or dependabot.
        """
        cmd = ["gh", "api", "--paginate", f"/repos/{repo}/{secret_type}/secrets"]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode == 404 or b"404" in res.stderr:
            return None
        if res.returncode != 0:
            err = res.stderr.decode() if res.stderr else res.stdout.decode()
            raise Exception(f"Failed to list {secret_type} secrets: {err.strip()}")
        if not res.stdout.strip():
            return None
        return json.loads(res.stdout).get("secrets", [])

    @invalidate(
        "fetch_actions_secrets",
        "fetch_dependabot_secrets",
        "fetch_codespaces_secrets",
        "fetch_secrets",
    )
    def secret_set(self, repo, name, value, secret_type="actions"):
        """Create or update a repository secret of the given type

        `gh` encrypts the value locally with the repository's public key
        before it is sent to GitHub, so the plaintext never leaves the
        machine. `secret_type` is one of actions, agents, codespaces or
        dependabot.
        """
        cmd = [
            "gh",
            "secret",
            "set",
            name,
            "--repo",
            repo,
            "--app",
            secret_type,
        ]
        res = subprocess.run(cmd, input=value.encode(), capture_output=True)
        if res.returncode != 0:
            err = res.stderr.decode() if res.stderr else res.stdout.decode()
            raise Exception(f"Failed to set secret {name}: {err.strip()}")
        return True

    def _clean_org_secret(self, secret):
        """Clean org secret to only adjustable fields"""
        return {
            "name": secret.get("name"),
            "created_at": secret.get("created_at"),
            "updated_at": secret.get("updated_at"),
            "visibility": secret.get("visibility"),
            "selected_repositories_url": secret.get("selected_repositories_url"),
        }

    @cache
    def fetch_org_actions_secrets(self, org):
        """Fetch organization-level Actions secrets (metadata only)"""
        cmd = ["gh", "api", "--paginate", f"/orgs/{org}/actions/secrets"]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode in (403, 404):
            return {"total_count": 0, "secrets": [], "note": "Requires admin:org scope or org admin access"}
        if res.returncode != 0:
            return {"total_count": 0, "secrets": [], "error": res.stderr.decode()}
        if not res.stdout.strip():
            return {"total_count": 0, "secrets": []}
        data = json.loads(res.stdout)
        return {
            "total_count": data.get("total_count"),
            "secrets": [self._clean_org_secret(s) for s in data.get("secrets", [])],
        }

    @cache
    def fetch_org_dependabot_secrets(self, org):
        """Fetch organization-level Dependabot secrets (metadata only)"""
        cmd = ["gh", "api", "--paginate", f"/orgs/{org}/dependabot/secrets"]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode in (403, 404):
            return {"total_count": 0, "secrets": [], "note": "Requires admin:org scope or org admin access"}
        if res.returncode != 0:
            return {"total_count": 0, "secrets": [], "error": res.stderr.decode()}
        if not res.stdout.strip():
            return {"total_count": 0, "secrets": []}
        data = json.loads(res.stdout)
        return {
            "total_count": data.get("total_count"),
            "secrets": [self._clean_org_secret(s) for s in data.get("secrets", [])],
        }

    @cache
    def fetch_org_codespaces_secrets(self, org):
        """Fetch organization-level Codespaces secrets (metadata only)"""
        cmd = ["gh", "api", "--paginate", f"/orgs/{org}/codespaces/secrets"]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode in (403, 404):
            return {"total_count": 0, "secrets": [], "note": "Requires admin:org scope or org admin access"}
        if res.returncode != 0:
            return {"total_count": 0, "secrets": [], "error": res.stderr.decode()}
        if not res.stdout.strip():
            return {"total_count": 0, "secrets": []}
        data = json.loads(res.stdout)
        return {
            "total_count": data.get("total_count"),
            "secrets": [self._clean_org_secret(s) for s in data.get("secrets", [])],
        }

    def fetch_all_settings(self, repo, branch="main"):
        """Fetch all repository settings organized by GitHub settings pages"""
        org = repo.split("/")[0]
        return {
            "general": self.fetch_repo(repo),
            "collaborators": self.fetch_collaborators(repo),
            "branches": {
                "branch_protection": self.fetch_branch_protection(repo, branch),
                "rulesets": self.fetch_rulesets(repo),
            },
            "actions": {
                "general": self.fetch_actions_permissions(repo),
                "workflow_permissions": self.fetch_actions_workflow_permissions(repo),
                "access": self.fetch_actions_access_level(repo),
                "secrets": self.fetch_actions_secrets(repo),
            },
            "webhooks": self.fetch_webhooks(repo),
            "environments": self.fetch_environments(repo),
            "security": {
                "code_security": self.fetch_code_security_config(repo),
                "code_scanning": self.fetch_code_scanning_setup(repo),
                "code_quality": self.fetch_code_quality_setup(repo),
                "dependabot_secrets": self.fetch_dependabot_secrets(repo),
            },
            "secrets": {
                "actions": self.fetch_actions_secrets(repo),
                "dependabot": self.fetch_dependabot_secrets(repo),
                "codespaces": self.fetch_codespaces_secrets(repo),
            },
            "organization_secrets": {
                "actions": self.fetch_org_actions_secrets(org),
                "dependabot": self.fetch_org_dependabot_secrets(org),
                "codespaces": self.fetch_org_codespaces_secrets(org),
            },
        }

    def push_all_settings(self, repo, settings, branch="main", dry_run=False, complete=False):
        """Push all repository settings from JSON.

        Per-repo settings (topics, collaborators and teams) vary between repos
        and are only applied when `complete` is True.
        """
        results = {}

        def apply(section, fn, *args):
            try:
                results[section] = fn(*args)
            except Exception as ex:
                results[section] = {"status": "error", "error": str(ex)}

        # General settings
        if "general" in settings:
            apply("general", self.push_general_settings, repo, settings["general"], dry_run, complete)

        # Collaborators/teams are per-repo; only touch them when complete
        if complete and "collaborators" in settings:
            apply("collaborators", self.push_collaborators, repo, settings["collaborators"], dry_run)
            apply("teams", self.push_teams, repo, settings["collaborators"], dry_run)

        # Branch protection and rulesets
        if "branches" in settings:
            branches = settings["branches"]
            if "branch_protection" in branches and branches["branch_protection"]:
                apply("branch_protection", self.push_branch_protection, repo, branch, branches["branch_protection"], dry_run)
            if "rulesets" in branches:
                apply("rulesets", self.push_rulesets, repo, branches["rulesets"], dry_run)

        # Actions settings
        if "actions" in settings:
            actions = settings["actions"]
            if "general" in actions:
                apply("actions_general", self.push_actions_permissions, repo, actions["general"], dry_run)
            if "workflow_permissions" in actions:
                apply("actions_workflow_permissions", self.push_actions_workflow_permissions, repo, actions["workflow_permissions"], dry_run)
            if "access" in actions and actions["access"]:
                apply("actions_access", self.push_actions_access_level, repo, actions["access"], dry_run)
            # Note: secrets are handled separately

        # Webhooks
        if "webhooks" in settings:
            apply("webhooks", self.push_webhooks, repo, settings["webhooks"], dry_run)

        # Environments
        if "environments" in settings:
            apply("environments", self.push_environments, repo, settings["environments"], dry_run)

        # Security settings
        if "security" in settings:
            security = settings["security"]
            if "code_security" in security and security["code_security"]:
                apply("code_security", self.push_code_security_config, repo, security["code_security"], dry_run)
            if "code_scanning" in security and security["code_scanning"]:
                apply("code_scanning", self.push_code_scanning_setup, repo, security["code_scanning"], dry_run)
            if "code_quality" in security and security["code_quality"]:
                apply("code_quality", self.push_code_quality_setup, repo, security["code_quality"], dry_run)

        return results

    def settings_diff(self, desired, current, complete=False):
        """Diff desired settings against a repo's current settings.

        Mirrors the sections push_all_settings actually applies. Returns a list of
        changes of the form {"section", "field", "name"?, "current", "desired", "action"?}.
        Per-repo settings (topics, collaborators, teams) are only diffed when
        `complete` is True.
        """
        changes = []

        # General settings - mirror push_general_settings allowed_fields
        allowed_general = {
            "has_issues", "has_projects", "has_wiki",
            "has_pages", "has_discussions", "allow_squash_merge", "allow_merge_commit",
            "allow_rebase_merge", "allow_auto_merge", "delete_branch_on_merge",
            "allow_update_branch", "use_squash_pr_title_as_default",
            "squash_merge_commit_message", "squash_merge_commit_title",
            "merge_commit_message", "merge_commit_title", "archived",
            "web_commit_signoff_required", "default_branch",
            "visibility"
        }
        if complete:
            allowed_general.add("topics")
        d = desired.get("general") or {}
        c = current.get("general") or {}
        for k in sorted(allowed_general):
            dv = d.get(k)
            if dv is None:
                continue
            if dv != c.get(k):
                changes.append({"section": "general", "field": k, "current": c.get(k), "desired": dv})

        # Collaborators - only applied when complete
        if complete:
            dc = desired.get("collaborators") or {}
            cc = current.get("collaborators") or {}
            for kind in ("direct", "outside"):
                dlist = {x.get("login"): x for x in dc.get(kind, []) if x.get("login")}
                clist = {x.get("login"): x for x in cc.get(kind, []) if x.get("login")}
                for login in sorted(dlist):
                    dp = self._collab_permission(dlist[login])
                    if login not in clist:
                        changes.append({"section": "collaborators", "field": kind, "name": login, "action": "add", "desired": dp})
                    elif dp != self._collab_permission(clist[login]):
                        changes.append({"section": "collaborators", "field": kind, "name": login, "current": self._collab_permission(clist[login]), "desired": dp})
            # Teams are matched by slug
            dteams = {t.get("slug"): t for t in dc.get("teams", []) if t.get("slug")}
            cteams = {t.get("slug"): t for t in cc.get("teams", []) if t.get("slug")}
            for slug in sorted(dteams):
                dp = dteams[slug].get("permission")
                if slug not in cteams:
                    changes.append({"section": "teams", "field": "permission", "name": slug, "action": "add", "desired": dp})
                elif dp != cteams[slug].get("permission"):
                    changes.append({"section": "teams", "field": "permission", "name": slug, "current": cteams[slug].get("permission"), "desired": dp})

        # Branches
        db = desired.get("branches") or {}
        cb = current.get("branches") or {}
        self._diff_scalars(changes, "branch_protection", self._normalize_branch_protection(db.get("branch_protection")) or {}, cb.get("branch_protection") or {})
        self._diff_by_name(changes, "rulesets", db.get("rulesets") or [], cb.get("rulesets") or [])

        # Actions
        da = desired.get("actions") or {}
        ca = current.get("actions") or {}
        self._diff_scalars(changes, "actions.general", da.get("general") or {}, ca.get("general") or {})
        self._diff_scalars(changes, "actions.workflow_permissions", da.get("workflow_permissions") or {}, ca.get("workflow_permissions") or {})
        self._diff_scalars(changes, "actions.access", da.get("access") or {}, ca.get("access") or {})

        # Webhooks (authoritative: extras are removed) and environments - match by name
        self._diff_by_name(changes, "webhooks", desired.get("webhooks") or [], current.get("webhooks") or [], report_removals=True)
        self._diff_by_name(changes, "environments", desired.get("environments") or [], current.get("environments") or [])

        # Security - code_security is never pushed; secrets are not pushed here
        ds = desired.get("security") or {}
        cs = current.get("security") or {}
        self._diff_scalars(changes, "code_scanning", ds.get("code_scanning") or {}, cs.get("code_scanning") or {})
        self._diff_scalars(changes, "code_quality", ds.get("code_quality") or {}, cs.get("code_quality") or {})

        return changes

    def _diff_scalars(self, changes, section, desired, current):
        """Diff two dicts of scalar/nested values. A None desired value means 'skip'."""
        for k, dv in desired.items():
            if dv is None:
                continue
            cv = current.get(k)
            if dv != cv:
                changes.append({"section": section, "field": k, "current": cv, "desired": dv})

    def _diff_by_name(self, changes, section, desired, current, report_removals=False):
        """Diff two lists of dicts, matched by 'name'. A None desired value means 'skip'."""
        desired_map = {x.get("name"): x for x in desired if x.get("name")}
        current_map = {x.get("name"): x for x in current if x.get("name")}
        for name in sorted(desired_map):
            d = desired_map[name]
            if name not in current_map:
                details = {k: v for k, v in d.items() if k != "name" and v is not None}
                changes.append({"section": section, "field": "name", "name": name, "action": "add", "desired": details})
                continue
            c = current_map[name]
            for k, dv in d.items():
                if k == "name" or dv is None:
                    continue
                cv = c.get(k)
                if dv != cv:
                    changes.append({"section": section, "field": k, "name": name, "current": cv, "desired": dv})
        if report_removals:
            for name in sorted(current_map):
                if name not in desired_map:
                    changes.append({"section": section, "field": "name", "name": name, "action": "remove", "current": current_map[name]})

    def _collab_permission(self, collab):
        """Normalize a collaborator entry to its highest permission string."""
        perm = collab.get("permission") if "permission" in collab else collab.get("permissions")
        if isinstance(perm, dict):
            for r in ("admin", "maintain", "push", "triage", "pull"):
                if perm.get(r):
                    return r
            return None
        return perm

    @invalidate("fetch_repo")
    def push_general_settings(self, repo, settings, dry_run=False, complete=False):
        """Push general repository settings.

        Topics are per-repo and only applied when `complete` is True.
        """
        # Filter to only adjustable fields - exclude identity fields (name, owner, etc.)
        # Also exclude fields that are unique per repo (description, homepage)
        allowed_fields = {
            "has_issues", "has_projects", "has_wiki",
            "has_pages", "has_discussions", "allow_squash_merge", "allow_merge_commit",
            "allow_rebase_merge", "allow_auto_merge", "delete_branch_on_merge",
            "allow_update_branch", "use_squash_pr_title_as_default",
            "squash_merge_commit_message", "squash_merge_commit_title",
            "merge_commit_message", "merge_commit_title", "archived",
            "web_commit_signoff_required", "default_branch",
            "visibility"
        }
        if complete:
            allowed_fields.add("topics")
        data = {k: v for k, v in settings.items() if k in allowed_fields and v is not None}
        if not data:
            return {"status": "skipped", "reason": "no adjustable fields"}

        if dry_run:
            return {"status": "dry_run", "changes": data}

        results = {"updated": [], "failed": []}

        def patch(fields):
            cmd = ["gh", "api", "--method", "PATCH", f"/repos/{repo}", "--input", "-"]
            res = subprocess.run(cmd, input=json.dumps(fields).encode(), capture_output=True)
            if res.returncode == 0:
                results["updated"].extend(fields.keys())
            else:
                err = res.stderr.decode() if res.stderr else res.stdout.decode()
                for k in fields:
                    results["failed"].append({"field": k, "error": err})

        # For topics, use the dedicated endpoint
        if "topics" in data:
            topics = data.pop("topics")
            cmd = ["gh", "api", "--method", "PUT", f"/repos/{repo}/topics", "--input", "-"]
            res = subprocess.run(cmd, input=json.dumps({"names": topics}).encode(), capture_output=True)
            if res.returncode == 0:
                results["updated"].append("topics")
            else:
                results["failed"].append({"field": "topics", "error": res.stderr.decode()})

        # GitHub validates these fields as pairs and rejects a lone value, so
        # send each pair together (filling in the current value when absent).
        pairs = (
            ("squash_merge_commit_title", "squash_merge_commit_message"),
            ("merge_commit_title", "merge_commit_message"),
        )
        handled = set()
        current = None
        for pair in pairs:
            fields = {k: data[k] for k in pair if k in data}
            if not fields:
                continue
            if len(fields) < len(pair):
                if current is None:
                    current = self.fetch_repo(repo)
                for k in pair:
                    if k not in fields and current.get(k) is not None:
                        fields[k] = current[k]
            patch(fields)
            handled.update(pair)

        # Update remaining fields individually to isolate partial failures
        for k, v in data.items():
            if k in handled:
                continue
            patch({k: v})

        if results["failed"]:
            return {"status": "partial", "results": results}
        return {"status": "updated", "results": results}

    @invalidate("fetch_collaborators")
    def push_collaborators(self, repo, settings, dry_run=False):
        """Push repository collaborators (direct and outside only)"""
        results = []

        for kind in ("direct", "outside"):
            for collab in settings.get(kind, []):
                login = collab.get("login")
                permission = collab.get("permission") or collab.get("permissions")
                if not login:
                    continue

                perm_value = "push"
                if isinstance(permission, dict):
                    if permission.get("admin"):
                        perm_value = "admin"
                    elif permission.get("maintain"):
                        perm_value = "maintain"
                    elif permission.get("push"):
                        perm_value = "push"
                    elif permission.get("triage"):
                        perm_value = "triage"
                    elif permission.get("pull"):
                        perm_value = "pull"
                elif isinstance(permission, str):
                    perm_value = permission

                if dry_run:
                    results.append({"login": login, "kind": kind, "status": "dry_run", "permission": perm_value})
                    continue

                cmd = ["gh", "api", "--method", "PUT", f"/repos/{repo}/collaborators/{login}", "-f", f"permission={perm_value}"]
                res = subprocess.run(cmd, capture_output=True)
                if res.returncode == 0:
                    results.append({"login": login, "kind": kind, "status": "updated", "permission": perm_value})
                else:
                    results.append({"login": login, "kind": kind, "status": "error", "error": res.stderr.decode()})

        return results

    @invalidate("fetch_branch_protection")
    def push_branch_protection(self, repo, branch, settings, dry_run=False):
        """Push branch protection rules"""
        normalized = self._normalize_branch_protection(settings)
        if dry_run:
            return {"status": "dry_run", "branch": branch, "changes": normalized}

        if not normalized or all(
            normalized.get(k) is None
            for k in ("required_status_checks", "required_pull_request_reviews", "restrictions")
        ):
            return {"status": "skipped", "reason": "no protection settings"}

        cmd = ["gh", "api", "--method", "PUT", f"/repos/{repo}/branches/{branch}/protection"]
        cmd.extend(["--input", "-"])
        proc = subprocess.run(cmd, input=json.dumps(normalized).encode(), capture_output=True)
        if proc.returncode == 404 or b"404" in proc.stderr:
            return {"status": "skipped", "reason": f"branch '{branch}' not found"}
        if proc.returncode != 0:
            return {"status": "error", "error": proc.stderr.decode()}
        return {"status": "updated"}

    @invalidate("fetch_rulesets")
    def push_rulesets(self, repo, rulesets, dry_run=False):
        """Push branch protection rulesets"""
        results = []
        for rs in rulesets:
            name = rs.get("name")
            if not name:
                continue

            if dry_run:
                results.append({"name": name, "status": "dry_run", "action": "create/update"})
                continue

            # Try to update existing ruleset first
            data = {k: v for k, v in rs.items() if k != "name" and v is not None}
            cmd = ["gh", "api", "--method", "POST", f"/repos/{repo}/rulesets"]
            cmd.extend(["--input", "-"])
            proc = subprocess.run(cmd, input=json.dumps(data).encode(), capture_output=True)
            if proc.returncode == 0:
                results.append({"name": name, "status": "created", "data": json.loads(proc.stdout)})
            else:
                results.append({"name": name, "status": "error", "error": proc.stderr.decode()})
        return results

    @invalidate("fetch_actions_permissions")
    def push_actions_permissions(self, repo, settings, dry_run=False):
        """Push GitHub Actions permissions"""
        if dry_run:
            return {"status": "dry_run", "changes": settings}

        data = {k: v for k, v in settings.items() if v is not None}
        if not data:
            return {"status": "skipped", "reason": "no settings"}

        cmd = ["gh", "api", "--method", "PUT", f"/repos/{repo}/actions/permissions"]
        cmd.extend(["--input", "-"])
        proc = subprocess.run(cmd, input=json.dumps(data).encode(), capture_output=True, check=True)
        if proc.stdout.strip():
            return {"status": "updated", "data": json.loads(proc.stdout)}
        return {"status": "updated"}

    @invalidate("fetch_actions_workflow_permissions")
    def push_actions_workflow_permissions(self, repo, settings, dry_run=False):
        """Push default workflow permissions"""
        if dry_run:
            return {"status": "dry_run", "changes": settings}

        data = {k: v for k, v in settings.items() if v is not None}
        if not data:
            return {"status": "skipped", "reason": "no settings"}

        cmd = ["gh", "api", "--method", "PUT", f"/repos/{repo}/actions/permissions/workflow"]
        cmd.extend(["--input", "-"])
        proc = subprocess.run(cmd, input=json.dumps(data).encode(), capture_output=True, check=True)
        if proc.stdout.strip():
            return {"status": "updated", "data": json.loads(proc.stdout)}
        return {"status": "updated"}

    @invalidate("fetch_actions_access_level")
    def push_actions_access_level(self, repo, settings, dry_run=False):
        """Push actions access level (private repos only)"""
        if dry_run:
            return {"status": "dry_run", "changes": settings}

        data = {k: v for k, v in settings.items() if v is not None}
        if not data:
            return {"status": "skipped", "reason": "no settings"}

        cmd = ["gh", "api", "--method", "PUT", f"/repos/{repo}/actions/permissions/access"]
        cmd.extend(["--input", "-"])
        proc = subprocess.run(cmd, input=json.dumps(data).encode(), capture_output=True)
        if proc.returncode == 422:
            return {"status": "skipped", "reason": "not applicable to public repos"}
        proc.check_returncode()
        return {"status": "updated", "data": json.loads(proc.stdout)}

    @invalidate("fetch_webhooks")
    def push_webhooks(self, repo, webhooks, dry_run=False):
        """Push webhooks, removing any that are not present in the desired set"""
        results = []
        desired_urls = {
            wh.get("config", {}).get("url")
            for wh in webhooks
            if wh.get("config", {}).get("url")
        }
        if dry_run:
            for wh in webhooks:
                name = wh.get("name")
                url = wh.get("config", {}).get("url")
                results.append({"name": name, "status": "dry_run", "url": url})
            return results

        # Fetch existing hooks once
        cmd = ["gh", "api", "--paginate", f"/repos/{repo}/hooks"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        existing = json.loads(res.stdout)

        for wh in webhooks:
            name = wh.get("name")
            config = wh.get("config", {})
            url = config.get("url")
            if not name or not url:
                continue

            data = {
                "name": name,
                "active": wh.get("active", True),
                "events": wh.get("events", ["push"]),
                "config": {
                    "url": url,
                    "content_type": config.get("content_type", "json"),
                    "insecure_ssl": config.get("insecure_ssl", "0"),
                }
            }
            existing_id = next(
                (w.get("id") for w in existing if w.get("config", {}).get("url") == url),
                None,
            )
            if existing_id:
                cmd = ["gh", "api", "--method", "PATCH", f"/repos/{repo}/hooks/{existing_id}"]
            else:
                cmd = ["gh", "api", "--method", "POST", f"/repos/{repo}/hooks"]
            cmd.extend(["--input", "-"])
            proc = subprocess.run(cmd, input=json.dumps(data).encode(), capture_output=True)
            if proc.returncode == 0:
                results.append({"name": name, "status": "updated" if existing_id else "created"})
            else:
                results.append({"name": name, "status": "error", "error": proc.stderr.decode()})

        # Remove hooks that are no longer desired
        for w in existing:
            if w.get("config", {}).get("url") in desired_urls:
                continue
            hook_id = w.get("id")
            proc = subprocess.run(
                ["gh", "api", "--method", "DELETE", f"/repos/{repo}/hooks/{hook_id}"],
                capture_output=True,
            )
            if proc.returncode == 0:
                results.append({"name": w.get("name"), "status": "deleted"})
            else:
                results.append({"name": w.get("name"), "status": "error", "error": proc.stderr.decode()})

        return results

    @invalidate("fetch_collaborators")
    def push_teams(self, repo, settings, dry_run=False):
        """Push team repository permissions from the collaborators.teams list"""
        teams = settings.get("teams", [])
        org, name = repo.split("/", 1)
        results = []
        for team in teams:
            slug = team.get("slug")
            permission = team.get("permission")
            if not slug or not permission:
                continue
            if dry_run:
                results.append({"slug": slug, "status": "dry_run", "permission": permission})
                continue
            cmd = [
                "gh", "api", "--method", "PUT",
                f"/orgs/{org}/teams/{slug}/repos/{org}/{name}",
                "-f", f"permission={permission}",
            ]
            proc = subprocess.run(cmd, capture_output=True)
            if proc.returncode == 0:
                results.append({"slug": slug, "status": "updated", "permission": permission})
            else:
                results.append({"slug": slug, "status": "error", "error": proc.stderr.decode()})
        return results

    @invalidate("fetch_environments")
    def push_environments(self, repo, environments, dry_run=False):
        """Push deployment environments"""
        results = []
        for env in environments:
            name = env.get("name")
            if not name:
                continue

            if dry_run:
                results.append({"name": name, "status": "dry_run"})
                continue

            data = {}
            if env.get("wait_timer") is not None:
                data["wait_timer"] = env["wait_timer"]
            if env.get("prevent_self_review") is not None:
                data["prevent_self_review"] = env["prevent_self_review"]
            if env.get("reviewers"):
                data["reviewers"] = env["reviewers"]
            if env.get("deployment_branch_policy"):
                data["deployment_branch_policy"] = env["deployment_branch_policy"]

            cmd = ["gh", "api", "--method", "PUT", f"/repos/{repo}/environments/{name}"]
            if data:
                cmd.extend(["--input", "-"])
                proc = subprocess.run(cmd, input=json.dumps(data).encode(), capture_output=True, check=True)
            else:
                proc = subprocess.run(cmd, capture_output=True, check=True)
            results.append({"name": name, "status": "updated", "data": json.loads(proc.stdout) if proc.stdout.strip() else {}})
        return results

    def push_code_security_config(self, repo, settings, dry_run=False):
        """Push code security configuration - not directly supported via API"""
        # This is managed via the security configuration API which is complex
        return {"status": "skipped", "reason": "code security config managed via org-level security configurations"}

    def push_code_scanning_setup(self, repo, settings, dry_run=False):
        """Push code scanning default setup"""
        if dry_run:
            return {"status": "dry_run", "changes": settings}

        data = {k: v for k, v in settings.items() if v is not None}
        if not data:
            return {"status": "skipped", "reason": "no settings"}

        cmd = ["gh", "api", "--method", "PATCH", f"/repos/{repo}/code-scanning/default-setup"]
        cmd.extend(["--input", "-"])
        proc = subprocess.run(cmd, input=json.dumps(data).encode(), capture_output=True)
        if proc.returncode == 404 or b"404" in proc.stderr:
            return {"status": "skipped", "reason": "code scanning not available for this repository"}
        if proc.returncode != 0:
            return {"status": "error", "error": proc.stderr.decode()}
        if proc.stdout.strip():
            return {"status": "updated", "data": json.loads(proc.stdout)}
        return {"status": "updated"}

    def push_code_quality_setup(self, repo, settings, dry_run=False):
        """Push code quality setup"""
        if dry_run:
            return {"status": "dry_run", "changes": settings}

        data = {k: v for k, v in settings.items() if v is not None}
        if not data:
            return {"status": "skipped", "reason": "no settings"}

        cmd = ["gh", "api", "--method", "PATCH", f"/repos/{repo}/code-quality/setup"]
        cmd.extend(["--input", "-"])
        proc = subprocess.run(cmd, input=json.dumps(data).encode(), capture_output=True)
        if proc.returncode == 404 or b"404" in proc.stderr:
            return {"status": "skipped", "reason": "code quality not available for this repository"}
        if proc.returncode != 0:
            return {"status": "error", "error": proc.stderr.decode()}
        if proc.stdout.strip():
            return {"status": "updated", "data": json.loads(proc.stdout)}
        return {"status": "updated"}

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

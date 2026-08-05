import os
import json
from .runner import GhRunner
from .cache import cache_call

REPO_CONFIG_LOCATION = os.path.expanduser("~/.ghm/repos.json")


@cache_call
def fetch_buildpack_toml(repo):
    import urllib.request
    import subprocess

    fp = urllib.request.urlopen(
        f"https://raw.githubusercontent.com/{repo}/main/buildpack.toml"
    )
    cmd = ["yj", "-tj"]
    res = subprocess.run(cmd, capture_output=True, check=True, input=fp.read())
    return json.loads(res.stdout)


def check_requirements():
    from subprocess import CalledProcessError

    try:
        GhRunner().help()
        return True
    except CalledProcessError:
        return False


def load_repos(remote_repos=False, org=None, pr_list=None):
    """Loads a JSON formatted list of repositories to be used by the script"""
    if pr_list:
        from urllib.parse import urlparse

        repos = set()
        with open(pr_list) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parsed = urlparse(line)
                path_parts = parsed.path.strip("/").split("/")
                if len(path_parts) >= 4 and path_parts[2] == "pull":
                    repos.add(f"{path_parts[0]}/{path_parts[1]}")
        return sorted(repos)
    if remote_repos:
        repos = GhRunner().list_repos(org=org)
        return [repo["full_name"] for repo in repos if "full_name" in repo.keys()]
    else:
        repo_config_location = os.environ.get("GHM_REPO_CONFIG", REPO_CONFIG_LOCATION)

        repos = json.load(open(repo_config_location))
        if not hasattr(repos, "append") or not hasattr(repos, "__len__"):
            raise TypeError("Invalid configuration file")
        return repos

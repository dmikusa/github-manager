import os
import json
from .runner import GhRunner

REPO_CONFIG_LOCATION = os.path.expanduser('~/.ghm/repos.json')


def check_requirements():
    from subprocess import CalledProcessError
    try:
        GhRunner().help()
        return True
    except CalledProcessError:
        return False


def load_repos():
    """Loads a JSON formatted list of repositories to be used by the script"""
    repos = json.load(open(REPO_CONFIG_LOCATION))
    if not hasattr(repos, "append") or not hasattr(repos, "__len__"):
        raise TypeError("Invalid configuration file")
    return repos
